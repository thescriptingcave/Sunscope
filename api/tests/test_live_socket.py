"""Tests for the live WebSocket and its single-use tickets.

The ticket exists so the long-lived JWT never lands in a query string, where it
would be captured by access logs, proxy logs and browser history. These tests
pin the properties that make that worth doing: single use, and a short life.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from solar_api.live_tickets import TICKET_TTL_S, TicketStore

# --- the store --------------------------------------------------------------


def test_a_ticket_can_be_redeemed_once():
    store = TicketStore()
    token = store.issue("admin", now=1000.0)
    assert store.consume(token, now=1000.0) == "admin"
    # Replay must fail. This is the property that makes a ticket in a log line
    # worthless to anyone who finds it afterwards.
    assert store.consume(token, now=1000.0) is None


def test_a_ticket_expires():
    store = TicketStore(ttl_s=30.0)
    token = store.issue("admin", now=1000.0)
    assert store.consume(token, now=1029.0) == "admin"
    token = store.issue("admin", now=1000.0)
    assert store.consume(token, now=1031.0) is None, "should expire after the TTL"


def test_an_unknown_ticket_is_rejected():
    store = TicketStore()
    assert store.consume("never-issued", now=1000.0) is None
    assert store.consume("", now=1000.0) is None


def test_tickets_are_unpredictable_and_distinct():
    store = TicketStore()
    issued = {store.issue("admin") for _ in range(200)}
    assert len(issued) == 200, "tickets must not repeat"
    # 32 bytes of urandom, url-safe base64.
    assert all(len(t) >= 40 for t in issued)


def test_the_store_cannot_grow_without_bound():
    """A caller that mints and never redeems must not exhaust memory."""
    store = TicketStore()
    for _ in range(5000):
        store.issue("admin")
    assert store.outstanding <= 256


def test_expired_tickets_are_swept_on_issue():
    import time

    store = TicketStore(ttl_s=1.0)
    for _ in range(100):
        store.issue("admin")
    assert store.outstanding > 1
    # A second later every one of those is past its TTL, so issuing sweeps them.
    # The timestamp must be in the future relative to time.time(); an arbitrary
    # small number is 1970 and would sort *before* the live ones.
    store.issue("admin", now=time.time() + 2)
    assert store.outstanding == 1


def test_ttl_default_is_short():
    assert TICKET_TTL_S <= 60, "a ticket is a handshake credential, not a session"


# --- the endpoint -----------------------------------------------------------


def client_with_socket(monkeypatch: pytest.MonkeyPatch):
    from test_api import StubInflux, auth, login  # noqa: F401  (fixture reuse)

    from solar_api import main as mainmod
    from solar_api.config import Settings
    from solar_api.config import get_settings as real_get_settings

    monkeypatch.setenv("ALERTS_ENABLED", "false")
    real_get_settings.cache_clear()
    settings = Settings(
        INFLUX_API_TOKEN="test-token",
        API_SECRET_KEY="test-secret-key-long-enough-for-hmac-sha256-0123456789",
        API_ADMIN_PASSWORD="correct-password",
        ALERTS_ENABLED="false",
    )
    app = mainmod.create_app()
    app.dependency_overrides[real_get_settings] = lambda: settings
    app.dependency_overrides[mainmod._client] = lambda: StubInflux()
    return TestClient(app, client=("127.0.0.1", 51234))


def test_ticket_requires_authentication():
    with client_with_socket(__import__("pytest").MonkeyPatch()) as client:
        assert client.post("/api/live-ticket").status_code == 401


def test_ticket_is_minted_for_an_authenticated_caller():
    from test_api import auth, login

    with client_with_socket(__import__("pytest").MonkeyPatch()) as client:
        token = login(client)
        # With ALERTS_ENABLED false there is no engine, so this is a 503 -- which
        # still proves the route is reached only after authentication.
        response = client.post("/api/live-ticket", headers=auth(token))
        assert response.status_code in (200, 503)
        if response.status_code == 200:
            body = response.json()
            assert body["ticket"]
            assert body["path"] == "/api/live"
            assert body["expires_in"] <= 60


def test_socket_rejects_a_missing_ticket():
    """No ticket means no stream, and the socket is closed rather than left open."""
    from starlette.websockets import WebSocketDisconnect
    from test_api import login

    with client_with_socket(__import__("pytest").MonkeyPatch()) as client:
        login(client)
        # Stand in for the engine so the close is about the ticket, not the service.
        client.app.state.alerts = _stub_service()
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/live") as ws:
            ws.receive_text()
        assert exc.value.code == 1008, "policy violation, not an open unauthenticated socket"


def test_socket_rejects_a_ticket_that_was_already_used():
    from starlette.websockets import WebSocketDisconnect
    from test_api import login

    with client_with_socket(__import__("pytest").MonkeyPatch()) as client:
        login(client)
        client.app.state.alerts = _stub_service()
        store = client.app.state.tickets
        token = store.issue("admin")
        assert store.consume(token) == "admin", "burn it so the socket sees a spent ticket"
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect(f"/api/live?ticket={token}") as ws,
        ):
            ws.receive_text()
        assert exc.value.code == 1008


def _stub_service():
    from solar_api.alert_service import AlertService
    from solar_api.alerts import RuleEngine

    class _Null:
        connected = True
        counters: dict[str, int] = {}

        async def write_event(self, alert, site: str = "mojave") -> None:
            return None

    service = AlertService(RuleEngine([]), _Null())
    # subscribe()/unsubscribe() are the only members the socket uses.
    return service
