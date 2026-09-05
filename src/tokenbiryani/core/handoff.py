"""Single-use tickets that hand an admin key from the terminal to the browser.

The key itself must never travel in a URL — it lands in shell history, in the
browser's own history, in a referrer, and in any proxy log on the way. That rule is
what the console's sign-in copy promises, and it is not negotiable.

A ticket can travel there, because it is worth nothing after one redemption:

    tokenbiryani console   reads the admin key from the config file
                           mints a ticket, authenticated with that key
                           opens  /console?t=<ticket>
    the page               redeems it once for the key, then rewrites the URL

The properties that make that safe are all here rather than spread across the
gateway: one redemption, a short life, and loopback only. The last one matters most
— a ticket is a bearer credential with no second factor, so it is minted only for a
gateway that nothing off-box can reach in the first place.
"""

from __future__ import annotations

import secrets
import time
from typing import Dict, Optional, Tuple

#: Long enough that guessing is hopeless inside the lifetime below.
TICKET_BYTES = 24

#: A ticket exists to survive the trip from an exec() to a page load. Sixty seconds
#: is generous for that and short enough that a shell history full of stale tickets
#: is a history full of nothing.
TTL_SECONDS = 60.0

#: Hosts a ticket may be minted for. A gateway reachable off-box needs a real login,
#: not a bearer token in a query string.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})


class HandoffError(RuntimeError):
    """Raised when a ticket cannot be minted, or cannot be redeemed."""


def is_loopback(host: str) -> bool:
    return (host or "").strip().strip("[]").lower() in LOOPBACK_HOSTS


class TicketBook:
    """In-memory, process-local, and deliberately not persisted.

    A ticket that survived a restart would be a credential in a file, which is the
    thing this exists to avoid. Losing them on restart costs one re-run of the
    command that mints them.
    """

    def __init__(self, ttl_seconds: float = TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        #: ticket -> (key, expires_at)
        self._tickets: Dict[str, Tuple[str, float]] = {}

    def _sweep(self, now: float) -> None:
        expired = [t for t, (_, expires) in self._tickets.items() if expires <= now]
        for ticket in expired:
            self._tickets.pop(ticket, None)

    def mint(self, key: str, now: Optional[float] = None) -> Tuple[str, float]:
        """Return (ticket, ttl_seconds) for a key that has already been authenticated."""
        if not key:
            # An open gateway has no key to hand over; the console needs none either.
            raise HandoffError("this gateway has no keys configured, so it needs no sign-in")
        now = time.time() if now is None else now
        self._sweep(now)
        ticket = secrets.token_urlsafe(TICKET_BYTES)
        self._tickets[ticket] = (key, now + self.ttl_seconds)
        return ticket, self.ttl_seconds

    def redeem(self, ticket: str, now: Optional[float] = None) -> str:
        """Exchange a ticket for its key, exactly once."""
        now = time.time() if now is None else now
        self._sweep(now)
        # pop, not get: a replayed ticket must fail even if the first use raced.
        found = self._tickets.pop(str(ticket or ""), None)
        if found is None:
            raise HandoffError("that sign-in link has already been used, or has expired")
        key, expires = found
        if expires <= now:
            raise HandoffError("that sign-in link has expired")
        return key
