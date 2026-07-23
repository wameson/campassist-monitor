"""CampAssist availability monitor — one cycle per GitHub Actions run.

Cycle: jittered start → read active watches → expire past-date watches →
error watches with invalid campground ids or persistently-404ing campgrounds →
dedupe poll plan by (campground_id, month) → poll recreation.gov politely
(one browser UA per run, shuffled order, 1.2–2.8 s gaps, exponential
backoff, all under a per-cycle time budget) → delta-detect per watch via
state_hash → APNs alert with (site, date) dedup + 6 h cooldown → batched
last_checked_at write → one run_summaries row → 30-day retention pruning.

Write budget (see PLAN.md): a cycle with no availability changes performs
at most 5 DB writes regardless of watch count.

Failure containment: a failure that belongs to one watch is caught,
recorded, and skipped — never propagated — so the rest of the watches are
still polled and alerted and the cycle still writes its run summary and
prunes. Only a failure a write actually pinned to one row moves that watch
to status='error'. The exit status is then decided by error *rate* over the
watches the cycle actually tried to serve: isolated failures keep the
scheduled run green, while breakage crossing the systemic threshold exits
non-zero so the Action turns red.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

import httpx

from apns import DELIVERED, RETRYABLE_FAILURE, APNsClient
from db import SupabaseClient

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

AVAILABILITY_URL = "https://www.recreation.gov/api/camps/availability/campground/{campground_id}/month"

START_JITTER_MAX_SECONDS = 240.0
INTER_REQUEST_DELAY_RANGE = (1.2, 2.8)
BACKOFF_DELAYS_SECONDS = [2, 4, 8]
# Keeps jitter (≤240 s) + polling + one in-flight request (≤20 s) + the
# bookkeeping writes inside the workflow's 15-minute timeout even under
# sustained 403/429 blocking.
CYCLE_TIME_BUDGET_SECONDS = 480.0
# Fixed westmost-US offset (UTC-8, no DST) for deriving "today": same-night
# openings at US campgrounds stay alertable during US evening hours after
# UTC midnight.
WESTMOST_US_OFFSET = timezone(timedelta(hours=-8))
RETRYABLE_STATUS = {403, 429}
ALERT_COOLDOWN_HOURS = 6
RETENTION_DAYS = 30
POLL_HORIZON_MONTHS = 12
NOT_FOUND_ERROR_THRESHOLD = 3
CAMPGROUND_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# Systemic-failure threshold (tune here). A cycle exits non-zero only when
# contained watch failures affect more than SYSTEMIC_ERROR_RATE of the watches
# it actually tried to serve AND at least SYSTEMIC_ERROR_FLOOR watches. The
# rate is what makes broad breakage loud — a whole-pool write failure once ran
# green-looking for days; the floor keeps one bad row in a tiny pool (1 of 2)
# from crying wolf, while 2 of 2 still goes red. Failures that belong to no
# watch (the run_summaries INSERT, retention pruning) are always systemic.
SYSTEMIC_ERROR_RATE = 0.25
SYSTEMIC_ERROR_FLOOR = 2
# Caps on what a failing cycle may cost: error text stays readable, and a
# batched write that fails for a large pool is not retried one row at a time
# (it is systemic anyway — fanning out would spend hundreds of writes).
MAX_ERROR_MESSAGE_CHARS = 200
MAX_LOGGED_WATCH_ERRORS = 10
PER_ID_FALLBACK_MAX = 50
# Wall-clock room, past the poll budget, that per-id fan-out may use. The
# fan-out runs after polling, so its worst case is this plus one in-flight
# request (≤30 s in db.py) — it must never eat the workflow's 15-minute
# timeout, or the run dies before the summary row and the pruning.
PER_ID_FALLBACK_BUDGET_SECONDS = 300.0


# --- jitter ---------------------------------------------------------------

def start_delay(rng: random.Random) -> float:
    """Random run-start delay to desynchronize from the exact cron tick."""
    return rng.uniform(0, START_JITTER_MAX_SECONDS)


def inter_request_delay(rng: random.Random) -> float:
    """Humanized gap between recreation.gov requests."""
    return rng.uniform(*INTER_REQUEST_DELAY_RANGE)


# --- dates ----------------------------------------------------------------

def as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def iso_now(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def monitor_today(now: datetime) -> date:
    """'Today' for the poll plan, alert date filter, and watch expiry."""
    return now.astimezone(WESTMOST_US_OFFSET).date()


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


def date_in_watch(d: date, start: date, end: date) -> bool:
    """Nights of the stay: check-out day availability is irrelevant."""
    if end > start:
        return start <= d < end
    return d == start


# --- poll plan ------------------------------------------------------------

def dedupe_poll_plan(watches: list[dict], today: date) -> list[tuple[str, date]]:
    """One (campground_id, month) entry per unique pair across ALL users."""
    plan = set()
    for watch in watches:
        start = as_date(watch["start_date"])
        end = as_date(watch["end_date"])
        for month in months_for_watch(start, end, today):
            plan.add((str(watch["campground_id"]), month))
    return sorted(plan)


# --- recreation.gov -------------------------------------------------------

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

    for attempt in range(len(BACKOFF_DELAYS_SECONDS) + 1):
        try:
            resp = http.get(url, params=params, headers=headers)
            status = resp.status_code
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            status = None
            failure = f"{campground_id}/{month.isoformat()}: {exc!r}"
        if status == 200:
            try:
                body = resp.json()
            except ValueError:
                failure = f"{campground_id}/{month.isoformat()}: invalid JSON"
                break
            parsed = parse_availability(body)
            if parsed is not None:
                return parsed
            failure = f"{campground_id}/{month.isoformat()}: unrecognized response body"
            break
        if status is not None:
            failure = f"{campground_id}/{month.isoformat()}: HTTP {status}"
            if not (status in RETRYABLE_STATUS or status >= 500):
                if status == 404 and not_found is not None:
                    not_found.add(campground_id)
                break
        if attempt < len(BACKOFF_DELAYS_SECONDS):
            if budget_exhausted():
                break
            sleep(BACKOFF_DELAYS_SECONDS[attempt])
    if errors is not None:
        errors.append(failure)
    return None


# --- delta detection ------------------------------------------------------

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def state_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def extract_relevant(availability: dict, watch: dict, today: date) -> dict[str, dict] | None:
    """Current open-site state relevant to one watch, or None if any of
    the watch's months failed to poll this cycle (keep old hash, retry
    next run rather than hashing partial data). Past nights are excluded:
    they are unbookable, so they count toward neither the hash nor alerts.
    A watch wholly beyond the polling horizon has no pollable months yet,
    so it also returns None; a watch straddling the horizon is hashed on
    its in-horizon months alone."""
    start = as_date(watch["start_date"])
    end = as_date(watch["end_date"])
    wanted = {str(s) for s in (watch.get("site_ids") or [])}

    months = months_for_watch(start, end, today)
    if not months:
        return None

    merged: dict[str, dict] = {}
    for month in months:
        parsed = availability.get((str(watch["campground_id"]), month))
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


def available_sites(current: dict[str, dict]) -> list[dict]:
    openings = [
        {"campsite_id": cs["campsite_id"], "site": cs["site"], "date": d}
        for cs in current.values()
        for d in cs["dates"]
    ]
    openings.sort(key=lambda o: (o["date"], o["site"]))
    return openings


# --- alert dedup ----------------------------------------------------------

def filter_unalerted(
    db, watch: dict, openings: list[dict], now: datetime, cooldown_hours: int = ALERT_COOLDOWN_HOURS
) -> list[dict]:
    """Drop openings whose (site, date) was alerted within the cooldown."""
    if not openings:
        return []
    rows = db.select("sent_alerts", {"watch_id": f"eq.{watch['id']}"})
    cutoff = now - timedelta(hours=cooldown_hours)
    recent = {
        (str(r["site_id"]), as_date(r["date"]).isoformat())
        for r in rows
        if parse_timestamp(r["sent_at"]) > cutoff
    }
    return [o for o in openings if (o["campsite_id"], o["date"]) not in recent]


def alert_rows(watch: dict, openings: list[dict], now: datetime) -> list[dict]:
    return [
        {
            "watch_id": watch["id"],
            "site_id": o["campsite_id"],
            "date": o["date"],
            "sent_at": iso_now(now),
        }
        for o in openings
    ]


# --- failure containment --------------------------------------------------

def summarize_exception(exc: BaseException) -> str:
    """One-line, length-capped rendering of a contained failure."""
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())
    if len(detail) > MAX_ERROR_MESSAGE_CHARS:
        detail = detail[: MAX_ERROR_MESSAGE_CHARS - 1] + "…"
    return detail


def is_permanent_failure(exc: BaseException) -> bool:
    """True when a contained DB failure would fail identically next cycle.

    A PostgREST 4xx other than 429 is a rejected *request* — missing column,
    constraint violation, malformed payload — so retrying it forever is
    pointless and the watch it belongs to is marked status='error' instead.
    429s, 5xx, timeouts and transport errors are transient: the watch stays
    'monitoring' and is simply retried next cycle.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status is not None and 400 <= status < 500 and status != 429


