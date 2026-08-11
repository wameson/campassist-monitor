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

import argparse
import hashlib
import json
import os
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
# The batched `id=in.(…)` PATCH URL grows one UUID (~39 URL-encoded bytes) per
# served watch, so a single unchunked write crosses common gateway URI limits
# (nginx's 8 KB default ≈ 200 ids) well below 100 users — turning every run red
# and freezing last_checked_at. Splitting the ids into fixed-size chunks bounds
# each URL: 150 ids ≈ 5.9 KB, comfortably under 8 KB, while staying above the
# 100-watch no-change budget test so that cycle is still a single request. Each
# chunk keeps the same isolate-the-bad-row fan-out semantics; the shared
# FanoutBudget is charged across all of them (see patch_watches).
PATCH_ID_CHUNK_MAX = 150
# Rows per page for the cycle's `watches` reads. PostgREST's `max-rows` (a
# Supabase server setting this repo does not control) silently truncates a
# single unbounded GET to the first max-rows rows — every watch past that count
# would go unpolled behind a green run. paginated_select walks id=gt.<last>
# until an empty page, which is correct for any max-rows value, including one
# below this page size.
SELECT_PAGE_SIZE = 1000
# The columns the cycle actually reads off a monitoring watch — every field
# run(), apns.send_alert, or any provider touches. Projecting the monitoring
# read to these trims per-row egress (the dominant Supabase egress term at
# scale) vs select=*. WARN columns an unapplied migration may lack are included,
# so on a *drifted* database the projected read is rejected (42703) and
# read_monitoring_watches falls back to the tolerant select=* read — preserving
# the `.get()`-tolerance the WARN classification in preflight.REQUIRED rests on.
# NEVER narrow this below what the code reads: an omitted-but-present column
# raises no error, only silently wrong behaviour (`.get()` sees its default).
# error_reason is deliberately absent (a monitoring watch never carries one; the
# errored-watch census reads it and keeps its own select=* for that reason), as
# are the write-only/app-only columns campground_state, created_at,
# last_checked_at, last_found_at, flex_max_nights.
WATCH_READ_COLUMNS = (
    "id",
    "user_id",
    "provider",
    "provider_ref",
    "campground_id",
    "campground_name",
    "site_ids",
    "include_ada_only",
    "start_date",
    "end_date",
    "date_mode",
    "flex_min_nights",
    "consecutive_not_found",
    "state_hash",
    "status",
)
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
# against that same 600 s and has ample headroom: it is 6 GETs, one per table,
# each bounded by the client's 30 s timeout (db.py) — at most 120 s of the 600.
# That ceiling holds on a broken Supabase too: a probe failure does not end the
# pass (a blip on one table must not hide drift on another), so the worst case
# stays those same 6 probes, on a run where the cycle would achieve nothing
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
# Since the jitter came down to 20 s, the slowest *poll phase* reaches the
# fan-out with room to spare rather than already past the deadline: 20 s of
# jitter plus a fully spent CYCLE_TIME_BUDGET_SECONDS (480 s) is 500 s of the
# 600 s, leaving ~100 s of per-row isolation where 240 s of jitter used to
# arrive with none. That covers the jitter and the poll phase only — the
# preflight above is charged against the same 600 s, so a cycle slow enough
# there can still arrive past the deadline and get no fan-out at all, the trade
# README.md and PLAN.md hedge the same way. The deadline still binds, and still
# yields to the summary row and the pruning when it does; it just no longer
# binds on a healthy run. test_fanout_deadline_fits_inside_the_job_timeout
# holds the jitter-plus-poll term of that arithmetic, not the whole worst case.
# These are the whole-fleet run's profile and the default provider profile (the
# 15-minute job). going_to_camp's per-provider job uses a longer timeout and a
# larger derived fanout deadline — see POLL_PROFILES below, which reuses the same
# JOB_SETUP_RESERVE_SECONDS/SHUTDOWN_RESERVE_SECONDS reserves.
JOB_TIMEOUT_SECONDS = 900.0
JOB_SETUP_RESERVE_SECONDS = 120.0
SHUTDOWN_RESERVE_SECONDS = 180.0
FANOUT_DEADLINE_SECONDS = (
    JOB_TIMEOUT_SECONDS - JOB_SETUP_RESERVE_SECONDS - SHUTDOWN_RESERVE_SECONDS
)
PER_ID_FALLBACK_BUDGET_SECONDS = 100.0

# --- per-provider poll profiles -------------------------------------------
#
# CYCLE_TIME_BUDGET_SECONDS above is the whole-fleet budget the monolithic
# `python scripts/monitor.py` still uses. The workflow instead runs one poll job
# per provider (.github/workflows/monitor.yml), so each provider polls in its own
# runner with its own budget below and their ~4x spread in per-unit cost —
# recreation.gov ~2.5-3 s per campground-month, going_to_camp ~11 s per park,
# use_direct ~one POST per facility — no longer competes for a single budget. That
# is the reliability fix: a going_to_camp-heavy fleet can no longer consume the
# shared budget and starve recreation.gov of poll time in the same run, because
# every provider now gets the full budget below to itself instead of the three
# sharing one.
#
# Each profile pins a job timeout and a poll budget, and the SAME arithmetic that
# ties CYCLE_TIME_BUDGET_SECONDS to JOB_TIMEOUT_SECONDS above must still close for
# each job:
#     JOB_SETUP_RESERVE + fanout_deadline + SHUTDOWN_RESERVE <= job_timeout
#     START_JITTER_MAX  + time_budget                        <= fanout_deadline
# test_provider_budget_arithmetic_closes_for_each_job holds both, per provider.
# going_to_camp — the costliest per unit, and the provider a fleet leans on — gets
# a longer 20-minute job and a 720 s budget (~65 parks/cycle) so its larger real
# cost is not capped at the ~43 parks a 480 s budget buys; the cheap providers
# keep the standard 15-minute job, whose 480 s already over-serves their fleet.
class PollProfile(NamedTuple):
    job_timeout_seconds: float
    time_budget_seconds: float

    @property
    def fanout_deadline_seconds(self) -> float:
        return self.job_timeout_seconds - JOB_SETUP_RESERVE_SECONDS - SHUTDOWN_RESERVE_SECONDS


DEFAULT_POLL_PROFILE = PollProfile(JOB_TIMEOUT_SECONDS, CYCLE_TIME_BUDGET_SECONDS)
# Keyed by `watches.provider`. A test holds the keys to exactly the registered
# providers, so a new conformer without a profile fails loudly rather than
# silently polling on the default budget.
POLL_PROFILES: dict[str, PollProfile] = {
    "recreation_gov": DEFAULT_POLL_PROFILE,        # 15-minute job / 480 s budget
    "use_direct":     DEFAULT_POLL_PROFILE,        # 15-minute job / 480 s budget
    "going_to_camp":  PollProfile(1200.0, 720.0),  # 20-minute job / 720 s budget
}


