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

# Matches the `watches.provider` default: a row that predates the column, or a
# fixture written before it existed, is a recreation.gov watch.
DEFAULT_PROVIDER = "recreation_gov"

PROVIDERS: dict[str, Provider] = {
    RecreationGovProvider.name: RecreationGovProvider(),
    GoingToCampProvider.name: GoingToCampProvider(),
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


__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "GoingToCampProvider",
    "PollKey",
    "Provider",
    "RecreationGovProvider",
    "provider_for",
    "provider_name",
]
