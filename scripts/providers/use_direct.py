"""UseDirect / Tyler (ReserveCalifornia and siblings), the third Provider conformer.

One read-only POST per (facility, stay) against a tenant's RDR availability
grid, parsed defensively into the shared availability shape. ReserveCalifornia
is the one tenant wired today; the seam is multi-tenant on purpose, because
UseDirect runs one deployment per state, each on its own vetted host.

Shared wire contract this conformer owns (the app builds to it):
  * `watches.provider` value: ``use_direct``.
  * `watches.campground_id` format: ``<tenant>_<facilityId>`` — a registered
    tenant key, an underscore, then the UseDirect FacilityId as a decimal
    integer, e.g. ``ca_377``. Underscore, never colon, so it matches
    ``monitor.CAMPGROUND_ID_RE`` (``[A-Za-z0-9_-]+``). `provider_ref` is unused.

Security invariant (the load-bearing one): a tenant's request host and path
prefix are vetted constants in `TENANTS` below. The campground_id's tenant key
only *selects* one of those entries — an allowlist lookup, never host
construction — so an unknown key selects nothing and the watch is unpollable,
and the only client value that ever reaches a request is the integer FacilityId,
in the POST body. No host, URL, scheme or path is ever derived from
`campground_id`, `provider_ref`, or any other client-writable value (SSRF). That
is also why California's host is not inferred from any pattern: California has
migrated off ``*.usedirect.com`` onto a Tyler cloud host, so each state's host is
vetted and pinned separately.

Request posture: one fixed, honest User-Agent — the session UA the cycle hands
every provider, used verbatim and NEVER regenerated per request. The reference
implementation (camply) rotates a random Chrome UA on this exact availability
POST as fingerprint evasion; the captain ruled that out and the probe proved it
unnecessary, so this module carries no User-Agent of its own and imports no UA
generator (`test_the_user_agent_is_fixed_never_randomised` is the regression).
Read-only throughout: no account, key, cookie jar or browser, and never a
booking write — the one POST is the availability read, whose body creates
nothing.

Per-tenant vocabulary: the grid response carries bare per-system unit codes
(`UnitCategoryId`, `UnitTypeGroupId`) whose meaning is tenant-specific, exactly
as GoingToCamp's attribute enums are. This build reads none of them — the shared
availability shape needs only the site's stable id (`UnitId`), its display label
(`Name`, present in the grid body itself) and its per-night `IsFree` — so none
can be shared across tenants, and nothing a code would gate on is ever
suppressed. A future build that decodes them must key its vocabulary per tenant
(the GoingToCamp lesson) and fail open, never sharing one tenant's codes with
another.

ADA: the grid carries a per-unit `IsAda` flag, but this conformer does not read
it and applies no ADA exclusion, exactly like recreation.gov (README
"Providers"). `IsAda` is not established to mean "reserved for campers with
disabilities" — it reads like recreation.gov's "has accessibility features", not
GoingToCamp's explicit "ADA Only" reservation restriction — and excluding on an
unproven flag would hide bookable sites, the one harm the exclusion exists to
prevent. So `include_ada_only` is not read here either. Should a tenant ever be
confirmed to publish a genuine reservation restriction, an exclusion can be
added the way GoingToCamp's is, per that tenant's positively-marked flag alone.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import NamedTuple

import httpx

from common import as_date, date_in_watch

from .base import PollKey, fetch_with_backoff

# The availability grid endpoint, appended to each tenant's `base`/`rdr_path`.
# A platform-wide UseDirect constant, not a per-tenant value.
GRID_ENDPOINT = "rdr/search/grid"

# The grid body wants US month-day-year; the response keys slices by ISO
# datetime and also carries an ISO `Date` field per slice (read below).
GRID_DATE_FORMAT = "%m-%d-%Y"

# Same 12-month horizon the other conformers poll to: a stay nobody can book yet
# is not worth a request.
POLL_HORIZON_MONTHS = 12

# A FacilityId is a positive integer. Bounded in length so a client cannot pack a
# multi-kilobyte "identifier" into the campground_id and grow the POST body this
# provider sends — the same defence going_to_camp applies to provider_ref ids.
FACILITY_ID_RE = re.compile(r"[0-9]{1,15}")

# A safety cap on how many units one facility's grid response may carry, the
# single-POST analogue of going_to_camp's MAX_CHILD_MAPS. It is set orders of
# magnitude above any real facility (the largest are a few hundred sites), so it
# is not a tuning knob: exceeding it means the response drifted into something
# this build does not understand, which recurs identically every cycle. That is a
# permanent per-unit fault, so `parse_grid` RAISES `FacilityTooLarge` rather than
# returning a failed unit — the cycle contains it as a cycle failure and the run
# goes red, instead of a runaway response quietly consuming the cycle behind a
# green exit while its watches look healthy.
MAX_UNITS_PER_FACILITY = 5000


class Tenant(NamedTuple):
    """One vetted UseDirect deployment. `base` (scheme+host, no trailing slash)
    and `rdr_path` (the per-tenant path prefix, empty for California) are
    hardcoded constants; the campground_id's tenant key only selects a `Tenant`
    from the `TENANTS` allowlist, so no client value ever becomes a host (SSRF).
    `booking_url` is the public booking site the push notification opens."""

    base: str
    rdr_path: str
    booking_url: str

    @property
    def grid_url(self) -> str:
        """This tenant's availability POST URL, built from its pinned constants
        alone — `f"{base}/{rdr_path}{GRID_ENDPOINT}"`, matching the reference
        implementation's per-tenant construction (empty `rdr_path` collapses the
        double slash to one, e.g. California's ``…tylerapp.com/rdr/search/grid``;
        a prefixed tenant like Arizona would insert ``azrdr/``)."""
        return f"{self.base}/{self.rdr_path}{GRID_ENDPOINT}"


# The tenant allowlist. ReserveCalifornia only, by scope: the seam is built so
# more states are cheap (add a vetted row), but exactly one is wired. California
# host + empty rdr_path verified 2026-08-01 from camply `main` and a live probe
# (firstmate `usedirect-posture-probe`); California has left `calirdr.usedirect.com`
# for the Tyler cloud host below. Each further state's host and rdr_path must be
# vetted and pinned here individually — never inferred from California's, and
# never derived from a watch's data.
TENANTS: dict[str, Tenant] = {
    "ca": Tenant(
        base="https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com",
        rdr_path="",
        booking_url="https://www.reservecalifornia.com/",
    ),
}


class UnpollableFacility(ValueError):
    """This watch's `campground_id` does not resolve to a facility this build can
    poll — an unknown tenant key, or a FacilityId that is not a bounded positive
    integer. Raised naming only the field at fault, never the client-written
    value: the value is client-supplied and the cycle renders this into the
    operator channel (`providers.unpollable_reason`)."""


class FacilityTooLarge(RuntimeError):
    """This facility's grid response carried more units than the
    MAX_UNITS_PER_FACILITY safety cap, so it cannot be polled under the cap.

    Raised rather than returned as a failed unit, because the two mean opposite
    things to the cycle: a failed unit is transient by contract (keep the old
    state_hash, retry next run), whereas a response past the cap fails
    identically every cycle. The cycle contains this as a cycle failure so the
    run goes red until an operator investigates the drift, instead of leaving the
    park unserved behind a green exit. The message carries counts, the cap and
    the validated `campground_id` label the error lines use — nothing a client
    wrote unchecked — and only the operator channel renders it in full (the
    world-readable rendering of an exception with no response is its type name
    alone)."""


# --- campground_id --------------------------------------------------------

def parse_campground_id(campground_id: str) -> tuple[Tenant, int]:
    """(tenant, facility_id) for a ``<tenant>_<facilityId>`` campground_id, or
    `UnpollableFacility`.

    The tenant key is resolved through the `TENANTS` allowlist — an unknown key
    resolves to nothing, it never becomes a host (SSRF) — and the FacilityId is
    validated as a bounded integer, the only value that reaches a request. A
    campground_id this build cannot resolve is a permanently unpollable watch,
    not a transient fault, so callers surface it through `unpollable_reason`
    (errored once) or re-raise it inside per-watch containment; `poll_plan`,
    which runs outside containment, must swallow it and plan nothing."""
    tenant_key, sep, facility = campground_id.partition("_")
    if not sep:
        raise UnpollableFacility("campground_id is not in <tenant>_<facilityId> form")
    tenant = TENANTS.get(tenant_key)
    if tenant is None:
        raise UnpollableFacility("campground_id names an unregistered UseDirect tenant")
    if not FACILITY_ID_RE.fullmatch(facility):
        raise UnpollableFacility("campground_id facility id is not a bounded integer")
    return tenant, int(facility)


# --- poll plan ------------------------------------------------------------

def horizon_date(today: date) -> date:
    """First-of-month containing today + POLL_HORIZON_MONTHS: a stay starting
    after it is not polled yet."""
    years, month0 = divmod(today.month - 1 + POLL_HORIZON_MONTHS, 12)
    return date(today.year + years, month0 + 1, 1)


def poll_range(start: date, end: date, today: date) -> tuple[date, date] | None:
    """The (first night, last night) to request, both INCLUSIVE, or None when
    there is nothing to poll — a stay whose last night has passed, or one still
    beyond the horizon.

    Unlike recreation.gov (which asks by month) and going_to_camp (whose API
    treats the requested end as one-past-the-last-served-night), UseDirect's grid
    `EndDate` is itself a night: StartDate..EndDate inclusive are the nights
    priced. So this returns the stay's own night span — last night is
    `end - 1 day` for a real stay, `start` for a single-night watch — clamped
    forward to today and back to the horizon, so no past or out-of-horizon night
    can reach a request, the state hash or an alert. `extract_relevant` still
    drops the check-out day and past nights from whatever the grid returns."""
    last_night = end - timedelta(days=1) if end > start else start
    if last_night < today:
        return None
    first = max(start, today)
    horizon = horizon_date(today)
    if first > horizon:
        return None
    return first, min(last_night, horizon)


# --- fetch and parse ------------------------------------------------------

def grid_body(facility_id: int, first: date, last: date) -> dict:
    """The availability POST body — the probe-verified field shape. `WebOnly`
    and `InSeasonOnly` mirror the site's own default search; `UnitSort` is the
    reference implementation's constant. Dates are the inclusive night span in
    the grid's MM-DD-YYYY format."""
    return {
        "FacilityId": facility_id,
        "StartDate": first.strftime(GRID_DATE_FORMAT),
        "EndDate": last.strftime(GRID_DATE_FORMAT),
        "UnitSort": "orderby",
        "InSeasonOnly": True,
        "WebOnly": True,
    }


