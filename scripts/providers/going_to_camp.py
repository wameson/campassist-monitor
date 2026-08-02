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

A second, park-scoped endpoint describes those resources: `RESOURCES_URL`
carries each site's real label, its "ADA Only" flag, its capacity and its
allowed equipment (see the per-site metadata section below). The availability
API names sites by `resourceId` alone, so this is where "Site 42" comes from —
and where the ADA-Only exclusion in `extract_relevant` gets its flag.

Request posture: keyless GETs, plus the one read-only pricing POST this API
offers no GET for (`FEE_DETAILS_URL`, captain-approved) — and never a browser.
The SPA behind this host is Azure-WAF captcha-gated; `/api/*` is not, and
staying on `/api/*` with a browser UA and the pacing below is what keeps it
that way.

Security: the request host is the HOST constant below, shared by every URL in
this module. `watches.provider_ref` is client-writable, so only the two numeric
identifiers this provider needs are read out of it (`resource_location_id`,
`map_id`), and they only ever travel as query-string values. No host, URL, path
or scheme is ever derived from it (SSRF).
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import NamedTuple
from urllib.parse import urlencode

import httpx

from common import as_date, capped_line, date_in_watch

from .base import PollKey, fetch_with_backoff, horizon_month_start, night_span_bounds

# The one host this provider talks to. A code constant, never provider_ref.
HOST = "washington.goingtocamp.com"
AVAILABILITY_URL = f"https://{HOST}/api/availability/map"
BOOKING_URL = f"https://{HOST}/create-booking/results"
# The park's resource catalog: one keyless GET per park, per process.
RESOURCES_URL = f"https://{HOST}/api/resourcelocation/resources"
# The two vocabulary tables the catalog's enum indices decode through.
ATTRIBUTES_URL = f"https://{HOST}/api/attribute/filterable"
EQUIPMENT_URL = f"https://{HOST}/api/equipment"
# Per-night price. POST-only — a plain GET answers 405 — which is why this one
# request departs from the GET-only posture (README "Providers").
FEE_DETAILS_URL = f"https://{HOST}/api/resource/feeDetails"

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
# was captured against). This is a SAFETY cap, not a tuning knob: it is set an
# order of magnitude above any observed park purely so a single park cannot run
# away with the cycle if the API drifts, and it is not meant to bind in normal
# operation. Exceeding it is therefore treated as a fault an operator has to
# clear (ParkTooLarge) rather than as one more failed poll, so it can never
# leave a park quietly unserved on a green run. The cycle's own time budget
# still bounds the worst case underneath it.
MAX_CHILD_MAPS = 40

# Pacing inside one poll unit, matching the 1.2-2.8 s the cycle leaves between
# units so the recursion cannot burst.
CHILD_MAP_DELAY_SECONDS = 1.5

# --- the resource catalog's vocabulary ------------------------------------
#
# Attribute *definition* ids, from GET /api/attribute/filterable. They are
# magic negative ints, so `test_the_pinned_attribute_ids_still_mean_what_they
# _say` holds each one against the captured vocabulary: a renumbering upstream
# surfaces as a red test rather than as a field that silently stops decoding.
ADA_ONLY_DEF = -32759  # enum 0 = Yes, 1 = No
SERVICE_TYPE_DEF = -32768  # the hookup enum, decoded below

# Which Service Type values carry which utility. Deliberately two sets, not
# one: enum 5 (Electric Hook-ups, no water) is only 74 sites statewide, so an
# implementation that folds water into electric is right 98.8% of the time and
# silently wrong on exactly the sites a water filter would be asked about.
#
# These are enum indices of one known definition, so they are read directly —
# unlike a Yes/No attribute, whose index says nothing without the vocabulary
# (0 is "Yes" on ADA Only and "Not Available" on Pad Location). The same test
# holds these values to the captured vocabulary, so the shortcut cannot drift
# silently either.
ELECTRIC_SERVICE_TYPES = frozenset({5, 6, 7})
WATER_SERVICE_TYPES = frozenset({6, 7})

