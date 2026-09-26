"""Endpoint tests.

The InfluxDB client is stubbed, so these test the HTTP surface, the auth gate,
the allowlist rejections, and the read-only guard -- without needing a database.

What is deliberately NOT tested here: whether the SQL actually runs. That is
covered by the live check in ``tests/test_live.py``, which is skipped unless the
stack is up, because a stub cannot tell you that InfluxDB accepts a dialect the
stub happily returns rows for.
"""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from solar_api import main as mainmod
from solar_api.auth import limiter
from solar_api.config import Settings
from solar_api.config import get_settings as real_get_settings
from solar_api.influx import InfluxError


class StubInflux:
    """Records queries instead of executing them."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows if rows is not None else [{"inverter_id": "INV-01", "ac_power_w": 1.0}]
        #: When set, query() raises this instead of returning rows.
        self.raise_with: Exception | None = None

    async def query(
        self, sql: str, params: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        if self.raise_with is not None:
            raise self.raise_with
        return self.rows

    async def close(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient wired to a stub InfluxDB.

    Two things that are easy to get wrong here:

    * The InfluxDB dependency must be overridden via ``dependency_overrides``,
      not by patching a module symbol. Both ``main`` and ``auth`` import
      ``get_settings``, and patching one leaves the other reading the real .env,
      which silently invalidates every test token.
    * ``app.state.influx`` is *overwritten* by the lifespan handler. The stub has
      to be installed after the context manager starts, otherwise tests mutate a
      real client that no route is using.
    """
    limiter.reset()
    # The lifespan calls `get_settings()` directly rather than through Depends,
    # so `dependency_overrides` does not reach it. The env var is the only way
    # to stop the lifespan from starting a real MQTT subscription against a
    # broker that does not exist in the test environment. Clearing the lru_cache
    # makes the change visible to a Settings already built this session.
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    real_get_settings.cache_clear()
    settings = Settings(
        INFLUX_API_TOKEN="test-token",
        API_SECRET_KEY="test-secret-key-long-enough-for-hmac-sha256-0123456789",
        API_ADMIN_PASSWORD="correct-password",
        API_ADMIN_USERNAME="admin",
        # No broker in the test environment. The alert engine is exercised
        # directly in test_alert_service.py, where its input can be controlled;
        # letting it run here would just add a background task that retries
        # against a broker that does not exist.
        ALERTS_ENABLED="false",
    )
    stub = StubInflux()
    app = mainmod.create_app()
    app.dependency_overrides[real_get_settings] = lambda: settings
    # Routes resolve InfluxDB through Depends(_client).
    app.dependency_overrides[mainmod._client] = lambda: stub

    # client= pins the peer address so the localhost-only guard on /api/explore
    # sees a real loopback address rather than Starlette's "testclient".
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        test_client.app.state.influx = stub
        test_client.stub = stub
        yield test_client


def login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "correct-password"}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------


def test_health_is_unauthenticated(client: TestClient):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/now", "/api/summary", "/api/series",
        "/api/strings", "/api/events", "/api/meta",
    ],
)
def test_endpoints_require_a_token(client: TestClient, path: str):
    assert client.get(path).status_code == 401


def test_garbage_token_is_rejected(client: TestClient):
    assert client.get("/api/now", headers=auth("not-a-jwt")).status_code == 401


def test_token_signed_with_another_key_is_rejected(client: TestClient):
    forged = jwt.encode(
        {"sub": "admin", "exp": int(time.time()) + 60}, "wrong-key", algorithm="HS256"
    )
    assert client.get("/api/now", headers=auth(forged)).status_code == 401


def test_expired_token_is_rejected(client: TestClient):
    settings = real_get_settings()
    expired = jwt.encode(
        {"sub": "admin", "exp": int(time.time()) - 10},
        settings.api_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert client.get("/api/now", headers=auth(expired)).status_code == 401


def test_login_rejects_a_wrong_password(client: TestClient):
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "nope"}
    )
    assert response.status_code == 401


def test_login_rejects_a_wrong_username(client: TestClient):
    response = client.post(
        "/api/auth/login", json={"username": "someone", "password": "correct-password"}
    )
    assert response.status_code == 401


def test_login_is_rate_limited(client: TestClient):
    settings = real_get_settings()
    for _ in range(settings.login_rate_limit):
        client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    blocked = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert blocked.status_code == 429
    # Even the correct password is refused while the window is open.
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": "correct-password"}
    ).status_code == 429


def test_successful_login_returns_a_usable_token(client: TestClient):
    token = login(client)
    assert client.get("/api/auth/me", headers=auth(token)).json() == {"subject": "admin"}


# --- allowlist enforcement at the HTTP boundary -----------------------------