def _slice_date(slot: dict, key) -> date | None:
    """The calendar date one slice stands for, from its own `Date` field or, as a
    fallback, the ISO-datetime key it is stored under. None when neither parses."""
    for candidate in (slot.get("Date"), key):
        if isinstance(candidate, str):
            try:
                return as_date(candidate)
            except (ValueError, TypeError):
                continue
    return None


def _is_free(slot: dict) -> bool:
    """True only for a night the grid POSITIVELY reports bookable (`IsFree` is
    exactly the JSON boolean true). Anything else — a held slice (`IsBlocked`, a
    `Lock`, a reservation draw), a missing or non-boolean field, junk — is taken.
    'Unknown means taken' is the safe direction for AVAILABILITY: a false opening
    is noise the user can shrug off, whereas alerting on a site that is not
    actually free wastes the one signal this whole system exists to send. (The
    opposite direction — never suppress on an unreadable *designation* — is why
    no ADA/unit-code filter runs here at all.)"""
    return slot.get("IsFree") is True


def parse_grid(raw, *, label: str = "facility") -> dict[str, dict] | None:
    """Defensively parse one grid response into
    ``{unit_id: {"campsite_id", "site", "availabilities": {iso: is_open}}}``, or
    None when the body's shape is unrecognized.

    Site identity is the `UnitId` (the Units dict is keyed by it); the display
    label is the unit's `Name`, present in the grid body itself, so unlike
    going_to_camp no second catalog request is needed for it. A well-formed
    response with an empty `Units` dict parses as authoritative "nothing open";
    a body with no `Facility.Units` dict, or a non-empty `Units` in which no
    entry carries a `Slices` dict (the entry shape itself changed), returns None
    — unrecognized, never "no availability". This is the challenge-page defence:
    an HTML challenge served as 200 fails `resp.json()` upstream, and a JSON body
    of some other shape (a queue-it/waiting-room payload) lands here without
    `Facility.Units` and is treated as a failed poll to retry, never parsed as an
    empty park.

    Raises `FacilityTooLarge` when the unit count exceeds the safety cap (see
    that exception and MAX_UNITS_PER_FACILITY)."""
    if not isinstance(raw, dict):
        return None
    facility = raw.get("Facility")
    if not isinstance(facility, dict):
        return None
    units = facility.get("Units")
    if not isinstance(units, dict):
        return None
    if len(units) > MAX_UNITS_PER_FACILITY:
        raise FacilityTooLarge(
            f"{label}: {len(units)} units in one facility exceeds the "
            f"MAX_UNITS_PER_FACILITY safety cap of {MAX_UNITS_PER_FACILITY}"
        )

    sites: dict[str, dict] = {}
    recognized = False
    for unit_key, unit in units.items():
        if not isinstance(unit, dict):
            continue
        slices = unit.get("Slices")
        if not isinstance(slices, dict):
            continue
        recognized = True
        unit_id = str(unit_key)
        dates: dict[str, bool] = {}
        for slice_key, slot in slices.items():
            if not isinstance(slot, dict):
                continue
            slice_date = _slice_date(slot, slice_key)
            if slice_date is not None:
                dates[slice_date.isoformat()] = _is_free(slot)
        name = unit.get("Name")
        sites[unit_id] = {
            "campsite_id": unit_id,
            "site": name if isinstance(name, str) and name.strip() else unit_id,
            "availabilities": dates,
        }
    if units and not recognized:
        return None
    return sites


