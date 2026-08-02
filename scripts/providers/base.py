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

from datetime import date, timedelta
from typing import Callable, Protocol, runtime_checkable

import httpx

from common import as_date, capped_line, date_in_watch

# One unit of polling work, opaque to the cycle apart from its first element.
#
# PollKey[0] MUST be the watch's own `campground_id`: the cycle's 404-strike
# bookkeeping and its campgrounds_polled telemetry are campground-scoped. The
# rest of the key is the provider's own subdivision of a campground's work
# (recreation.gov: the first-of-month date of one month-availability request).
# The cycle deduplicates the poll plan across all users on the whole key, and
# a key only has to be unique within its own provider: the cycle tags every
# planned key with the name of the conformer that produced it, so a
# campground_id two providers happen to share still dispatches to each of them
# separately (see monitor.PollUnit).
PollKey = tuple[str, object]

# Polite-polling parameters every provider shares, so a second provider cannot
# quietly hammer harder than the first: a blocked or failed request is retried
# after 2 s, 4 s, 8 s and then given up on for this cycle.
BACKOFF_DELAYS_SECONDS = [2, 4, 8]
RETRYABLE_STATUS = {403, 429}


def horizon_month_start(today: date, horizon_months: int) -> date:
    """First-of-month containing today + `horizon_months` — the last month a
    provider's poll plan may include.

    Shared because every conformer polls to the same rolling horizon with the
    same month-rollover arithmetic (the `divmod` carries December into the next
    year); centralizing it keeps that one non-obvious calculation in one place.
    Each conformer names it in its own vocabulary and passes its own
    POLL_HORIZON_MONTHS.
    """
    years, month0 = divmod(today.month - 1 + horizon_months, 12)
    return date(today.year + years, month0 + 1, 1)


def night_span_bounds(
    start: date, end: date, today: date, horizon_months: int
) -> tuple[date, date, date] | None:
    """The shared bounds the two day-range conformers clamp their poll request
    to, or None when there is nothing to poll — a stay whose last night has
    passed, or one still beyond the horizon.

    Returns `(first, last_night, horizon)`: `first` is the first night to
    request, clamped forward to today; `last_night` is the stay's own last night
    (`end - 1 day`, or `start` for a single-night stay); `horizon` is the last
    pollable date (`horizon_month_start`). Only the request *end* differs between
    the two conformers, so each builds it from these bounds itself —
    going_to_camp clamps the stay `end`, use_direct the `last_night` — because
    the two APIs disagree on whether the requested end is itself a served night
    (see each conformer's poll_range)."""
    last_night = end - timedelta(days=1) if end > start else start
    if last_night < today:
        return None
    first = max(start, today)
    horizon = horizon_month_start(today, horizon_months)
    if first > horizon:
        return None
    return first, last_night, horizon


def open_dates_in_window(
    availabilities: dict,
    today: date,
    start: date,
    end: date,
    is_open: Callable[[object], bool] = bool,
) -> list[str]:
    """The sorted ISO dates in `availabilities` that are open and fall inside
    the watch's remaining, in-window nights.

    `availabilities` maps an ISO date to the provider's own per-night openness
    value, and `is_open` decides open from it — a plain bool for the
    boolean-shaped grids (going_to_camp, use_direct), a status test for
    recreation.gov (`status == "Available"`). A date is kept only when it is not
    in the past (`>= today`) and lies within the stay (`date_in_watch`): this is
    the one date-window filter every conformer's `extract_relevant` applies
    before hashing or alerting, so past nights and the check-out day count
    toward neither the state hash nor a push.
    """
    return sorted(
        d
        for d, value in availabilities.items()
        if is_open(value)
        and as_date(d) >= today
        and date_in_watch(as_date(d), start, end)
    )


def relevant_open_sites(
    parsed: dict,
    wanted: set[str],
    today: date,
    start: date,
    end: date,
    *,
    exclude: Callable[[dict], bool] | None = None,
) -> dict[str, dict]:
    """The watch's current open-site state from a boolean-grid provider's parsed
    sites, normalized to the shared shape {site_id: {"campsite_id","site","dates"}}.

    Shared by the two conformers whose site identity is a single stable id and
    whose per-night value is a plain bool (going_to_camp, use_direct).
    recreation.gov is deliberately not a caller: it matches its wanted set on the
    site *label* too and reads a status string, not a bool, so its own loop stays
    separate. Each parsed site is matched on its id alone
    (`{site_id, campsite_id} & wanted`, skipped only when the watch named sites),
    then kept only when it still has open, in-window nights (`open_dates_in_window`).

    `exclude`, when given, drops a site the filter should hide — going_to_camp's
    ADA-Only exclusion. It runs AFTER the wanted match (a site the watch named is
    never dropped, which the caller's predicate also enforces) and BEFORE the
    caller hashes the shape, so a hidden site appearing or vanishing is not a
    delta and costs no write.
    """
    current: dict[str, dict] = {}
    for site_id, site in parsed.items():
        if wanted and not ({site_id, site["campsite_id"]} & wanted):
            continue
        if exclude is not None and exclude(site):
            continue
        open_dates = open_dates_in_window(site["availabilities"], today, start, end)
        if open_dates:
            current[site_id] = {
                "campsite_id": site["campsite_id"],
                "site": site["site"],
                "dates": open_dates,
            }
    return current


