"""FastAPI application.

Read-only over InfluxDB. Endpoints:

    POST /api/auth/login     single-user login, returns a JWT
    GET  /api/auth/me         echo the authenticated subject
    GET  /api/now             current state, from the Last Value Cache
    GET  /api/summary         fleet summary: rollup + per-inverter aggregates
    GET  /api/series          one metric, time-bucketed
    GET  /api/strings         per-inverter string imbalance
    GET  /api/events          recent alarms
    GET  /api/alerts          alerts the engine holds right now (in memory)
    GET  /api/alert-rules     the loaded rule set, so thresholds are auditable
    GET  /api/alert-stats     engine counters, for the health strip
    GET  /api/explore         read-only SQL passthrough (localhost only)
    GET  /api/meta            allowlists, so the UI can build pickers
    GET  /healthz             liveness, unauthenticated
"""

from __future__ import annotations

import ipaddress
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import sql as sqlmod
from .alert_config import load_rules
from .alert_service import AlertService
from .alerts import RuleEngine
from .auth import (
    issue_token,
    rate_limit_login,
    rate_limit_query,
    require_auth,
    verify_password,
)
from .config import PWA_DIST, Settings, get_settings
from .influx import InfluxClient, InfluxError

log = logging.getLogger("solar_api")

SITE = "mojave"


class LoginRequest(BaseModel):
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"  # noqa: S105 - a type tag, not a secret
    expires_in: int


def _client(settings: Settings = Depends(get_settings)) -> InfluxClient:
    return InfluxClient(settings)


#: Address ranges that can only be reached from this machine or its Docker
#: network. Listed explicitly rather than using ``ipaddress.is_private``, which
#: also covers the documentation and reserved ranges -- 203.0.113.0/24 among
#: them -- and being over-permissive is the wrong direction for a security guard.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _is_local_address(host: str | None) -> bool:
    """True when a request provably originated on this machine or its containers.

    The private ranges are not a concession to sloppiness: the API's port is
    published on ``127.0.0.1`` only, and Docker's port forwarding rewrites the
    client address to the bridge gateway, so a request from the host
    legitimately arrives from something like ``172.22.0.1``. Without accepting
    those, every real request was rejected and the endpoint was unusable.
    """
    if host is None:
        # No peer information at all. Treat as local, matching the previous
        # behaviour; a missing client is not evidence of a remote caller.
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname rather than an IP. Not provably local, so refuse.
        return False
    if ip.is_loopback or ip.is_link_local:
        return True
    return any(ip in network for network in _LOCAL_NETWORKS)


def _alerts(request: Request) -> AlertService:
    """The live alert service, or a clear error if it is not running.

    A dependency rather than a module global so tests can substitute one, and
    so a missing service is a 503 the caller can act on rather than an
    AttributeError from somewhere deeper.
    """
    service = getattr(request.app.state, "alerts", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="alert engine is not running",
        )
    return service


