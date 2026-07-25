"""GoingToCamp / Aspira (Washington State Parks), the second Provider conformer.

The availability API is map-scoped and recursive: a park's *root* map answers
with pointers to its child maps (`mapLinkAvailabilities`) and an empty
`resourceAvailabilities`; the per-site data lives one level down. So one poll
unit here is a whole (park, date-range) and `poll` does the root->child
recursion internally — 2-5 GETs against the same host, paced and charged
against the cycle's own time budget, returning the one normalized result the
cycle reads. Nothing above this module knows any of that.

A site is a `resourceId` and its per-night value carries an `availability`
enum; `0` is the only value confirmed to mean bookable, so every other value —
including one this build has never seen — is parsed as NOT available.

Security: the request host is the HOST constant below, shared by
AVAILABILITY_URL and BOOKING_URL. `watches.provider_ref` is client-writable, so
only the two numeric identifiers this provider needs are read out of it
(`resource_location_id`, `map_id`), and they only ever travel as query-string
values. No host, URL, path or scheme is ever derived from it (SSRF).
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import httpx

from common import as_date, capped_line, date_in_watch

from .base import BACKOFF_DELAYS_SECONDS, RETRYABLE_STATUS, PollKey

# The one host this provider talks to. A code constant, never provider_ref.
HOST = "washington.goingtocamp.com"
AVAILABILITY_URL = f"https://{HOST}/api/availability/map"
BOOKING_URL = f"https://{HOST}/create-booking/results"

# Fixed booking-flow parameters: the "one non-group site, party of one" search
# every watch makes today. CampAssist has no party-size or equipment field, so
# these are constants rather than anything a watch (or a client) can set.
BOOKING_CATEGORY_ID = 0
EQUIPMENT_CATEGORY_ID = -32768  # non-group equipment
PARTY_SIZE = 1
NUM_EQUIPMENT = 1

# The `availability` enum value that means bookable. Codes 1, 3, 5 and 7 were
# all observed in the wild for various unbookable states; only 0 is confirmed,
# so the parse tests for it and nothing else.
AVAILABLE = 0

# Same 12-month horizon the other conformer polls to: a stay nobody can book
# yet is not worth 2-5 GETs a cycle.
POLL_HORIZON_MONTHS = 12

# A park's root map fans out to a handful of child maps (4 for the park this
# was captured against). The cap bounds one park's share of the cycle: a park
# that suddenly answers with far more is failed loudly for this cycle rather
# than allowed to spend the whole time budget by itself.
MAX_CHILD_MAPS = 12

# Pacing inside one poll unit, matching the 1.2-2.8 s the cycle leaves between
# units so the recursion cannot burst.
CHILD_MAP_DELAY_SECONDS = 1.5

# A bounded integer literal — long enough for the platform's negative 32-bit
# ids, short enough that a client cannot post a megabyte "identifier".
IDENTIFIER_RE = re.compile(r"-?\d{1,19}")


class InvalidProviderRef(ValueError):
    """This watch's `provider_ref` does not carry the identifiers going_to_camp
    needs. Raised only with a field name, never the offending value: the value
    is client-supplied and the exception is rendered into the operator log."""


# --- provider_ref ---------------------------------------------------------

def _identifier(ref: dict, field: str) -> int:
    """One numeric identifier out of the client-written provider_ref.

    Identifiers only: the result is used as a query-string value and nothing
    else, so no part of it can ever steer a request at another host."""
    value = ref.get(field)
    if isinstance(value, bool) or value is None:
        raise InvalidProviderRef(f"provider_ref.{field} is missing")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and IDENTIFIER_RE.fullmatch(value.strip()):
        return int(value)
    raise InvalidProviderRef(f"provider_ref.{field} is not an integer identifier")


def provider_ref_ids(watch: dict) -> tuple[int, int]:
    """(resource_location_id, map_id) for this watch, or InvalidProviderRef.

    A going_to_camp watch is unpollable without both, so a missing or malformed
    ref is this watch's own error — contained and reported by the cycle — and
    never a crash and never a request."""
    ref = watch.get("provider_ref")
    if not isinstance(ref, dict):
        raise InvalidProviderRef("provider_ref is not an object")
    return _identifier(ref, "resource_location_id"), _identifier(ref, "map_id")


# --- poll plan ------------------------------------------------------------

def horizon_date(today: date) -> date:
    """First-of-month containing today + POLL_HORIZON_MONTHS: a stay starting
    after it is not polled yet."""
    years, month0 = divmod(today.month - 1 + POLL_HORIZON_MONTHS, 12)
    return date(today.year + years, month0 + 1, 1)


def poll_range(start: date, end: date, today: date) -> tuple[date, date] | None:
    """The date range to ask the API for, or None when there is nothing to poll
    — a stay whose last night has passed, or one still beyond the horizon.

    The range is the stay itself, clamped forward to today. The check-out day
    stays in the request (it is what the booking search is given) and is
    dropped from the result by `date_in_watch`, exactly as the other conformer
    drops it from a month it polled anyway."""
    last_night = end - timedelta(days=1) if end > start else start
    if last_night < today:
        return None
    first = max(start, today)
    if first > horizon_date(today):
        return None
    return first, max(end, first)


# --- fetch and parse ------------------------------------------------------

def parse_map(raw, start: date, end: date) -> tuple[dict[str, dict], list[int]] | None:
    """Defensively parse one /api/availability/map response into
    ({resource_id: {"campsite_id", "site", "availabilities": {iso: is_open}}},
    [child map id, …]), or None when the body's shape is unrecognized.

    `availabilities` is keyed by the night each element of the per-night array
    stands for: element *i* is `start` + *i* days, and elements past `end` (or
    past the end of a short array) are simply absent. Only `availability == 0`
    is open; every other value, and every entry that is not a JSON object, is
    parsed as taken. A resource whose value is not a per-night array at all is
    skipped, and a non-empty `resourceAvailabilities` in which *no* value is an
    array means the entry shape itself has changed — unrecognized, not empty.

    Per-site labels are not served by any endpoint this build can reach, so
    `site` degrades to the resourceId, the same way the other conformer falls
    back to the campsite id when a site name is missing.
    """
    if not isinstance(raw, dict):
        return None
    resources = raw.get("resourceAvailabilities")
    links = raw.get("mapLinkAvailabilities")
    if not isinstance(resources, dict) and not isinstance(links, dict):
        return None  # neither half of the documented body is there

    sites: dict[str, dict] = {}
    recognized = False
    if isinstance(resources, dict):
        for resource_key, nights in resources.items():
            if not isinstance(nights, list):
                continue
            recognized = True
            dates: dict[str, bool] = {}
            for offset, night in enumerate(nights):
                night_date = start + timedelta(days=offset)
                if night_date > end:
                    break
                dates[night_date.isoformat()] = is_open(night)
            resource_id = str(resource_key)
            sites[resource_id] = {
                "campsite_id": resource_id,
                "site": resource_id,
                "availabilities": dates,
            }
        if resources and not recognized:
            return None

    children: list[int] = []
    if isinstance(links, dict):
        for link_key in links:
            try:
                children.append(int(str(link_key)))
            except ValueError:
                continue  # a child id we cannot address is one we cannot poll
    return sites, children


def is_open(night) -> bool:
    """True only for a night this build is sure is bookable. Anything else —
    an unknown enum value, a bool, a string, a missing field, junk — is taken."""
    if not isinstance(night, dict):
        return False
    availability = night.get("availability")
    if isinstance(availability, bool) or not isinstance(availability, int):
        return False
    return availability == AVAILABLE


def fetch_map(
    http: httpx.Client,
    resource_location_id: int,
    map_id: int,
    start: date,
    end: date,
    user_agent: str,
    *,
    label: str,
    sleep,
    errors: list[str] | None,
    budget_exhausted,
    not_found: set[str] | None,
    not_found_id: str | None,
) -> tuple[dict[str, dict], list[int]] | None:
    """GET one map with the same backoff the other conformer uses: retry a
    403/429/5xx after 2 s, 4 s, 8 s, then give up for this cycle.

    A 200 whose body is invalid JSON or has no recognizable map shape is a
    non-retryable failure — the unit counts as failed, never as "no
    availability". A 404 records `not_found_id` when one is given, which is
    only for the park's *root* map: a child map that has gone missing is API
    drift, not evidence the park does not exist, and must not strike the
    watches.
    """
    params = {
        "mapId": map_id,
        "resourceLocationId": resource_location_id,
        "bookingCategoryId": BOOKING_CATEGORY_ID,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "isReserving": "true",
        "getDailyAvailability": "true",
        "partySize": PARTY_SIZE,
        "numEquipment": NUM_EQUIPMENT,
        "equipmentCategoryId": EQUIPMENT_CATEGORY_ID,
    }
    headers = {"User-Agent": user_agent, "Accept": "application/json"}

    for attempt in range(len(BACKOFF_DELAYS_SECONDS) + 1):
        try:
            resp = http.get(AVAILABILITY_URL, params=params, headers=headers)
            status = resp.status_code
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            status = None
            failure = f"{label}: {exc!r}"
        if status == 200:
            try:
                body = resp.json()
            except ValueError:
                failure = f"{label}: invalid JSON"
                break
            parsed = parse_map(body, start, end)
            if parsed is not None:
                return parsed
            failure = f"{label}: unrecognized response body"
            break
        if status is not None:
            failure = f"{label}: HTTP {status}"
            if not (status in RETRYABLE_STATUS or status >= 500):
                if status == 404 and not_found is not None and not_found_id is not None:
                    not_found.add(not_found_id)
                break
        if attempt < len(BACKOFF_DELAYS_SECONDS):
            if budget_exhausted():
                break
            sleep(BACKOFF_DELAYS_SECONDS[attempt])
    if errors is not None:
        errors.append(capped_line(failure))
    return None


def poll_park(
    http: httpx.Client,
    campground_id: str,
    resource_location_id: int,
    root_map_id: int,
    start: date,
    end: date,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
    not_found: set[str] | None = None,
) -> dict[str, dict] | None:
    """One park's whole date range: the root map, then each child map it names.

    Returns the merged per-site state, or None if any part of the recursion
    failed — a park polled only halfway would read as sites that vanished, so a
    partial result is a failed unit and the cycle keeps the old state_hash.
    """
    label = f"{campground_id}/{start.isoformat()}"
    root = fetch_map(
        http, resource_location_id, root_map_id, start, end, user_agent,
        label=label, sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        not_found=not_found, not_found_id=campground_id,
    )
    if root is None:
        return None
    sites, children = root

    if len(children) > MAX_CHILD_MAPS:
        if errors is not None:
            errors.append(capped_line(
                f"{label}: {len(children)} child maps exceeds the {MAX_CHILD_MAPS} "
                "polled per park, skipped"
            ))
        return None

    for child_map_id in children:
        if budget_exhausted():
            if errors is not None:
                errors.append(capped_line(
                    f"{label}: time budget exhausted mid-recursion, park not polled"
                ))
            return None
        sleep(CHILD_MAP_DELAY_SECONDS)
        child = fetch_map(
            http, resource_location_id, child_map_id, start, end, user_agent,
            label=label, sleep=sleep, errors=errors,
            budget_exhausted=budget_exhausted,
            # a missing child map is drift inside a park that answered, so it
            # never strikes the watches with a not-found
            not_found=None, not_found_id=None,
        )
        if child is None:
            return None
        child_sites, _ = child  # one level of recursion is all the API needs
        for resource_id, site in child_sites.items():
            entry = sites.setdefault(resource_id, {
                "campsite_id": site["campsite_id"],
                "site": site["site"],
                "availabilities": {},
            })
            entry["availabilities"].update(site["availabilities"])
    return sites


# --- the conformer --------------------------------------------------------

class GoingToCampProvider:
    """GoingToCamp behind the Provider protocol (see providers/base.py)."""

    name = "going_to_camp"

    def poll_plan(self, watch: dict, today: date) -> list[PollKey]:
        """One key per watch: this provider's poll unit is the whole park over
        the whole stay, since the recursion happens inside `poll`.

        A watch whose provider_ref is unusable plans nothing — `poll_dispatch`
        runs outside the cycle's per-watch containment, so this must not raise.
        `extract_relevant`, which does run inside it, re-reads the ref and lets
        the failure surface there as this watch's own."""
        try:
            resource_location_id, map_id = provider_ref_ids(watch)
        except InvalidProviderRef:
            return []
        span = poll_range(
            as_date(watch["start_date"]), as_date(watch["end_date"]), today
        )
        if span is None:
            return []
        start, end = span
        return [(
            str(watch["campground_id"]),
            (resource_location_id, map_id, start.isoformat(), end.isoformat()),
        )]

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
        campground_id, (resource_location_id, map_id, start, end) = key
        return poll_park(
            http, campground_id, resource_location_id, map_id,
            as_date(start), as_date(end), user_agent,
            sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
            not_found=not_found,
        )

    def extract_relevant(
        self, availability: dict, watch: dict, today: date
    ) -> dict[str, dict] | None:
        """Current open-site state for one watch in the shape every provider
        shares, or None when its park failed to poll this cycle (keep the old
        hash and retry next run). Past nights and the check-out day are
        excluded: they are unbookable, so they count toward neither the hash
        nor an alert."""
        provider_ref_ids(watch)  # unusable ref -> this watch's own failure
        keys = self.poll_plan(watch, today)
        if not keys:
            return None
        parsed = availability.get(keys[0])
        if parsed is None:
            return None

        start = as_date(watch["start_date"])
        end = as_date(watch["end_date"])
        wanted = {str(s) for s in (watch.get("site_ids") or [])}

        current: dict[str, dict] = {}
        for resource_id, site in parsed.items():
            if wanted and not ({resource_id, site["campsite_id"], site["site"]} & wanted):
                continue
            open_dates = sorted(
                d
                for d, open_night in site["availabilities"].items()
                if open_night
                and as_date(d) >= today
                and date_in_watch(as_date(d), start, end)
            )
            if open_dates:
                current[resource_id] = {
                    "campsite_id": site["campsite_id"],
                    "site": site["site"],
                    "dates": open_dates,
                }
        return current

    def booking_url(self, watch: dict, openings: list[dict]) -> str:
        """The park's booking search, pre-filled with the watch's dates.

        The SPA takes no `resourceId` preselect, so unlike recreation.gov's
        per-campsite page this deep link is park-and-dates only and `openings`
        is unused: the user lands on the live picker, which is the right place
        to be when availability is this fleeting."""
        resource_location_id, map_id = provider_ref_ids(watch)
        params = [
            ("mapId", map_id),
            ("bookingCategoryId", BOOKING_CATEGORY_ID),
            ("startDate", as_date(watch["start_date"]).isoformat()),
            ("endDate", as_date(watch["end_date"]).isoformat()),
            ("isReserving", "true"),
            ("equipmentId", EQUIPMENT_CATEGORY_ID),
            ("partySize", PARTY_SIZE),
            ("resourceLocationId", resource_location_id),
        ]
        return f"{BOOKING_URL}?{urlencode(params)}"
