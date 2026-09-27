"""End-to-end RBAC over HTTP, against a real user file.

``test_users.py`` covers the store and the role algebra. This file covers the part that
actually matters to an attacker: what the running API does with the answer.

The fixture builds an actual ``users.yaml`` on disk, because the security property under
test is precisely that a password is compared against a stored digest. Mocking the store
here would test the mock.

The headline case is ``/api/explore``. It runs arbitrary SQL using an InfluxDB token
that has no permission scoping of its own (InfluxDB 3 Core does not issue scoped tokens),
so "the caller is authenticated" is not a sufficient condition for it. A viewer must get
403 -- and, per the rest of this project's findings, must get 403 *specifically*, not 401
or a hang, or the UI cannot tell "log in again" from "you may not".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from solar_api.auth import decode_token
from solar_api.config import Settings


class StubInflux:
    """Records queries instead of executing them.

    So a 403 can be distinguished from a query that reached the database and failed for
    some unrelated reason -- the distinction the RBAC assertions rest on.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: list[dict[str, Any]] = [{"n": 1}]

    async def query(self, sql: str, params: dict[str, Any] | None = None, **_: Any):
        self.calls.append((sql, params or {}))
        return self.rows

    async def close(self) -> None:
        return None

ADMIN_PASSWORD = "admin-password-for-tests"
VIEWER_PASSWORD = "viewer-password-for-tests"

#: Everything a viewer is meant to reach. If a new read-only endpoint is added this list
#: should grow with it, so that adding a capability is a deliberate act.
VIEWER_ALLOWED = [
    "/api/now",
    "/api/summary",
    "/api/series?table=telemetry&bucket=1m",
    "/api/strings",
    "/api/events",
    "/api/alerts",
    "/api/alert-rules",
    "/api/alert-stats",
    "/api/meta",
]


@pytest.fixture
def rbac_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An app whose user file holds one admin and one viewer.

    Follows the wiring in test_api.py's ``client`` fixture: ``create_app()`` takes no
    arguments, so configuration reaches the routes through ``dependency_overrides``.
    ``users_file=`` is passed explicitly rather than set as an environment variable,
    because the session fixture in conftest has already primed ``get_settings``'s
    lru_cache and an env var alone would be ignored.
    """
    from solar_api import main as mainmod
    from solar_api import users as usersmod
    from solar_api.config import get_settings as real_get_settings

    # Cheap hashing. The store's *verification* path -- format parsing, salt handling,
    # digest comparison -- is untouched by this; only the deliberate delay is removed.
    monkeypatch.setattr(usersmod, "DEFAULT_ITERATIONS", 1_000)

    path = tmp_path / "users.yaml"
    path.write_text(
        "users:\n"
        "  - username: admin\n"
        "    role: admin\n"
        f'    password_hash: "{usersmod.hash_password(ADMIN_PASSWORD, iterations=1_000)}"\n'
        "  - username: viewer\n"
        "    role: viewer\n"
        f'    password_hash: "{usersmod.hash_password(VIEWER_PASSWORD, iterations=1_000)}"\n'
    )
    usersmod._cached_users.cache_clear()
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    real_get_settings.cache_clear()

    # The login rate limiter is a module-level singleton keyed on peer address, and every
    # test client here presents the same loopback one. Without this reset the third test
    # in the file gets 429s from its predecessors' logins -- a real cross-file leak that
    # only shows up in a full run.
    from solar_api.auth import limiter

    limiter.reset()

    settings = Settings(
        INFLUX_API_TOKEN="test-token",
        API_SECRET_KEY="test-secret-key-long-enough-for-hmac-sha256-0123456789",
        # A different password from either account in the file. A login that succeeds
        # with this would mean the file was bypassed.
        API_ADMIN_PASSWORD="the-env-fallback-password",
        API_ADMIN_USERNAME="admin",
        API_USERS_FILE=path,
        ALERTS_ENABLED="false",
    )

    stub = StubInflux()
    app = mainmod.create_app()
    app.dependency_overrides[real_get_settings] = lambda: settings
    app.dependency_overrides[mainmod._client] = lambda: stub

    # client= pins the peer address so the localhost-only guard on /api/explore sees a
    # real loopback address rather than Starlette's "testclient".
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        test_client.app.state.influx = stub
        test_client.stub = stub
        # The exact Settings the routes were wired with. Tests that need to mint a token
        # or read the user file must use this one, not a freshly built Settings: a second
        # instance with the same secret happens to work today, but nothing guarantees it.
        test_client.settings = settings
        yield test_client
    usersmod._cached_users.cache_clear()
    real_get_settings.cache_clear()


def token_for(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- login ------------------------------------------------------------------


def test_each_role_can_log_in_and_learn_which_it_is(rbac_client: TestClient):
    for username, password, role in (
        ("admin", ADMIN_PASSWORD, "admin"),
        ("viewer", VIEWER_PASSWORD, "viewer"),
    ):
        token = token_for(rbac_client, username, password)
        assert rbac_client.get("/api/auth/me", headers=bearer(token)).json() == {
            "subject": username,
            "role": role,
        }


def test_a_wrong_password_and_an_unknown_user_are_indistinguishable(rbac_client: TestClient):
    """A distinct error per failure mode is a username oracle.

    Splitting them is the most common way a login form hands an attacker the list of valid
    accounts for free, and it is not a hypothetical: this project already found a
    first-party instance of the same class of mistake in ``/api/explore``.
    """
    wrong_password = rbac_client.post(
        "/api/auth/login", json={"username": "viewer", "password": "nope"}
    )
    unknown_user = rbac_client.post(
        "/api/auth/login", json={"username": "nobody", "password": VIEWER_PASSWORD}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_the_env_fallback_is_not_used_when_a_user_file_exists(
    rbac_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A hashed store must not be bypassable with the .env password.

    If the file is present but a credential in it fails, the correct behaviour is to
    refuse. Falling through to the .env account would mean rotating a digest in
    users.yaml could not actually lock anyone out.
    """
    monkeypatch.setenv("API_ADMIN_PASSWORD", "the-old-env-password")
    response = rbac_client.post(
        "/api/auth/login", json={"username": "admin", "password": "the-old-env-password"}
    )
    assert response.status_code == 401