# The Yes/No labels of a boolean-shaped enum, read off the vocabulary rather
# than cast from the index: enum 0 is "Yes" on ADA Only and "Not Available" on
# Pad Location, so the index alone means nothing.
ENUM_YES = "Yes"
ENUM_NO = "No"

# The one culture this build reads. Every localizedValues array observed is
# en-US only; a body that ever carries more is served its en-US entry.
CULTURE = "en-US"

# Equipment sub-category names, from GET /api/equipment: "1 Tent", "2 Tents",
# "1 Van/Camper", "1 RV/Trailer up to 30'", and the group category's bare
# "Tents"/"Trailers". Matched by name because the ids collide across
# namespaces — sub-equipment -32759 is an RV/Trailer, attribute definition
# -32759 is ADA Only — so only the resolved name is safe to reason about.
_TENT_COUNT_RE = re.compile(r"\A(\d+) Tents?\Z")
_TENT_RE = re.compile(r"\bTents?\b")
_RV_RE = re.compile(r"\bRV\b|\bTrailers?\b|Van/Camper")

# feeDetails' `feeType`: 1 is Nightly and the only one that is a per-night
# price. 7 (PerPersonCapacityCategory, group camps) prices a party, not a
# night, and everything else in the platform's enum is unobserved here — both
# read as "no nightly price", never as a guess.
FEE_TYPE_NIGHTLY = 1

# A bounded integer literal — long enough for the platform's negative 32-bit
# ids, short enough that a client cannot post a megabyte "identifier".
IDENTIFIER_RE = re.compile(r"-?\d{1,19}")
# The same bound by magnitude, for an identifier that arrives as a jsonb
# number rather than as a string: Postgres jsonb numbers are
# arbitrary-precision, so without it a client could turn every query string
# this provider sends into a multi-kilobyte one.
IDENTIFIER_MAX = 10 ** 19


class InvalidProviderRef(ValueError):
    """This watch's `provider_ref` does not carry the identifiers going_to_camp
    needs. Raised only with a field name, never the offending value: the value
    is client-supplied and the exception is rendered into the operator log."""


class ParkTooLarge(RuntimeError):
    """This park's map fan-out exceeded MAX_CHILD_MAPS, so it cannot be polled
    at all under the current safety cap.

    Raised rather than returned as a failed unit, because the two mean opposite
    things to the cycle: a failed unit is transient by contract — keep the old
    state_hash, retry next run — whereas a park over the cap fails identically
    every cycle, which would leave its watches 'monitoring', never alerted and
    looking perfectly healthy on a run that still exits 0. The cycle contains
    this as a cycle failure instead, so the run goes red until an operator
    raises the cap or investigates the drift. The message carries counts, the
    cap and the same validated `campground_id` label the error lines use —
    nothing a client wrote unchecked — and only the operator channel renders it
    at all (the world-readable rendering of an exception with no response is its
    type name alone)."""


# --- provider_ref ---------------------------------------------------------

def _identifier(ref: dict, field: str) -> int:
    """One numeric identifier out of the client-written provider_ref.

    Identifiers only: the result is used as a query-string value and nothing
    else, so no part of it can ever steer a request at another host. Both input
    forms — a jsonb number and a numeric string — are bounded to the same
    magnitude, so neither can grow the query string this provider sends."""
    value = ref.get(field)
    if isinstance(value, bool) or value is None:
        raise InvalidProviderRef(f"provider_ref.{field} is missing")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and IDENTIFIER_RE.fullmatch(value.strip()):
        number = int(value)
    else:
        raise InvalidProviderRef(f"provider_ref.{field} is not an integer identifier")
    if abs(number) >= IDENTIFIER_MAX:
        raise InvalidProviderRef(f"provider_ref.{field} is too large for an identifier")
    return number


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
    return horizon_month_start(today, POLL_HORIZON_MONTHS)