class PatchOutcome(NamedTuple):
    """Which watches a batched PATCH could not write, and which of those
    failures a write actually *pinned* to that row (`isolated`).

    An unpinned failure is one exception blamed on every id in the batch;
    nobody showed that row is bad, so it may be recorded and counted but must
    never move a watch to status='error' — users must not have to recreate a
    watch over a failure that was never attributed to it.
    """

    failures: dict[str, BaseException]
    isolated: frozenset[str]


def isolated_failure(watch_id: str, exc: BaseException) -> PatchOutcome:
    """A failure the caller already pinned to exactly one watch."""
    return PatchOutcome({watch_id: exc}, frozenset({watch_id}))


def patch_watches(db, watch_ids, data: dict, *, fanout_allowed=lambda: True) -> PatchOutcome:
    """PATCH `data` onto many watches, isolating the row that is actually bad.

    The healthy path is the single batched `id=in.(…)` write the write budget
    assumes. Only a *permanently* rejected batch (a PostgREST 4xx other than
    429) is retried one id at a time, so one unwritable row cannot silently
    drop everyone else's update. A transient batch failure (429, 5xx, timeout,
    transport error) is never fanned out: it marks nothing errored, so
    isolating it buys nothing, while dozens of sequential 30-second PATCHes
    against a struggling Supabase would blow the workflow timeout and kill the
    run before its summary and pruning. For the same reason a fan-out stops
    once `fanout_allowed()` goes false, and a batch larger than
    PER_ID_FALLBACK_MAX is not fanned out at all.

    Ids the fan-out never reached — and every id of a batch that was not fanned
    out — carry the batch exception but are absent from `isolated`.
    """
    ids = sorted(str(i) for i in watch_ids)
    if not ids:
        return PatchOutcome({}, frozenset())
    try:
        db.patch("watches", {"id": f"in.({','.join(ids)})"}, data)
        return PatchOutcome({}, frozenset())
    except Exception as exc:  # containment boundary
        batch_failure = exc
    if len(ids) == 1:
        # the batch *was* a single-row write, so it named the bad row itself
        return isolated_failure(ids[0], batch_failure)
    if not is_permanent_failure(batch_failure) or len(ids) > PER_ID_FALLBACK_MAX:
        return PatchOutcome({watch_id: batch_failure for watch_id in ids}, frozenset())
    failures: dict[str, BaseException] = {}
    isolated: set[str] = set()
    for watch_id in ids:
        if not fanout_allowed():
            failures[watch_id] = batch_failure
            continue
        try:
            db.patch("watches", {"id": f"eq.{watch_id}"}, data)
        except Exception as exc:  # containment boundary
            failures[watch_id] = exc
            isolated.add(watch_id)
    return PatchOutcome(failures, frozenset(isolated))