def test_series_rejects_an_unknown_table(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/series?table=nope&metric=ac_power_w", headers=auth(token)
    )
    assert response.status_code == 400
    assert "unknown table" in response.json()["detail"]


def test_series_rejects_an_unknown_metric(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/series?table=inverter_telemetry&metric=secret_column", headers=auth(token)
    )
    assert response.status_code == 400


def test_series_rejects_an_unknown_interval(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/series?table=inverter_telemetry&metric=ac_power_w&interval=1h%27%3B--",
        headers=auth(token),
    )
    assert response.status_code == 400


def test_series_binds_the_time_window_as_parameters(client: TestClient):
    token = login(client)
    client.get(
        "/api/series?table=inverter_telemetry&metric=ac_power_w&start=2026-09-25T00:00:00Z"
        "&end=2026-09-26T00:00:00Z",
        headers=auth(token),
    )
    sql, params = client.stub.calls[-1]
    assert "$start_time" in sql and "$site" in sql
    assert params["start_time"] == "2026-09-25T00:00:00Z"
    assert params["site"] == "mojave"


def test_series_rejects_a_range_beyond_the_limit(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/series?table=inverter_telemetry&metric=ac_power_w"
        "&start=2020-01-01T00:00:00Z&end=2026-01-01T00:00:00Z",
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "exceeds" in response.json()["detail"]


# --- read-only guard on /api/explore ----------------------------------------


def test_explore_allows_a_select(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/explore", params={"sql": "SELECT * FROM site_rollup"}, headers=auth(token)
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE site_rollup",
        "DELETE FROM site_rollup",
        "UPDATE site_rollup SET pr_ratio = 1",
        "SELECT 1; DROP TABLE site_rollup",
    ],
)
def test_explore_rejects_writes(client: TestClient, sql: str):
    token = login(client)
    response = client.get("/api/explore", params={"sql": sql}, headers=auth(token))
    assert response.status_code == 400


def test_explore_requires_auth(client: TestClient):
    assert client.get("/api/explore", params={"sql": "SELECT 1"}).status_code == 401


# --- error handling ---------------------------------------------------------


def test_influx_failure_becomes_a_502(client: TestClient):
    token = login(client)
    client.stub.raise_with = InfluxError("InfluxDB returned 401 Unauthorized")
    response = client.get("/api/now", headers=auth(token))
    assert response.status_code == 502


def test_influx_error_message_never_leaks_the_token(client: TestClient):
    """A 502 body must not contain the credential."""
    token = login(client)
    client.stub.raise_with = InfluxError("connection refused")
    body = client.get("/api/now", headers=auth(token)).text
    assert real_get_settings().influx_api_token not in body
    assert "test-token" not in body


# --- meta -------------------------------------------------------------------


def test_meta_exposes_the_allowlists(client: TestClient):
    token = login(client)
    meta = client.get("/api/meta", headers=auth(token)).json()
    assert "inverter_telemetry" in meta["tables"]
    assert "ac_power_w" in meta["metrics"]["inverter_telemetry"]
    assert "5m" in meta["intervals"]
    assert set(meta["severities"]) == {"info", "warning", "critical"}


