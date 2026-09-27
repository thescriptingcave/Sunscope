"""Authentication and role-based authorisation.

A username and password become a signed JWT that carries the user's **role**, so
authorisation needs no lookup on the hot path and cannot drift from what was
issued at login.

Passwords are hashed at rest -- see :mod:`solar_api.users`. The pre-RBAC design compared an
unhashed ``.env`` value with :func:`hmac.compare_digest`, and its own docstring recorded that
this "would not be acceptable for a multi-user system, where a database-stored hash is the
only thing that protects a password at rest". That is now this system. The ``.env`` account
survives as a single-operator fallback and is reported at startup as degraded.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .users import ROLE_ADMIN, ROLE_VIEWER, User, check_password, resolve_user

#: bearer scheme. auto_error=False so a missing header gives our own 401 shape.
bearer = HTTPBearer(auto_error=False)


def authenticate(username: str, candidate: str, settings: Settings) -> User | None:
    """Return the user when the password is right, else ``None``.

    Uniform failure: an unknown username and a wrong password are indistinguishable to the
    caller, and both are cheap enough not to leak through timing -- the unknown-user path
    still runs a hash comparison against a dummy digest.
    """
    users = resolve_user(settings)
    user = next((u for u in users if u.username == username), None)
    if user is None:
        # Spend the time anyway, so response latency does not reveal whether the
        # username exists.
        check_password(candidate, User("_absent", ROLE_ADMIN, "pbkdf2-sha256$1$00$00"))
        return None
    return user if check_password(candidate, user) else None


def issue_token(user: User, settings: Settings) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)`` for *user*.

    The role is a claim, so authorisation never has to re-read the user file. The cost is
    that a role change does not take effect until the token expires -- ``jwt_ttl_seconds``,
    which is why it is not a large number. Rotating ``API_SECRET_KEY`` invalidates every
    outstanding token at once, which is the blunt instrument for a compromised account.
    """
    now = int(time.time())
    payload = {
        "sub": user.username,
        "role": user.role,
        "iat": now,
        "exp": now + settings.jwt_ttl_seconds,
    }
    token = jwt.encode(payload, settings.api_secret_key, algorithm=settings.jwt_algorithm)
    return token, settings.jwt_ttl_seconds


def decode_token(token: str, settings: Settings) -> dict:
    """Verify a JWT. Raises :class:`HTTPException` on any problem."""
    try:
        return jwt.decode(
            token,
            settings.api_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        # Deliberately vague: a precise reason tells an attacker which half of
        # the guess was right.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc


class SlidingWindowLimiter:
    """In-process sliding-window rate limiter.

    Deliberately in-process: it is per-instance, so with several API workers the
    effective limit is multiplied by the worker count. That is fine for one
    local process and is called out in the security doc's known gaps.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_s: float) -> None:
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(bucket[0] + window_s - now, 0.0)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        bucket.append(now)

    def reset(self) -> None:
        self._hits.clear()


#: Shared limiter. Tests reset it explicitly.
limiter = SlidingWindowLimiter()


def rate_limit_login(request_ip: str, settings: Settings) -> None:
    limiter.check(f"login:{request_ip}", settings.login_rate_limit, settings.login_rate_window_s)


def rate_limit_query(subject: str, settings: Settings) -> None:
    limiter.check(f"query:{subject}", settings.query_rate_limit, settings.query_rate_window_s)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    """FastAPI dependency: resolve the caller's username from the JWT."""
    return _claims_to_subject(_require_claims(credentials, settings))


def _require_claims(
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token(credentials.credentials, settings)
    if not claims.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return claims


def _claims_to_subject(claims: dict) -> str:
    return str(claims["sub"])


async def current_role(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    """The caller's role, from the token.

    Defaults to ``viewer`` when the claim is absent, so a token minted before roles existed
    has less privilege rather than more.
    """
    return str(_require_claims(credentials, settings).get("role", ROLE_VIEWER))


def require_capability(capability: str):
    """Dependency factory: require *capability*, or 403.

    **403, not 401.** The caller is authenticated; they are simply not allowed. Returning 401
    would tell a viewer their token is bad and send them to log in again, which is both wrong
    and confusing.

    A token with no ``role`` claim is treated as ``viewer``, not ``admin``. A token minted
    before roles existed, or by an older build, therefore loses privilege rather than gaining
    it -- the safe direction for a missing claim to fail.
    """

    async def dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        settings: Settings = Depends(get_settings),
    ) -> str:
        claims = _require_claims(credentials, settings)
        role = claims.get("role", ROLE_VIEWER)
        user = User(str(claims["sub"]), role, "")
        if not user.can(capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {role!r} may not {capability}",
            )
        return str(claims["sub"])

    return dependency


def require_role(capability: str):
    """Alias kept for readability at the call site: ``Depends(require_role("..."))``."""
    return require_capability(capability)