def is_systemic(failed: int, considered: int) -> bool:
    """Whether this cycle's contained watch failures are broad enough to fail
    the run (see SYSTEMIC_ERROR_RATE / SYSTEMIC_ERROR_FLOOR). Both counts are
    over the watches the cycle actually tried to serve."""
    if failed < SYSTEMIC_ERROR_FLOOR:
        return False
    return failed > considered * SYSTEMIC_ERROR_RATE


# --- main cycle -----------------------------------------------------------

def run(
    db,
    apns,
    http,
    *,
    rng: random.Random | None = None,
    sleep=time.sleep,
    now_fn=lambda: datetime.now(timezone.utc),
    monotonic=time.monotonic,
    time_budget_seconds: float = CYCLE_TIME_BUDGET_SECONDS,
    per_id_fallback_budget_seconds: float = PER_ID_FALLBACK_BUDGET_SECONDS,
) -> dict:
    rng = rng or random.Random()
    started = monotonic()
    deadline = started + time_budget_seconds
    # Fan-out runs after the poll budget is spent, so it gets its own deadline
    # rather than sharing budget_exhausted() (which is already true by then).
    fanout_deadline = deadline + per_id_fallback_budget_seconds

    def budget_exhausted() -> bool:
        return monotonic() >= deadline

    def fanout_allowed() -> bool:
        return monotonic() < fanout_deadline

    errors: list[str] = []
    now = now_fn()
    today = monitor_today(now)

    # Contained failures. watch_failures holds the first failure per watch
    # (its message); mark_errored is the subset whose failure was pinned to
    # that row *and* looks permanent, so it should surface on the watch itself;
    # blocked_ids are watches whose failure stops the cycle from serving them
    # further; cycle_failures are failures attributable to no single watch,
    # which are systemic by definition.
    watch_failures: dict[str, str] = {}
    mark_errored: set[str] = set()
    blocked_ids: set[str] = set()
    cycle_failures: list[str] = []

    def write_watches(watch_ids, data: dict) -> PatchOutcome:
        return patch_watches(db, watch_ids, data, fanout_allowed=fanout_allowed)

    def record_failures(
        outcome: PatchOutcome, context: str, *, errorable=True, blocking=True
    ) -> None:
        for watch_id, exc in outcome.failures.items():
            if watch_id not in watch_failures:  # one watch, one recorded failure
                watch_failures[watch_id] = f"{context}: {summarize_exception(exc)}"
                if errorable and watch_id in outcome.isolated and is_permanent_failure(exc):
                    mark_errored.add(watch_id)
            if blocking:
                blocked_ids.add(watch_id)

    def blocked(watch: dict) -> bool:
        return str(watch["id"]) in blocked_ids

    watches = db.select("watches", {"status": "eq.monitoring"})

    # Backend-owned lifecycle: expire past-date watches (one batched write).
    # An expiring watch is leaving the pool either way, so a failure here is
    # recorded but never turned into status='error' (errorable=False).
    expired_ids = {w["id"] for w in watches if as_date(w["end_date"]) < today}
    if expired_ids:
        record_failures(
            write_watches(expired_ids, {"status": "expired"}),
            "expire",
            errorable=False,
            blocking=False,
        )
    active = [w for w in watches if w["id"] not in expired_ids]

    # Backend-owned lifecycle: watches with malformed campground ids can
    # never poll successfully, so they move to status='error' once (one
    # batched write, failure case only) instead of re-erroring every cycle.
    invalid = [w for w in active if not CAMPGROUND_ID_RE.fullmatch(str(w["campground_id"]))]
    if invalid:
        for campground_id in sorted({str(w["campground_id"]) for w in invalid}):
            errors.append(f"{campground_id!r}: invalid campground_id, skipped")
        # errorable=False: this write *is* the status='error' write, so there
        # is nothing for the end-of-cycle marking to retry.
        record_failures(
            write_watches((w["id"] for w in invalid), {"status": "error"}),
            "error-invalid",
            errorable=False,
            blocking=False,
        )
        invalid_watch_ids = {w["id"] for w in invalid}
        active = [w for w in active if w["id"] not in invalid_watch_ids]

    plan = dedupe_poll_plan(active, today)
    rng.shuffle(plan)
    session_ua = rng.choice(USER_AGENTS)  # one UA per run, rotated across runs

    not_found: set[str] = set()
    availability: dict[tuple[str, date], dict | None] = {}
    for i, (campground_id, month) in enumerate(plan):
        if budget_exhausted():
            errors.append(
                f"time budget exhausted: skipped {len(plan) - i} remaining poll(s)"
            )
            break
        availability[(campground_id, month)] = poll_with_backoff(
            http, campground_id, month, session_ua,
            sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
            not_found=not_found,
        )
        if i < len(plan) - 1 and not budget_exhausted():
            sleep(inter_request_delay(rng))

    # Backend-owned lifecycle: a syntactically valid campground id that
    # keeps 404ing (typo or delisted campground) errors its watches after
    # NOT_FOUND_ERROR_THRESHOLD consecutive cycles instead of warning
    # forever; any successful poll resets the strike count. All writes here
    # happen only in the failure/recovery cases, so a healthy no-change
    # cycle stays within the write budget.
    polled_ok = {cg for (cg, _), parsed in availability.items() if parsed is not None}
    strike_updates: dict[int, set] = {}
    errored_404 = []
    for watch in active:
        campground_id = str(watch["campground_id"])
        strikes = int(watch.get("consecutive_not_found") or 0)
        if campground_id in polled_ok:
            if strikes:
                strike_updates.setdefault(0, set()).add(watch["id"])
        elif campground_id in not_found:
            strikes += 1
            if strikes >= NOT_FOUND_ERROR_THRESHOLD:
                errored_404.append(watch)
            else:
                strike_updates.setdefault(strikes, set()).add(watch["id"])
    # The strike count is bookkeeping: a rejected write is recorded and counted,
    # but must neither error the watch nor stop the cycle from serving it. The
    # reset group is the case that matters — those campgrounds polled fine this
    # cycle, so those watches may have a new opening to alert on.
    for strikes in sorted(strike_updates):
        record_failures(
            write_watches(strike_updates[strikes], {"consecutive_not_found": strikes}),
            "strike-count",
            errorable=False,
            blocking=False,
        )
    if errored_404:
        for campground_id in sorted({str(w["campground_id"]) for w in errored_404}):
            errors.append(
                f"{campground_id}: not found for {NOT_FOUND_ERROR_THRESHOLD} "
                "consecutive cycles, watch(es) errored"
            )
        record_failures(
            write_watches(
                (w["id"] for w in errored_404),
                {"status": "error", "consecutive_not_found": NOT_FOUND_ERROR_THRESHOLD},
            ),
            "error-404",
            blocking=False,
        )
        errored_404_ids = {w["id"] for w in errored_404}
        active = [w for w in active if w["id"] not in errored_404_ids]

    # Per-watch processing is contained: anything unexpected here (a rejected
    # write, a malformed row, an APNs client bug) fails just this watch. The
    # others still alert, and the cycle still reaches its bookkeeping below.
    alerts_sent = 0
    for watch in active:
        if blocked(watch):
            continue  # already failed a lifecycle write that blocks serving it
        try:
            current = extract_relevant(availability, watch, today)
            if current is None:
                continue  # poll failed for this watch's months; keep old hash
            new_hash = state_hash(current)
            if new_hash == watch.get("state_hash"):
                continue
            openings = available_sites(current)
            fresh = filter_unalerted(db, watch, openings, now)
            delivered = False
            if fresh:
                outcome = apns.send_alert(watch, fresh, db, errors=errors)
                if outcome == RETRYABLE_FAILURE:
                    continue  # keep old hash so the alert is retried next cycle
                if outcome == DELIVERED:
                    db.upsert("sent_alerts", alert_rows(watch, fresh, now), on_conflict="watch_id,site_id,date")
                    alerts_sent += len(fresh)
                    delivered = True
            db.patch(
                "watches",
                {"id": f"eq.{watch['id']}"},
                {"state_hash": new_hash, **({"last_found_at": iso_now(now)} if delivered else {})},
            )
        except Exception as exc:  # containment boundary
            record_failures(isolated_failure(str(watch["id"]), exc), "process")

    # The served set: watches this cycle actually tried to serve — still active
    # after the lifecycle passes (not expired this cycle, not errored for an
    # invalid or persistently-404ing campground) and not left unpolled by the
    # time budget. It is both the denominator and the scope of the numerator of
    # the systemic rate, so a cycle that failed every watch it served goes red
    # however many watches left the pool for unrelated reasons.
    #
    # Bookkeeping covers the served watches whose needed months were all
    # attempted: skipped watches keep their old last_checked_at and the summary
    # reports what was actually polled rather than what was planned.
    attempted = set(availability)
    served, checked = [], []
    for w in active:
        needed = months_for_watch(
            as_date(w["start_date"]), as_date(w["end_date"]), today
        )
        if not all((str(w["campground_id"]), month) in attempted for month in needed):
            continue  # never reached: the poll time budget ran out first
        served.append(w)
        if needed and not blocked(w):
            checked.append(w)
    served_ids = {str(w["id"]) for w in served}
    if checked:
        outcome = write_watches((w["id"] for w in checked), {"last_checked_at": iso_now(now)})
        record_failures(outcome, "last-checked")
        checked = [w for w in checked if str(w["id"]) not in outcome.failures]

    # Threshold gate (B): decide isolated vs systemic before writing the
    # summary, so the run_summaries row records which verdict was reached.
    considered = len(served)
    failed_served = sum(1 for watch_id in watch_failures if watch_id in served_ids)
    systemic = is_systemic(failed_served, considered)

    # Isolated, permanent failures surface on the watch itself (A) so the user
    # sees a broken watch instead of one that silently stops updating. Systemic
    # breakage is the operator's to fix, so the pool is left intact rather than
    # erroring every watch at once. Failing to mark belongs to no watch — it is
    # a DB-level problem that would otherwise leave every affected watch quietly
    # 'monitoring' — so it counts as systemic.
    if mark_errored and not systemic:
        outcome = write_watches(mark_errored, {"status": "error"})
        for watch_id, exc in outcome.failures.items():
            errors.append(f"watch {watch_id}: error-mark: {summarize_exception(exc)}")
        if outcome.failures:
            cycle_failures.append(
                f"error-mark: {len(outcome.failures)} of {len(mark_errored)} watch(es) "
                "could not be moved to status='error'"
            )

    if watch_failures:
        tally = (
            f"{failed_served} of {considered} served watch(es) failed this cycle "
            f"({'systemic' if systemic else 'isolated'})"
        )
        unserved = len(watch_failures) - failed_served
        if unserved:
            tally += f", plus {unserved} on watch(es) this cycle did not serve"
        errors.append(tally)
        for watch_id in sorted(watch_failures)[:MAX_LOGGED_WATCH_ERRORS]:
            errors.append(f"watch {watch_id}: {watch_failures[watch_id]}")
        undisplayed = len(watch_failures) - MAX_LOGGED_WATCH_ERRORS
        if undisplayed > 0:
            errors.append(f"and {undisplayed} more watch error(s)")

    summary = {
        "watches_checked": len(checked),
        "campgrounds_polled": len({cg for cg, _ in availability}),
        "alerts_sent": alerts_sent,
        "duration_ms": int((monotonic() - started) * 1000),
        "errors": "; ".join(errors) or None,
    }
    # The summary row and the pruning are contained too: losing one must not
    # cost the other, and neither belongs to a watch, so either failing is
    # systemic — the run goes red even if every watch was served.
    def contained(label: str, write, *args) -> None:
        try:
            write(*args)
        except Exception as exc:  # containment boundary
            cycle_failures.append(f"{label}: {summarize_exception(exc)}")

    retention_cutoff = iso_now(now - timedelta(days=RETENTION_DAYS))
    contained("run_summaries insert", db.insert, "run_summaries", summary)
    contained("sent_alerts prune", db.delete, "sent_alerts", {"sent_at": f"lt.{retention_cutoff}"})
    contained("run_summaries prune", db.delete, "run_summaries", {"ran_at": f"lt.{retention_cutoff}"})

    return {
        **summary,
        "watches_considered": considered,
        "watch_errors": len(watch_failures),
        # Failures with no watch to blame. Recorded here rather than in the
        # run_summaries row: that row is written (or lost) before they happen.
        "cycle_errors": "; ".join(cycle_failures) or None,
        "systemic_failure": systemic or bool(cycle_failures),
    }


