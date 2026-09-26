"""InfluxDB access.

Talks to ``POST /api/v3/query_sql`` over HTTPS with httpx, rather than through
the official InfluxDB 3 client. Two reasons, both deliberate:

* **Parameters.** InfluxDB 3 Core binds parameters only as ``$name`` in ``WHERE``
  predicates, and that is the entire SQL-injection defence in
  ``docs/04-security.md``. The official client exposes no
  ``query_with_parameters``, so it cannot use the defence.
* **No gRPC.** The official client queries over Flight SQL, which is the same
  gRPC transport that made Grafana fail against a plaintext server. HTTP needs
  no handshake negotiation and lets us pin the CA explicitly.

Every query in this module goes through :meth:`InfluxClient.query`, which sends
``params`` as a separate JSON field. Nothing here builds SQL by string
interpolation of user input; see :mod:`solar_api.sql` for the allowlists.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    # Type-only, to keep the storage client independent of the rule engine.
    from .alerts import Alert

from .config import Settings, read_secret_file

log = logging.getLogger(__name__)

#: Sentinel distinguishing "no value supplied" from "supplied as null".
MISSING = object()


def _last_cache_sql(table: str, cache: str) -> str:
    """Build the last_cache() call.

    S608 (string-built SQL) is unavoidable here: InfluxDB 3 requires
    last_cache()'s arguments to be *string literals* and does not accept bound
    parameters there. The safety comes from :func:`solar_api.sql._dimension`'s
    sibling check -- both arguments are validated against closed allowlists
    before this is called, so the interpolation is over known-safe strings and
    never over caller input.
    """
    from .sql import CACHES, TABLES

    if table not in TABLES:
        raise InfluxError(f"unknown table {table!r}")
    if cache not in CACHES:
        raise InfluxError(f"unknown cache {cache!r}")
    return f"SELECT * FROM last_cache('{table}', '{cache}')"  # noqa: S608


class InfluxError(RuntimeError):
    """Any failure talking to InfluxDB.

    Deliberately does not carry the token or the full request body, so it is safe
    to log or return.
    """


class InfluxClient:
    """Thin async client for the InfluxDB 3 SQL query API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.influx_url,
            timeout=settings.influx_timeout_s,
            verify=self._verify(),
        )

    def _verify(self) -> Any:
        """TLS verification config.

        Prefers pinning the combined CA bundle over disabling verification.
        ``influx_verify_tls=False`` exists for emergencies and is a worse
        option than it looks, so it is only honoured when explicitly set.
        """
        if not self.settings.influx_verify_tls:
            return False
        bundle = self.settings.influx_ca_bundle
        if bundle and bundle.exists():
            return str(bundle)
        return True

    def token(self) -> str:
        """Resolve the token, preferring the secret file over the environment."""
        return (
            read_secret_file(self.settings.influx_token_file)
            or self.settings.influx_api_token
        )

    async def query(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Run a SQL query and return rows as dicts.

        ``params`` is sent as a distinct JSON field, which is what makes this
        injection-safe: the value never becomes part of the SQL text.
        """
        token = self.token()
        if not token:
            raise InfluxError("no InfluxDB token configured")

        body: dict[str, Any] = {
            "db": database or self.settings.influx_db,
            "q": sql,
        }
        if params:
            body["params"] = dict(params)

        try:
            response = await self._client.post(
                "/api/v3/query_sql",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            # httpx exception text can include the URL, never the token.
            raise InfluxError(f"InfluxDB request failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            # The body can echo the query, which is fine, but not the token.
            detail = response.text[:300]
            raise InfluxError(f"InfluxDB returned {response.status_code}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise InfluxError("InfluxDB returned a non-JSON response") from exc

        if not isinstance(payload, list):
            raise InfluxError("unexpected response shape from InfluxDB")
        return payload

    async def last_cache(self, table: str, cache: str) -> list[dict[str, Any]]:
        """Read a Last Value Cache.

        ``last_cache`` is a ``FROM``-clause table function whose two arguments
        must be **string literals** -- parameters are not accepted there. Both
        values are therefore validated against an allowlist before being
        interpolated, which is the one place this module builds SQL from
        variables, and the reason it is safe.
        """
        from .sql import CACHES, TABLES  # local import avoids a cycle

        if table not in TABLES:
            raise InfluxError(f"unknown table {table!r}")
        if cache not in CACHES:
            raise InfluxError(f"unknown cache {cache!r}")
        return await self.query(_last_cache_sql(table, cache))

    async def write_event(self, alert: Alert, site: str = "mojave") -> None:
        """Persist one alert transition as a point in the ``events`` table.

        The API's own token is read-only in spirit, but InfluxDB 3 Core has no
        permission-scoped tokens (see docs/04-security.md 4.3), so this can
        write. Line protocol is assembled by hand rather than via a helper
        because the only writer is this one method and the field set is fixed.

        Severity and source are tag columns, matching what the simulator
        publishes for its own events, so both write paths land in the same
        series and one query covers simulator and API alerts alike.
        """
        token = self.token()
        if not token:
            raise InfluxError("no InfluxDB token configured")

        # A resolution is recorded at severity `info` on the same rule: the
        # interesting fact is that the fault ended, and overwriting severity
        # would lose the record of how serious it had been.
        severity = alert.severity if not alert.is_resolution else "info"
        code = alert.rule_id if not alert.is_resolution else f"{alert.rule_id}_RESOLVED"

        lines = [
            f"events,site={self.escape_tag(site)},"
            f"severity={self.escape_tag(severity)},"
            f"source={self.escape_tag(alert.subject)},"
            # `rule` is part of the tag set so that two alerts on one device
            # which resolve in the same evaluation do not collide on
            # (measurement, tags, timestamp) and overwrite each other.
            f"rule={self.escape_tag(alert.rule_id)} "
            f'code="{self.escape_field(code)}",'
            f'message="{self.escape_field(alert.message)}",'
        ]
        # value/threshold are float64 columns. A resolution carries the value
        # that tripped the rule, which is what makes the pair readable together.
        value = "null" if alert.value is None else f"{alert.value}"
        threshold = "null" if alert.threshold is None else f"{alert.threshold}"
        # A resolution is timestamped when the fault *ended*, not when it began.
        # Writing `fired_at` here put every resolution at the same instant as its
        # own firing, so the event history claimed faults cleared the moment they
        # started.
        stamp = alert.resolved_at if alert.resolved_at is not None else alert.fired_at
        lines[0] += f"value={value},threshold={threshold} {int(stamp * 1_000_000_000)}"

        try:
            # `/api/v3/write_lp`, not `/api/v3/write`: the latter does not exist
            # in InfluxDB 3 Core 3.11 and answers 404. (`/api/v2/write` exists but
            # is the 2.x API and demands a bucket, so it is not usable here.)
            response = await self._client.post(
                "/api/v3/write_lp",
                params={"db": self.settings.influx_db, "precision": "ns"},
                content="\n".join(lines).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
        except httpx.HTTPError as exc:
            raise InfluxError(f"event write failed: {exc}") from exc
        if response.status_code >= 300:
            raise InfluxError(
                f"event write failed: HTTP {response.status_code} {response.text[:200]}"
            )

    @staticmethod
    def escape_tag(value: str) -> str:
        """Escape a line-protocol tag value.

        Commas, equals and spaces separate fields in a tag set, so an unescaped
        device id would silently create extra tags. An inverter id is
        alphanumeric in practice, but this is the boundary where a hostile or
        merely surprising value must not be able to change the shape of the
        write.
        """
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace("=", "\\=")
            .replace(" ", "\\ ")
        )

    @staticmethod
    def escape_field(value: str) -> str:
        """Escape a line-protocol string field value."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
