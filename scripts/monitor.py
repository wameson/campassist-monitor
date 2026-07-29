"""CampAssist availability monitor — one cycle per GitHub Actions run.

Cycle: jittered start → read active watches (and count the errored ones, which
are terminal and would otherwise be invisible) → expire past-date watches →
route each watch to the provider its `provider` column names → error watches
with invalid campground ids, with provider config their provider cannot poll,
or with persistently-404ing campgrounds, recording why on each row →
dedupe the poll plan across all users
→ poll each provider politely (one browser UA per run, shuffled order, 1.2–2.8 s
gaps, exponential backoff, all under a per-cycle time budget) → delta-detect
per watch via state_hash → APNs alert
with (campsite id, date) dedup + 6 h cooldown → batched last_checked_at write
→ one run_summaries row → 30-day retention pruning.

Everything site-specific — the request host, the parsing, the normalization to
the shared availability shape, the booking link — lives behind the `Provider`
protocol in providers/, never here. This module is provider-neutral: it plans,
paces, hashes, alerts, contains failures and does its bookkeeping the same way
whatever site a watch polls.

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
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

import httpx

from apns import CONFIG_FAILURE, DELIVERED, PERMANENT_FAILURE, RETRYABLE_FAILURE, APNsClient
# MAX_ERROR_MESSAGE_CHARS travels with capped_line: it is this module's
# published bound on every line reaching run_summaries.errors, and sits in
# common.py only so a provider can cap against the very same constant.
from common import MAX_ERROR_MESSAGE_CHARS, as_date, capped_line
from db import SupabaseClient
# MISSING_COLUMN_CODE is shared with the preflight rather than restated: both
# ask the same question of PostgREST — is this column absent from the live
# database? — the preflight before the cycle, `rejects_missing_column` while it
# writes (which additionally accepts the code PostgREST uses for a *write*, see
# WRITE_MISSING_COLUMN_CODES).
from preflight import MISSING_COLUMN_CODE, preflight
from providers import (
    PROVIDERS,
    PollKey,
    Provider,
    provider_for,
    provider_name,
    unpollable_reason,
)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

# Start jitter, cut from 240 s on 2026-07-28 (captain-approved, measured). Two
# findings set the new value. It was costing a *mean of 120 s of billed runner
# time per run* against ~10 s of real work — 92% of the monitor's Actions
# minutes were time.sleep() — which alone put an honest */30 cadence at
# ~4,154 min/mo against a 2,000-minute free tier, i.e. an outage at the default
# $0 spending limit, not a bill. And under the `schedule` trigger it was buying
# nothing: GitHub's own throttling already spreads delivery near-uniformly
# across all 60 minutes of the hour, so the desync below was already being
# provided for free.
#
# It is 20 s rather than 0 because the trigger is planned to move to an
# external cron that fires at exact wall-clock times, which restores the
# desync rationale; reinstating a removed constant later is a second change.
# 20 s is the top of the free band: billing is per job rounded up to the whole
# minute, and a jitter-free job measured 11–25 s end to end, so anything ≤20 s
# still lands inside the 1-minute floor while 30 s or more doubles the bill.
# Cost is flat below that edge and the desync grows with the spread, so take
# the maximum — it still leaves 15–30 s of headroom against runner variance.
START_JITTER_MAX_SECONDS = 20.0
INTER_REQUEST_DELAY_RANGE = (1.2, 2.8)
# Keeps jitter (≤20 s) + polling + one in-flight request (≤20 s) + the
# bookkeeping writes inside the workflow's 15-minute timeout even under
# sustained 403/429 blocking.
CYCLE_TIME_BUDGET_SECONDS = 480.0
# Fixed westmost-US offset (UTC-8, no DST) for deriving "today": same-night
# openings at US campgrounds stay alertable during US evening hours after
# UTC midnight.
WESTMOST_US_OFFSET = timezone(timedelta(hours=-8))
ALERT_COOLDOWN_HOURS = 6
RETENTION_DAYS = 30
NOT_FOUND_ERROR_THRESHOLD = 3
CAMPGROUND_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# Why a watch was moved to status='error', recorded in watches.error_reason
# alongside every such write. status='error' is terminal — an errored watch
# leaves the only query the cycle runs and no code path puts it back — so the
# single status bit could not tell an operator (or any future retry policy, or
# the app) apart a watch a data fix would revive from one that is dead for good.
# These four values are the whole vocabulary a *write* may use: a small stable
# machine-readable set, deliberately not prose, so a policy can classify on them.
#
#   invalid_campground_id    the id fails CAMPGROUND_ID_RE and is never polled.
#   unreadable_provider_ref  the watch's own provider says it can never poll the
#                            client-written config on this row.
#   campground_not_found     the campground 404ed for NOT_FOUND_ERROR_THRESHOLD
#                            consecutive cycles — a delisted or non-reservable
#                            facility, the one genuinely permanent cause.
#   watch_write_rejected     a write pinned to this row was rejected outright
#                            (see is_permanent_failure) — the retry-worthy one:
#                            a missing column recovers the moment its migration
#                            is applied.
ERROR_REASON_INVALID_CAMPGROUND_ID = "invalid_campground_id"
ERROR_REASON_UNREADABLE_PROVIDER_REF = "unreadable_provider_ref"
ERROR_REASON_CAMPGROUND_NOT_FOUND = "campground_not_found"
ERROR_REASON_WRITE_REJECTED = "watch_write_rejected"
ERROR_REASONS = frozenset({
    ERROR_REASON_INVALID_CAMPGROUND_ID,
    ERROR_REASON_UNREADABLE_PROVIDER_REF,
    ERROR_REASON_CAMPGROUND_NOT_FOUND,
    ERROR_REASON_WRITE_REJECTED,
})
# Reported by the census below, never written: a row errored before 0004 was
# applied (or by an older build) carries no reason, and `watches` is a table its
# owner can write, so any value outside the vocabulary is bucketed rather than
# echoed into the log.
ERROR_REASON_UNRECORDED = "unrecorded"
ERROR_REASON_OTHER = "other"
ERROR_REASON_COLUMN = "error_reason"

# How PostgREST says "that column is not there" about a column named in a write
# *body*, which is the only thing `rejects_missing_column` classifies. Two codes
# mean it: `PGRST204` is PostgREST's own schema-cache miss ("Could not find the
# 'error_reason' column of 'watches' in the schema cache"), while `42703` is
# PostgreSQL's, which surfaces for select lists and filters — and which
# preflight.py cites having seen on a write during the 0001 incident, possibly
# from a PostgREST older than the one now fronting this database. Both are
# permanent 4xx and both mean the same thing, so the write path accepts either:
# recognizing only one would leave an unapplied migration failing every
# status='error' write outright, the failing-forever shape error_reason exists
# to expose. The preflight's own select-list probe keeps MISSING_COLUMN_CODE
# alone — there, 42703 is the only code the question can produce.
WRITE_MISSING_COLUMN_CODES = frozenset({MISSING_COLUMN_CODE, "PGRST204"})

# Systemic-failure threshold (tune here). A cycle exits non-zero only when
# contained watch failures affect more than SYSTEMIC_ERROR_RATE of the watches
# it actually tried to serve AND at least SYSTEMIC_ERROR_FLOOR watches. The
# rate is what makes broad breakage loud — a whole-pool write failure once ran
# green-looking for days; the floor keeps one bad row in a tiny pool (1 of 2)
# from crying wolf, while 2 of 2 still goes red. Failures that belong to no
# watch (the run_summaries INSERT, retention pruning) are always systemic.
SYSTEMIC_ERROR_RATE = 0.25
SYSTEMIC_ERROR_FLOOR = 2
# Pool-wide APNs backstop, independent of per-reason rating: a push rejection
# whose reason code apns.send_alert did not enumerate as a config fault still
# counts as PERMANENT_FAILURE and stays out of the rate, so an unenumerated
# pool-wide fault could otherwise deliver zero alerts on a green run. When
# outright APNs rejections wipe out nearly every served push, the cycle is
# systemic regardless of reason. The rate is high and the floor keeps a handful
# of genuinely dead device tokens in a healthy pool green.
APNS_WIPEOUT_RATE = 0.8
APNS_WIPEOUT_FLOOR = 2
# Caps on what a failing cycle may cost: error text stays readable (the
# per-line bound is MAX_ERROR_MESSAGE_CHARS, imported above), and a batched
# write that fails for a large pool is not retried one row at a time (it is
# systemic anyway — fanning out would spend hundreds of writes).
MAX_LOGGED_WATCH_ERRORS = 10
PER_ID_FALLBACK_MAX = 50
# The per-id fan-out must never cost the run its summary row and its pruning,
# so it is capped twice and the arithmetic closes against the workflow's
# `timeout-minutes: 15` (900 s, .github/workflows/monitor.yml):
#
#   120 s  job setup      checkout + setup-python + pip install
# + 600 s  FANOUT_DEADLINE_SECONDS, measured from PROCESS start — the start
#          jitter main() sleeps (≤20 s) and the schema preflight that runs
#          before it are inside it, not on top of it
#
# The preflight (preflight.py, called from main() before the jitter) is charged
# against that same 600 s and has ample headroom: it is 4 GETs, one per table,
# each bounded by the client's 30 s timeout (db.py) — at most 120 s of the 600.
# That ceiling holds on a broken Supabase too: a probe failure does not end the
# pass (a blip on one table must not hide drift on another), so the worst case
# stays those same 4 probes, on a run where the cycle would achieve nothing
# anyway. The per-column narrowing pass fires only on real drift, on a run that
# is already halting or warning, and costs at worst roughly one extra GET per
# column of the drifted table, so it cannot blow a healthy run's fanout or
# timeout budget.
# + 180 s  shutdown       one in-flight PATCH (30 s, db.py) + the run_summaries
#                         insert and both prunes (30 s each), plus margin
# = 900 s  the whole job
#
# PER_ID_FALLBACK_BUDGET_SECONDS is the second cap: the total wall-clock every
# fan-out of one cycle may spend between them, charged by the time each per-id
# write actually takes, so an early-finishing poll phase cannot hand the fan-out
# the leftover budget. No per-id write starts once the allowance is gone, so a
# whole cycle's fan-out spend is at most 100 s plus the one PATCH still in
# flight (30 s, db.py) — across every fan-out site together, not per site.
#
# Since the jitter came down to 20 s, the worst case reaches the fan-out with
# room to spare rather than already past the deadline: 20 s of jitter plus a
# fully spent CYCLE_TIME_BUDGET_SECONDS (480 s) is 500 s of the 600 s, so even
# the slowest cycle keeps ~100 s of fan-out — the isolation the deadline used
# to spend first. The deadline still binds, and still yields to the summary row
# and the pruning when it does; it just no longer binds on a healthy run.
# test_fanout_deadline_fits_inside_the_job_timeout holds that arithmetic.
JOB_TIMEOUT_SECONDS = 900.0
JOB_SETUP_RESERVE_SECONDS = 120.0
SHUTDOWN_RESERVE_SECONDS = 180.0
FANOUT_DEADLINE_SECONDS = (
    JOB_TIMEOUT_SECONDS - JOB_SETUP_RESERVE_SECONDS - SHUTDOWN_RESERVE_SECONDS
)
PER_ID_FALLBACK_BUDGET_SECONDS = 100.0

# Process start, captured at import — before main() sleeps its start jitter —
# so the fan-out deadline covers the jitter instead of stacking on top of it.
# The jitter is small now, but the anchor stays: the preflight also runs before
# run() is entered, and the deadline is meant to cover everything since import.
PROCESS_STARTED = time.monotonic()


# --- jitter ---------------------------------------------------------------

def start_delay(rng: random.Random) -> float:
    """Random run-start delay to desynchronize from the exact trigger tick.

    Kept small deliberately — see START_JITTER_MAX_SECONDS for why 20 s and not
    the 240 s this was, nor 0.
    """
    return rng.uniform(0, START_JITTER_MAX_SECONDS)


def inter_request_delay(rng: random.Random) -> float:
    """Humanized gap between provider requests."""
    return rng.uniform(*INTER_REQUEST_DELAY_RANGE)


# --- dates ----------------------------------------------------------------

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


# --- poll plan ------------------------------------------------------------

# One planned unit of work: the name of the provider that must serve it, plus
# that provider's own PollKey. The cycle keys every poll-phase structure on
# this pair rather than on the PollKey alone, so routing is anchored to the
# watch's `provider` column and never to its client-writable campground_id —
# two providers naming the same campground stay separate units, each polled,
# struck and read back through its own conformer.
PollUnit = tuple[str, PollKey]


def poll_dispatch(watches: list[dict], today: date) -> dict[PollUnit, Provider]:
    """Every poll unit this cycle needs, each mapped to the conformer that must
    serve it. One entry per unique unit across ALL users: the many watches that
    share a campground and a month collapse into the single request their
    provider has to make. Each watch's units and the conformer recorded for them
    both come from `provider_for(watch)`, so a mixed-provider pool plans in one
    pass and a colliding campground_id cannot reach the wrong site; every watch
    must already be routable (see `provider_for`)."""
    dispatch: dict[PollUnit, Provider] = {}
    for watch in watches:
        provider = provider_for(watch)
        for key in provider.poll_plan(watch, today):
            dispatch[(provider.name, key)] = provider
    return dispatch


def sorted_poll_units(units) -> list[PollUnit]:
    """Deterministic pre-shuffle order across providers, whose keys need not
    share a comparable type beyond the campground_id."""
    return sorted(units, key=lambda unit: (unit[1][0], str(unit[1][1]), unit[0]))


def dedupe_poll_plan(watches: list[dict], today: date) -> list[PollKey]:
    """The cycle's deduplicated poll plan as bare PollKeys, in pre-shuffle
    order (see `poll_dispatch`, which also records who serves each one)."""
    return [key for _, key in sorted_poll_units(poll_dispatch(watches, today))]


# --- delta detection ------------------------------------------------------

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def state_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


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
    """Drop openings whose (campsite_id, date) was alerted within the cooldown.

    Keyed on `campsite_id`, never the `site` label: a provider may render the
    same campsite under a different label from one cycle to the next (a
    going_to_camp catalog fetch that fails falls back to the resourceId), and
    dedup that moved with the label would re-push an opening already sent."""
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

def rejection_reason(response, *, safe: bool) -> str:
    """The server's own account of why a request was rejected: PostgREST answers
    a bad write with a JSON body naming the column, constraint or payload at
    fault, which is the one thing an operator needs and the one thing httpx's
    exception message leaves out. Only body fields are read — never the request,
    whose headers carry the service-role key.

    `safe=True` keeps only `message` (a column or constraint name — no row
    data), for the world-readable run_summaries row. `safe=False` adds
    `details`/`hint`, which on a constraint violation echo the offending key
    values (e.g. `Key (watch_id, …)=(…)`), for the operator-only Action log.
    """
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        keys = ("message",) if safe else ("message", "details", "hint")
        parts = [str(body[key]) for key in keys if body.get(key)]
        if parts:
            return " ".join(parts)
    if safe:
        return ""  # an opaque body could hold anything; keep it out of the row
    try:
        return response.text or ""
    except Exception:
        return ""


def append_both(public: list[str], operator: list[str], line: str) -> None:
    """Append one identical line to both the world-readable and operator-only
    error channels at once, so a future edit cannot add it to one and silently
    forget the other. Only for lines whose text is the same on both channels —
    the sanitized-vs-full pairs stay written out separately on purpose."""
    public.append(line)
    operator.append(line)


def summarize_exception(exc: BaseException, *, safe: bool = False) -> str:
    """One-line, length-capped rendering of a contained failure.

    A rejected request is rendered as its status plus the server's reason rather
    than str(exc): httpx's message spends its length on the full request URL and
    a documentation link, which would push the reason past
    MAX_ERROR_MESSAGE_CHARS and leave run_summaries.errors saying only that
    *something* 400ed.

    `safe=True` is the rendering persisted to the world-readable
    run_summaries.errors: it keeps the status code and the PostgREST `message`
    (column/constraint name) but drops `details`/`hint`, which can echo row
    values. For an exception with no response to read, `safe=True` keeps only
    the type name: an arbitrary exception's message is built by whoever raised
    it and can quote the row value that upset it. `safe=False` is the fuller
    rendering for the operator-only Action annotation.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        detail = type(exc).__name__ if safe else f"{type(exc).__name__}: {exc}"
    else:
        reason = rejection_reason(response, safe=safe)
        detail = f"{type(exc).__name__}: {status} {reason}".rstrip()
    return capped_line(detail)


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