def poll_profile(provider: str) -> PollProfile:
    """The poll profile for a provider, defaulting to the 15-minute profile so an
    unmapped provider still polls rather than crashing the job."""
    return POLL_PROFILES.get(provider, DEFAULT_POLL_PROFILE)


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


# --- flexible-date match (Phase 16) ---------------------------------------
#
# Provider-agnostic, and layered on top of the shared availability shape after
# extract_relevant, so both providers get it with no per-provider code. A
# flexible watch — watches.date_mode = 'flexible' — asks for "any N-night window
# inside [start_date, end_date)" rather than one fixed stay. The DATE bounds are
# REUSED: start_date is the earliest check-in, end_date the latest check-out, so
# poll_plan and extract_relevant already produce every candidate night. All this
# layer does is drop open nights that are not part of a fully-open consecutive
# run long enough to hold a window, so the watch alerts only when a stay fits.
#
# flex_min_nights is the gate; flex_max_nights does not narrow the alert set (a
# run of R >= min nights already holds a min-length window covering every one of
# its nights, and a longer allowed window can only add more). See PLAN.md
# "Phase 16".

def flex_min_nights(watch: dict) -> int | None:
    """The shortest qualifying run (in nights) for a FLEXIBLE watch, or None for
    a fixed watch — the degenerate single-window case the rest of the cycle
    already handles byte-identically.

    Read `.get`-tolerantly so a build (or a live row) that predates the Phase 16
    columns reads as fixed: `date_mode` absent → 'fixed' → None. A flexible watch
    whose `flex_min_nights` is unset falls back to 1 (any open night in the range
    qualifies) — the permissive default never over-suppresses an opening."""
    if str(watch.get("date_mode") or "fixed") != "flexible":
        return None
    raw = watch.get("flex_min_nights")
    try:
        nights = int(raw)
    except (TypeError, ValueError):
        return 1
    return nights if nights >= 1 else 1


def qualifying_nights(days: list, min_nights: int) -> set:
    """The dates that belong to a maximal run of >= `min_nights` consecutive
    calendar days. A run shorter than `min_nights` holds no window, so none of
    its nights qualify; a run at least that long holds a `min_nights`-length
    window covering every night in it, so all of them do."""
    ordered = sorted(set(days))
    keep: set = set()
    if not ordered:
        return keep
    run = [ordered[0]]
    for d in ordered[1:]:
        if (d - run[-1]).days == 1:
            run.append(d)
        else:
            if len(run) >= min_nights:
                keep.update(run)
            run = [d]
    if len(run) >= min_nights:
        keep.update(run)
    return keep


def apply_flex_window(current: dict[str, dict], watch: dict) -> dict[str, dict]:
    """Reduce a flexible watch's open-site state to only the nights that sit
    inside a fully-open qualifying window, so a lone open night in a range never
    fires a watch that wants a multi-night stay.

    A FIXED watch (the common case) is returned **unchanged, same object** — the
    hash, openings, alert and dedup downstream are byte-identical to before
    Phase 16. So is a flexible watch with a one-night floor, and a flexible watch
    every one of whose runs already qualifies (content-identical). This adds no
    Supabase writes: it runs inside the per-watch containment in `run`, purely on
    the in-memory shape, before the state_hash delta check — so an unchanged
    qualifying window hashes the same and does not re-alert, and a newly-formed
    one changes the hash and fires exactly once."""
    min_nights = flex_min_nights(watch)
    if min_nights is None or min_nights <= 1:
        return current
    reduced: dict[str, dict] = {}
    for site_id, cs in current.items():
        keep = qualifying_nights([as_date(d) for d in cs["dates"]], min_nights)
        if not keep:
            continue
        reduced[site_id] = {
            "campsite_id": cs["campsite_id"],
            "site": cs["site"],
            "dates": [d for d in cs["dates"] if as_date(d) in keep],
        }
    return reduced


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


def history_row(watch: dict, openings: list[dict], now: datetime) -> dict:
    """The app's read-facing record of one DELIVERED push (alert_history).

    One row per delivered alert, mirroring the push payload the app parses
    (apns.send_alert): the campground name, the watch's requested window, and the
    opening count this push announced. `start_date`/`end_date` are the watch's
    stored DATE strings verbatim — the same values apns.py interpolates into the
    body — so no timezone shift is introduced. `site_count` is `len(openings)`,
    the fresh openings this push was for, matching the body's "N site(s) open"."""
    return {
        "watch_id": watch["id"],
        "campground_name": watch["campground_name"],
        "start_date": watch["start_date"],
        "end_date": watch["end_date"],
        "site_count": len(openings),
        "delivered_at": iso_now(now),
    }


# --- failure containment --------------------------------------------------

def _response_body_dict(response) -> dict:
    """A rejection's JSON body as a dict, or {} when it is missing, unparseable
    or not an object. Only the response body is ever read — never the request,
    whose headers carry the service-role key — which is the rule every reader of
    a PostgREST rejection here shares."""
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


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
    body = _response_body_dict(response)
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
    body = _response_body_dict(getattr(exc, "response", None))
    if str(body.get("code")) not in WRITE_MISSING_COLUMN_CODES:
        return False
    return column in str(body.get("message") or "")


def is_missing_column_rejection(exc: BaseException) -> bool:
    """True when PostgREST rejected a request because a named column is absent
    from the live database — an unapplied migration — whatever the column.

    Unlike `rejects_missing_column`, the caller does not know which column: a
    projected read names several at once, so only the code is checked, never the
    `message` (which would echo one arbitrary column name). Same permanent-4xx +
    missing-column-code signature otherwise. Used by `read_monitoring_watches`
    to fall back from a column-scoped read to the tolerant select=* read on a
    drifted database.
    """
    if not is_permanent_failure(exc):
        return False
    body = _response_body_dict(getattr(exc, "response", None))
    return str(body.get("code")) in WRITE_MISSING_COLUMN_CODES


def paginated_select(db, table: str, params: dict, *, page_size: int = SELECT_PAGE_SIZE) -> list[dict]:
    """Read every matching row, keyset-paginated by `id` ascending.

    A single unbounded GET is silently truncated to PostgREST's `max-rows` cap
    (a Supabase server setting this repo does not control), so a fleet past that
    count would go unpolled behind a green run — a silent skip that is worse
    than a slow one. Walking `id=gt.<last>` until an empty page returns is
    correct for *any* max-rows value, including one smaller than `page_size`;
    the cost is one trailing empty GET. Reads only, so the write budget is
    untouched.
    """
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        page_params = {**params, "order": "id.asc", "limit": str(page_size)}
        if cursor is not None:
            page_params["id"] = f"gt.{cursor}"
        page = db.select(table, page_params)
        if not page:
            break
        rows.extend(page)
        cursor = str(page[-1]["id"])
    return rows


