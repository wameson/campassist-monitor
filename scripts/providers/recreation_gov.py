"""recreation.gov, the first Provider conformer.

One GET per (campground, month) against the public availability API, parsed
defensively into the shared availability shape. The poll unit is a month
because that is what the endpoint serves; nothing above this module knows that.

Security: the request host is the AVAILABILITY_URL constant below and the
booking host is BOOKING_URL_TEMPLATE. Nothing here reads `watches.provider_ref`
— a provider's host or URL is never derived from client-writable data (SSRF).
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx

from common import as_date, date_in_watch

from .base import PollKey, fetch_with_backoff

AVAILABILITY_URL = "https://www.recreation.gov/api/camps/availability/campground/{campground_id}/month"
BOOKING_URL_TEMPLATE = "https://www.recreation.gov/camping/campsites/{campsite_id}"

POLL_HORIZON_MONTHS = 12


# --- poll plan ------------------------------------------------------------

def horizon_month(today: date) -> date:
    """First-of-month containing today + POLL_HORIZON_MONTHS: the last
    month the poll plan may include."""
    years, month0 = divmod(today.month - 1 + POLL_HORIZON_MONTHS, 12)
    return date(today.year + years, month0 + 1, 1)


def months_for_watch(start: date, end: date, today: date) -> list[date]:
    """First-of-month dates covering the stay's remaining nights — one API
    call each. The check-out day's month is not polled, and months entirely
    in the past or beyond the polling horizon (today + POLL_HORIZON_MONTHS)
    are skipped; a watch wholly beyond the horizon yields no months until
    the horizon reaches it."""
    last_night = end - timedelta(days=1) if end > start else start
    last_month = min(last_night, horizon_month(today))
    months = []
    cur = max(start, today).replace(day=1)
    while cur <= last_month:
        months.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)
    return months


# --- fetch and parse ------------------------------------------------------

def parse_availability(raw) -> dict[str, dict] | None:
    """Defensively parse a recreation.gov month-availability response.

    Missing or renamed fields inside campsites degrade to a partial parse —
    never a crash. Returns {campsite_id: {"campsite_id", "site",
    "availabilities": {date: status}}}. A body with no recognizable
    'campsites' dict returns None (unrecognized response shape — not
    authoritative), as does a non-empty campsites dict in which no entry
    carries a recognizable availabilities dict (the entry shape itself has
    changed); a well-formed empty campsites dict — or campsites whose
    availabilities dicts are genuinely empty — parses as authoritative.
    """
    if not isinstance(raw, dict):
        return None
    campsites = raw.get("campsites")
    if not isinstance(campsites, dict):
        return None
    recognized = False
    sites: dict[str, dict] = {}
    for cs_key, cs in campsites.items():
        if not isinstance(cs, dict):
            continue
        availabilities = cs.get("availabilities")
        dates: dict[str, str] = {}
        if isinstance(availabilities, dict):
            recognized = True
            for date_str, status in availabilities.items():
                if not isinstance(status, str):
                    continue
                try:
                    d = as_date(date_str)
                except (ValueError, TypeError):
                    continue
                dates[d.isoformat()] = status
        campsite_id = cs.get("campsite_id", cs_key)
        site = cs.get("site")
        sites[str(cs_key)] = {
            "campsite_id": str(campsite_id),
            "site": site if isinstance(site, str) else str(cs_key),
            "availabilities": dates,
        }
    if campsites and not recognized:
        return None
    return sites


def poll_with_backoff(
    http: httpx.Client,
    campground_id: str,
    month: date,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
    not_found: set[str] | None = None,
) -> dict[str, dict] | None:
    """GET one campground-month with exponential backoff on 403/429/5xx.

    Retries after 2 s, 4 s, 8 s, then gives up for this cycle (returns
    None) so the rest of the run continues. A 200 whose body is invalid
    JSON or has no recognizable campsites dict is a non-retryable failure:
    the month counts as failed rather than as empty availability. A 404
    additionally records the campground into `not_found` so the caller can
    error watches whose campground keeps missing. Once budget_exhausted()
    reports the cycle's time budget is spent, remaining retries and their
    backoff sleeps are skipped.
    """
    url = AVAILABILITY_URL.format(campground_id=campground_id)
    params = {"start_date": f"{month.isoformat()}T00:00:00.000Z"}
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    return fetch_with_backoff(
        lambda: http.get(url, params=params, headers=headers),
        parse_availability,
        label=f"{campground_id}/{month.isoformat()}",
        sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        not_found=not_found, not_found_id=campground_id,
    )


# --- the conformer --------------------------------------------------------

class RecreationGovProvider:
    """recreation.gov behind the Provider protocol (see providers/base.py)."""

    name = "recreation_gov"

    def poll_plan(self, watch: dict, today: date) -> list[PollKey]:
        campground_id = str(watch["campground_id"])
        months = months_for_watch(
            as_date(watch["start_date"]), as_date(watch["end_date"]), today
        )
        return [(campground_id, month) for month in months]

    def poll(
        self,
        http,
        key: PollKey,
        user_agent: str,
        *,
        sleep=time.sleep,
        errors: list[str] | None = None,
        budget_exhausted=lambda: False,
        not_found: set[str] | None = None,
    ) -> dict[str, dict] | None:
        campground_id, month = key
        return poll_with_backoff(
            http, campground_id, month, user_agent,
            sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
            not_found=not_found,
        )

    def extract_relevant(
        self, availability: dict, watch: dict, today: date
    ) -> dict[str, dict] | None:
        """Current open-site state relevant to one watch, or None if any of
        the watch's months failed to poll this cycle (keep old hash, retry
        next run rather than hashing partial data). Past nights are excluded:
        they are unbookable, so they count toward neither the hash nor alerts.
        A watch wholly beyond the polling horizon has no pollable months yet,
        so it also returns None; a watch straddling the horizon is hashed on
        its in-horizon months alone.

        No ADA-only exclusion here, deliberately, and `include_ada_only` is not
        read: this API has no such concept. It publishes `is_accessible`, which
        its own UI defines as "has features for better accessibility" — a
        statement about the site, not about who may reserve it — and a survey of
        1,571 campsites at 24 accessibility-bearing facilities found no
        restriction marker in any field or free text. Filtering on the wider
        flag was measured to hide roughly 2.75 bookable sites per restricted one
        it would correctly hide, so the captain scoped the filter to the
        provider that states the fact exactly (going_to_camp). Hiding sites a
        platform never called restricted is the same harm the feature exists to
        prevent, pointed at a different group."""
        start = as_date(watch["start_date"])
        end = as_date(watch["end_date"])
        wanted = {str(s) for s in (watch.get("site_ids") or [])}

        keys = self.poll_plan(watch, today)
        if not keys:
            return None

        merged: dict[str, dict] = {}
        for key in keys:
            parsed = availability.get(key)
            if parsed is None:
                return None
            for cs_id, cs in parsed.items():
                entry = merged.setdefault(
                    cs_id, {"campsite_id": cs["campsite_id"], "site": cs["site"], "dates": {}}
                )
                entry["dates"].update(cs["availabilities"])

        current: dict[str, dict] = {}
        for cs_id, cs in merged.items():
            if wanted and not ({cs_id, cs["campsite_id"], cs["site"]} & wanted):
                continue
            open_dates = sorted(
                d
                for d, status in cs["dates"].items()
                if status == "Available"
                and as_date(d) >= today
                and date_in_watch(as_date(d), start, end)
            )
            if open_dates:
                current[cs_id] = {
                    "campsite_id": cs["campsite_id"],
                    "site": cs["site"],
                    "dates": open_dates,
                }
        return current

    def booking_url(self, watch: dict, openings: list[dict]) -> str:
        return BOOKING_URL_TEMPLATE.format(campsite_id=openings[0]["campsite_id"])