def rejects_missing_column(exc: BaseException, column: str) -> bool:
    """True when PostgREST rejected this write *because* `column` is absent from
    the live database: a permanent rejection carrying one of the missing-column
    codes and naming the column in its `message`. That is the exact signature of
    a migration nobody has applied yet, and the only write failure worth retrying
    without the column — any other rejection would fail identically the second
    time.

    Only `message` is read, never `details`/`hint`: those echo row values (see
    `summarize_exception`'s safe rendering), so a rejection about some other
    column whose echoed values happened to contain this column's name would be
    misread as drift — and latch the reason off for the rest of a cycle on a
    database where the column exists. And only the response body is read, never
    the request, whose headers carry the service-role key (the rule
    `rejection_reason` follows).
    """
    if not is_permanent_failure(exc):
        return False
    response = getattr(exc, "response", None)
    try:
        body = response.json()
    except Exception:
        return False
    if not isinstance(body, dict) or str(body.get("code")) not in WRITE_MISSING_COLUMN_CODES:
        return False
    return column in str(body.get("message") or "")


def error_reason_census(rows) -> list[tuple[str, int]]:
    """Per-reason tally of the errored watches, commonest first.

    Operator-channel only, and bucketed rather than echoed: `watches` is a table
    its owner can write, so a reason outside this build's vocabulary counts as
    `other` and a row that predates the column counts as `unrecorded`. No row
    value ever reaches either log through here.
    """
    def bucket(row) -> str:
        reason = row.get(ERROR_REASON_COLUMN)
        if not reason:  # absent column, or a row errored before 0004
            return ERROR_REASON_UNRECORDED
        return str(reason) if str(reason) in ERROR_REASONS else ERROR_REASON_OTHER

    counts = Counter(bucket(row) for row in rows)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


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
    """A failure a write pinned to exactly one watch's own row."""
    return PatchOutcome({watch_id: exc}, frozenset({watch_id}))