def single_unit_open_sites(
    availability: dict,
    keys: list[PollKey],
    watch: dict,
    today: date,
    *,
    exclude: Callable[[dict, set[str]], bool] | None = None,
) -> dict[str, dict] | None:
    """The watch's current open-site state for a provider whose whole poll is a
    single unit (going_to_camp, use_direct), or None when that unit failed this
    cycle — so the caller keeps the old state_hash and retries next run.

    `keys` is the watch's own `poll_plan` output: empty means there is nothing
    to poll yet (None), otherwise the one key's parsed result is read from
    `availability`, where None is the failed-unit signal. The stay window and the
    `site_ids` wanted-set are then derived the one way every conformer derives
    them and handed to `relevant_open_sites`. This is the shared skeleton of the
    two single-unit conformers' `extract_relevant`; each still runs its own
    provider-ref backstop and supplies its own `exclude` before calling here.

    `exclude`, when given, is the provider's site-hiding predicate and receives
    `(site, wanted)` — going_to_camp's ADA-Only exclusion needs the wanted-set
    this helper computes (it never hides a site the watch named). It is applied
    exactly as `relevant_open_sites` documents: after the wanted match and before
    the caller hashes the shape.
    """
    if not keys:
        return None
    parsed = availability.get(keys[0])
    if parsed is None:
        return None
    start = as_date(watch["start_date"])
    end = as_date(watch["end_date"])
    wanted = {str(s) for s in (watch.get("site_ids") or [])}
    site_exclude = None if exclude is None else (lambda site: exclude(site, wanted))
    return relevant_open_sites(parsed, wanted, today, start, end, exclude=site_exclude)


def fetch_with_backoff(
    request: Callable[[], httpx.Response],
    parse: Callable[[object], object],
    *,
    label: str,
    sleep,
    errors: list[str] | None,
    budget_exhausted,
    not_found: set[str] | None,
    not_found_id: str | None,
):
    """One poll unit with the backoff every conformer shares: run `request`,
    and on a 403/429/5xx retry after 2 s, 4 s, 8 s, then give up for this cycle
    (return None) so the rest of the run continues.

    A 200 whose body is invalid JSON, or whose `parse(body)` cannot recognize a
    shape, is a NON-retryable failure — the unit counts as failed, never as "no
    availability". A 404 records `not_found_id` into `not_found` (when both are
    given) so the caller can drive the cycle's 404-strike lifecycle; a caller
    that must not strike on 404 (going_to_camp's child maps) passes
    `not_found_id=None`. Once `budget_exhausted()` reports the time budget spent,
    remaining retries and their sleeps are skipped.

    `request` and `parse` are the only provider-specific parts: `request` does
    the GET/POST (its host/URL a pinned constant, never derived from a
    client-writable value — SSRF), and `parse` is the conformer's own body
    parser, which may itself raise a permanent per-unit fault (use_direct's
    FacilityTooLarge) — such a raise propagates, unlike a returned None.
    """
    for attempt in range(len(BACKOFF_DELAYS_SECONDS) + 1):
        try:
            resp = request()
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
            parsed = parse(body)
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


@runtime_checkable
class Provider(Protocol):
    """One campground site, behind the four things the cycle asks of it.

    Conformers are stateless and shared across watches (see PROVIDERS in
    providers/__init__.py), so nothing here may cache per-watch state. A
    conformer whose watches carry client-written configuration of its own may
    also implement the optional `unpollable_reason(watch)` hook, which lets the
    cycle error a permanently unpollable watch once instead of failing it every
    cycle (see providers.unpollable_reason).
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

        Returning None is for a failure a retry could clear. Raising is
        reserved for the opposite: a fault on this unit that will recur
        identically every cycle and needs an operator (going_to_camp's
        ParkTooLarge). The cycle contains it as a *cycle* failure, so the unit
        still behaves as failed for the watches on it — old state_hash kept,
        nothing alerted — but the run goes red instead of leaving a park
        permanently unserved behind a green exit code.

        `errors` collects one already-capped line per failure for the
        world-readable run summary; `budget_exhausted()` cuts retries short
        once the cycle's time budget is spent; `not_found` receives the
        campground_id when the provider learns the campground does not exist,
        which drives the cycle's 404-strike lifecycle (the cycle keeps one such
        set per provider, so a 404 only ever strikes this provider's watches).
        """
        ...

    def extract_relevant(
        self, availability: dict, watch: dict, today: date
    ) -> dict[str, dict] | None:
        """The watch's current open-site state, normalized to the shape every
        provider shares — {site_id: {"campsite_id", "site", "dates": [iso…]}} —
        or None when any of the watch's poll units failed this cycle.

        `availability` is this provider's own {PollKey: parsed-or-None} slice
        of the cycle's results; a provider reads only the keys its own
        poll_plan produced.
        """
        ...

    def booking_url(self, watch: dict, openings: list[dict]) -> str:
        """The deep link the push notification opens for these openings."""
        ...
