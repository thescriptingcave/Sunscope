"""Users, roles, and password hashing at rest.

WHY THIS EXISTS
---------------
There was one user, one password, and that password was **not hashed**: it was compared with
``hmac.compare_digest`` against a value in ``.env``. The module that said so also said it
"would not be acceptable for a multi-user system, where a database-stored hash is the only
thing that protects a password at rest". This is that system, so the hash is now the thing.

Two roles, and the boundary is the one that matters:

============  ==========================================================
``viewer``    Read telemetry, read alerts, use the live feed. **No raw
              SQL.** Cannot reach ``/api/explore``.
``admin``     Everything, including ``/api/explore``.
============  ==========================================================

``/api/explore`` is the endpoint to draw the line at. It runs arbitrary read-only SQL through
an **admin-scoped** InfluxDB token, because InfluxDB 3 Core has no permission-scoped tokens
(``docs/04-security.md`` 4.4). Every authenticated user could reach it, so "authenticated" was
doing no work. With roles, a read-only account can be issued without handing out the database.

Where users live
----------------
``api/config/users.yaml``, a file on disk. A file rather than a database, because:

* there are two of them, and a table would be the wrong shape for that;
* the API is otherwise read-only, and writing credentials to the telemetry database would
  contradict ``docs/01-design.md``;
* it is rotatable by editing one line, with no migration and no second system to keep
  consistent with it.

It is **gitignored**, with ``api/config/users.yaml.example`` committed in its place. A
PBKDF2 digest is not a plaintext secret, but it *is* offline-crackable: publishing a
digest for a password an attacker can guess turns the account into a published
credential, and the obvious way to end up doing that is to generate a throwaway viewer
account, commit it "just as an example", and forget that the password is now in a public
repository's history. The example file documents the format with digests that cannot
verify against anything.

The single-operator fallback is preserved: with no user file configured, the ``.env``
credentials still work and get the ``admin`` role. That keeps an existing checkout running
after an upgrade, and it is reported at startup so nobody believes they have RBAC when they
do not.

Password format
---------------
``pbkdf2-sha256$<iterations>$<salt-hex>$<hash-hex>``, with the parameters stored alongside
the digest so they can be raised later without invalidating existing passwords. PBKDF2 is in
the standard library, which is the deciding factor: a key-derivation function chosen to avoid
a dependency is fine, one chosen to avoid thinking about it is not. 600,000 iterations is
OWASP's current floor for PBKDF2-HMAC-SHA256.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger("solar_api")

#: The only two roles. Kept to two on purpose: a matrix nobody has asked for is a matrix
#: nobody tests, and this one is chosen so the boundary matches the actual risk.
Role = Literal["admin", "viewer"]

ROLE_ADMIN: Role = "admin"
ROLE_VIEWER: Role = "viewer"
ROLES: tuple[Role, ...] = (ROLE_ADMIN, ROLE_VIEWER)

#: What each role may do. Enforced by ``require_role``; kept here so the policy is readable
#: in one place rather than scattered across decorators.
#:
#: Anything not listed is admin-only. That default-deny shape is deliberate: adding a new
#: endpoint without thinking about roles should lock it down, not expose it.
ROLE_CAPABILITIES: dict[Role, frozenset[str]] = {
    ROLE_VIEWER: frozenset(
        {
            "telemetry:read",  # /api/now, /api/summary, /api/series, /api/strings, /api/events
            "alerts:read",  # /api/alerts, /api/alert-rules, /api/alert-stats
            "live:read",  # /api/live-ticket, /api/live
            "meta:read",  # /api/meta
        }
    ),
    ROLE_ADMIN: frozenset({"*"}),  # everything
}

#: OWASP's current floor for PBKDF2-HMAC-SHA256. Deliberately slow: it is a login, and a
#: fast hash makes offline cracking of a leaked file cheap.
#:
#: Immutable and separate from ``DEFAULT_ITERATIONS`` on purpose. The test suite lowers the
#: latter to keep the run quick, and a test that asserts the *production* value must not be
#: asserting the number a fixture just overwrote -- which is exactly the bug this split
#: prevents. ``test_the_production_iteration_count_is_the_documented_one`` pins this.
PRODUCTION_ITERATIONS = 600_000
_SALT_BYTES = 16

#: What hashing actually uses. Overridable so the suite is not dominated by a deliberate
#: delay; the verification path -- salt handling, digest comparison, format parsing -- is
#: identical either way.
DEFAULT_ITERATIONS = int(os.environ.get("SUNSCOPE_PBKDF2_ITERATIONS", PRODUCTION_ITERATIONS))


class UserStoreError(RuntimeError):
    """The user file is present but unusable. A startup failure, not a runtime surprise."""


@dataclass(frozen=True)
class User:
    username: str
    role: Role
    password_hash: str

    def can(self, capability: str) -> bool:
        """Default-deny: unlisted capabilities require admin."""
        grants = ROLE_CAPABILITIES.get(self.role, frozenset())
        return "*" in grants or capability in grants


# --- hashing -----------------------------------------------------------------


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Hash a password into the stored format.

    Salt is per-user and random, so two users with the same password get different digests and
    a leaked file cannot be attacked with a shared rainbow table.
    """
    if not password:
        raise UserStoreError("refusing to hash an empty password")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2-sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password_hashed(candidate: str, stored: str) -> bool:
    """Verify against a stored digest. Constant-time, and never raises on bad input.

    A malformed stored value returns False rather than raising, so a corrupt file denies
    access instead of taking the API down -- and so a probe cannot distinguish the two.
    """
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2-sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", candidate.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # compare_digest on equal-length bytes; pbkdf2 output length is fixed by the algorithm
    # so a length mismatch is already a failure.
    return hmac.compare_digest(actual, expected)