def offset_paginated_select(
    db, table: str, params: dict, *, order: str, page_size: int = SELECT_PAGE_SIZE
) -> list[dict]:
    """Read every matching row by OFFSET pagination, for a relation with no unique
    column to keyset on (the monitoring_plan view is `SELECT DISTINCT` over five
    columns). The offset advances by the number of rows a page ACTUALLY returned,
    not by `page_size`, so — exactly like the keyset reader — it is correct for
    any PostgREST max-rows cap, including one below `page_size`: it stops only on
    an empty page, never on a short one. `order` is a deterministic sort so the
    pages tile the set. Reads only, so the write budget is untouched."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = db.select(
            table, {**params, "order": order, "limit": str(page_size), "offset": str(offset)}
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
    return rows


def read_monitoring_watches(
    db, provider: str | None = None, extra: dict | None = None
) -> list[dict]:
    """The cycle's monitoring set: column-scoped and paginated, optionally
    scoped to one provider so a per-provider poll job reads only its own slice.

    Scoped to WATCH_READ_COLUMNS to trim per-row egress vs select=* (the
    dominant Supabase egress term at scale). On a live database missing a WARN
    column the projection names (an unapplied migration), PostgREST rejects the
    whole read with 42703; we fall back to the tolerant select=* read — the
    `.get()`-tolerant read the WARN classification in preflight.REQUIRED depends
    on — for the rest of this cycle. Any other read failure propagates as it did
    before, aborting the cycle: without the monitoring set there is nothing to
    poll. Both the projected and the fallback read are paginated so a max-rows
    cap cannot silently drop watches.

    The `provider` scope is applied server-side on the healthy path. But
    `watches.provider` is itself a WARN column: on a database where 0002 has not
    been applied the column is absent, which makes an `eq.<provider>` filter on
    it unusable too. So the drift fallback drops the filter and re-derives the
    scope client-side through `provider_name` — which `.get()`-defaults an absent
    column to recreation_gov — preserving the exact WARN tolerance the projected
    read already had. On such a database every watch reads as recreation_gov, so
    the recreation_gov job serves them all and the other providers' jobs serve
    none, which is correct: a pre-0002 database has only recreation.gov watches.

    `extra` adds filter params to narrow the read below the whole monitoring set
    (the egress read-reduction's scoped process-set reads pass one, e.g.
    `{"campground_id": "in.(…)"}` or `{"updated_at": "gte.…"}`); the provider
    scope, drift fallback and pagination hold identically for a narrowed read.
    """
    base = {"status": "eq.monitoring", **(extra or {})}
    scoped = {**base, "provider": f"eq.{provider}"} if provider is not None else base
    try:
        return paginated_select(db, "watches", {**scoped, "select": ",".join(WATCH_READ_COLUMNS)})
    except Exception as exc:  # containment: retry drift-tolerantly, re-raise the rest
        if is_missing_column_rejection(exc):
            rows = paginated_select(db, "watches", base)
            if provider is not None:
                rows = [w for w in rows if provider_name(w) == provider]
            return rows
        raise


# --- egress read-reduction (plan view + per-unit hashes + watermark) --------
#
# The dominant Supabase egress term is the per-cycle `watches` read — every
# monitoring row, every cycle, growing linearly with the fleet while the deduped
# poll plan grows only sublinearly. The reduction keeps polling every unit (a
# quiet unit is still polled, so an opening on it is still noticed) but reads
# back WATCH rows only where it must: on a campground whose raw availability
# changed this cycle, plus a few small bounded sets. Three server-side objects
# make that possible, each with a runtime fallback to the full read so the cycle
# is correct BEFORE its migration is applied (see the docstrings and preflight):
#   * monitoring_plan (view, 0011) — the deduped poll-planning inputs.
#   * poll_units (table, 0010)      — per-unit last-seen raw-availability hash.
#   * watches.updated_at (0009)     — the "edited since last cycle" watermark,
#                                     closing the new/edited/unpaused-watch hole
#                                     (an unchanged UNIT does not imply an
#                                     unchanged WATCH).

MONITORING_PLAN_VIEW = "monitoring_plan"
POLL_UNITS_TABLE = "poll_units"
# The columns the view exposes (== what the providers' poll_plan reads). A synthetic
# "watch" built from these routes and plans exactly like the real row.
PLAN_COLUMNS = ("provider", "campground_id", "provider_ref", "start_date", "end_date")
# Watermark safety margin: the process read pulls watches edited since
# (last cycle's ran_at − this). It must exceed one cycle's wall time (so an edit
# racing the previous cycle's own read is still caught next cycle) and one cycle
# interval (so a single skipped trigger does not drop an edit). One hour clears
# both comfortably; over-reading an hour of rare user edits is safe, missing one
# is the exact missed-opening bug this margin guards.
WATERMARK_MARGIN_SECONDS = 3600
# Above this many distinct campgrounds needing a row read, the scoped
# `campground_id=in.(…)` read is abandoned for a single full monitoring read:
# the id list would otherwise grow past a gateway URI limit (the same cliff
# PATCH_ID_CHUNK_MAX guards on the write side), and near-fleet-wide churn is
# cheaper to serve as one read than as many chunked ones. The first cycle after
# this feature ships takes this path (every unit is unseen, so every campground
# is "changed") and then quiet cycles fall back to the tiny scoped reads.
CAMPGROUND_IN_MAX = 150
# poll_units keys are read back chunked so their `unit_key=in.(…)` URL cannot
# outgrow a gateway limit either. A unit_key is a fixed 64-char hex digest, so a
# chunk of 100 is ~6.5 KB, well under 8 KB.
POLL_UNIT_READ_CHUNK = 100

# Codes that positively identify a missing schema object — a column (42703 /
# PostgREST's write-body PGRST204) or a whole relation (42P01 / PGRST205). The
# read-reduction treats any of these as "this server-side object is not applied
# yet" and falls back to the full monitoring read, exactly the WARN-tolerance
# preflight.REQUIRED records for these objects.
_MISSING_SCHEMA_CODES = frozenset(WRITE_MISSING_COLUMN_CODES | {"42P01", "PGRST205"})


def is_missing_schema_object(exc: BaseException) -> bool:
    """True when PostgREST rejected a request because a named column OR a whole
    relation is absent from the live database — an unapplied migration. Broader
    than `is_missing_column_rejection` (which is column-only): the plan view and
    poll_units table are whole relations, so their absence surfaces as a
    missing-relation code. Only the response body is read, never the request
    (whose headers carry the service-role key)."""
    if not is_permanent_failure(exc):
        return False
    body = _response_body_dict(getattr(exc, "response", None))
    return str(body.get("code")) in _MISSING_SCHEMA_CODES


def _jsonable(obj):
    """Normalize a PollKey's parts to JSON-safe primitives so a unit gets one
    canonical string form: dates → ISO, tuples → lists, recursively."""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def unit_key(unit: PollUnit) -> str:
    """A stable, URL-safe, collision-free id for one (provider, PollKey) poll
    unit — the poll_units primary key. A SHA-256 hex digest of the unit's
    canonical form: fixed length and free of the commas/brackets a JSON key would
    carry (which would break an `in.(…)` filter), while still one-to-one with the
    unit. The subkey may hold dates or nested tuples, so it is `_jsonable`-d
    first."""
    provider, key = unit
    return state_hash([provider, _jsonable(key)])


def read_watermark(db, now: datetime) -> str | None:
    """ISO lower bound for 'a user edited this watch since the last cycle': the
    most recent run_summaries.ran_at minus WATERMARK_MARGIN_SECONDS, or None when
    there is no prior run (a fresh database — then the process read cannot scope
    to edits and reads the full monitoring set). One row read, no writes."""
    rows = db.select(
        "run_summaries", {"select": "ran_at", "order": "ran_at.desc", "limit": "1"}
    )
    if not rows or not rows[0].get("ran_at"):
        return None
    return iso_now(parse_timestamp(rows[0]["ran_at"]) - timedelta(seconds=WATERMARK_MARGIN_SECONDS))


def read_plan_rows(db, provider: str | None = None) -> list[dict]:
    """The deduped poll-planning inputs from the monitoring_plan view — one row
    per distinct (provider, campground_id, provider_ref, start_date, end_date)
    across every monitoring watch, optionally scoped to one provider so a
    per-provider poll job plans only its own slice. Offset-paginated (a view has
    no unique id to keyset on) advancing by the rows actually returned, so a
    PostgREST max-rows cap cannot silently truncate the plan and leave units
    unpolled. Raises the underlying rejection when the view is absent; run()
    treats that as 'plan view unavailable' and falls back to planning from the
    full watches read. The view always carries `provider` (0011 selects it), so
    the scope is safe server-side; the view's own absence is the only drift here,
    and it falls back to the drift-tolerant scoped watches read."""
    params = {"provider": f"eq.{provider}"} if provider is not None else {}
    return offset_paginated_select(db, MONITORING_PLAN_VIEW, params, order="campground_id.asc")


def read_poll_unit_hashes(db, unit_keys) -> dict[str, str]:
    """The stored last-seen raw-availability hash for each of `unit_keys`, read
    from poll_units in fixed-size chunks so the `unit_key=in.(…)` URL stays
    bounded — and scoped to only the units this cycle polls, so the read's egress
    tracks the (sublinear) plan size, never the table's slow accumulation of
    aged-out units. Raises when the table is absent; run() then treats every unit
    as changed (the full-read fallback)."""
    keys = sorted({str(k) for k in unit_keys})
    hashes: dict[str, str] = {}
    for i in range(0, len(keys), POLL_UNIT_READ_CHUNK):
        chunk = keys[i : i + POLL_UNIT_READ_CHUNK]
        rows = db.select(
            POLL_UNITS_TABLE,
            {"unit_key": f"in.({','.join(chunk)})", "select": "unit_key,raw_hash"},
        )
        for row in rows:
            hashes[str(row["unit_key"])] = str(row.get("raw_hash"))
    return hashes


def read_process_set(
    db, today: date, watermark: str | None, campgrounds: set[str],
    provider: str | None = None,
) -> list[dict]:
    """The watch rows the cycle must actually read and process, as a union of the
    small bounded sets that can change a camper's outcome even when most of the
    fleet cannot:
      * watches on a campground that CHANGED (or 404ed, or is invalid/unpollable)
        this cycle — the availability-driven set;
      * watches EDITED since the last cycle (`updated_at >= watermark`) — the
        new/created, edited, and paused->monitoring rows an unchanged unit does
        not cover (the correctness hole updated_at exists to close);
      * watches carrying a 404 strike (for reset when their campground recovers);
      * watches whose stay has ended (for expiry).
    Deduplicated by id. Every read is column-scoped and paginated like the full
    read, and the whole thing degrades safely: no watermark yet (fresh DB), too
    many changed campgrounds to scope by id, or ANY missing-schema rejection
    (e.g. updated_at not applied) all fall back to `read_monitoring_watches` —
    the full, drift-tolerant monitoring read — so the cycle is never LESS correct
    than reading everything, only cheaper when it safely can be."""
    if watermark is None or len(campgrounds) > CAMPGROUND_IN_MAX:
        return read_monitoring_watches(db, provider)
    try:
        by_id: dict[str, dict] = {}
        def add(rows):
            for row in rows:
                by_id[str(row["id"])] = row
        if campgrounds:
            add(read_monitoring_watches(
                db, provider, {"campground_id": f"in.({','.join(sorted(campgrounds))})"}
            ))
        add(read_monitoring_watches(db, provider, {"updated_at": f"gte.{watermark}"}))
        add(read_monitoring_watches(db, provider, {"consecutive_not_found": "gt.0"}))
        add(read_monitoring_watches(db, provider, {"end_date": f"lt.{today.isoformat()}"}))
        return list(by_id.values())
    except Exception as exc:  # any unapplied object -> the full read is always correct
        if is_missing_schema_object(exc):
            return read_monitoring_watches(db, provider)
        raise


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
    chunk_size: int = PATCH_ID_CHUNK_MAX,
) -> PatchOutcome:
    """PATCH `data` onto many watches, isolating the row that is actually bad.

    The ids are split into fixed-size chunks (`chunk_size`) so the `id=in.(…)`
    URL of each write stays well under common gateway URI limits: an unchunked
    write of the whole served set grows past those limits below 100 users,
    turning every run red and freezing last_checked_at (see PATCH_ID_CHUNK_MAX).
    A pool small enough to fit one chunk is still a single request, so the
    no-change write budget is unchanged at the scales it covers.

    Within each chunk the healthy path is one batched `id=in.(…)` write. Only a
    *permanently* rejected batch (a PostgREST 4xx other than 429) is retried one
    id at a time, so one unwritable row cannot silently drop everyone else's
    update. A transient batch failure (429, 5xx, timeout, transport error) is
    never fanned out: it marks nothing errored, so isolating it buys nothing,
    while dozens of sequential 30-second PATCHes against a struggling Supabase
    would blow the workflow timeout and kill the run before its summary and
    pruning. For the same reason a fan-out stops as soon as `budget` is spent
    (the budget is shared across every chunk), and a batch larger than
    PER_ID_FALLBACK_MAX is not fanned out at all.

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
    if len(ids) <= chunk_size:
        return _patch_id_batch(db, ids, data, budget=budget, fanout=fanout)
    failures: dict[str, BaseException] = {}
    isolated: set[str] = set()
    for start in range(0, len(ids), chunk_size):
        outcome = _patch_id_batch(
            db, ids[start : start + chunk_size], data, budget=budget, fanout=fanout
        )
        failures.update(outcome.failures)
        isolated |= outcome.isolated
    return PatchOutcome(failures, frozenset(isolated))


