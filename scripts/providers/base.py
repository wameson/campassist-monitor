"""What the monitor cycle needs from a campground provider.

The cycle in monitor.py plans, paces, hashes, alerts and does its bookkeeping
without knowing which site a watch polls. Everything site-specific — the
request host, the response parsing, the normalization to the shared
availability shape, and the booking deep link — sits behind the `Provider`
protocol here, one conformer per `watches.provider` value.

Security invariant: a provider's request host/URL is a constant in its own
module. `watches.provider_ref` is client-writable and carries identifiers only,
so no provider may ever derive a host, URL or path from it (SSRF).
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

# One unit of polling work, opaque to the cycle apart from its first element.
#
# PollKey[0] MUST be the watch's own `campground_id`: the cycle's 404-strike
# bookkeeping and its campgrounds_polled telemetry are campground-scoped. The
# rest of the key is the provider's own subdivision of a campground's work
# (recreation.gov: the first-of-month date of one month-availability request).
# The cycle deduplicates the poll plan across all users on the whole key, and
# keys must stay unique across providers — which holds because a campground_id
# belongs to exactly one provider.
PollKey = tuple[str, object]

# Polite-polling parameters every provider shares, so a second provider cannot
# quietly hammer harder than the first: a blocked or failed request is retried
# after 2 s, 4 s, 8 s and then given up on for this cycle.
BACKOFF_DELAYS_SECONDS = [2, 4, 8]
RETRYABLE_STATUS = {403, 429}


@runtime_checkable
class Provider(Protocol):
    """One campground site, behind the four things the cycle asks of it.

    Conformers are stateless and shared across watches (see PROVIDERS in
    providers/__init__.py), so nothing here may cache per-watch state.
    """

    #: matches the `watches.provider` value this conformer serves
    name: str

    def poll_plan(self, watch: dict, today: date) -> list[PollKey]:
        """Every poll unit this watch needs this cycle, deduplicated against
        the other watches' plans by the cycle. Empty when there is nothing to
        poll yet (e.g. a stay wholly beyond the polling horizon)."""
        ...

    def poll(
        self,
        http,
        key: PollKey,
        user_agent: str,
        *,
        sleep=...,
        errors: list[str] | None = None,
        budget_exhausted=...,
        not_found: set[str] | None = None,
    ) -> dict | None:
        """Fetch and parse one poll unit, or None if it failed this cycle (the
        cycle then keeps the affected watches' old state_hash and retries next
        run rather than treating the gap as "no availability").

        `errors` collects one already-capped line per failure for the
        world-readable run summary; `budget_exhausted()` cuts retries short
        once the cycle's time budget is spent; `not_found` receives the
        campground_id when the provider learns the campground does not exist,
        which drives the cycle's 404-strike lifecycle.
        """
        ...

    def extract_relevant(
        self, availability: dict, watch: dict, today: date
    ) -> dict[str, dict] | None:
        """The watch's current open-site state, normalized to the shape every
        provider shares — {site_id: {"campsite_id", "site", "dates": [iso…]}} —
        or None when any of the watch's poll units failed this cycle.

        `availability` is the whole cycle's {PollKey: parsed-or-None} map; a
        provider reads only the keys its own poll_plan produced.
        """
        ...

    def booking_url(self, watch: dict, openings: list[dict]) -> str:
        """The deep link the push notification opens for these openings."""
        ...
