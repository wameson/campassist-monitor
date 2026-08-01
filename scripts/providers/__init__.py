"""The campground providers this build serves, one per `watches.provider`.

Adding a provider is: write a conformer of the `Provider` protocol
(providers/base.py) in its own module, then register it below. The monitor
cycle needs no change — it routes each watch by its `provider` column and
skips watches whose provider is not registered here.
"""

from __future__ import annotations

from .base import PollKey, Provider
from .going_to_camp import GoingToCampProvider
from .recreation_gov import RecreationGovProvider
from .use_direct import UseDirectProvider

# Matches the `watches.provider` default: a row that predates the column, or a
# fixture written before it existed, is a recreation.gov watch.
DEFAULT_PROVIDER = "recreation_gov"

PROVIDERS: dict[str, Provider] = {
    RecreationGovProvider.name: RecreationGovProvider(),
    GoingToCampProvider.name: GoingToCampProvider(),
    UseDirectProvider.name: UseDirectProvider(),
}


def provider_name(watch: dict) -> str:
    """The provider a watch belongs to, whether or not this build serves it."""
    return str(watch.get("provider") or DEFAULT_PROVIDER)


def provider_for(watch: dict) -> Provider:
    """The conformer that serves this watch.

    Raises KeyError for a provider this build does not know — a row written by
    a newer client. Callers holding watches straight out of the database filter
    them first (`provider_name(watch) in PROVIDERS`); everything downstream of
    that filter is entitled to raise rather than silently poll the wrong site.
    """
    return PROVIDERS[provider_name(watch)]


def unpollable_reason(watch: dict) -> str | None:
    """Why this watch's provider can never poll it — whatever the site's state —
    or None when it is pollable, which is the answer for most watches.

    An *optional* conformer hook, for a provider whose watches carry
    client-written configuration of its own: going_to_camp needs two identifiers
    out of `watches.provider_ref`, and a row without them is not a transient
    fault but a permanently unpollable watch, so the cycle errors it once (the
    same lifecycle a malformed `campground_id` gets) instead of failing it
    forever while the user sees nothing wrong. A provider with no such
    configuration — recreation.gov, whose `campground_id` is the whole identity —
    simply does not implement the hook.

    A conformer implementing it must not raise (the lifecycle pass runs outside
    the cycle's per-watch containment) and must return a short reason naming only
    the field at fault, never the client-supplied value, because the cycle
    reports it. Only watches on a registered provider may be passed in (see
    `provider_for`).
    """
    reason = getattr(provider_for(watch), "unpollable_reason", None)
    return reason(watch) if reason is not None else None


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "GoingToCampProvider",
    "PollKey",
    "Provider",
    "RecreationGovProvider",
    "UseDirectProvider",
    "provider_for",
    "provider_name",
    "unpollable_reason",
]