def _patch_id_batch(
    db,
    ids: list[str],
    data: dict,
    *,
    budget: FanoutBudget | None,
    fanout,
) -> PatchOutcome:
    """One chunk's batched PATCH with the per-id isolating fallback. `ids` is a
    non-empty, sorted list already bounded to a single URL by patch_watches."""
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


# --- retention pruning (once per cycle, one owner) -------------------------
#
# The three time-scoped tables and the column each is pruned on. They are
# whole-fleet and provider-neutral, so when the poll is split into one job per
# provider they must NOT be pruned once per provider — that would triple the
# writes and have three jobs delete the same rows. The plan job owns pruning for
# the whole cycle (see plan_main); the per-provider poll jobs pass prune=False.
# sent_alerts and run_summaries are HALT tables (a prune failure is systemic);
# alert_history is WARN (its table may be absent pre-0005), so its failure is
# reported but never rated — mirroring the insert path.
RETENTION_RATED_TABLES = (("sent_alerts", "sent_at"), ("run_summaries", "ran_at"))
RETENTION_WARN_TABLE = ("alert_history", "delivered_at")


def prune_retention(db, now: datetime) -> tuple[dict[str, BaseException], BaseException | None]:
    """Delete rows older than RETENTION_DAYS from the three time-scoped tables.

    Each delete is contained so one failure never stops the others. Returns
    (rated_failures, warn_failure): rated_failures maps a `<table> prune` label
    to the exception for the HALT tables (systemic when non-empty), and
    warn_failure is the alert_history exception (reported, never rated) or None.
    """
    cutoff = iso_now(now - timedelta(days=RETENTION_DAYS))
    rated: dict[str, BaseException] = {}
    for table, column in RETENTION_RATED_TABLES:
        try:
            db.delete(table, {column: f"lt.{cutoff}"})
        except Exception as exc:  # containment boundary
            rated[f"{table} prune"] = exc
    table, column = RETENTION_WARN_TABLE
    try:
        db.delete(table, {column: f"lt.{cutoff}"})
        warn: BaseException | None = None
    except Exception as exc:  # reported, never rated
        warn = exc
    return rated, warn


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
    provider: str | None = None,
    prune: bool = True,
    rng: random.Random | None = None,
    sleep=time.sleep,
    now_fn=lambda: datetime.now(timezone.utc),
    monotonic=time.monotonic,
    time_budget_seconds: float = CYCLE_TIME_BUDGET_SECONDS,
    process_started: float | None = PROCESS_STARTED,
    per_id_fallback_budget_seconds: float = PER_ID_FALLBACK_BUDGET_SECONDS,
    fanout_deadline_seconds: float = FANOUT_DEADLINE_SECONDS,
) -> dict:
    # `provider` scopes the whole cycle to one provider's watches, for the
    # per-provider poll jobs the workflow runs; None serves every provider (the
    # monolithic `python scripts/monitor.py` path, and every existing test). When
    # scoped, the read, the errored-watch census and the summary row all narrow to
    # that provider, so three jobs never each poll the whole fleet nor write a
    # summary claiming to describe the whole cycle. `prune` is False for those
    # poll jobs: retention pruning is whole-fleet and belongs to a single owner
    # (the plan job), never replicated per provider (see prune_retention).
    #
    # `provider` is captured here because the per-watch loops below rebind the
    # name to `provider_for(watch)` (a Provider object); the cycle-scope value is
    # this string (or None) and must survive them.
    scoped_provider = provider
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

    # --- egress read-reduction: plan from the view, poll, then read back only
    #     the watch rows that can matter this cycle ------------------------------
    #
    # When the cycle is scoped to one provider (a per-provider poll job), every
    # read below narrows to that provider's slice — the plan view, the full-read
    # fallback, and the process-set read alike — so three jobs never each plan or
    # read the whole fleet. The whole-fleet run (scoped_provider is None) plans
    # and reads every provider, exactly as before the split.
    #
    # 'today's watermark for "a user edited this watch since the last cycle". A
    # failure here is non-fatal: None just means the process read cannot scope to
    # edits and reads the full monitoring set (correct, only costlier).
    try:
        watermark = read_watermark(db, now)
    except Exception:  # optimization input only; never a reason to redden a run
        watermark = None

    # The poll plan covers EVERY monitoring watch's units — a quiet unit is still
    # polled so an opening on it is still noticed. The deduped planning inputs
    # come from the monitoring_plan view (one row per real unit); if the view is
    # absent (or blips), fall back to the full watches read and plan from it,
    # exactly as before the reduction. `full_watches` is reused as the process set
    # in that fallback so the cycle never reads the whole table twice.
    try:
        plan_rows = read_plan_rows(db, scoped_provider)
        have_view = True
        full_watches: list[dict] | None = None
    except Exception:
        have_view = False
        full_watches = read_monitoring_watches(db, scoped_provider)
        plan_rows = full_watches

    # Filter the plan for what may be POLLED: a provider this build serves, a
    # campground_id that is well-formed. Unroutable/invalid rows are never polled
    # (garbage requests); expired and unpollable rows self-exclude because their
    # poll_plan is empty.
    #
    # Invalid/unpollable watches are NOT added to the process read's campground
    # scope, deliberately: a malformed campground_id can carry characters unsafe
    # in a `campground_id=in.(…)` filter, and it is never needed there. Such a
    # watch's config is client-written, so it is set at create/edit time and the
    # `updated_at` watermark reads it back on the very next cycle to error it;
    # any that predate this feature were already errored by the pre-reduction
    # cycle, which read and validated every row. So the scoped read stays over
    # poll-derived, regex-valid campground_ids only.
    plan_routable = [r for r in plan_rows if provider_name(r) in PROVIDERS]
    plan_pollable = [
        r for r in plan_routable
        if CAMPGROUND_ID_RE.fullmatch(str(r["campground_id"]))
    ]

    # Each planned unit carries the conformer `provider_for` chose for the
    # watches that asked for it, so dispatch can never be decided by the
    # client-writable campground_id. The pacing below still runs one shared,
    # shuffled queue across providers rather than a burst per site.
    dispatch = poll_dispatch(plan_pollable, today)
    plan = sorted_poll_units(dispatch)
    rng.shuffle(plan)
    session_ua = rng.choice(USER_AGENTS)  # one UA per run, rotated across runs

    # A campground_id is only unique within its own provider, so the 404-strike
    # inputs are scoped per provider: one site's 404 must never strike a watch
    # on another site that happens to name the same id.
    not_found: dict[str, set[str]] = {}
    availability: dict[PollUnit, dict | None] = {}
    # A cycle that runs out of poll budget mid-plan leaves the remaining parks
    # unpolled: their watches do not fire this cycle. That is a completed miss,
    # not a transient the next cycle heals, and it belongs to no single watch —
    # the plan is shared across every user. So it goes through record_cycle_failure
    # (systemic by definition, red run, both audiences via a safe aggregate
    # label), never a bare errors.append that would leave the run green. There is
    # deliberately no tolerant threshold: the budget only trips once the serial
    # poll work already exceeds one cycle's budget, so any skip already means the
    # fleet (or a stalled upstream) is over capacity — a K>0 threshold would just
    # re-hide the silent-miss it took a two-day outage to learn about. The count
    # is also surfaced on the result (polls_skipped), mirroring the errored-watch
    # census, with no added read or write.
    polls_skipped = 0
    for i, unit in enumerate(plan):
        if budget_exhausted():
            polls_skipped = len(plan) - i
            record_cycle_failure(
                f"time budget exhausted: skipped {polls_skipped} remaining poll(s) "
                f"of {len(plan)} planned — fleet exceeds one cycle's poll budget"
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

    # Change detection: compare each polled unit's RAW availability hash against
    # the one stored last cycle (poll_units). A unit whose hash is unchanged
    # cannot have changed any watch's per-watch state_hash, so its rows are not
    # read; a changed or unseen unit's campground IS read back and processed. The
    # hashes are read scoped to only the units this cycle polls (bounded egress),
    # and if the table is absent (unapplied 0010) every unit reads as changed —
    # the full-read fallback.
    stored_hashes: dict[str, str] = {}
    have_hashes = False
    if have_view:
        try:
            stored_hashes = read_poll_unit_hashes(db, (unit_key(u) for u in dispatch))
            have_hashes = True
        except Exception:  # table absent or a blip: treat every unit as changed
            have_hashes = False
    changed_campgrounds: set[str] = set()
    changed_unit_rows: list[dict] = []
    for unit, parsed in availability.items():
        if parsed is None:
            continue  # failed poll: keep the old hash, do not count as changed
        uk = unit_key(unit)
        digest = state_hash(parsed)
        if stored_hashes.get(uk) != digest:
            campground_id = str(unit[1][0])
            changed_campgrounds.add(campground_id)
            changed_unit_rows.append(
                {"unit_key": uk, "raw_hash": digest, "campground_id": campground_id}
            )
    # Only poll-derived campground_ids reach the scoped read's `in.(…)` filter:
    # changed units and 404ed campgrounds, both of which passed CAMPGROUND_ID_RE
    # to be polled at all (so they carry no URL-unsafe characters).
    not_found_cgs = {cg for cgs in not_found.values() for cg in cgs}
    relevant_cgs = changed_campgrounds | not_found_cgs

    # The watch rows to actually process. Fast path: only the rows a change,
    # edit, strike, or expiry can touch. Otherwise (view or poll_units absent) the
    # full monitoring set, exactly as before the reduction.
    if have_view and have_hashes:
        watches = read_process_set(db, today, watermark, relevant_cgs, scoped_provider)
    elif full_watches is not None:
        watches = full_watches
    else:
        watches = read_monitoring_watches(db, scoped_provider)

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
    # what keeps the read `.get`-tolerant. Left as a single unpaginated GET: this
    # population (errored watches with a future trip) is small and bounded, unlike
    # the monitoring set, so it is not the max-rows truncation risk that one is.
    try:
        errored = db.select(
            "watches", {"status": "eq.error", "end_date": f"gte.{today.isoformat()}"}
        )
    except Exception as exc:  # containment: a census must never cost a cycle
        errored = None
        errors.append("errored-watch census unavailable")
        detail_only.append(capped_line(f"errored-watch census: {summarize_exception(exc)}"))
    # A per-provider poll job counts only its own provider's errored watches, so
    # its summary reports the population that job is responsible for. Filtered
    # client-side (not with a `provider=eq.` filter) to keep the census read the
    # single drift-tolerant select=* it must stay — see the note above.
    if errored is not None and scoped_provider is not None:
        errored = [w for w in errored if provider_name(w) == scoped_provider]
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
    #
    # A watch that saw a delta this cycle but did NOT settle it — an APNs
    # retryable/config failure or a rejected state_hash write, both of which keep
    # the old hash to retry next cycle — must be re-processed next cycle. Under
    # the read-reduction that only happens if its unit is read again, so its
    # unit's hash is withheld from the poll_units upsert (deferred_unit_keys):
    # an un-stored unit reads as "changed" next cycle and its rows are pulled
    # back. Without this, a transient APNs outage would silently defer a push
    # until the campground's availability happened to change — a delayed opening.
    deferred_unit_keys: set[str] = set()

    def defer_watch_units(watch: dict) -> None:
        prov = provider_for(watch)
        for key in prov.poll_plan(watch, today):
            deferred_unit_keys.add(unit_key((prov.name, key)))

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
            # Flexible watches (Phase 16): keep only nights inside a fully-open
            # qualifying window. Provider-agnostic and a no-op for fixed watches
            # (returned unchanged), so fixed behaviour is byte-identical and the
            # hash/alert path below is untouched.
            current = apply_flex_window(current, watch)
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
                    # outage clears or the operator rotates the bad credential.
                    # Withhold the unit's hash so the reduction re-reads this
                    # watch next cycle rather than skipping the unchanged unit.
                    defer_watch_units(watch)
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
                    try:
                        # The app's server-truth Alert History: one row per
                        # delivered push, in the same step and condition as
                        # last_found_at below, so badge and history cannot
                        # disagree. Purely additive — a failure here (e.g. an
                        # unapplied 0005) is reported but UNRATED and never blocks
                        # the row's own state_hash/last_found_at write, so a
                        # missing alert_history table cannot redden the run or
                        # stop a single push.
                        db.insert("alert_history", history_row(watch, fresh, now))
                    except Exception as exc:
                        record_failures(
                            unattributed_failure(watch_id, exc),
                            "alert",
                            blocking=False,
                            rated=False,
                        )
        except Exception as exc:  # sent_alerts / APNs: not this watch's row
            # Table-scoped, like the delivery failure above: the watch was
            # polled, so it is still stamped last_checked_at rather than left
            # looking unchecked. The state_hash was not advanced, so the delta is
            # unsettled — withhold the unit so next cycle re-reads and retries.
            record_failures(unattributed_failure(watch_id, exc), "alert", blocking=False)
            defer_watch_units(watch)
            continue

        try:  # the one write pinned to this watch's own row
            db.patch(
                "watches",
                {"id": f"eq.{watch_id}"},
                {"state_hash": new_hash, **({"last_found_at": iso_now(now)} if delivered else {})},
            )
        except Exception as exc:  # containment boundary
            # The hash did not land, so the delta is unsettled: withhold the unit
            # so the reduction re-reads this watch next cycle instead of skipping.
            record_failures(isolated_failure(watch_id, exc), "process")
            defer_watch_units(watch)

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

    # Freshness stamp: set last_checked_at on every monitoring, non-expired watch
    # the per-id write above did not cover — the quiet-unit rows the read-
    # reduction never pulled. One filter-scoped PATCH (no id list, so no URL-
    # length cliff), run AFTER error-marking so a watch errored this cycle is not
    # shown as freshly checked. The reduction shrinks the row READ, not the poll —
    # every unit is still polled every cycle, so every monitoring watch genuinely
    # is still being checked and stamping them all is accurate (captain's ruling).
    # Its affected-row count is watches_checked: how many watches are actually
    # being monitored, which the per-id `len(checked)` can no longer report once
    # the quiet rows are unread. A pool-wide stamp failure is a systemic signal
    # (the whole fleet went unstamped), so it reddens the run like any cycle-scope
    # write failure.
    # Skipped only when the poll did NOT cover the fleet this cycle (the time
    # budget ran out mid-plan, already a systemic red run): blanket-stamping then
    # would falsely mark the un-polled watches as freshly checked. On a normal
    # cycle every unit was polled, so the blanket stamp is accurate and its count
    # is watches_checked; on a truncated cycle we fall back to the per-id
    # `len(checked)`, stamping only the watches actually served.
    watches_checked = len(checked)
    try:
        if not polls_skipped:
            # Scoped to this provider on a per-provider job so it stamps (and
            # counts) only that job's slice, never another provider's quiet rows;
            # the whole-fleet run adds no provider filter and stamps every
            # monitoring row, as before the split.
            blanket_filter = {"status": "eq.monitoring", "end_date": f"gte.{today.isoformat()}"}
            if scoped_provider is not None:
                blanket_filter["provider"] = f"eq.{scoped_provider}"
            stamped = db.patch(
                "watches",
                blanket_filter,
                {"last_checked_at": iso_now(now)},
            )
            if stamped is not None:
                watches_checked = stamped
    except Exception as exc:  # containment boundary
        if is_permanent_failure(exc):
            # A permanent rejection (e.g. a missing last_checked_at column) leaves
            # the whole fleet unstamped and will recur every cycle — systemic.
            record_cycle_failure("last_checked blanket stamp", exc)
        else:
            # A transient blip (429/5xx/timeout) is retried next cycle, like any
            # other transient write failure: reported to both audiences but never
            # reddening the run.
            errors.append(capped_line(f"last_checked blanket stamp: {summarize_exception(exc, safe=True)}"))
            detail_only.append(capped_line(f"last_checked blanket stamp: {summarize_exception(exc)}"))

    # Record this cycle's changed units so next cycle can skip their unchanged
    # rows. Only CHANGED units are written, so a quiet cycle writes nothing here
    # and the ≤5 write budget holds; a failure is self-healing (next cycle simply
    # re-detects the change, and pushes stay deduped by sent_alerts), so it is
    # reported UNRATED and never reddens the run — mirroring the alert_history
    # write. With poll_units absent (unapplied 0010) have_hashes is False and
    # nothing is written here.
    settled_unit_rows = [r for r in changed_unit_rows if r["unit_key"] not in deferred_unit_keys]
    if have_hashes and settled_unit_rows:
        try:
            db.upsert(POLL_UNITS_TABLE, settled_unit_rows, on_conflict="unit_key")
        except Exception as exc:  # reported, never rated
            errors.append(capped_line(f"poll_units upsert: {summarize_exception(exc, safe=True)}"))
            detail_only.append(capped_line(f"poll_units upsert: {summarize_exception(exc)}"))

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

    # Retention pruning is whole-fleet and provider-neutral, so a per-provider
    # poll job (prune=False) never runs it — that is the plan job's job, once per
    # cycle (see prune_retention / plan_main). When this is the monolithic run
    # (prune=True), it prunes here, before the summary row's verdict is chosen, so
    # a HALT-table prune failure (systemic) lands on the label. The alert_history
    # failure is reported to both audiences but UNRATED, mirroring its insert
    # path, so an unapplied 0005 cannot redden the run.
    if prune:
        rated_prune_failures, alert_history_prune_failure = prune_retention(db, now)
        for label, exc in rated_prune_failures.items():
            record_cycle_failure(label, exc)
        if alert_history_prune_failure is not None:
            errors.append(capped_line(
                f"alert_history prune: {summarize_exception(alert_history_prune_failure, safe=True)}"
            ))
            detail_only.append(capped_line(
                f"alert_history prune: {summarize_exception(alert_history_prune_failure)}"
            ))

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
        "watches_checked": watches_checked,
        "campgrounds_polled": len({key[0] for _, key in availability}),
        "alerts_sent": alerts_sent,
        "duration_ms": int((monotonic() - started) * 1000),
        "errors": "; ".join(public_errors) or None,
        # Names which provider this row summarizes, so three per-cycle rows are not
        # each an unlabeled slice reading like the whole cycle. Only written when
        # scoped: the monolithic run omits it, keeping its row byte-identical and
        # needing no 0013. WARN-classified: run_summaries.provider is dropped and
        # retried on a database without 0013 (insert_summary), so an unapplied
        # migration cannot redden every run.
        **({"provider": scoped_provider} if scoped_provider is not None else {}),
    }

    def insert_summary(*_ignored) -> None:
        try:
            db.insert("run_summaries", summary)
        except Exception as exc:  # drift tolerance for the WARN `provider` column
            if "provider" in summary and rejects_missing_column(exc, "provider"):
                db.insert("run_summaries", {k: v for k, v in summary.items() if k != "provider"})
            else:
                raise

    # Written last, so both prunes' outcomes are already in the verdict above.
    contained("run_summaries insert", insert_summary)

    return {
        **summary,
        "watches_considered": considered,
        "watch_errors": len(watch_failures),
        # The errored population this cycle inherited (None if the census
        # select itself failed). Not a column of run_summaries — the persisted
        # signal is the count line the census put on `errors`, which is what
        # turns a silent green run into one that says three watches are dead.
        "watches_errored": watches_errored,
        # Parks the poll budget ran out before reaching, so their watches were
        # not served this cycle. Non-zero means the run went systemic above
        # (record_cycle_failure), so this is the machine-readable companion to
        # that failure line — surfaced like watches_errored, never a persisted
        # run_summaries column, so the write budget is untouched.
        "polls_skipped": polls_skipped,
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


def _execute(
    db,
    rng: random.Random,
    process_started: float | None,
    *,
    provider: str | None,
    prune: bool,
    time_budget_seconds: float,
    fanout_deadline_seconds: float,
) -> None:
    """The shared post-preflight body: jitter, poll, print, annotate, exit. The
    monolithic and per-provider entrypoints differ only in what they hand it."""
    delay = start_delay(rng)
    print(f"start jitter: sleeping {delay:.0f}s", flush=True)
    time.sleep(delay)

    apns = APNsClient.from_env()
    with httpx.Client(http2=True, timeout=20, follow_redirects=True) as http:
        result = run(
            db, apns, http, rng=rng, process_started=process_started,
            provider=provider, prune=prune,
            time_budget_seconds=time_budget_seconds,
            fanout_deadline_seconds=fanout_deadline_seconds,
        )
    print(json.dumps(result), flush=True)
    for annotation in (error_annotation(result), failure_annotation(result)):
        if annotation:
            print(annotation, flush=True)
    raise SystemExit(exit_code(result))


def main(process_started: float | None = PROCESS_STARTED) -> None:
    # The whole-fleet path (`python scripts/monitor.py`, no flags): polls every
    # provider in one run and owns pruning, exactly as before the per-provider
    # split. Kept working so a revert of the workflow still monitors, and it is
    # the path run()'s existing tests exercise.
    #
    # `process_started` is plumbing, not policy: the default is the import-time
    # anchor the CLI entrypoint has always used, so this path behaves exactly as
    # before. The parameter exists so a long-lived host (one reusing a warm
    # process across invocations) could forward a *fresh* reading each time,
    # because run() binds its own process_started default once at import.
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
    _execute(
        db, rng, process_started,
        provider=None, prune=True,
        time_budget_seconds=CYCLE_TIME_BUDGET_SECONDS,
        fanout_deadline_seconds=FANOUT_DEADLINE_SECONDS,
    )


def poll_main(provider: str, process_started: float | None = PROCESS_STARTED) -> None:
    """One provider's poll job (`--provider <name>`): reads and serves only that
    provider's watches, on that provider's own budget, and never prunes."""
    profile = poll_profile(provider)
    rng = random.Random()
    db = SupabaseClient.from_env()
    preflight(db)  # each poll job guards drift independently, as the whole-fleet run did
    _execute(
        db, rng, process_started,
        provider=provider, prune=False,
        time_budget_seconds=profile.time_budget_seconds,
        fanout_deadline_seconds=profile.fanout_deadline_seconds,
    )


def providers_with_watches(db) -> list[str]:
    """The registered providers that currently have at least one monitoring
    watch, sorted. On any read failure it falls back to EVERY registered provider
    rather than skipping one: a plan-side blip must never silently drop a
    provider's whole poll for the cycle. `watches.provider` is a WARN column, so
    a database without 0002 rejects the projected read; it is retried without the
    column, and provider_name() `.get()`-defaults every row to recreation_gov —
    correct, since a pre-0002 database has only recreation.gov watches."""
    try:
        try:
            rows = paginated_select(
                db, "watches", {"status": "eq.monitoring", "select": "id,provider"}
            )
        except Exception as exc:
            if not is_missing_column_rejection(exc):
                raise
            rows = paginated_select(db, "watches", {"status": "eq.monitoring", "select": "id"})
        return sorted({provider_name(r) for r in rows} & set(PROVIDERS))
    except Exception:  # fail-safe: a plan blip runs every provider, never none
        return sorted(PROVIDERS)


def _emit_github_output(name: str, value: str) -> None:
    """Set a GitHub Actions step output (and echo to stdout for local runs)."""
    print(f"{name}={value}", flush=True)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def plan_main() -> None:
    """The plan job (`--plan`): emit the per-provider poll matrix, then own the
    once-per-cycle retention prune. It is the one job that runs every cycle, so
    pruning — whole-fleet and provider-neutral — belongs here and not replicated
    across the poll jobs. A HALT-table prune failure reddens this job (the split
    equivalent of the systemic verdict the monolithic run gave a prune failure);
    the alert_history prune stays reported-but-unrated, as its insert path is."""
    db = SupabaseClient.from_env()
    providers = providers_with_watches(db)
    matrix = {
        "include": [
            {"provider": name, "timeout": int(poll_profile(name).job_timeout_seconds // 60)}
            for name in providers
        ]
    }
    # A provider with no watches is absent from the matrix, so it runs no poll
    # job at all; an empty fleet emits run_poll=false and the workflow skips the
    # whole matrix rather than expanding an empty one.
    _emit_github_output("run_poll", "true" if providers else "false")
    _emit_github_output("matrix", json.dumps(matrix, separators=(",", ":")))

    rated, warn = prune_retention(db, datetime.now(timezone.utc))
    if warn is not None:
        print(f"::warning::alert_history prune: {summarize_exception(warn)}", flush=True)
    if rated:
        for label, exc in sorted(rated.items()):
            print(f"::error::{label}: {summarize_exception(exc)}", flush=True)
        raise SystemExit(1)


def cli(argv: list[str] | None = None) -> None:
    """Dispatch the three run modes. No flags is the whole-fleet run; --provider
    is one provider's poll job; --plan emits the matrix and prunes."""
    parser = argparse.ArgumentParser(description="CampAssist availability monitor")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--provider", help="poll only this provider's watches (one poll job)")
    group.add_argument(
        "--plan", action="store_true",
        help="emit the per-provider job matrix and run the retention prune (the plan job)",
    )
    args = parser.parse_args(argv)
    if args.plan:
        plan_main()
    elif args.provider:
        poll_main(args.provider)
    else:
        main()


if __name__ == "__main__":
    cli()
