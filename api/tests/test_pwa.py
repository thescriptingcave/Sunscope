"""Tests for the PWA hosting and the SPA fallback.

The path handling here is the security-relevant part. A catch-all route that
serves files from disk is a classic way to leak the rest of the filesystem, so
these tests pin the containment behaviour rather than just checking that pages
load.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api import StubInflux

from solar_api import main as mainmod
from solar_api.config import Settings
from solar_api.config import get_settings as real_get_settings


@pytest.fixture
def spa_client(monkeypatch: pytest.MonkeyPatch):
    """A client with the PWA mounted, or skipped if the bundle is absent."""
    from solar_api.config import PWA_DIST

    if not (PWA_DIST / "index.html").exists():
        pytest.skip(f"no PWA bundle at {PWA_DIST}; run `npm run build` in web/")

    settings = Settings(
        INFLUX_API_TOKEN="test-token",
        API_SECRET_KEY="test-secret-key-long-enough-for-hmac-sha256-0123",
        API_ADMIN_PASSWORD="correct-password",
    )
    stub = StubInflux()
    app = mainmod.create_app()
    app.dependency_overrides[real_get_settings] = lambda: settings
    app.dependency_overrides[mainmod._client] = lambda: stub
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        client.stub = stub
        yield client


def test_root_serves_the_app_shell(spa_client: TestClient):
    response = spa_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<div id=\"root\">" in response.text


def test_manifest_and_service_worker_are_served(spa_client: TestClient):
    manifest = spa_client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["short_name"] == "Solar"

    worker = spa_client.get("/sw.js")
    assert worker.status_code == 200
    # The worker must not cache API responses; see the note in web/public/sw.js.
    assert "/api" in worker.text


def test_hashed_assets_resolve(spa_client: TestClient):
    """The shell references /assets/<hash>.js; those must actually be served."""
    import re

    shell = spa_client.get("/").text
    assets = re.findall(r'/assets/[\w.-]+', shell)
    assert assets, "index.html references no built assets"
    for asset in assets:
        assert spa_client.get(asset).status_code == 200, f"{asset} is referenced but 404s"


def test_unknown_api_path_is_a_json_404(spa_client: TestClient):
    """Must not fall through to index.html.

    Returning the shell for an unknown API path would make a client-side typo
    look like a working app, and the caller would get HTML where it expected
    JSON.
    """
    response = spa_client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]


def test_client_side_route_gets_the_shell(spa_client: TestClient):
    """A frontend route like /inverters must return the app, not a 404."""
    response = spa_client.get("/inverters")
    assert response.status_code == 200
    assert "<div id=\"root\">" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/../.env",
        "/../../.env",
        "/..%2f.env",
        "/%2e%2e%2f.env",
        "/../../../etc/passwd",
        "/..%2f..%2f..%2fetc/passwd",
        "/assets/../../.env",
    ],
)
def test_path_traversal_cannot_escape_the_bundle(spa_client: TestClient, path: str):
    """Containment: nothing outside the build directory may ever be served.

    Traversal attempts are expected to fall through to the SPA shell, which is
    the correct and harmless outcome. What must never happen is the *contents* of
    a file outside the bundle appearing in the response.
    """
    response = spa_client.get(path)
    assert response.status_code in (200, 404)
    body = response.text
    for secret_marker in ("apiv3_", "API_SECRET_KEY", "API_ADMIN_PASSWORD", "root:x:"):
        assert secret_marker not in body, f"{path} leaked {secret_marker!r}"


def test_api_routes_still_win_over_the_catch_all(spa_client: TestClient):
    """/api/now must be routed as an API endpoint, not as a client-side route."""
    assert spa_client.get("/api/now").status_code == 401
    assert spa_client.get("/healthz").status_code == 200
