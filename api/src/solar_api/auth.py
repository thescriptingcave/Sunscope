"""Authentication: single-user JWT login with rate limiting.

Deliberately simple, because it is a single-operator tool: one username and
password from the environment, exchanged for a signed JWT.

The password is compared with :func:`hmac.compare_digest` rather than hashed.
That is a reasonable trade for a credential that lives in a local ``.env`` and
is never stored in a database, but it would **not** be acceptable for a
multi-user system, where a database-stored hash is the only thing that protects
a password at rest. See the known gaps in ``docs/04-security.md``.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

#: bearer scheme. auto_error=False so a missing header gives our own 401 shape.
bearer = HTTPBearer(auto_error=False)


def verify_password(candidate: str, settings: Settings) -> bool:
    """Constant-time comparison against the configured password."""
    expected = settings.api_admin_password
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def issue_token(username: str, settings: Settings) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    now = int(time.time())
    payload = {
        "sub": username,
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
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = decode_token(credentials.credentials, settings)
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        )
    return str(subject)