def unattributed_failure(watch_id: str, exc: BaseException) -> PatchOutcome:
    """A failure that surfaced while serving one watch but that no write pinned
    to its row — a table-scoped `sent_alerts` select/upsert, or an APNs client
    error. It is recorded and counted against that watch, but (absent from
    `isolated`) never moves it to status='error': a `sent_alerts` schema drift
    is not evidence that this user's watch is broken."""
    return PatchOutcome({watch_id: exc}, frozenset())


class FanoutBudget:
    """Cycle-wide cap on the per-id fallback, enforcing two limits at once.

    `allowance` is the total wall-clock every fan-out of one cycle may spend
    between them, charged by the time the per-id writes actually take, so a
    cycle whose polling finished early cannot hand the fan-out the leftover.
    Each write is charged when it *returns*, the last one of a fan-out included,
    so one fan-out cannot hand the next site time it has already spent; since no
    write starts on an exhausted allowance, the cycle overshoots by at most the
    single PATCH still in flight.
    `deadline` is an absolute monotonic instant derived from process start
    (see FANOUT_DEADLINE_SECONDS). Whichever binds first stops the fan-out,
    so the run always reaches its summary insert and pruning inside the job
    timeout — a run killed mid-fan-out is the silent outage all of this
    exists to prevent.
    """

    def __init__(self, allowance: float, deadline: float, monotonic=time.monotonic):
        self.remaining = float(allowance)
        self.deadline = deadline
        self._monotonic = monotonic

    def allowed(self) -> bool:
        return self.remaining > 0 and self._monotonic() < self.deadline

    @contextmanager
    def charging(self):
        """Charge the allowance for the write run inside, however it ends."""
        started = self._monotonic()
        try:
            yield
        finally:
            self.remaining -= self._monotonic() - started