def poll_facility(
    http: httpx.Client,
    campground_id: str,
    tenant: Tenant,
    facility_id: int,
    first: date,
    last: date,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
    not_found: set[str] | None = None,
) -> dict[str, dict] | None:
    """POST one facility's grid with the same backoff the other conformers use:
    retry a 403/429/5xx after 2 s, 4 s, 8 s, then give up for this cycle
    (return None). A 200 whose body is invalid JSON or has no recognizable grid
    shape is a non-retryable failure — the unit counts as failed, never as "no
    availability". A 404 additionally records the campground into `not_found`,
    driving the cycle's 404-strike lifecycle. Once `budget_exhausted()` reports
    the cycle's time budget is spent, remaining retries and their sleeps are
    skipped.

    The URL is the tenant's pinned `grid_url`; the only client-derived value in
    the request is the integer `facility_id` in the body (SSRF). The
    `user_agent` is used verbatim — this function neither reads nor invents a
    User-Agent of its own."""
    url = tenant.grid_url
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = grid_body(facility_id, first, last)
    label = f"{campground_id}/{first.isoformat()}"
    return fetch_with_backoff(
        lambda: http.post(url, headers=headers, json=body),
        lambda data: parse_grid(data, label=label),  # may raise FacilityTooLarge
        label=label,
        sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        not_found=not_found, not_found_id=campground_id,
    )