def poll_range(start: date, end: date, today: date) -> tuple[date, date] | None:
    """The date range to ask the API for, or None when there is nothing to poll
    — a stay whose last night has passed, or one still beyond the horizon.

    The range is the stay itself, clamped forward to today and back to the
    horizon, so a stay running years past the horizon is never requested in
    full and no out-of-horizon night can reach the state hash or an alert. Like
    the other conformer, a watch straddling the horizon is served on its
    in-horizon nights alone (recreation.gov clamps its last *month* the same
    way; this API is asked for days, so the clamp is per-night).

    The boundary follows from what the response means: `parse_map` reads
    element *i* of a per-night array as `start` + *i* days and stops after the
    night keyed at the requested end, so the requested end IS the last night
    served. Asking for the horizon therefore serves exactly the nights
    first..horizon and not one past it. An in-horizon stay is not clamped at
    all, so its check-out day stays in the request (it is what the booking
    search is given) and is dropped from the result by `date_in_watch`, exactly
    as the other conformer drops it from a month it polled anyway."""
    bounds = night_span_bounds(start, end, today, POLL_HORIZON_MONTHS)
    if bounds is None:
        return None
    first, _last_night, horizon = bounds
    return first, max(min(end, horizon), first)


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

    The availability body names no site, so `site` starts as the resourceId —
    the same way the other conformer falls back to the campsite id when a site
    name is missing. `poll_park` then overwrites it with the real label from
    the park catalog (`apply_site_metadata`), which is also where the optional
    `ada_only` key comes from; both are left off any resource that catalog does
    not describe.
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
    return fetch_with_backoff(
        lambda: http.get(AVAILABILITY_URL, params=params, headers=headers),
        lambda body: parse_map(body, start, end),
        label=label,
        sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        not_found=not_found, not_found_id=not_found_id,
    )


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
    """One park's whole date range: the root map, then every map reachable from
    it. One level down is all the captured park needs, but a park that nests
    deeper is followed to the bottom rather than having its deeper sites quietly
    dropped — a merged result missing sites is the false delta this whole
    function is shaped to avoid.

    Returns the merged per-site state, or None if any part of the traversal
    failed — a park polled only halfway would read as sites that vanished, so a
    partial result is a failed unit and the cycle keeps the old state_hash. The
    traversal is bounded three ways: MAX_CHILD_MAPS total maps below the root
    however deep they nest, a map already visited is never fetched twice (so a
    cycle in the map graph terminates), and the cycle's own time budget stops it
    wherever it has got to. Only the first of those three is permanent, so it
    alone raises ParkTooLarge instead of returning a failed unit.
    """
    label = f"{campground_id}/{start.isoformat()}"
    # Child fetches are labelled apart from the root's: only the root's 404
    # strikes the watch, so an operator reading run_summaries must be able to
    # tell the two failures apart. Both halves stay free of client-written
    # values (campground_id is validated against monitor.CAMPGROUND_ID_RE).
    child_label = f"{label} child map"
    root = fetch_map(
        http, resource_location_id, root_map_id, start, end, user_agent,
        label=label, sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        not_found=not_found, not_found_id=campground_id,
    )
    if root is None:
        return None
    sites, children = root

    seen = {root_map_id}
    queue: list[int] = []

    def schedule(map_ids: list[int]) -> None:
        """Queue the maps this one names that have not been seen yet, and stop
        the park dead if its whole fan-out is past the safety cap — before
        another request is spent on it."""
        for map_id in map_ids:
            if map_id in seen:
                continue  # already polled or queued: a cycle in the map graph
            seen.add(map_id)
            queue.append(map_id)
        if len(seen) - 1 > MAX_CHILD_MAPS:
            raise ParkTooLarge(
                f"{label}: {len(seen) - 1} child maps below one park exceeds the "
                f"MAX_CHILD_MAPS safety cap of {MAX_CHILD_MAPS}"
            )

    schedule(children)
    while queue:
        child_map_id = queue.pop(0)
        if budget_exhausted():
            if errors is not None:
                errors.append(capped_line(
                    f"{label}: time budget exhausted mid-recursion, park not polled"
                ))
            return None
        sleep(CHILD_MAP_DELAY_SECONDS)
        child = fetch_map(
            http, resource_location_id, child_map_id, start, end, user_agent,
            label=child_label, sleep=sleep, errors=errors,
            budget_exhausted=budget_exhausted,
            # a missing child map is drift inside a park that answered, so it
            # never strikes the watches with a not-found
            not_found=None, not_found_id=None,
        )
        if child is None:
            return None
        child_sites, grandchildren = child
        schedule(grandchildren)
        for resource_id, site in child_sites.items():
            entry = sites.setdefault(resource_id, {
                "campsite_id": site["campsite_id"],
                "site": site["site"],
                "availabilities": {},
            })
            entry["availabilities"].update(site["availabilities"])

    if sites:
        apply_site_metadata(
            sites, http, resource_location_id, user_agent,
            label=f"{label} site metadata", sleep=sleep, errors=errors,
            budget_exhausted=budget_exhausted,
        )
    return sites