# --- the boundary -----------------------------------------------------------


def test_a_viewer_can_read_everything_it_is_meant_to(rbac_client: TestClient):
    token = token_for(rbac_client, "viewer", VIEWER_PASSWORD)
    for path in VIEWER_ALLOWED:
        response = rbac_client.get(path, headers=bearer(token))
        assert response.status_code != 403, f"{path} should be allowed for a viewer"
        assert response.status_code != 401, f"{path} rejected the viewer's token: {path}"


def test_a_viewer_is_refused_raw_sql_with_403_not_401(rbac_client: TestClient):
    """The whole point of the role.

    403 rather than 401 because the token is valid; 401 would make a correct client log
    the user out and re-authenticate forever against a policy it cannot change.
    """
    token = token_for(rbac_client, "viewer", VIEWER_PASSWORD)
    response = rbac_client.get(
        "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(token)
    )
    assert response.status_code == 403, response.text
    assert "viewer" in response.text, "the error should name the role it refused"


def test_an_admin_is_allowed_raw_sql(rbac_client: TestClient):
    token = token_for(rbac_client, "admin", ADMIN_PASSWORD)
    response = rbac_client.get(
        "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(token)
    )
    # Reached the query layer; a 502/400 means the SQL guard rejected it, which is the
    # next check down and is covered in test_api.py.
    assert response.status_code != 403, response.text


def test_no_token_is_still_401_not_403(rbac_client: TestClient):
    """Anonymous and unauthorised are different states and must not be conflated."""
    assert rbac_client.get("/api/explore", params={"sql": "SELECT 1"}).status_code == 401


def test_a_token_with_no_role_claim_is_treated_as_a_viewer(rbac_client: TestClient):
    """Tokens minted before roles existed must lose privilege, not gain it.

    The claim is *absent*, not empty, in a token issued by the previous version of this
    code. Defaulting it to admin would mean every token already in the wild -- in a
    browser, in a Postman collection, in a shell history -- gained the raw-SQL surface the
    moment this shipped.

    Signed by hand rather than through ``issue_token`` on purpose: the whole point is to
    reproduce a token that today's code cannot produce.
    """
    import time

    import jwt

    settings = rbac_client.settings
    now = int(time.time())
    legacy = jwt.encode(
        # Exactly the pre-RBAC payload: sub, iat, exp, and no role.
        {"sub": "legacy-admin", "iat": now, "exp": now + 300},
        settings.api_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    assert "role" not in decode_token(legacy, settings), "fixture is wrong; the claim is present"

    assert rbac_client.get("/api/auth/me", headers=bearer(legacy)).json()["role"] == "viewer"
    assert (
        rbac_client.get(
            "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(legacy)
        ).status_code
        == 403
    )


# --- the role change is a policy decision, not a live lookup ---------------


def test_a_role_change_applies_to_new_logins_and_not_to_live_tokens(rbac_client: TestClient):
    """Documented behaviour, pinned so a change to it is deliberate.

    The role is baked into the token at login, so demoting a user does not evict their
    existing session -- it takes effect when that token expires, or immediately for
    everyone if API_SECRET_KEY is rotated. The alternative, re-reading the file on every
    request, would mean a file read per API call; the trade is stated in
    docs/04-security.md so an operator does not discover it by being surprised.
    """
    from solar_api import users as usersmod

    user_file = rbac_client.settings.users_file
    before = user_file.read_text()
    token = token_for(rbac_client, "viewer", VIEWER_PASSWORD)
    assert rbac_client.get(
        "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(token)
    ).status_code == 403

    # Promote the same account to admin in the file.
    user_file.write_text(before.replace("role: viewer", "role: admin"))
    usersmod._cached_users.cache_clear()

    new_token = token_for(rbac_client, "viewer", VIEWER_PASSWORD)
    assert rbac_client.get(
        "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(new_token)
    ).status_code != 403
    # ... and the old token is unaffected, as documented.
    assert rbac_client.get(
        "/api/explore", params={"sql": "SELECT 1"}, headers=bearer(token)
    ).status_code == 403