def _window(
    start: str | None, end: str | None, settings: Settings
) -> tuple[str, str]:
    """Resolve the time range, defaulting to the last 24 hours.

    ``start`` and ``end`` are RFC 3339 strings bound as ``$start_time`` /
    ``$end_time``. InfluxDB 3 cannot parameterise an INTERVAL literal, so the
    default is computed here and passed as a timestamp instead.
    """
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    def parse(value: str, label: str) -> datetime:
        """Parse an RFC 3339 bound, or fail with a 400.

        A malformed timestamp is user input, not a server fault: letting
        ValueError escape produces a 500, which misreports the problem and
        buries it in the error log as an unhandled exception.
        """
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{label} must be an RFC 3339 timestamp, e.g. 2026-09-25T12:00:00Z",
            ) from exc

    end_dt = parse(end, "end") if end else now
    start_dt = parse(start, "start") if start else end_dt - timedelta(hours=24)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=UTC)

    span = (end_dt - start_dt).days
    if span > settings.max_range_days:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"time range exceeds {settings.max_range_days} days",
        )
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start_dt.astimezone(UTC).strftime(fmt), end_dt.astimezone(UTC).strftime(fmt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    problems = settings.validate_runtime()
    if problems:
        # Fail loudly at startup rather than 500ing on the first request.
        for problem in problems:
            log.error("configuration problem: %s", problem)
        raise SystemExit(1)
    client = InfluxClient(settings)
    app.state.influx = client

    # The alert engine subscribes to MQTT for the process lifetime. A bad rule
    # file is a hard startup failure on purpose: an engine that silently loaded
    # no rules would report "all clear" forever, which is the most dangerous
    # possible failure for the component whose job is to say something is wrong.
    # Declared before the branch so the shutdown path can always reference it.
    # Binding this only inside the `if` and then using it in `finally` made every
    # shutdown raise UnboundLocalError whenever alerting was disabled.
    alert_service: AlertService | None = None
    rules = load_rules(settings.alert_rules_file)
    if settings.alerts_enabled:
        alert_service = AlertService(RuleEngine(rules), client)
        app.state.alerts = alert_service
        await alert_service.start()
        log.info("alert engine started with %d rules", len(rules))
    else:
        # Tests drive the engine directly and have no broker to subscribe to.
        log.info("alert engine disabled by configuration")

    log.info(
        "solar-api ready: influx=%s db=%s ca=%s",
        settings.influx_url,
        settings.influx_db,
        "pinned" if settings.influx_ca_bundle and settings.influx_ca_bundle.exists() else "default",
    )
    try:
        yield
    finally:
        if alert_service is not None:
            await alert_service.stop()
        await client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Solar Farm API",
        version="0.1.0",
        description="Read-only API over the solar farm telemetry in InfluxDB 3.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        # Exact origins, never "*". The PWA is a specific origin and does not
        # need wildcard CORS; allowing it would let any page read telemetry
        # using a token it happens to have.
        allow_origins=settings.api_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(InfluxError)
    async def _influx_error(_request: Request, exc: InfluxError) -> Any:
        # The message is safe to surface: InfluxError never carries the token.
        log.warning("influx error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    @app.exception_handler(sqlmod.QueryError)
    async def _query_error(_request: Request, exc: sqlmod.QueryError) -> Any:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # -- health --------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- auth ----------------------------------------------------------------

    @app.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
    async def login(
        body: LoginRequest,
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> TokenResponse:
        client_ip = request.client.host if request.client else "unknown"
        rate_limit_login(client_ip, settings)

        # Both comparisons run, so a wrong username and a wrong password take
        # the same time and cannot be told apart by timing.
        user_ok = body.username == settings.api_admin_username
        pass_ok = verify_password(body.password, settings)
        if not (user_ok and pass_ok):
            log.info("failed login for %r from %s", body.username, client_ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
            )
        token, ttl = issue_token(body.username, settings)
        return TokenResponse(token=token, expires_in=ttl)

    @app.get("/api/auth/me", tags=["auth"])
    async def me(subject: str = Depends(require_auth)) -> dict[str, str]:
        return {"subject": subject}

    # -- telemetry -----------------------------------------------------------

    @app.get("/api/now", tags=["telemetry"])
    async def now(
        _: str = Depends(require_auth),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """Current state for every inverter, from the in-memory Last Value Cache.

        This is what makes a cold PWA load instant: an in-memory server-side
        structure rather than an aggregate over hours of Parquet.
        """
        rows = await client.query(sqlmod.build_now_query())
        return {
            "source": "last_value_cache",
            "site": SITE,
            "count": len(rows),
            "devices": rows,
        }

    @app.get("/api/summary", tags=["telemetry"])
    async def summary(
        start: str | None = None,
        end: str | None = None,
        subject: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """Fleet summary: latest rollup plus per-inverter aggregates."""
        rate_limit_query(subject, settings)
        start_ts, end_ts = _window(start, end, settings)
        query, params = sqlmod.build_fleet_summary()
        params.update({"start_time": start_ts, "end_time": end_ts})
        rows = await client.query(query, params)

        rollup = None
        rollup_time = None
        devices = []
        # Numeric rollup fields only. The rollup timestamp used to sit in this
        # dict too, which made `rollup: dict[str, number]` a lie that pushed
        # every consumer to re-check types; it is hoisted to its own key below.
        rollup_keys = {
            "total_ac_power_w", "daily_yield_kwh",
            "pr_ratio", "capacity_factor", "inverters_online", "strings_online",
        }
        for row in rows:
            if rollup is None and row.get("rollup_time") is not None:
                rollup = {k: v for k, v in row.items() if k in rollup_keys}
                rollup_time = row["rollup_time"]
            devices.append(
                {
                    k: v
                    for k, v in row.items()
                    if k in {
                        "inverter_id", "avg_ac_power_w", "peak_ac_power_w",
                        "max_heatsink_temp_c", "avg_efficiency", "clipped_samples",
                    }
                }
            )
        return {"site": SITE, "window": {"start": start_ts, "end": end_ts},
                "rollup_time": rollup_time, "rollup": rollup, "devices": devices}

    @app.get("/api/series", tags=["telemetry"])
    async def series(
        table: str = Query(default="inverter_telemetry"),
        metric: str = Query(default="ac_power_w"),
        start: str | None = None,
        end: str | None = None,
        interval: str = Query(default="5m"),
        group_by: str | None = None,
        limit: int = Query(default=5000, ge=1, le=5000),
        subject: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """One metric over time, aggregated into buckets.

        ``table``, ``metric``, ``interval`` and ``group_by`` are all validated
        against allowlists before any SQL is built; ``site`` and the time range
        are bound parameters.
        """
        rate_limit_query(subject, settings)
        start_ts, end_ts = _window(start, end, settings)
        built = sqlmod.build_series_query(
            table=table,
            metric=metric,
            start=start_ts,
            end=end_ts,
            interval=interval,
            group_by=group_by,
            site=SITE,
            limit=min(limit, settings.max_rows),
        )
        rows = await client.query(built.sql, built.params)
        return {
            "site": SITE,
            "table": table,
            "metric": built.metric,
            "interval": built.interval,
            "group_by": built.group_by,
            "window": {"start": start_ts, "end": end_ts},
            "count": len(rows),
            "points": rows,
        }

    @app.get("/api/strings", tags=["telemetry"])
    async def strings(
        start: str | None = None,
        end: str | None = None,
        min_imbalance: float = Query(default=0.0, ge=0.0, le=1.0),
        expected_strings: int = Query(
            default=3, ge=1, le=12,
            description="Strings per inverter; samples missing any are not scored",
        ),
        subject: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """Per-inverter string spread.

        The fault signature for a degrading string: normal operation sits under
        5 %, and a genuinely bad string exceeds 20 %.

        Spreads are measured between strings **at the same timestamp**, so the
        result is not contaminated by the day/night cycle over the window.
        """
        rate_limit_query(subject, settings)
        start_ts, end_ts = _window(start, end, settings)
        query, params = sqlmod.build_string_imbalance(min_imbalance, expected_strings)
        params.update({"start_time": start_ts, "end_time": end_ts})
        rows = await client.query(query, params)
        return {"site": SITE, "count": len(rows), "inverters": rows}

    @app.get("/api/events", tags=["telemetry"])
    async def events(
        start: str | None = None,
        end: str | None = None,
        severity: list[str] | None = Query(default=None),
        subject: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """Recent alarms, optionally filtered by severity."""
        rate_limit_query(subject, settings)
        start_ts, end_ts = _window(start, end, settings)
        query, params = sqlmod.build_event_feed(severity)
        params.update({"start_time": start_ts, "end_time": end_ts})
        rows = await client.query(query, params)
        return {"site": SITE, "count": len(rows), "events": rows}

    # -- alerting ------------------------------------------------------------

    @app.get("/api/alerts", tags=["alerting"])
    async def alerts(
        severity: list[str] | None = Query(default=None),
        subject_name: str | None = Query(default=None, alias="subject"),
        include_resolved: bool = Query(default=False),
        _auth: str = Depends(require_auth),
        service: AlertService = Depends(_alerts),
    ) -> dict[str, Any]:
        """Alerts the engine is currently holding, in memory.

        Distinct from ``/api/events``, which is the historical record read back
        from InfluxDB. This is the live view: it reflects state the engine holds
        right now, including alerts that have not yet been flushed to storage,
        and it stays correct when the database is unreachable.

        That distinction matters most for the staleness rules, whose entire
        purpose is to notice that data has stopped. If reading the feed required
        a successful database round trip, the alert about the database being
        unreachable could not be displayed.
        """
        wanted = set(severity) if severity else None
        items = [a.to_dict() for a in service.active_alerts()]
        if wanted:
            items = [a for a in items if a["severity"] in wanted]
        if subject_name:
            items = [a for a in items if a["subject"] == subject_name]
        if not include_resolved:
            items = [a for a in items if a["active"]]

        counts = {"critical": 0, "warning": 0, "info": 0}
        for item in items:
            if item["severity"] in counts:
                counts[item["severity"]] += 1
        return {
            "site": SITE,
            "count": len(items),
            "counts": counts,
            "engine_connected": service.connected,
            "alerts": items,
        }

    @app.get("/api/alert-rules", tags=["alerting"])
    async def alert_rules(
        _auth: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """The loaded rule set.

        Exposed so the dashboard can show what is being watched for, and so an
        operator can confirm a threshold change took effect without restarting
        anything.
        """
        rules = load_rules(settings.alert_rules_file)
        return {
            "site": SITE,
            "count": len(rules),
            "rules": [
                {
                    "id": rule.id,
                    "description": rule.description,
                    "scope": rule.scope,
                    "severity": rule.severity,
                    "kind": rule.kind,
                    "debounce_s": rule.debounce_s,
                    "stale_after_s": rule.stale_after_s,
                    "conditions": [
                        {"metric": c.metric, "operator": c.operator, "threshold": c.threshold}
                        for c in rule.conditions
                    ],
                }
                for rule in rules
            ],
        }

    @app.get("/api/alert-stats", tags=["alerting"])
    async def alert_stats(
        _auth: str = Depends(require_auth),
        service: AlertService = Depends(_alerts),
    ) -> dict[str, Any]:
        """Engine counters, for the health strip.

        ``errors`` is the number worth watching: a rising count means the engine
        is running but something in the loop is failing, which is a different
        problem from ``engine_connected`` being false and much easier to miss.
        """
        return {
            "site": SITE,
            "engine_connected": service.connected,
            "active": len(service.active_alerts()),
            **service.counters,
        }

    @app.get("/api/explore", tags=["telemetry"])
    async def explore(
        # `request` has no default: FastAPI injects it, and a defaulted Request
        # would be misread as a query parameter.
        request: Request,
        sql: str = Query(..., min_length=1, max_length=4000),
        params: str | None = Query(
            default=None,
            description='JSON object of $name bindings, e.g. {"site":"mojave"}',
        ),
        subject: str = Depends(require_auth),
        settings: Settings = Depends(get_settings),
        client: InfluxClient = Depends(_client),
    ) -> dict[str, Any]:
        """Run read-only SQL.

        Replaces the Grafana/Explorer role: a query surface over the same data,
        without the gRPC dependency that blocked both.

        Bindings are accepted as a JSON object in ``params`` and travel to
        InfluxDB as a separate field, never spliced into the SQL text. Without
        them an exploration tool could only run queries with literals inlined,
        which is both tedious and the exact practice the parameter binding exists
        to avoid -- so the endpoint could not demonstrate, or be used with, the
        one mechanism that makes ad-hoc SQL safe.

        The guard is a deny-list, which is weaker than an allow-list. That is a
        disclosed trade, made necessary because InfluxDB 3 Core has no
        read-only tokens, so this endpoint holds an admin token.

        "Local" has to mean more than literal loopback. The port is published on
        the host's 127.0.0.1 only, but Docker rewrites the source address when it
        forwards, so the API sees the caller as the bridge gateway -- 172.22.0.1
        in this stack -- and a check against ("127.0.0.1", "::1") rejects every
        real request. That made this endpoint, the documented replacement for
        Grafana's Explorer, unusable in practice, while the test suite passed
        because TestClient pins the peer to 127.0.0.1 and never reproduced the
        deployment's actual network path.

        So the test is "loopback, or an address that can only arrive from this
        host": loopback, RFC 1918 private, or link-local. Nothing on a routable
        network can produce those, and the port is not exposed off-host.
        Strengthen before exposing this anywhere.
        """
        rate_limit_query(subject, settings)
        remote = request.client.host if request.client else None
        if not _is_local_address(remote):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="raw SQL is restricted to the local host",
            )
        bindings: dict[str, Any] = {}
        if params:
            try:
                parsed = json.loads(params)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"params must be a JSON object: {exc}",
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="params must be a JSON object, not a list or scalar",
                )
            bindings = parsed
        checked = sqlmod.build_read_only_sql(sql)
        rows = await client.query(checked, bindings or None)
        return {"sql": checked, "count": len(rows), "rows": rows}

    @app.get("/api/meta", tags=["telemetry"])
    async def meta(_: str = Depends(require_auth)) -> dict[str, Any]:
        """Allowlists and capabilities.

        Exposed so the PWA can build its pickers from the same lists the API
        validates against, rather than duplicating them in the frontend where
        the two could drift.
        """
        return {
            "site": SITE,
            "tables": sorted(sqlmod.TABLES),
            "metrics": {t: sorted(m) for t, m in sqlmod.METRICS.items()},
            "dimensions": {t: sorted(d) for t, d in sqlmod.DIMENSIONS.items()},
            "intervals": sorted(sqlmod.INTERVALS),
            "severities": sorted(sqlmod.EVENT_SEVERITIES),
            "caches": sorted(sqlmod.CACHES),
        }

    _mount_pwa(app, settings)
    return app


def _mount_pwa(app: FastAPI, settings: Settings) -> None:
    """Serve the built PWA at `/`, making the API the single origin.

    This is why same-origin serving matters rather than being a convenience:
    with one origin there is no CORS in production, the service worker scope
    covers the whole app, and only one port needs exposing if this is ever put
    behind a tunnel.

    Skipped when ``web/dist`` is absent, so the API still runs standalone — which
    is the case in CI and for anyone hitting the API directly.
    """
    dist = PWA_DIST
    if not (dist / "index.html").exists():
        log.info("PWA bundle not found at %s; serving the API only", dist)
        return

    # Mounted at the end so it never shadows /api. The SPA fallback below hands
    # routing to index.html for any unmatched GET that is not an API path.
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa(full_path: str) -> Response:
        if full_path.startswith("api/"):
            # An unknown /api path must 404 as JSON, not fall through to the SPA:
            # returning index.html here would make a typo look like a working app.
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path in {"", "/"}:
            return FileResponse(dist / "index.html")
        candidate = (dist / full_path).resolve()
        # Resolve before serving: a path like ../../.env must not escape dist.
        if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        # Anything else is a client-side route.
        return FileResponse(dist / "index.html")

    log.info("serving PWA from %s", dist)


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "solar_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
