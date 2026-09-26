"""Short-lived single-use tickets for the live WebSocket.

A browser cannot set an ``Authorization`` header on a WebSocket handshake, so the
obvious workaround is to put the JWT in the query string. That is what most
examples do and it is a bad idea: URLs land in access logs, proxy logs and
browser history, and a JWT is valid for hours.

So the live socket is opened with a ticket instead. The ticket is

* minted only by an authenticated caller,
* valid for 30 seconds,
* consumed on first use,
* worthless once used.

A leaked ticket -- from a log line, a shoulder-surfer, a shared screen -- buys an
attacker at most one unauthenticated live feed for half a minute. The long-lived
credential never leaves an ``Authorization`` header.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

#: Long enough to cover a handshake and a slow client, short enough that a
#: leaked ticket is not a meaningful credential.
TICKET_TTL_S = 30.0

#: Cap the map so a caller that mints and never uses cannot grow it without
#: bound. Expired entries are swept on every issue and consume.
MAX_TICKETS = 256


@dataclass
class _Ticket:
    subject: str
    expires_at: float


class TicketStore:
    """In-memory, single-process ticket store.

    Deliberately not shared storage: this exists to make a local development
    stack work, and a single API process is all there is. If the API is ever
    scaled to more than one worker or replica, this must become a shared store or
    the second process will reject tickets the first minted.
    """

    def __init__(self, ttl_s: float = TICKET_TTL_S) -> None:
        self._ttl = ttl_s
        self._tickets: dict[str, _Ticket] = {}

    def issue(self, subject: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        # url-safe, and long enough that guessing is hopeless.
        token = secrets.token_urlsafe(32)
        self._tickets[token] = _Ticket(subject=subject, expires_at=now + self._ttl)
        # Sweep *after* inserting. Sweeping first let the map reach
        # MAX_TICKETS + 1, because the cap was only enforced on the following
        # call. A small leak, but an off-by-one in a bound is still wrong.
        self._sweep(now)
        return token

    def consume(self, token: str, now: float | None = None) -> str | None:
        """Return the subject, or ``None`` if unknown, expired or already used.

        Popping on success is what makes it single-use: a replayed ticket from a
        log line gets nothing.
        """
        now = time.time() if now is None else now
        self._sweep(now)
        ticket = self._tickets.pop(token, None)
        if ticket is None or ticket.expires_at < now:
            return None
        return ticket.subject

    @property
    def outstanding(self) -> int:
        return len(self._tickets)

    def _sweep(self, now: float) -> None:
        expired = [k for k, v in self._tickets.items() if v.expires_at < now]
        for key in expired:
            del self._tickets[key]
        # Oldest-first eviction if somehow still full after sweeping.
        while len(self._tickets) > MAX_TICKETS:
            oldest = min(self._tickets, key=lambda k: self._tickets[k].expires_at)
            del self._tickets[oldest]