def test_malformed_timestamp_is_a_400_not_a_500(client: TestClient):
    """Bad user input must not surface as a server error.

    `fromisoformat` raises ValueError on anything malformed, which escaped as a
    500 and made a client-side mistake look like an outage.
    """
    token = login(client)
    response = client.get(
        "/api/series?table=inverter_telemetry&metric=ac_power_w"
        "&start=x'%20OR%201%3D1%20--",
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "RFC 3339" in response.json()["detail"]


def test_end_must_parse_too(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/series?table=inverter_telemetry&metric=ac_power_w&end=not-a-date",
        headers=auth(token),
    )
    assert response.status_code == 400


def test_summary_rollup_is_numeric_and_carries_its_own_timestamp(client: TestClient):
    token = login(client)
    """The PWA types `rollup` as `Record<string, number>` and renders it directly.

    `rollup_time` used to live inside that dict, so the type was a lie and a
    consumer iterating the values would hit a string. It is now hoisted to a
    sibling key, which makes the declared type true.
    """
    body = client.get("/api/summary", headers=auth(token)).json()
    assert "rollup_time" in body
    assert body["rollup_time"] is None or isinstance(body["rollup_time"], str)

    rollup = body["rollup"]
    if rollup is None:
        return
    for key, value in rollup.items():
        assert isinstance(value, (int, float)), f"rollup[{key}] is {type(value).__name__}"


# --- alerting ---------------------------------------------------------------


def test_alert_endpoints_require_a_token(client: TestClient):
    token = login(client)
    for path in ("/api/alerts", "/api/alert-rules", "/api/alert-stats"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=auth(token)).status_code in (200, 503), path


def test_alert_rules_endpoint_exposes_the_shipped_rules(client: TestClient):
    """The UI shows what is being watched for, so this must not be empty."""
    body = client.get("/api/alert-rules", headers=auth(login(client))).json()
    assert body["count"] > 0
    ids = {rule["id"] for rule in body["rules"]}
    assert "telemetry_stale" in ids
    for rule in body["rules"]:
        assert rule["severity"] in ("info", "warning", "critical")
        # A staleness rule has no metric, so an empty condition list is correct
        # there and wrong everywhere else.
        if rule["kind"] == "threshold":
            assert rule["conditions"], f"{rule['id']} has no conditions"


def test_alert_endpoints_are_503_when_the_engine_is_not_running(client: TestClient):
    """A clear 503, not a 500 from deep inside the app.

    The engine being absent is an operational state an operator needs to
    distinguish from a broken endpoint.
    """
    token = login(client)
    # ALERTS_ENABLED is false in tests, so app.state.alerts is never set.
    for path in ("/api/alerts", "/api/alert-stats"):
        response = client.get(path, headers=auth(token))
        assert response.status_code == 503
        assert "alert engine" in response.json()["detail"]


def test_alerts_endpoint_reports_live_state(client: TestClient):
    """The in-memory view, including an engine that is not connected.

    This endpoint must work without a database round trip: the whole point of
    the staleness rules is to notice that data stopped arriving, so a feed that
    itself depends on storage cannot be the thing that reports it.
    """
    token = login(client)

    class Disconnected:
        connected = False
        counters = {"received": 0, "alerts_fired": 0, "errors": 0, "dropped": 0}

        def active_alerts(self):
            return []

    client.app.state.alerts = Disconnected()
    body = client.get("/api/alerts", headers=auth(token)).json()
    assert body["engine_connected"] is False
    assert body["alerts"] == []
    assert set(body["counts"]) == {"critical", "warning", "info"}


def test_alerts_endpoint_surfaces_a_fired_alert(client: TestClient):
    """A real engine holding a critical alert must show up, correctly tallied."""
    from solar_api.alert_service import AlertService
    from solar_api.alerts import Condition, Rule, RuleEngine

    class NullInflux:
        async def write_event(self, alert, site: str = "mojave") -> None:
            return None

    engine = RuleEngine([
        Rule(
            id="telemetry_stale", description="no telemetry", kind="staleness",
            scope="inverter", severity="critical", stale_after_s=1.0,
        ),
        # A threshold rule too, so a transition driven through the service's own
        # ingestion path is covered and the counters have something to report.
        Rule(
            id="inverter_fault", description="inverter fault", scope="inverter",
            severity="critical", debounce_s=0,
            conditions=(Condition("status_code", "==", 5.0),),
        ),
    ])
    service = AlertService(engine, NullInflux())
    token = login(client)

    # A reading arrives, then the device goes quiet: drive the engine through
    # its real ingestion path so the test covers routing as well as state.
    service._handle(
        "solar/mojave/block/BLK-A/inverter/INV-01/telemetry",
        json.dumps({"ac_power_w": 200_000.0, "status_code": 3}).encode(),
    )
    assert service.active_alerts() == [], "must not alert while data is arriving"

    # The device reports a fault. This goes through _dispatch, so the service
    # counters observe the transition.
    service._handle(
        "solar/mojave/block/BLK-A/inverter/INV-01/telemetry",
        json.dumps({"ac_power_w": 0.0, "status_code": 5}).encode(),
    )

    # Then it goes silent entirely: rewind the subject and run a tick, which is
    # the only path by which a staleness rule can fire.
    service._engine._subjects["INV-01"].last_seen -= 10
    service._engine.tick()

    client.app.state.alerts = service
    body = client.get("/api/alerts", headers=auth(token)).json()
    assert body["count"] == 2
    assert body["counts"]["critical"] == 2
    by_rule = {a["rule_id"]: a for a in body["alerts"]}
    assert set(by_rule) == {"telemetry_stale", "inverter_fault"}
    assert by_rule["telemetry_stale"]["subject"] == "INV-01"
    assert by_rule["telemetry_stale"]["active"] is True
    # The value is the silence age, which is what an operator needs to judge it.
    assert by_rule["telemetry_stale"]["value"] >= 10.0

    stats = client.get("/api/alert-stats", headers=auth(token)).json()
    assert stats["active"] == 2
    # Only the threshold rule went through the service, so exactly one is
    # counted here; the staleness alert was produced by calling the engine
    # directly, which is how this test can reach a timed-out state at all.
    assert stats["alerts_fired"] == 1
    assert stats["received"] == 2
    assert stats["errors"] == 0