def error_annotation(summary: dict) -> str | None:
    """GitHub Actions warning annotation when the cycle recorded errors,
    surfacing them in the run history. On its own it does not fail the run:
    isolated errors keep the schedule green (see failure_annotation)."""
    if summary.get("errors"):
        return f"::warning::monitor completed with errors: {summary['errors']}"
    return None


def failure_annotation(result: dict) -> str | None:
    """GitHub Actions error annotation for a run that exits non-zero: the
    failures crossed the systemic threshold, so this cycle served few or no
    watches and needs an operator."""
    if not result.get("systemic_failure"):
        return None
    detail = "; ".join(
        part for part in (result.get("errors"), result.get("cycle_errors")) if part
    )
    return f"::error::monitor cycle failed systemically: {detail or 'see run_summaries'}"


def exit_code(result: dict) -> int:
    """0 for a cycle whose failures were isolated (healthy watches were still
    served), 1 when they were systemic and the run must go red."""
    return 1 if result.get("systemic_failure") else 0


def main() -> None:
    rng = random.Random()
    delay = start_delay(rng)
    print(f"start jitter: sleeping {delay:.0f}s", flush=True)
    time.sleep(delay)

    db = SupabaseClient.from_env()
    apns = APNsClient.from_env()
    with httpx.Client(http2=True, timeout=20, follow_redirects=True) as http:
        result = run(db, apns, http, rng=rng)
    print(json.dumps(result), flush=True)
    for annotation in (error_annotation(result), failure_annotation(result)):
        if annotation:
            print(annotation, flush=True)
    raise SystemExit(exit_code(result))


if __name__ == "__main__":
    main()