def patch_watches(
    db,
    watch_ids,
    data: dict,
    *,
    budget: FanoutBudget | None = None,
    fanout=True,
) -> PatchOutcome:
    """PATCH `data` onto many watches, isolating the row that is actually bad.

    The healthy path is the single batched `id=in.(…)` write the write budget
    assumes. Only a *permanently* rejected batch (a PostgREST 4xx other than
    429) is retried one id at a time, so one unwritable row cannot silently
    drop everyone else's update. A transient batch failure (429, 5xx, timeout,
    transport error) is never fanned out: it marks nothing errored, so
    isolating it buys nothing, while dozens of sequential 30-second PATCHes
    against a struggling Supabase would blow the workflow timeout and kill the
    run before its summary and pruning. For the same reason a fan-out stops as
    soon as `budget` is spent, and a batch larger than PER_ID_FALLBACK_MAX is
    not fanned out at all.

    `fanout` lets a caller veto the fallback for rejections it knows are about
    the *payload* rather than any row: `False`, or a predicate on the batch
    exception so the veto can be narrowed to one signature and every other
    rejection keeps the isolating behaviour a caller may depend on
    (`write_errored` uses the predicate form — see there).

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
    if not (fanout(batch_failure) if callable(fanout) else fanout):
        return PatchOutcome({watch_id: batch_failure for watch_id in ids}, frozenset())
    if not is_permanent_failure(batch_failure) or len(ids) > PER_ID_FALLBACK_MAX:
        return PatchOutcome({watch_id: batch_failure for watch_id in ids}, frozenset())
    failures: dict[str, BaseException] = {}
    isolated: set[str] = set()
    for watch_id in ids:
        if budget is not None and not budget.allowed():
            failures[watch_id] = batch_failure
            continue
        try:
            with budget.charging() if budget is not None else nullcontext():
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
    process_started: float | None = PROCESS_STARTED,
    per_id_fallback_budget_seconds: float = PER_ID_FALLBACK_BUDGET_SECONDS,
    fanout_deadline_seconds: float = FANOUT_DEADLINE_SECONDS,
) -> dict:
    rng = rng or random.Random()
    started = monotonic()
    deadline = started + time_budget_seconds
    # The fan-out runs after the poll budget is spent, so it cannot share
    # budget_exhausted() (already true by then) and gets its own cap instead —
    # anchored by default to process start, not to run() entry, so the start
    # jitter (≤20 s) and the preflight ahead of it count against it rather than
    # being added on top of it. That default holds for every caller on the real
    # clock, wrapped or instrumented or not. A caller on a clock of its own
    # (tests) is on another timeline PROCESS_STARTED says nothing about, so it
    # passes process_started=None to anchor to this run's own `started` reading
    # instead — same clock domain, no jitter to account for — or an explicit
    # reading of its own clock.
    if process_started is None:
        process_started = started
    fanout_budget = FanoutBudget(
        per_id_fallback_budget_seconds,
        process_started + fanout_deadline_seconds,
        monotonic,
    )

    def budget_exhausted() -> bool:
        return monotonic() >= deadline

    # `errors` is copied verbatim into both renderings, so only text that is
    # already safe to publish may go in it; `detail_only` carries the
    # operator-only half of a notice whose persisted form is an aggregate.
    errors: list[str] = []
    detail_only: list[str] = []
    now = now_fn()
    today = monitor_today(now)

    # Contained failures. watch_failures holds the first (context, exception)
    # per watch, rendered into the error text only at the end so the persisted
    # row and the operator log can each get their own rendering; mark_errored is
    # the subset whose failure was pinned to that row *and* looks permanent, so
    # it should surface on the watch itself; rated_ids is the subset whose
    # failure describes this cycle's own health, so it counts toward the
    # systemic rate; blocked_ids are watches whose failure stops the cycle from
    # serving them further; cycle_failures are
    # failures attributable to no single watch, which are systemic by
    # definition, and are rendered twice like the per-watch ones — sanitized
    # for the persisted row, full for the operator log.
    watch_failures: dict[str, tuple[str, BaseException]] = {}
    mark_errored: set[str] = set()
    rated_ids: set[str] = set()
    blocked_ids: set[str] = set()
    cycle_failures: list[str] = []
    public_cycle_failures: list[str] = []
    # Watches whose push was rejected outright (PERMANENT_FAILURE) this cycle,
    # for the pool-wide APNs wipeout backstop below — tracked apart from the
    # per-watch rated flag so an unenumerated reason code still can't hide a
    # near-total delivery outage behind a green run.
    apns_rejected_ids: set[str] = set()

    def write_watches(watch_ids, data: dict, *, fanout=True) -> PatchOutcome:
        return patch_watches(db, watch_ids, data, budget=fanout_budget, fanout=fanout)

    # `error_reason` is WARN-classified in preflight.REQUIRED, which obliges the
    # cycle to keep working on a live database where 0004 has not been applied
    # yet: erroring a watch is a lifecycle step, and a monitor that could not
    # take it would turn an unapplied migration into a watch failing identically
    # every cycle forever — the very shape this column exists to make visible.
    # So the reason rides along with the status write and, if PostgREST rejects
    # that write *because the column is missing*, it is retried once without it:
    # the status lands either way and the reason is best-effort. Nothing else is
    # retried — any other rejection would fail the same way twice — and after one
    # such rejection the column is left alone for the rest of the cycle, so a
    # drifted database costs one extra write, not one per error site. Both
    # attempts happen only on paths that were already writing, so the no-change
    # write budget is untouched.
    #
    # That one-extra-write cost is literal, which is what the WARN classification
    # rests on: the reason-carrying attempt vetoes the per-id fan-out for exactly
    # this signature, so a drifted batch of N watches costs 2 writes rather than
    # N+2 and charges the cycle-wide FanoutBudget nothing — the fallback is there
    # to find the one bad *row*, and a column missing from the whole table is not
    # one. Every other rejection still fans out exactly as it always did, because
    # the 404 site depends on isolation to error a genuinely bad row.
    reason_writable = True

    def write_errored(watch_ids, reason: str, extra: dict | None = None) -> PatchOutcome:
        nonlocal reason_writable
        data = {"status": "error", **(extra or {})}
        if not reason_writable:
            return write_watches(watch_ids, data)
        outcome = write_watches(
            watch_ids,
            {**data, ERROR_REASON_COLUMN: reason},
            fanout=lambda exc: not rejects_missing_column(exc, ERROR_REASON_COLUMN),
        )
        drifted = {
            watch_id for watch_id, exc in outcome.failures.items()
            if rejects_missing_column(exc, ERROR_REASON_COLUMN)
        }
        if not drifted:
            return outcome
        reason_writable = False
        retried = write_watches(sorted(drifted), data)
        failures = {
            watch_id: exc for watch_id, exc in outcome.failures.items()
            if watch_id not in drifted
        }
        failures.update(retried.failures)
        return PatchOutcome(failures, (outcome.isolated - drifted) | retried.isolated)

    def record_failures(
        outcome: PatchOutcome, context: str, *, errorable=True, blocking=True, rated=True
    ) -> None:
        """Record one contained failure per watch. `rated=False` reports a
        failure that says nothing about this cycle's health — a per-device APNs
        rejection (a dead device token) is one user's problem, not an operational
        fault — so it is surfaced to both audiences but kept out of the rate that
        decides the run's exit status. A pool-wide provider/config APNs fault
        stays `rated=True` so a signing-key or bundle-id outage exits non-zero."""
        for watch_id, exc in outcome.failures.items():
            # One watch, one recorded failure — but every failure still gets
            # its own say on whether the watch is errored, counted or blocking,
            # so an earlier bookkeeping-only failure cannot shadow a later
            # isolated one.
            watch_failures.setdefault(watch_id, (context, exc))
            if errorable and watch_id in outcome.isolated and is_permanent_failure(exc):
                mark_errored.add(watch_id)
            if rated:
                rated_ids.add(watch_id)
            if blocking:
                blocked_ids.add(watch_id)

    def record_cycle_failure(label: str, exc: BaseException | None = None) -> None:
        """A failure belonging to no single watch. `label` alone is already an
        aggregate safe to persist; an exception is rendered for each audience."""
        if exc is None:
            append_both(public_cycle_failures, cycle_failures, label)
            return
        cycle_failures.append(capped_line(f"{label}: {summarize_exception(exc)}"))
        public_cycle_failures.append(
            capped_line(f"{label}: {summarize_exception(exc, safe=True)}")
        )

    def blocked(watch: dict) -> bool:
        return str(watch["id"]) in blocked_ids

    watches = db.select("watches", {"status": "eq.monitoring"})

    # Visibility: how many watches are sitting in status='error'. That state is
    # terminal — an errored watch is absent from the select above and no code
    # path ever puts it back — so without this census a cycle serving one watch
    # of four reads exactly like a clean cycle serving all four. It is how three
    # of five watches once went unmonitored for two days across a wall of green
    # runs. One extra select, no writes, so the no-change write budget is
    # untouched. The count is the population *entering* the cycle: watches this
    # cycle errors are reported by the lifecycle passes below and join the census
    # from the next cycle on.
    #
    # Scoped to trips that have not passed, because a warning that fires forever
    # is worth exactly as much as no warning at all: an errored watch whose dates
    # are behind us is nothing anyone can act on, and left in the count it would
    # desensitize an operator to the very signal this census exists to give. The
    # expiry pass below could instead move those rows to 'expired' — arguably the
    # more correct state — but that adds writes and mixes two concerns, so it is
    # a deliberate possible follow-up, not an oversight.
    #
    # No projection: naming error_reason in a select list would be rejected by
    # exactly the database this design must tolerate (one where 0004 has not been
    # applied), which is how preflight probes for a missing column. select=* is
    # what keeps the read `.get`-tolerant.
    try:
        errored = db.select(
            "watches", {"status": "eq.error", "end_date": f"gte.{today.isoformat()}"}
        )
    except Exception as exc:  # containment: a census must never cost a cycle
        errored = None
        errors.append("errored-watch census unavailable")
        detail_only.append(capped_line(f"errored-watch census: {summarize_exception(exc)}"))
    watches_errored = None if errored is None else len(errored)
    if errored:
        # The count is an aggregate that names nobody, so it goes on the
        # world-readable row — that is the whole point of the census, since a
        # NULL `errors` is what made the outage invisible. The per-reason
        # breakdown is operator-only: `error_reason` sits on a row its owner can
        # write, and the breakdown is an operator's signal either way.
        errors.append(f"{watches_errored} watch(es) in status='error', not monitored")
        detail_only.append(capped_line(
            "errored watches by reason: "
            + ", ".join(f"{reason}: {count}" for reason, count in error_reason_census(errored))
        ))

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

    # Provider routing. Expiry above is provider-neutral and still applies to
    # every watch; from here down the work is delegated to the conformer named
    # by the watch's `provider` column, so a watch is only ever polled by the
    # site it actually belongs to. A provider this build does not serve is not
    # an error on the watch — a newer client may write rows an older monitor
    # has no conformer for — so those watches are left untouched (still
    # 'monitoring', unpolled, outside the served set and so outside the
    # systemic rate) and reported to both audiences as a count.
    unroutable = [w for w in active if provider_name(w) not in PROVIDERS]
    if unroutable:
        errors.append(
            f"{len(unroutable)} watch(es) on a provider this build does not "
            "serve, skipped"
        )
        # Which provider is missing is what tells the operator whether the
        # conformer is simply not shipped yet, so it goes to the operator-only
        # channel — the count alone leaves the signal unactionable.
        for name in sorted({provider_name(w) for w in unroutable}):
            detail_only.append(capped_line(f"{name}: no conformer in this build, skipped"))
        active = [w for w in active if provider_name(w) in PROVIDERS]

    # Backend-owned lifecycle: watches with malformed campground ids can
    # never poll successfully, so they move to status='error' once (one
    # batched write, failure case only) instead of re-erroring every cycle.
    invalid = [w for w in active if not CAMPGROUND_ID_RE.fullmatch(str(w["campground_id"]))]
    if invalid:
        # A rejected campground_id is arbitrary user-supplied text — exactly the
        # kind of value the world-readable row must not republish — so the row
        # gets the count and the operator log gets the values.
        invalid_ids = sorted({str(w["campground_id"]) for w in invalid})
        errors.append(
            f"{len(invalid_ids)} invalid campground_id(s) on {len(invalid)} "
            "watch(es), errored and skipped"
        )
        for campground_id in invalid_ids:
            detail_only.append(capped_line(f"{campground_id!r}: invalid campground_id, skipped"))
        # errorable=False: this write *is* the status='error' write, so there
        # is nothing for the end-of-cycle marking to retry.
        record_failures(
            write_errored((w["id"] for w in invalid), ERROR_REASON_INVALID_CAMPGROUND_ID),
            "error-invalid",
            errorable=False,
            blocking=False,
        )
        invalid_watch_ids = {w["id"] for w in invalid}
        active = [w for w in active if w["id"] not in invalid_watch_ids]

    # Backend-owned lifecycle: the same treatment for a watch its own provider
    # says it can never poll — client-written provider config the conformer
    # cannot read (see providers.unpollable_reason). That fails identically every
    # cycle, so the watch moves to status='error' once instead of failing inside
    # containment forever while its user sees a watch that looks healthy. Once
    # errored it leaves the pool, so the write happens once and a steady-state
    # cycle stays inside the write budget — unless the condition is pool-wide,
    # which is the operator's to fix rather than the users' (see below).
    unpollable = [(w, reason) for w in active if (reason := unpollable_reason(w))]
    if unpollable:
        # The same guard the end-of-cycle error marking applies (A), evaluated
        # here rather than reused from there: these watches leave the pool
        # before the served set exists, so by then the whole pool being
        # unpollable reads as `is_systemic(0, 0)` — False exactly when the
        # breakage is broadest. A shared cause (a client writing the wrong key
        # name on every row it creates) is the operator's to fix, so the pool is
        # left intact and the run goes red instead of erroring every watch.
        pool_wide = is_systemic(len(unpollable), len(active))
        # The reason names only the field at fault, never what the client wrote,
        # but the world-readable row still gets the count alone — the field name
        # is what tells the operator which client is writing bad rows, and that
        # is an operator's concern.
        if pool_wide:
            record_cycle_failure(
                f"{len(unpollable)} of {len(active)} watch(es) their provider "
                "cannot poll: a shared cause, left monitoring for an operator"
            )
        else:
            errors.append(
                f"{len(unpollable)} watch(es) their provider cannot poll, errored and skipped"
            )
            # errorable=False: this write *is* the status='error' write (as above).
            record_failures(
                write_errored(
                    (w["id"] for w, _ in unpollable), ERROR_REASON_UNREADABLE_PROVIDER_REF
                ),
                "error-unpollable",
                errorable=False,
                blocking=False,
            )
        disposition = "left monitoring" if pool_wide else "errored"
        for reason in sorted({reason for _, reason in unpollable}):
            detail_only.append(capped_line(f"{reason}: watch(es) {disposition} and skipped"))
        unpollable_ids = {w["id"] for w, _ in unpollable}
        active = [w for w in active if w["id"] not in unpollable_ids]

    # Each planned unit carries the conformer `provider_for` chose for the
    # watches that asked for it, so dispatch can never be decided by the
    # client-writable campground_id. The pacing below still runs one shared,
    # shuffled queue across providers rather than a burst per site.
    dispatch = poll_dispatch(active, today)
    plan = sorted_poll_units(dispatch)
    rng.shuffle(plan)
    session_ua = rng.choice(USER_AGENTS)  # one UA per run, rotated across runs

    # A campground_id is only unique within its own provider, so the 404-strike
    # inputs are scoped per provider: one site's 404 must never strike a watch
    # on another site that happens to name the same id.
    not_found: dict[str, set[str]] = {}
    availability: dict[PollUnit, dict | None] = {}
    for i, unit in enumerate(plan):
        if budget_exhausted():
            errors.append(
                f"time budget exhausted: skipped {len(plan) - i} remaining poll(s)"
            )
            break
        name, key = unit
        try:
            availability[unit] = dispatch[unit].poll(
                http, key, session_ua,
                sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
                not_found=not_found.setdefault(name, set()),
            )
        except Exception as exc:  # containment boundary
            # A provider raising out of poll belongs to no single watch — the
            # unit is shared by everyone watching that park — and by contract
            # (Provider.poll) it means a fault no retry clears: a park whose map
            # fan-out is past the provider's safety cap, or a conformer bug. The
            # unit is recorded as failed like any other, so the watches on it
            # keep their old hash rather than reading the gap as "nothing
            # available", and the failure is recorded against the cycle so the
            # run goes red instead of leaving that park unserved on a green run.
            availability[unit] = None
            record_cycle_failure(f"poll {name}", exc)
        if i < len(plan) - 1 and not budget_exhausted():
            sleep(inter_request_delay(rng))

    # Split the results back into the per-provider {PollKey: parsed-or-None}
    # map each conformer's extract_relevant reads — a provider is only ever
    # handed the units it planned itself.
    polled: dict[str, dict[PollKey, dict | None]] = {}
    polled_ok: dict[str, set[str]] = {}
    for (name, key), parsed in availability.items():
        polled.setdefault(name, {})[key] = parsed
        if parsed is not None:
            polled_ok.setdefault(name, set()).add(key[0])

    # Backend-owned lifecycle: a syntactically valid campground id that
    # keeps 404ing (typo or delisted campground) errors its watches after
    # NOT_FOUND_ERROR_THRESHOLD consecutive cycles instead of warning
    # forever; any successful poll resets the strike count. All writes here
    # happen only in the failure/recovery cases, so a healthy no-change
    # cycle stays within the write budget.
    strike_updates: dict[int, set] = {}
    errored_404 = []
    for watch in active:
        name = provider_name(watch)
        campground_id = str(watch["campground_id"])
        strikes = int(watch.get("consecutive_not_found") or 0)
        if campground_id in polled_ok.get(name, ()):
            if strikes:
                strike_updates.setdefault(0, set()).add(watch["id"])
        elif campground_id in not_found.get(name, ()):
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
            # campground_id is user-supplied and lands in the world-readable row,
            # so it goes through capped_line like every other producer there.
            errors.append(capped_line(
                f"{campground_id}: not found for {NOT_FOUND_ERROR_THRESHOLD} "
                "consecutive cycles, watch(es) errored"
            ))
        record_failures(
            write_errored(
                (w["id"] for w in errored_404),
                ERROR_REASON_CAMPGROUND_NOT_FOUND,
                {"consecutive_not_found": NOT_FOUND_ERROR_THRESHOLD},
            ),
            "error-404",
            blocking=False,
        )
        errored_404_ids = {w["id"] for w in errored_404}
        active = [w for w in active if w["id"] not in errored_404_ids]

    # Per-watch processing is contained: anything unexpected here fails just
    # this watch. The others still alert, and the cycle still reaches its
    # bookkeeping below. Attribution is split by scope so status='error' is only
    # ever set from a failure a write pinned to *this watch's own row*: the
    # delta/hash work and the final `watches` row write are isolated (they are
    # about this row), while the `sent_alerts` select/upsert and the APNs send
    # are table- or service-scoped and recorded as unattributed — a
    # `sent_alerts` schema drift is not evidence this user's watch is broken.
    alerts_sent = 0
    for watch in active:
        if blocked(watch):
            continue  # already failed a lifecycle write that blocks serving it
        watch_id = str(watch["id"])
        try:
            provider = provider_for(watch)
            current = provider.extract_relevant(polled.get(provider.name, {}), watch, today)
            if current is None:
                continue  # poll failed for this watch's units; keep old hash
            new_hash = state_hash(current)
            if new_hash == watch.get("state_hash"):
                continue
            openings = available_sites(current)
        except Exception as exc:  # this row's own data: pin it
            record_failures(isolated_failure(watch_id, exc), "process")
            continue

        delivered = False
        delivery_failures: list[BaseException] = []
        try:
            fresh = filter_unalerted(db, watch, openings, now)
            if fresh:
                outcome = apns.send_alert(watch, fresh, db, failures=delivery_failures)
                if delivery_failures:
                    # A push that did not land is recorded against this watch
                    # like any other unattributed failure — reported, rendered
                    # once per audience, and never able to error the row. It
                    # does not block the cycle's bookkeeping for the watch: the
                    # watch was polled, only the delivery failed. A per-device
                    # rejection (a dead device token, an unbuildable push URL) is
                    # that one device's problem rather than this cycle's, so it
                    # is reported outside the systemic rate; a provider/config
                    # fault (CONFIG_FAILURE) is rated so a pool-wide APNs outage
                    # still turns the run red.
                    record_failures(
                        unattributed_failure(watch_id, delivery_failures[0]),
                        "alert",
                        blocking=False,
                        rated=outcome != PERMANENT_FAILURE,
                    )
                    if outcome == PERMANENT_FAILURE:
                        apns_rejected_ids.add(watch_id)
                if outcome in (RETRYABLE_FAILURE, CONFIG_FAILURE):
                    # keep old hash so the alert is retried next cycle — once the
                    # outage clears or the operator rotates the bad credential
                    continue
                if outcome == DELIVERED:
                    alerts_sent += len(fresh)
                    delivered = True
                    try:
                        db.upsert(
                            "sent_alerts",
                            alert_rows(watch, fresh, now),
                            on_conflict="watch_id,site_id,date",
                        )
                    except Exception as exc:  # the push landed, the dedup row did not
                        # Recorded like any other table-scoped failure, then the
                        # state_hash write below still runs: with no dedup row
                        # it is the only thing standing between a `sent_alerts`
                        # drift and the identical push going out to this user
                        # every cycle until the drift is fixed.
                        record_failures(
                            unattributed_failure(watch_id, exc), "alert", blocking=False
                        )
        except Exception as exc:  # sent_alerts / APNs: not this watch's row
            # Table-scoped, like the delivery failure above: the watch was
            # polled, so it is still stamped last_checked_at rather than left
            # looking unchecked.
            record_failures(unattributed_failure(watch_id, exc), "alert", blocking=False)
            continue

        try:  # the one write pinned to this watch's own row
            db.patch(
                "watches",
                {"id": f"eq.{watch_id}"},
                {"state_hash": new_hash, **({"last_found_at": iso_now(now)} if delivered else {})},
            )
        except Exception as exc:  # containment boundary
            record_failures(isolated_failure(watch_id, exc), "process")

    # The served set: watches this cycle actually tried to serve — still active
    # after the lifecycle passes (not expired this cycle, not errored for an
    # invalid or persistently-404ing campground), with something pollable this
    # cycle, and not left unpolled by the time budget. It is both the
    # denominator and the scope of the numerator of the systemic rate, so a
    # cycle that failed every watch it served goes red however many watches left
    # the pool — or never entered it — for unrelated reasons.
    #
    # Bookkeeping covers exactly the served watches that were not blocked:
    # skipped watches keep their old last_checked_at and the summary reports
    # what was actually polled rather than what was planned.
    attempted = set(availability)
    served, checked = [], []
    for w in active:
        provider = provider_for(w)
        needed = provider.poll_plan(w, today)
        if not needed:
            continue  # wholly beyond the poll horizon: nothing to serve yet
        if not all((provider.name, key) in attempted for key in needed):
            continue  # never reached: the poll time budget ran out first
        served.append(w)
        if not blocked(w):
            checked.append(w)
    served_ids = {str(w["id"]) for w in served}
    if checked:
        outcome = write_watches((w["id"] for w in checked), {"last_checked_at": iso_now(now)})
        record_failures(outcome, "last-checked")
        checked = [w for w in checked if str(w["id"]) not in outcome.failures]

    # Threshold gate (B): the watch-error rate over the served set, counting
    # only the failures that describe the cycle's own health (see `rated`).
    considered = len(served)
    failed_served = sum(1 for watch_id in rated_ids if watch_id in served_ids)
    # Pool-wide APNs wipeout backstop (C): a distinct systemic condition, kept
    # separate from the per-watch rated flag. Even a rejection reason we did not
    # enumerate as a config fault cannot yield a silent green outage — when
    # outright rejections wipe out nearly every served push, the run goes red
    # regardless of per-reason rating. Scoped to the served set like the rate
    # above, with a floor so a handful of dead device tokens stays green.
    apns_rejected_served = sum(1 for watch_id in apns_rejected_ids if watch_id in served_ids)
    apns_wipeout = (
        apns_rejected_served >= APNS_WIPEOUT_FLOOR
        and apns_rejected_served > considered * APNS_WIPEOUT_RATE
    )
    systemic = is_systemic(failed_served, considered) or apns_wipeout

    # Isolated, permanent failures surface on the watch itself (A) so the user
    # sees a broken watch instead of one that silently stops updating. Systemic
    # breakage is the operator's to fix, so the pool is left intact rather than
    # erroring every watch at once.
    error_mark_failures: dict[str, BaseException] = {}
    if mark_errored and not systemic:
        error_mark_failures = write_errored(mark_errored, ERROR_REASON_WRITE_REJECTED).failures
        if error_mark_failures:
            # Belongs to no single watch — a DB-level problem that would
            # otherwise leave every affected watch quietly 'monitoring' — so it
            # counts as systemic. The aggregate count names nobody, so both
            # audiences get this one string; only the per-watch detail below is
            # operator-only.
            record_cycle_failure(
                f"error-mark: {len(error_mark_failures)} of {len(mark_errored)} watch(es) "
                "could not be moved to status='error'"
            )

    # Contain the retention pruning *before* the summary row is written, so a
    # prune failure is already known when the row's verdict label is chosen —
    # the persisted "(isolated)"/"(systemic)" tag then always matches the exit
    # code. (A summary INSERT that itself fails is the one unrepresentable case,
    # since there is then no row to mislabel.)
    def contained(label: str, write, *args) -> None:
        try:
            write(*args)
        except Exception as exc:  # containment boundary
            record_cycle_failure(label, exc)

    retention_cutoff = iso_now(now - timedelta(days=RETENTION_DAYS))
    contained("sent_alerts prune", db.delete, "sent_alerts", {"sent_at": f"lt.{retention_cutoff}"})
    contained("run_summaries prune", db.delete, "run_summaries", {"ran_at": f"lt.{retention_cutoff}"})

    # The persisted verdict must match the exit code, which is systemic OR any
    # failure belonging to no watch, so fold cycle_failures in before labelling.
    systemic_run = systemic or bool(cycle_failures)

    # Two renderings of the same failures. `public_errors` is persisted to
    # run_summaries.errors, which RLS makes world-readable, so watch UUIDs
    # become per-run ordinals and PostgREST details/hint are dropped (keeping
    # the status code and the column/constraint name). `detail_errors` keeps the
    # UUIDs and full reason for the operator-only GitHub Actions annotation.
    public_errors = list(errors)
    detail_errors = errors + detail_only
    # The verdict label is emitted whenever the run has a verdict to state, so a
    # red run whose only failure belonged to no watch still persists its label
    # and its reason instead of a NULL that reads like a clean cycle.
    if watch_failures or systemic_run:
        tally = (
            f"{failed_served} of {considered} served watch(es) failed this cycle "
            f"({'systemic' if systemic_run else 'isolated'})"
        )
        unserved = sum(1 for watch_id in watch_failures if watch_id not in served_ids)
        if unserved:
            tally += f", plus {unserved} on watch(es) this cycle did not serve"
        unrated = sum(
            1 for watch_id in watch_failures
            if watch_id in served_ids and watch_id not in rated_ids
        )
        if unrated:
            tally += f", plus {unrated} outside the rate"
        append_both(public_errors, detail_errors, tally)
        for ordinal, watch_id in enumerate(sorted(watch_failures)[:MAX_LOGGED_WATCH_ERRORS], start=1):
            context, exc = watch_failures[watch_id]
            public_errors.append(
                capped_line(f"watch #{ordinal}: {context}: {summarize_exception(exc, safe=True)}")
            )
            detail_errors.append(
                capped_line(f"watch {watch_id}: {context}: {summarize_exception(exc)}")
            )
        undisplayed = len(watch_failures) - MAX_LOGGED_WATCH_ERRORS
        if undisplayed > 0:
            append_both(public_errors, detail_errors, f"and {undisplayed} more watch error(s)")
    for watch_id, exc in sorted(error_mark_failures.items()):
        detail_errors.append(capped_line(f"watch {watch_id}: error-mark: {summarize_exception(exc)}"))
    # Cycle failures reach the persisted row sanitized. Their full rendering
    # travels in `cycle_errors` alone, which the operator annotation joins on —
    # putting it in `detail_errors` too would print every one of them twice.
    public_errors.extend(public_cycle_failures)

    summary = {
        "watches_checked": len(checked),
        "campgrounds_polled": len({key[0] for _, key in availability}),
        "alerts_sent": alerts_sent,
        "duration_ms": int((monotonic() - started) * 1000),
        "errors": "; ".join(public_errors) or None,
    }
    # Written last, so both prunes' outcomes are already in the verdict above.
    contained("run_summaries insert", db.insert, "run_summaries", summary)

    return {
        **summary,
        "watches_considered": considered,
        "watch_errors": len(watch_failures),
        # The errored population this cycle inherited (None if the census
        # select itself failed). Not a column of run_summaries — the persisted
        # signal is the count line the census put on `errors`, which is what
        # turns a silent green run into one that says three watches are dead.
        "watches_errored": watches_errored,
        # Full-detail rendering (watch UUIDs + PostgREST details) for the
        # operator-only Action annotation; never persisted to run_summaries.
        "errors_detail": "; ".join(detail_errors) or None,
        # Failures with no watch to blame. Not persisted in the run_summaries
        # row: some occur as/after it is written.
        "cycle_errors": "; ".join(cycle_failures) or None,
        "systemic_failure": systemic or bool(cycle_failures),
    }


def error_annotation(result: dict) -> str | None:
    """GitHub Actions warning annotation when the cycle recorded errors,
    surfacing them in the run history. On its own it does not fail the run:
    isolated errors keep the schedule green (see failure_annotation). It uses
    the full-detail rendering (`errors_detail`) — the Action log is
    operator-only, so it carries the watch UUIDs and PostgREST details the
    world-readable run_summaries row deliberately omits."""
    detail = result.get("errors_detail") or result.get("errors")
    if detail:
        return f"::warning::monitor completed with errors: {detail}"
    return None


def failure_annotation(result: dict) -> str | None:
    """GitHub Actions error annotation for a run that exits non-zero: the
    failures crossed the systemic threshold, so this cycle served few or no
    watches and needs an operator. Full-detail, operator-only (see
    error_annotation)."""
    if not result.get("systemic_failure"):
        return None
    detail = "; ".join(
        part for part in (
            result.get("errors_detail") or result.get("errors"),
            result.get("cycle_errors"),
        ) if part
    )
    return f"::error::monitor cycle failed systemically: {detail or 'see run_summaries'}"


def exit_code(result: dict) -> int:
    """0 for a cycle whose failures were isolated (healthy watches were still
    served), 1 when they were systemic and the run must go red."""
    return 1 if result.get("systemic_failure") else 0


def main() -> None:
    rng = random.Random()
    db = SupabaseClient.from_env()
    # Schema-drift guard, deliberately ahead of the jitter sleep: a run halted by
    # drift goes red before sleeping at all. That mattered far more when the
    # jitter was 240 s of billed Actions minutes; at 20 s the ordering is kept
    # because it is still free — the probe is Supabase-only, and the jitter
    # exists to desynchronize *provider* polling, so nothing is owed to a
    # campground host by a run that never reaches one. Halting drift raises
    # SystemExit(1) here, before run()'s first write; everything else warns and
    # falls through.
    preflight(db)

    delay = start_delay(rng)
    print(f"start jitter: sleeping {delay:.0f}s", flush=True)
    time.sleep(delay)

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