# --- the store ---------------------------------------------------------------


def _coerce_role(raw: Any, username: str) -> Role:
    if raw not in ROLES:
        raise UserStoreError(
            f"user {username!r} has role {raw!r}; expected one of {', '.join(ROLES)}"
        )
    return raw  # type: ignore[return-value]


def load_users(path: Path) -> list[User]:
    """Parse a user file. Raises :class:`UserStoreError` on anything malformed."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise UserStoreError(f"{path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise UserStoreError(f"cannot read {path}: {exc}") from exc

    entries = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise UserStoreError(f"{path} must contain a 'users:' list")

    users: list[User] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise UserStoreError(
                f"{path}: every user must be a mapping, got {type(entry).__name__}"
            )
        username = str(entry.get("username", "")).strip()
        digest = str(entry.get("password_hash", "")).strip()
        if not username or not digest:
            raise UserStoreError(f"{path}: every user needs 'username' and 'password_hash'")
        if username in seen:
            raise UserStoreError(f"{path}: duplicate user {username!r}")
        seen.add(username)
        users.append(
            User(
                username=username,
                role=_coerce_role(entry.get("role", ROLE_VIEWER), username),
                password_hash=digest,
            )
        )
    if not users:
        raise UserStoreError(f"{path} lists no users")
    return users


@lru_cache(maxsize=4)
def _cached_users(resolved: str, mtime: float) -> tuple[User, ...]:  # noqa: ARG001
    return tuple(load_users(Path(resolved)))


def get_users(path: Path | None) -> tuple[User, ...]:
    """Users from the file at *path*, or an empty tuple when there is no file.

    Cached on the file's mtime so editing the file takes effect without a restart, which is
    the point of keeping it in the repository rather than a database.
    """
    if path is None or not path.exists():
        return ()
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:  # pragma: no cover - stat failing after exists() is a race
        raise UserStoreError(f"cannot stat {path}: {exc}") from exc
    return _cached_users(str(path), mtime)


def find_user(users: tuple[User, ...], username: str) -> User | None:
    for user in users:
        if user.username == username:
            return user
    return None


# --- the single-operator fallback -------------------------------------------


def env_admin(username: str, password: str) -> User | None:
    """The pre-RBAC credentials, if they are configured.

    Kept so an existing checkout keeps working after an upgrade, and so the single-operator
    case stays a supported deployment rather than a migration. It is a **degraded mode**:
    the password is still the unhashed ``.env`` value, and the startup banner says so.
    """
    if not password:
        return None
    return User(username=username, role=ROLE_ADMIN, password_hash=f"env:{password}")


def check_password(candidate: str, user: User) -> bool:
    """Verify against whichever form the user's password is stored in.

    Two forms exist on purpose: the hashed one from ``api/config/users.yaml``, and the
    unhashed ``.env`` value for the single-operator fallback. Keeping the comparison in one
    place is what stops the fallback from becoming a second, less careful code path.
    """
    if user.password_hash.startswith("env:"):
        return hmac.compare_digest(candidate.encode(), user.password_hash[4:].encode())
    return verify_password_hashed(candidate, user.password_hash)


def resolve_user(settings: Any) -> tuple[User, ...]:
    """Every user the API will accept: the file's, else the env admin, else nothing.

    Never both. If a user file exists it is authoritative, so a stale ``.env`` password
    cannot be used to slip past a rotated credential.
    """
    users = get_users(settings.users_file)
    if users:
        if settings.api_admin_password:
            log.warning(
                "%s is configured, so API_ADMIN_PASSWORD is ignored. Remove it from .env "
                "once you are happy with the user file.",
                settings.users_file,
            )
        return users
    fallback = env_admin(settings.api_admin_username, settings.api_admin_password)
    if fallback:
        log.warning(
            "No user file at %s: falling back to the single %r account from .env, whose "
            "password is NOT hashed at rest. Run scripts/gen-secrets.sh to create a user file.",
            settings.users_file,
            fallback.username,
        )
        return (fallback,)
    return ()


# --- CLI ---------------------------------------------------------------------


def _main() -> int:  # pragma: no cover - operator tool
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="hash a password for api/config/users.yaml")
    parser.add_argument(
        "password",
        nargs="?",
        help="the password. Prefer omitting it and piping on stdin: an argument lands in "
        "the shell history and in `ps` output for as long as the process lives.",
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument(
        "--add",
        metavar="FILE",
        help="append the new entry to this users.yaml instead of printing the digest. "
        "Use '-' for stdout. Refuses to overwrite: a regenerated file would silently "
        "re-enable a password that was rotated out of it.",
    )
    parser.add_argument("--user", default="admin", help="username for --add (default: admin)")
    parser.add_argument(
        "--role",
        default=ROLE_ADMIN,
        choices=sorted(ROLE_CAPABILITIES),
        help="role for --add (default: admin)",
    )
    args = parser.parse_args()

    if args.password is not None:
        password = args.password
    elif sys.stdin.isatty():
        import getpass

        # getpass, not input: an echoed password is a password in the scrollback.
        password = getpass.getpass("password: ")
    else:
        # Piped, as gen-secrets.sh does. rstrip only the trailing newline the pipe adds,
        # so a password that genuinely ends in whitespace still works.
        password = sys.stdin.read().rstrip("\n")

    if len(password) < 12:
        print("warning: shorter than 12 characters", file=sys.stderr)
    digest = hash_password(password, iterations=args.iterations)

    if not args.add:
        print(digest)
        return 0

    # Deliberately hand-assembled rather than round-tripped through a YAML dumper: a dump
    # would strip every comment in the file, and those comments are the documentation.
    entry = (
        f"  - username: {args.user}\n"
        f"    role: {args.role}\n"
        f'    password_hash: "{digest}"\n'
    )
    if args.add == "-":
        sys.stdout.write(entry)
        return 0
    with open(args.add, "a+", encoding="utf-8") as handle:
        # A file this tool or gen-secrets.sh created earlier already has `users:`. A
        # hand-made one may not, and appending an entry to it yields a file that refuses
        # to load. The error is loud rather than silent, but a command that only works
        # against a file it made itself is a trap for whoever is handed the hint.
        #
        # Looked up as a top-level key rather than as a prefix: a real file starts with a
        # comment header, so requiring the first non-space character to be `users:` would
        # reject every file gen-secrets.sh writes.
        handle.seek(0)
        existing = handle.read()
        if not re.search(r"^users\s*:", existing, re.MULTILINE):
            if existing.strip():
                # Refuse rather than guess. Appending `users:` after an unrelated document
                # would produce something that parses as the wrong thing.
                print(
                    f"refusing to append: {args.add} exists but has no top-level 'users:' "
                    "key. Add the key yourself, or write to a new file.",
                    file=sys.stderr,
                )
                return 1
            handle.write("users:\n")
        handle.write(entry)
    print(f"appended {args.user} ({args.role}) to {args.add}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