def apply_site_metadata(
    sites: dict[str, dict],
    http,
    resource_location_id: int,
    user_agent: str,
    *,
    label: str,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
) -> None:
    """Decorate the polled sites from the park catalog, in place: the real
    label in place of the `resourceId` placeholder, and the platform's own
    "ADA Only" flag as an optional `ada_only` key.

    Cosmetic by design, so it can never fail the unit: a park whose catalog
    this cycle could not read, or a resource the catalog does not describe,
    keeps the `resourceId` fallback and is still polled, hashed and alerted
    on. The one cost of that fallback is that the label is part of the shared
    availability shape and therefore of `state_hash`, so a cycle where the
    catalog fetch flips outcome reads as a delta; alert dedup keys on
    `campsite_id`, which never changes, so it costs one `watches` write and
    cannot produce a duplicate push.

    `ada_only` is set only where the platform published the flag, so an absent
    key means "not known to be ADA-only" — a failed catalog fetch, a resource
    the catalog omits, a vocabulary this cycle could not decode. That is what
    makes `extract_relevant`'s exclusion fail open: it can only ever drop a
    site the platform positively marked.
    """
    described = site_metadata(
        http, resource_location_id, user_agent, label=label,
        sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
    )
    for resource_id, site in sites.items():
        entry = described.get(resource_id)
        if entry is None:
            continue
        if entry.label:
            site["site"] = entry.label
        if entry.ada_only is not None:
            site["ada_only"] = entry.ada_only


# --- per-site metadata ----------------------------------------------------
#
# The availability API names a site by `resourceId` and nothing else. One
# further keyless GET per park — RESOURCES_URL — describes every one of them:
# the real label ("84"), the platform's own "ADA Only" flag, capacity, and the
# equipment the site takes. Its enum indices are meaningless on their own, so
# they decode through two tiny vocabulary tables (ATTRIBUTES_URL, EQUIPMENT_URL)
# fetched once per process. All three are paced but unretried (`_fetch_json`):
# a cosmetic read that a retry-and-backoff could spend fifteen seconds on is a
# read that starves the availability polls it is supposed to decorate.
#
# Everything here degrades to "the platform did not say" rather than to "no".
# A field this build cannot read is None, never False: a later filter built on
# these fields must fail open, because a silently-suppressed opening is a
# failure the user cannot see, while an un-suppressed one is merely noise.


class Vocabulary(NamedTuple):
    """The two keyless lookup tables the catalog's indices decode through.

    `attributes` is {attribute definition id: {enum value: label}} and
    `equipment` is {sub-equipment category id: name}. Either may be empty when
    its fetch failed — that is a decode this build cannot make, so the fields
    that depend on it read as unknown.
    """

    attributes: dict[int, dict[int, str]]
    equipment: dict[int, str]

    def enum_label(self, definition_id: int, value: int) -> str | None:
        return self.attributes.get(definition_id, {}).get(value)


EMPTY_VOCABULARY = Vocabulary({}, {})


class SiteMetadata(NamedTuple):
    """One site as the catalog describes it. Every field is optional: a
    provider that omits one is normal here, not an error (coverage runs from
    100% for the label down to 0% at a handful of tiny parks)."""

    label: str | None
    ada_only: bool | None
    min_capacity: int | None
    max_capacity: int | None
    #: the decoded Service Type label, e.g. "Electrical Water Hook-up"
    service_type: str | None
    electric: bool | None
    water: bool | None
    #: the resolved equipment names, in the order the catalog listed them
    equipment: tuple[str, ...]
    allows_tent: bool | None
    allows_rv: bool | None
    #: the largest "N Tents" the site takes, when it names one
    max_tents: int | None