# --- the conformer --------------------------------------------------------

class UseDirectProvider:
    """UseDirect behind the Provider protocol (see providers/base.py)."""

    name = "use_direct"

    def unpollable_reason(self, watch: dict) -> str | None:
        """Why this watch can never be polled, or None (the optional conformer
        hook `providers.unpollable_reason` describes).

        A `campground_id` this build cannot resolve to a registered tenant and a
        bounded FacilityId fails identically every cycle, so the cycle errors the
        watch once rather than leaving the user a watch that looks healthy and
        never alerts. The reason names only the field at fault — the value is
        client-written and the cycle publishes this string — and must not raise
        (the lifecycle pass runs outside per-watch containment)."""
        try:
            parse_campground_id(str(watch["campground_id"]))
        except UnpollableFacility as exc:
            return f"use_direct: {exc}"
        return None

    def poll_plan(self, watch: dict, today: date) -> list[PollKey]:
        """One key per watch: this provider's poll unit is the whole facility
        over the whole stay, a single POST.

        A watch whose campground_id is unresolvable plans nothing —
        `poll_dispatch` runs OUTSIDE the cycle's per-watch containment, so this
        must not raise. `extract_relevant`, which does run inside it, re-parses
        the id and lets the failure surface there as this watch's own."""
        try:
            parse_campground_id(str(watch["campground_id"]))
        except UnpollableFacility:
            return []
        span = poll_range(
            as_date(watch["start_date"]), as_date(watch["end_date"]), today
        )
        if span is None:
            return []
        first, last = span
        return [(str(watch["campground_id"]), (first.isoformat(), last.isoformat()))]

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
        campground_id, (first, last) = key
        tenant, facility_id = parse_campground_id(campground_id)
        return poll_facility(
            http, campground_id, tenant, facility_id,
            as_date(first), as_date(last), user_agent,
            sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
            not_found=not_found,
        )

    def extract_relevant(
        self, availability: dict, watch: dict, today: date
    ) -> dict[str, dict] | None:
        """Current open-site state for one watch in the shape every provider
        shares, or None when its facility failed to poll this cycle (keep the old
        hash and retry next run). Past nights and the check-out day are excluded:
        they are unbookable, so they count toward neither the hash nor an alert.

        No ADA exclusion and no `include_ada_only` read, deliberately — see the
        module docstring: `IsAda` here is not established to mean "reserved", so
        filtering on it would risk hiding bookable sites."""
        # The cycle's lifecycle pass errors an unusable campground_id before it
        # gets here (see `unpollable_reason`); this stays the contained backstop,
        # since extract_relevant is the one entry point that runs inside per-watch
        # containment and can therefore report it as this watch's own failure.
        parse_campground_id(str(watch["campground_id"]))
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
        for site_id, site in parsed.items():
            # Matched on the stable UnitId alone (the shared-shape key and
            # campsite_id are both it), never the `Name` label: the client
            # persists the UnitId in `site_ids` and the label is display only.
            if wanted and not ({site_id, site["campsite_id"]} & wanted):
                continue
            open_dates = sorted(
                d
                for d, open_night in site["availabilities"].items()
                if open_night
                and as_date(d) >= today
                and date_in_watch(as_date(d), start, end)
            )
            if open_dates:
                current[site_id] = {
                    "campsite_id": site["campsite_id"],
                    "site": site["site"],
                    "dates": open_dates,
                }
        return current

    def booking_url(self, watch: dict, openings: list[dict]) -> str:
        """The tenant's public booking site. Like going_to_camp's park-and-dates
        deep link, this lands the user on the live booking surface rather than a
        per-site page: the grid response carries no PlaceId to build a verified
        facility deep link from, so the tenant's booking home is the honest,
        SSRF-safe target (a pinned per-tenant constant, never client data). A
        verified per-facility deep link is a later refinement, not a guess that
        might 404."""
        tenant, _ = parse_campground_id(str(watch["campground_id"]))
        return tenant.booking_url
