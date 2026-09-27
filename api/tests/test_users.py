"""Tests for users, roles, and password hashing at rest.

These matter more than their size suggests. The previous design compared an
unhashed ``.env`` password with ``hmac.compare_digest`` and its own docstring recorded
that this "would not be acceptable for a multi-user system". This is that system, so
these tests pin the properties that make it one:

* passwords are never stored in the clear, and the stored form is a real KDF;
* the salt is per-user, so a shared-password attack on a leaked file fails;
* roles are **default-deny**, so a new capability locks itself down;
* a token with no role claim loses privilege rather than gaining it;
* a malformed user file fails closed, at startup, loudly.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from solar_api.users import (
    ROLE_ADMIN,
    ROLE_VIEWER,
    User,
    UserStoreError,
    check_password,
    env_admin,
    find_user,
    get_users,
    hash_password,
    load_users,
    verify_password_hashed,
)


# PBKDF2 at the production iteration count is deliberately slow -- a login, and a fast
# hash makes offline cracking of a leaked file cheap. Lowering it for the suite keeps the
# tests quick while leaving the *verification* path unchanged: salt handling, digest
# comparison and format parsing are all still exercised, only the iteration count differs.
@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch: pytest.MonkeyPatch):
    """Drop the iteration count for tests. The real value is asserted separately."""
    monkeypatch.setenv("SUNSCOPE_PBKDF2_ITERATIONS", "1000")
    from solar_api import users as usersmod

    usersmod.DEFAULT_ITERATIONS = 1000
    yield
    usersmod.DEFAULT_ITERATIONS = 600_000


@pytest.fixture
def user_file(tmp_path: Path) -> Path:
    path = tmp_path / "users.yaml"
    path.write_text(
        "users:\n"
        "  - username: alice\n"
        "    role: admin\n"
        f'    password_hash: "{hash_password("correct-horse", iterations=1000)}"\n'
        "  - username: bob\n"
        "    role: viewer\n"
        f'    password_hash: "{hash_password("battery-staple", iterations=1000)}"\n'
    )
    return path


# --- hashing -----------------------------------------------------------------


def test_a_password_is_never_stored_in_the_clear():
    digest = hash_password("hunter2", iterations=1000)
    assert "hunter2" not in digest
    assert digest.startswith("pbkdf2-sha256$1000$")


def test_the_digest_round_trips():
    digest = hash_password("s3cret", iterations=1000)
    assert verify_password_hashed("s3cret", digest) is True
    assert verify_password_hashed("s3cret ", digest) is False
    assert verify_password_hashed("S3CRET", digest) is False


def test_two_users_with_the_same_password_get_different_digests():
    """A per-user salt, so a leaked file cannot be attacked with a shared table."""
    a = hash_password("identical", iterations=1000)
    b = hash_password("identical", iterations=1000)
    assert a != b
    assert verify_password_hashed("identical", a) is True
    assert verify_password_hashed("identical", b) is True


def test_a_malformed_digest_denies_rather_than_raising():
    """A corrupt entry must lock the account, not take the API down.

    It must also deny *quietly* in shape: a probe should not be able to tell a corrupt
    record from a wrong password by the error it gets back.
    """
    for stored in ("", "garbage", "pbkdf2-sha256$abc$zz$zz", "md5$1$aa$bb", "a$b$c"):
        assert verify_password_hashed("anything", stored) is False, stored


def test_an_empty_password_is_refused_outright():
    with pytest.raises(UserStoreError):
        hash_password("", iterations=1000)


def test_the_production_iteration_count_is_the_documented_one():
    """Guards the constant, not the speed.

    Lowering this is the quiet way to make offline cracking cheap. The cost is asserted
    rather than left to a comment.
    """
    from solar_api import users as usersmod

    # PRODUCTION_ITERATIONS, not DEFAULT_ITERATIONS: the fixture above has lowered the
    # latter, so asserting it would only prove the fixture works.
    assert usersmod.PRODUCTION_ITERATIONS >= 600_000, (
        "PBKDF2 iterations must not drop below OWASP's floor for SHA-256"
    )
    # And the cost is real. A constant nobody can feel is a constant nobody maintains.
    start = time.monotonic()
    hash_password("x", iterations=usersmod.PRODUCTION_ITERATIONS)
    elapsed = time.monotonic() - start
    assert elapsed > 0.05, f"{usersmod.PRODUCTION_ITERATIONS} iterations took {elapsed:.3f}s"


# --- roles -------------------------------------------------------------------


def test_admin_can_do_everything():
    admin = User("a", ROLE_ADMIN, "")
    for capability in ("telemetry:read", "alerts:read", "live:read", "sql:raw", "anything:new"):
        assert admin.can(capability) is True, capability


def test_a_viewer_can_read_but_not_run_sql():
    viewer = User("v", ROLE_VIEWER, "")
    for capability in ("telemetry:read", "alerts:read", "live:read", "meta:read"):
        assert viewer.can(capability) is True, capability
    assert viewer.can("sql:raw") is False


def test_capabilities_are_default_deny():
    """A capability nobody has thought about must be refused, not allowed.

    This is the property that makes adding an endpoint safe: a new `Depends` on a
    capability that is not in the viewer's grant set locks it down until someone
    deliberately opens it.
    """
    viewer = User("v", ROLE_VIEWER, "")
    assert viewer.can("some:brand-new:capability") is False


# --- the file ----------------------------------------------------------------


def test_the_file_loads(user_file: Path):
    users = load_users(user_file)
    assert [u.username for u in users] == ["alice", "bob"]
    assert find_user(tuple(users), "bob").role == ROLE_VIEWER


def test_the_file_is_cached_on_mtime_so_edits_take_effect(user_file: Path):
    from solar_api import users as usersmod

    usersmod._cached_users.cache_clear()
    assert len(get_users(user_file)) == 2
    user_file.write_text(
        user_file.read_text() + '  - username: carol\n    role: viewer\n'
        f'    password_hash: "{hash_password("c", iterations=1000)}"\n'
    )
    assert len(get_users(user_file)) == 3, "an edit must be visible without a restart"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("not: a list\n", "no users key"),
        ("users: []\n", "empty"),
        ("users:\n  - username: a\n", "no password_hash"),
        ("users:\n  - role: admin\n", "no username"),
        ("users:\n  - username: a\n    role: wizard\n    password_hash: x\n", "unknown role"),
        ("users:\n  - username: a\n    role: admin\n", "malformed hash"),
    ],
)
def test_a_broken_file_is_refused_loudly(tmp_path: Path, body: str, why: str):
    """A credential file that does not parse is a startup failure, not a silent default.

    Failing open here would mean an unreadable users file quietly downgrades everyone, or
    worse, quietly keeps the old password working.
    """
    path = tmp_path / "users.yaml"
    path.write_text(body)
    with pytest.raises(UserStoreError):
        load_users(path)


def test_duplicate_usernames_are_refused(tmp_path: Path):
    path = tmp_path / "users.yaml"
    digest = hash_password("x", iterations=1000)
    path.write_text(
        f'users:\n  - username: a\n    role: admin\n    password_hash: "{digest}"\n'
        f'  - username: a\n    role: viewer\n    password_hash: "{digest}"\n'
    )
    with pytest.raises(UserStoreError, match="duplicate"):
        load_users(path)


def test_a_missing_file_is_not_an_error(tmp_path: Path):
    """The single-operator case has no file, and that is a supported deployment."""
    assert get_users(tmp_path / "absent.yaml") == ()
    assert get_users(None) == ()


# --- the env fallback --------------------------------------------------------


def test_the_env_account_is_admin_and_still_compares_in_constant_time():
    """The pre-RBAC account survives as a fallback, and stays a degraded mode.

    Worth keeping: it means an existing checkout still runs after an upgrade. It is
    reported at startup as degraded precisely because the password is not hashed.
    """
    user = env_admin("admin", "from-dot-env")
    assert user is not None and user.role == ROLE_ADMIN
    assert user.can("sql:raw") is True
    assert check_password("from-dot-env", user) is True
    assert check_password("wrong", user) is False
    assert env_admin("admin", "") is None


# --- the operator CLI --------------------------------------------------------
#
# gen-secrets.sh drives this, and a mistake here produces a users.yaml that does not load
# or, worse, one that loads with the wrong account in it.


def _run_cli(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    """Invoke ``python -m solar_api.users`` in-process, capturing its streams.

    Returns ``(exit_code, stdout, stderr)``. ``stdin`` is installed rather than left
    alone: the CLI branches on ``isatty()`` to decide whether to prompt, and under
    pytest a real stdin is never a tty, so without this it reads the wrong thing.
    """
    import contextlib
    import io
    import sys

    from solar_api.users import _main

    out, err = io.StringIO(), io.StringIO()
    saved_argv, saved_stdin = sys.argv, sys.stdin
    sys.argv = ["solar_api.users", *argv]
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = _main()
    finally:
        sys.argv, sys.stdin = saved_argv, saved_stdin
    return code, out.getvalue(), err.getvalue()


def test_the_cli_prints_a_usable_digest_when_given_stdin():
    code, out, _ = _run_cli(["--iterations", "1000"], stdin="a-long-enough-password\n")
    assert code == 0
    digest = out.strip()
    assert verify_password_hashed("a-long-enough-password", digest) is True
    # The pipe adds a newline; a password that genuinely ends in whitespace must still work.
    assert verify_password_hashed("a-long-enough-password\n", digest) is False


def test_the_cli_add_writes_a_file_that_loads(tmp_path: Path):
    """The end-to-end operator path: hash a password, append it, then read it back."""
    path = tmp_path / "users.yaml"
    assert _run_cli(["--add", str(path), "--user", "alice", "--iterations", "1000"],
                    stdin="alice-password")[0] == 0
    assert _run_cli(["--add", str(path), "--user", "bob", "--role", "viewer",
                     "--iterations", "1000"], stdin="bob-password")[0] == 0

    users = load_users(path)
    assert [(u.username, u.role) for u in users] == [("alice", "admin"), ("bob", "viewer")]
    assert check_password("alice-password", find_user(tuple(users), "alice")) is True
    assert check_password("bob-password", find_user(tuple(users), "bob")) is True


def test_the_cli_add_creates_the_users_key_on_a_new_file(tmp_path: Path):
    """Appending an entry to a file with no `users:` key yields a file that will not load.

    Loud, but a command that only works against a file it made itself is a trap for
    whoever is handed the hint printed by gen-secrets.sh.
    """
    path = tmp_path / "users.yaml"
    assert _run_cli(["--add", str(path), "--iterations", "1000"], stdin="a-password")[0] == 0
    assert path.read_text().startswith("users:\n")


def test_the_cli_refuses_to_append_to_a_file_that_is_not_a_user_store(tmp_path: Path):
    """Guessing would produce something that parses as the wrong thing."""
    path = tmp_path / "other.yaml"
    path.write_text("something: else\n")
    code, _, err = _run_cli(["--add", str(path), "--iterations", "1000"], stdin="a-password")
    assert code == 1
    assert "refusing" in err
    assert path.read_text() == "something: else\n", "the file must be left untouched"


def test_the_cli_refuses_an_unknown_role(tmp_path: Path):
    """Rejected by argparse, which raises SystemExit rather than returning a code.

    The point is that it happens *before* the file is opened, so a typo in a role name
    cannot leave a half-written user store behind.
    """
    import pytest

    path = tmp_path / "u.yaml"
    with pytest.raises(SystemExit) as exit_info:
        _run_cli(["--add", str(path), "--role", "wizard"], stdin="a-password")
    assert exit_info.value.code == 2
    assert not path.exists(), "nothing should have been written"


def test_the_cli_add_works_on_a_file_that_starts_with_a_comment_header(tmp_path: Path):
    """The exact shape scripts/gen-secrets.sh writes.

    Requiring the first non-space character to be `users:` would reject every file that
    script produces, since all of them open with an explanatory header. Found by doing it
    the obvious way and then running the documented command against a real generated file.
    """
    path = tmp_path / "users.yaml"
    path.write_text("# a header\n# explaining things\nusers:\n")
    assert _run_cli(["--add", str(path), "--user", "carol", "--iterations", "1000"],
                    stdin="carol-password")[0] == 0
    assert [u.username for u in load_users(path)] == ["carol"]
