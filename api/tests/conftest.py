"""Make the API test suite independent of the developer's ``.env``.

WHY THIS FILE EXISTS
--------------------
The API's lifespan calls ``get_settings()`` **directly** rather than through
``Depends``, so a test's ``dependency_overrides`` cannot reach it: the lifespan
validates the real runtime configuration and raises ``SystemExit(1)`` when it is
incomplete. The per-test fixtures already work around this for the *routes* by
constructing a ``Settings(...)`` and overriding the dependency, but the lifespan
still reads the environment.

That made the "unit" tests pass on a developer machine -- where ``.env`` and
``secrets/`` exist because someone has run ``bootstrap.sh up`` -- and fail
anywhere they do not, which is exactly what CI found:

    ERROR at setup of test_health_is_unauthenticated
    ... SystemExit(1)  (from starlette/middleware/errors.py)

A test that needs a config file on disk is not a unit test. The three settings
``validate_runtime()`` insists on are set here, once, for the whole session, so
the suite behaves identically on a laptop and on a bare runner.

``secrets/api-read.token`` is deliberately *not* required: ``validate_runtime``
accepts either that file or ``INFLUX_API_TOKEN``, and the latter is set below.
Creating the file needs a live InfluxDB, which is the opposite of a unit test.

``get_settings`` is cached with ``lru_cache``, and the lifespan may already have
built a ``Settings`` from the developer's real ``.env`` before this runs, so the
cache is cleared on both entry and exit. Without that, whichever test imported
first would pin the configuration for the rest of the session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

#: The values ``Settings.validate_runtime()`` requires. Deliberately obvious
#: placeholders: nothing here may be used to reach a real system, and a test that
#: accidentally authenticated with them would fail loudly rather than quietly
#: succeed against production.
_TEST_ENV = {
    "API_SECRET_KEY": "test-secret-key-long-enough-for-hmac-sha256-0123456789",
    "API_ADMIN_PASSWORD": "correct-password",
    "API_ADMIN_USERNAME": "admin",
    "INFLUX_API_TOKEN": "test-token",
    # Point at a path that does not exist. Without this the suite reads the real
    # api/config/users.yaml, and every test that logs in as "admin" with
    # _TEST_ENV's password fails against the developer's actual digest -- a unit test
    # depending on the machine it runs on, which is the exact bug this file exists to
    # prevent. Tests that want the file present build their own; see test_users.py.
    "API_USERS_FILE": "/nonexistent/sunscope-test-users.yaml",
    # The alert engine subscribes to a broker; no test fixture starts one, and
    # test_alert_service.py drives the engine directly with controlled input.
    "ALERTS_ENABLED": "false",
}


@pytest.fixture(autouse=True, scope="session")
def _isolated_runtime_config() -> Iterator[None]:
    """Pin the runtime configuration for the whole session.

    Restores whatever was there before, so running pytest from inside the
    repository does not leave a mutated environment behind for the next command.
    """
    from solar_api.config import get_settings

    previous = {key: os.environ.get(key) for key in _TEST_ENV}
    os.environ.update(_TEST_ENV)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