def _localized(entries, field: str) -> str | None:
    """The en-US `field` of a localizedValues array, or None."""
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("cultureName") != CULTURE:
            continue
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _int(value) -> int | None:
    """A JSON number as an int, or None. `bool` is not an integer here."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _enum_value(defined, definition_id: int) -> int | None:
    """The single enum index one attribute definition carries on this site, or
    None when it is absent, multi-valued or not an integer."""
    if not isinstance(defined, list):
        return None
    for attribute in defined:
        if not isinstance(attribute, dict):
            continue
        if attribute.get("attributeDefinitionId") != definition_id:
            continue
        values = attribute.get("values")
        if not isinstance(values, list) or len(values) != 1:
            return None
        return _int(values[0])
    return None


def _yes_no(vocabulary: Vocabulary, definition_id: int, defined) -> bool | None:
    """A Yes/No attribute as a bool, resolved through the vocabulary.

    The index is never cast: enum 0 reads "Yes" on ADA Only and "Not Available"
    on Pad Location, so a definition this build could not fetch a vocabulary for
    answers None — unknown — and not False."""
    value = _enum_value(defined, definition_id)
    if value is None:
        return None
    label = vocabulary.enum_label(definition_id, value)
    if label == ENUM_YES:
        return True
    if label == ENUM_NO:
        return False
    return None


def _equipment(vocabulary: Vocabulary, allowed) -> tuple[str, ...]:
    """The site's allowed equipment, resolved to names through the vocabulary.
    An id the vocabulary does not name is dropped: an unnamed id says nothing
    about tents or RVs."""
    if not isinstance(allowed, list):
        return ()
    names = []
    for entry in allowed:
        if not isinstance(entry, dict):
            continue
        name = vocabulary.equipment.get(_int(entry.get("subEquipmentCategoryId")))
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _describe(raw: dict, vocabulary: Vocabulary) -> SiteMetadata:
    """One catalog record, defensively. Any field the record does not carry —
    or carries in a shape this build does not recognize — comes out None."""
    defined = raw.get("definedAttributes")
    service_type = _enum_value(defined, SERVICE_TYPE_DEF)
    equipment = _equipment(vocabulary, raw.get("allowedEquipment"))
    tent_counts = [
        int(match.group(1))
        for match in (_TENT_COUNT_RE.match(name) for name in equipment)
        if match
    ]
    return SiteMetadata(
        label=_localized(raw.get("localizedValues"), "name"),
        ada_only=_yes_no(vocabulary, ADA_ONLY_DEF, defined),
        min_capacity=_int(raw.get("minCapacity")),
        max_capacity=_int(raw.get("maxCapacity")),
        service_type=(
            None if service_type is None
            else vocabulary.enum_label(SERVICE_TYPE_DEF, service_type)
        ),
        electric=None if service_type is None else service_type in ELECTRIC_SERVICE_TYPES,
        water=None if service_type is None else service_type in WATER_SERVICE_TYPES,
        equipment=equipment,
        allows_tent=None if not equipment else any(_TENT_RE.search(n) for n in equipment),
        allows_rv=None if not equipment else any(_RV_RE.search(n) for n in equipment),
        max_tents=max(tent_counts) if tent_counts else None,
    )


def parse_resources(raw, vocabulary: Vocabulary) -> dict[str, SiteMetadata] | None:
    """Parse a RESOURCES_URL body — `{resourceId: record}` — into
    {resource id: SiteMetadata}, or None when the shape is unrecognized.

    A record that is not an object is skipped; a non-empty body in which *no*
    value is an object means the entry shape itself has changed, which is
    unrecognized rather than empty — the same rule `parse_map` follows.
    """
    if not isinstance(raw, dict):
        return None
    described: dict[str, SiteMetadata] = {}
    for resource_key, record in raw.items():
        if not isinstance(record, dict):
            continue
        described[str(resource_key)] = _describe(record, vocabulary)
    if raw and not described:
        return None
    return described


def parse_attribute_vocabulary(raw) -> dict[int, dict[int, str]]:
    """{definition id: {enum value: label}} from an ATTRIBUTES_URL body.
    An unreadable body yields {}, which reads downstream as "cannot decode"."""
    if not isinstance(raw, dict):
        return {}
    vocabulary: dict[int, dict[int, str]] = {}
    for definition in raw.values():
        if not isinstance(definition, dict):
            continue
        definition_id = _int(definition.get("attributeDefinitionId"))
        if definition_id is None:
            continue
        values = definition.get("values")
        labels: dict[int, str] = {}
        for value in values if isinstance(values, list) else ():
            if not isinstance(value, dict):
                continue
            enum_value = _int(value.get("enumValue"))
            label = _localized(value.get("localizedValues"), "displayName")
            if enum_value is not None and label:
                labels[enum_value] = label
        if labels:
            vocabulary[definition_id] = labels
    return vocabulary


def parse_equipment_vocabulary(raw) -> dict[int, str]:
    """{sub-equipment category id: name} from an EQUIPMENT_URL body.

    Flattened across the top-level categories ("Equipment", "Group"): a
    resource names only the sub-category, and the two do not collide.
    """
    if not isinstance(raw, list):
        return {}
    names: dict[int, str] = {}
    for category in raw:
        if not isinstance(category, dict):
            continue
        subs = category.get("subEquipmentCategories")
        for sub in subs if isinstance(subs, list) else ():
            if not isinstance(sub, dict):
                continue
            sub_id = _int(sub.get("subEquipmentCategoryId"))
            name = _localized(sub.get("localizedValues"), "name")
            if sub_id is not None and name:
                names.setdefault(sub_id, name)
    return names


def _fetch_json(
    http,
    url: str,
    params: dict | None,
    user_agent: str,
    *,
    label: str,
    sleep,
    errors: list[str] | None,
    budget_exhausted,
    post_body=None,
):
    """One paced request to a URL constant in this module, returning the
    decoded body or None.

    **One attempt, no backoff** — deliberately unlike the 2/4/8 s `fetch_map`
    spends on availability. Everything read through here is cosmetic to the
    poll, so a dead endpoint must not spend the cycle time the availability
    polls need: a failure here fails open to no label rather than retrying into
    the budget. Availability keeps its retries because a transient blip there
    costs a real unit.

    A 200 whose body is neither an object nor an array is a failure like any
    other rather than a value: handing it back would make an unusable body
    indistinguishable from a failed request, and cost the operator the one line
    every other unreadable shape reports.

    `post_body` is what makes this the module's only non-GET: FEE_DETAILS_URL
    answers `405` to a GET and `415` without a JSON content type, so a price
    read has to be a POST with a JSON body. It stays a read — the body is `[]`,
    nothing is created and no cart, cookie or token is involved.
    """
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if budget_exhausted():
        failure = f"{label}: time budget exhausted"
    else:
        sleep(CHILD_MAP_DELAY_SECONDS)  # paced like the child-map recursion
        try:
            if post_body is None:
                resp = http.get(url, params=params, headers=headers)
            else:
                resp = http.post(url, params=params, headers=headers, json=post_body)
            status = resp.status_code
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            status = None
            failure = f"{label}: {exc!r}"
        if status == 200:
            try:
                body = resp.json()
            except ValueError:
                failure = f"{label}: invalid JSON"
            else:
                if isinstance(body, (dict, list)):
                    return body
                failure = f"{label}: unexpected response body"
        elif status is not None:
            failure = f"{label}: HTTP {status}"
    if errors is not None:
        errors.append(capped_line(failure))
    return None


# Two process-lifetime caches. The monitor is a cron script, so one process is
# one cycle: this is a per-cycle cache of park-scoped catalogs and a
# park-independent vocabulary — never per-watch state, which the Provider
# protocol forbids. A failed fetch is cached as "nothing known" so one broken
# park cannot spend a request on every poll unit that names it; the next cycle
# is a new process and tries again.
_VOCABULARY: Vocabulary | None = None
_SITE_METADATA: dict[int, dict[str, SiteMetadata]] = {}


def clear_metadata_cache() -> None:
    """Empty both process caches. For tests; the cycle never needs it."""
    global _VOCABULARY
    _VOCABULARY = None
    _SITE_METADATA.clear()


def vocabulary(
    http,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
) -> Vocabulary:
    """The decoding tables, fetched at most once per process. Whichever half
    failed comes back empty rather than absent, so a caller never has to
    distinguish "not fetched" from "fetched and unreadable" — both mean the
    fields that depend on it are unknown."""
    global _VOCABULARY
    if _VOCABULARY is None:
        common = dict(sleep=sleep, errors=errors, budget_exhausted=budget_exhausted)
        _VOCABULARY = Vocabulary(
            parse_attribute_vocabulary(_fetch_json(
                http, ATTRIBUTES_URL, None, user_agent,
                label="going_to_camp attribute vocabulary", **common,
            )),
            parse_equipment_vocabulary(_fetch_json(
                http, EQUIPMENT_URL, None, user_agent,
                label="going_to_camp equipment vocabulary", **common,
            )),
        )
    return _VOCABULARY


def site_metadata(
    http,
    resource_location_id: int,
    user_agent: str,
    *,
    label: str,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
) -> dict[str, SiteMetadata]:
    """One park's catalog, fetched at most once per process, or {} when this
    cycle could not read it.

    Only `resource_location_id` — already bounded by `_identifier` on the way
    out of the client-written provider_ref — reaches the query string, and the
    URL is the module constant (SSRF).
    """
    cached = _SITE_METADATA.get(resource_location_id)
    if cached is not None:
        return cached
    known = vocabulary(
        http, user_agent, sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
    )
    body = _fetch_json(
        http, RESOURCES_URL, {"resourceLocationId": resource_location_id}, user_agent,
        label=label, sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
    )
    described = parse_resources(body, known) if body is not None else None
    if described is None and body is not None and errors is not None:
        errors.append(capped_line(f"{label}: unrecognized response body"))
    _SITE_METADATA[resource_location_id] = described or {}
    return _SITE_METADATA[resource_location_id]


# --- per-night price ------------------------------------------------------

def parse_fee_details(raw) -> Decimal | None:
    """The nightly rate in a FEE_DETAILS_URL body, or None.

    None covers every shape that is not a per-night dollar amount: an empty
    `resourceFeeDetails`, a body this build does not recognize, and — the one
    worth naming — a `feeType` that is not Nightly. Group camps answer
    `feeType 7`, a price per `billablePartySize` people; rendering that as a
    nightly rate would be wrong, so no price is the honest answer.

    `feeTotal` arrives as a raw JSON decimal (`46.00000`), so it comes back as
    a `Decimal` for a caller to format — never as a string to match on.
    """
    if not isinstance(raw, dict):
        return None
    details = raw.get("resourceFeeDetails")
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict) or _int(detail.get("feeType")) != FEE_TYPE_NIGHTLY:
            continue
        total = detail.get("feeTotal")
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            continue
        try:
            return Decimal(str(total))
        except InvalidOperation:
            continue
    return None


def fee_details(
    http,
    resource_id: int,
    start_date: date,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
) -> Decimal | None:
    """One site's nightly rate for one stay date, or None when this build
    cannot read one (see `parse_fee_details`, and any failed request).

    **The one non-GET this project sends.** The endpoint answers `405` to a GET
    and `415` without `Content-Type: application/json`, and the captain adopted
    it as a read-only pricing POST — the body is `[]`, it creates nothing and
    carries no cart, cookie or token. The posture it amends is
    "keyless GET, plus one read-only pricing POST; still never drive a browser".

    The rate is date-dependent (a seasonal step function) and a past date
    silently answers today's rate, so `start_date` must be the real stay date.

    SSRF: the URL is the module constant, and the query carries only an
    identifier bounded exactly like `provider_ref`'s and an ISO date this
    module formats itself.
    """
    if isinstance(resource_id, bool) or not isinstance(resource_id, int):
        raise TypeError("resource_id must be an integer identifier")
    if abs(resource_id) >= IDENTIFIER_MAX:
        raise ValueError("resource_id is too large for an identifier")
    body = _fetch_json(
        http, FEE_DETAILS_URL,
        {"resourceId": resource_id, "startDate": as_date(start_date).isoformat()},
        user_agent,
        label="going_to_camp fee details",
        sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        post_body=[],
    )
    return parse_fee_details(body)


# --- the conformer --------------------------------------------------------

class GoingToCampProvider:
    """GoingToCamp behind the Provider protocol (see providers/base.py)."""

    name = "going_to_camp"

    def unpollable_reason(self, watch: dict) -> str | None:
        """Why this watch can never be polled, or None (the optional conformer
        hook `providers.unpollable_reason` describes).

        A `provider_ref` this provider cannot read the two identifiers out of is
        not a transient fault: it fails identically every cycle, so the cycle
        errors the watch once rather than leaving the user a watch that looks
        healthy and never alerts. The reason names only the field at fault —
        the value is client-written and the cycle publishes this string."""
        try:
            provider_ref_ids(watch)
        except InvalidProviderRef as exc:
            return str(exc)
        return None

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
        nor an alert.

        Sites this platform marks "ADA Only" are excluded here too, unless the
        watch opted back in (`include_ada_only`) or named the site itself. See
        the exclusion's three rules at the loop below."""
        # The cycle's lifecycle pass errors an unusable ref before it gets here
        # (see `unpollable_reason`); this stays the contained backstop, since
        # extract_relevant is the one entry point that runs inside per-watch
        # containment and can therefore report it as this watch's own failure.
        provider_ref_ids(watch)
        keys = self.poll_plan(watch, today)
        if not keys:
            return None
        parsed = availability.get(keys[0])
        if parsed is None:
            return None

        start = as_date(watch["start_date"])
        end = as_date(watch["end_date"])
        wanted = {str(s) for s in (watch.get("site_ids") or [])}
        # The per-watch opt-in, read the way the drift guard's WARN
        # classification of this column requires: `.get` with a default, so a
        # live DB that has not had 0003 applied yet reads exactly what a
        # migrated row carries (false) instead of halting the cycle. Default
        # false applies to every watch, old and new — there is no backfill, so
        # this is the deliberate behaviour change the migration documents.
        include_ada_only = bool(watch.get("include_ada_only"))

        current: dict[str, dict] = {}
        for resource_id, site in parsed.items():
            # Matched on the stable identifiers alone. `site` carries the
            # catalog label, which reverts to the resourceId whenever the
            # catalog fetch fails, so a watch naming labels would match every
            # site on a healthy cycle and none at all on a degraded one —
            # openings suppressed with nothing the user can see. The label is
            # for display; the resourceId is the identity, and it is the
            # resourceId the client persists in `site_ids` for a per-site
            # GoingToCamp watch while showing the label.
            if wanted and not ({resource_id, site["campsite_id"]} & wanted):
                continue
            # "ADA Only" on this platform means only campers with disabilities
            # may reserve the site, so it is not an opening for a watch that
            # did not ask for it — the platform's own search excludes these by
            # default too. Three rules make that safe:
            #   * it runs AFTER the `wanted` match, and only for an undirected
            #     watch: a watch that named this site chose it deliberately and
            #     the filter must not overrule the choice.
            #   * it runs BEFORE the caller hashes this shape, so an ADA-only
            #     site opening and closing is not a delta at all — no phantom
            #     state_hash churn, no `sent_alerts` row, no `watches` write.
            #   * it fails open: only a site the catalog positively marked
            #     carries the flag (see `apply_site_metadata`), so a catalog
            #     this cycle could not read suppresses nothing. A suppressed
            #     opening is invisible to the user; a surplus one is only noise.
            if site.get("ada_only") and not include_ada_only and not wanted:
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
