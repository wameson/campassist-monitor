"""Per-watch failure containment and the threshold-gated run status.

A single watch's rejected write once aborted the whole cycle for days: no
watch was polled, nobody was alerted, no run summary was written, and the
schedule still looked green. These tests pin down both halves of the fix —
one watch's failure stays contained (A), and breakage broad enough to be
systemic still fails the run loudly (B).
"""

import random

import httpx
import pytest

import monitor
from helpers import (
    FAKE_SERVICE_KEY,
    NOW,
    FakeAPNs,
    FakeDB,
    FakeHTTP,
    FakeResponse,
    availability_payload,
    make_watch,
    postgrest_error,
)

QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)
OPEN_PAYLOAD = availability_payload({"100": {"2026-08-10": "Available"}})


class Clock:
    """Monotonic stand-in the test advances by hand."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def tick(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def fails_watch_patch(*watch_ids, columns=None, status=400):
    """Reject any watches PATCH that touches one of `watch_ids` — optionally
    only when it writes one of `columns`, which is how a column missing from a
    live DB fails: some writes to the row go through, one kind never does."""
    targets = {str(i) for i in watch_ids}

    def fail_on(call):
        op, table = call[0], call[1]
        if op != "patch" or table != "watches":
            return None
        if columns is not None and not (set(call[3]) & set(columns)):
            return None
        ids = set(str(call[2]["id"]).partition(".")[2].strip("()").split(","))
        if ids & targets:
            return postgrest_error(status, "column watches.last_checked_at does not exist")
        return None

    return fail_on


def pool(count, fail_on=None):
    """`count` watches on one shared campground, one per user."""
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(count)]
    return FakeDB({"watches": watches}, fail_on=fail_on), watches


def run_cycle(db, payload=OPEN_PAYLOAD, apns=None):
    apns = apns or FakeAPNs()
    result = monitor.run(db, apns, FakeHTTP(lambda cg: FakeResponse(200, payload)), **QUIET)
    return result, apns


# --- A: containment -------------------------------------------------------

def test_isolated_write_failure_completes_the_cycle():
    # w1's last_checked_at write is rejected the way the outage's PATCH was.
    # The batch containing it must not take the other three down with it.
    db, _ = pool(4, fail_on=fails_watch_patch("w1", columns=("last_checked_at",)))

    result, apns = run_cycle(db)

    # every watch was still polled and alerted, including the failing one
    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    assert result["alerts_sent"] == 4
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert all(rows[f"w{i}"]["last_checked_at"] is not None for i in (0, 2, 3))
    assert rows["w1"]["last_checked_at"] is None

    # the failing watch is surfaced to its user as an errored watch
    assert rows["w1"]["status"] == "error"
    assert all(rows[f"w{i}"]["status"] == "monitoring" for i in (0, 2, 3))

    # end-of-cycle bookkeeping still ran
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2
    assert result["watches_checked"] == 3  # the failed watch is not "checked"

    # isolated -> the run stays green, with the failure recorded as the log
    assert result["systemic_failure"] is False
    assert monitor.exit_code(result) == 0
    assert result["watch_errors"] == 1 and result["watches_considered"] == 4
    assert "1 of 4 served watch(es) failed this cycle (isolated)" in result["errors"]
    assert "watch w1: last-checked:" in result["errors"]
    assert monitor.error_annotation(result).startswith("::warning::")
    assert monitor.failure_annotation(result) is None


def test_failed_watch_does_not_stop_the_watches_after_it():
    # the first watch blows up mid-processing (its state_hash write is
    # rejected); the ones behind it in the loop still alert and store state
    db, _ = pool(4, fail_on=fails_watch_patch("w0", columns=("state_hash",)))

    result, apns = run_cycle(db)

    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w0"]["state_hash"] is None  # never stored: its write failed
    assert all(rows[f"w{i}"]["state_hash"] for i in (1, 2, 3))
    assert rows["w0"]["status"] == "error"
    assert "watch w0: process:" in result["errors"]
    assert monitor.exit_code(result) == 0


def test_transient_failure_does_not_error_the_watch():
    # a 503 is an outage, not a broken watch: it is recorded and retried next
    # cycle rather than parked in status='error' where a user must fix it
    db, _ = pool(4, fail_on=fails_watch_patch("w1", status=503))

    result, _ = run_cycle(db)

    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "monitoring"
    assert row["state_hash"] is None  # keeps the old hash, so it re-alerts
    assert not [c for c in db.calls_of("patch", "watches") if c[3] == {"status": "error"}]
    assert result["watch_errors"] == 1
    assert monitor.exit_code(result) == 0

    # next cycle, with the outage over, the watch is served normally again
    db.fail_on = lambda call: None
    result, _ = run_cycle(db)
    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["state_hash"] and row["last_checked_at"] is not None
    assert result["errors"] is None and monitor.exit_code(result) == 0


def test_batched_write_falls_back_to_per_id():
    # containment granularity: the batch is tried first (the write budget
    # depends on it), and only a failure fans out to per-id writes
    db, _ = pool(4, fail_on=fails_watch_patch("w1", columns=("last_checked_at",)))

    run_cycle(db)

    checked = [c for c in db.calls_of("patch", "watches") if "last_checked_at" in c[3]]
    assert str(checked[0][2]["id"]).startswith("in.(")  # batch attempted first
    assert [c[2]["id"] for c in checked[1:]] == ["eq.w0", "eq.w1", "eq.w2", "eq.w3"]


def test_unattributed_batch_failure_never_errors_watches():
    # 51 watches share a campground that has now 404ed three cycles running.
    # Their batched status='error' write is too big to fan out, so one exception
    # is blamed on every row without showing any single row is bad — those
    # watches must be counted and logged, never parked in status='error' where
    # their users would have to recreate them.
    doomed = [
        make_watch(
            id=f"d{i}",
            user_id=f"u{i}",
            campground_id="9999",
            consecutive_not_found=monitor.NOT_FOUND_ERROR_THRESHOLD - 1,
        )
        for i in range(monitor.PER_ID_FALLBACK_MAX + 1)
    ]
    healthy = [make_watch(id=f"h{i}", user_id=f"v{i}") for i in range(4)]

    def fail_on(call):
        if call[0] == "patch" and call[1] == "watches" and "status" in call[3]:
            return postgrest_error(400, "column watches.consecutive_not_found does not exist")
        return None

    db = FakeDB({"watches": doomed + healthy}, fail_on=fail_on)
    apns = FakeAPNs()
    result = monitor.run(
        db,
        apns,
        FakeHTTP(
            lambda cg: FakeResponse(404) if cg == "9999" else FakeResponse(200, OPEN_PAYLOAD)
        ),
        **QUIET,
    )

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert all(rows[w["id"]]["status"] == "monitoring" for w in doomed)
    # one batch, no fan-out, and no end-of-cycle status='error' retry either
    assert len([c for c in db.calls_of("patch", "watches") if "status" in c[3]]) == 1
    assert not [c for c in db.calls_of("patch", "watches") if c[3] == {"status": "error"}]

    # the healthy watches were served, and the failures are still counted
    assert sorted(w for w, _ in apns.alerts) == ["h0", "h1", "h2", "h3"]
    assert result["watch_errors"] == len(doomed)
    assert result["watches_considered"] == 4
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0
    assert f"plus {len(doomed)} on watch(es) this cycle did not serve" in result["errors"]


def test_strike_count_failure_still_serves_the_watch():
    # a rejected consecutive_not_found write is bookkeeping only: the watch's
    # campground polled fine and may have a new opening, so it must still be
    # delta-checked, alerted and stamped — and never errored
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(4)]
    watches[1]["consecutive_not_found"] = 1  # a reset is due for w1
    db = FakeDB(
        {"watches": watches},
        fail_on=fails_watch_patch("w1", columns=("consecutive_not_found",)),
    )

    result, apns = run_cycle(db)

    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["state_hash"] and row["last_checked_at"] is not None
    assert row["status"] == "monitoring"
    assert not [c for c in db.calls_of("patch", "watches") if c[3] == {"status": "error"}]

    # recorded and counted, but isolated — the run stays green
    assert result["watch_errors"] == 1 and result["watches_checked"] == 4
    assert "watch w1: strike-count:" in result["errors"]
    assert monitor.exit_code(result) == 0


def test_bookkeeping_failure_does_not_shadow_a_later_isolated_failure():
    # w1's strike-count write is rejected first (bookkeeping only, so it never
    # errors the watch), and its own state_hash write is then rejected too. The
    # earlier, non-errorable record must not swallow the later isolated one, or
    # the watch silently stops updating instead of surfacing as broken.
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(4)]
    watches[1]["consecutive_not_found"] = 1
    db = FakeDB(
        {"watches": watches},
        fail_on=fails_watch_patch("w1", columns=("consecutive_not_found", "state_hash")),
    )

    result, apns = run_cycle(db)

    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "error"
    # one watch, one message: the first failure is the one that is logged
    assert result["watch_errors"] == 1
    assert "watch w1: strike-count:" in result["errors"]
    assert monitor.exit_code(result) == 0


def test_healthy_cycle_keeps_the_write_budget():
    # containment must not cost the no-change cycle any extra write
    db, _ = pool(50)
    quiet_payload = availability_payload({"100": {"2026-08-10": "Reserved"}})
    run_cycle(db, quiet_payload)  # seeds state_hash

    db.calls.clear()
    result, apns = run_cycle(db, quiet_payload)
    assert apns.alerts == [] and db.write_count <= 5
    assert result["errors"] is None and result["watch_errors"] == 0
    assert monitor.exit_code(result) == 0


# --- B: threshold-gated run status ----------------------------------------

def test_systemic_failure_exits_nonzero():
    # every watch's write is rejected — the schedule must go red, not green
    db, watches = pool(4, fail_on=fails_watch_patch("w0", "w1", "w2", "w3"))

    result, _ = run_cycle(db)

    assert result["watch_errors"] == 4 and result["watches_considered"] == 4
    assert result["systemic_failure"] is True
    assert monitor.exit_code(result) == 1
    assert monitor.failure_annotation(result).startswith("::error::")
    assert "4 of 4 served watch(es) failed this cycle (systemic)" in result["errors"]

    # the summary row and pruning still run: the cycle reports, then fails
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2
    # systemic breakage is the operator's to fix, so the pool is left intact
    # rather than erroring every watch and making users recreate them
    assert all(r["status"] == "monitoring" for r in db.tables["watches"])


def test_summary_and_retention_failures_are_systemic():
    # failures with no watch to blame always fail the run, and neither of the
    # three end-of-cycle writes may be lost because an earlier one failed
    def fail_on(call):
        if call[0] == "insert" and call[1] == "run_summaries":
            return postgrest_error(400, "column run_summaries.errors does not exist")
        return None

    db, _ = pool(3, fail_on=fail_on)

    result, _ = run_cycle(db)

    assert "run_summaries insert" in result["cycle_errors"]
    assert result["watch_errors"] == 0  # no watch is to blame
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1
    assert len(db.calls_of("delete")) == 2  # pruning still ran
    assert "::error::" in monitor.failure_annotation(result)
    # the watches themselves were served normally
    assert all(r["last_checked_at"] is not None for r in db.tables["watches"])


def test_expired_watches_are_out_of_the_systemic_rate():
    # 8 of 10 watches expire this cycle; the 2 the cycle actually served both
    # fail. 2-of-10 would look isolated and stay green — 2-of-2 is total
    # breakage of everything this run tried to do, and must go red.
    expired = [
        make_watch(id=f"e{i}", user_id=f"u{i}", start_date="2026-06-30", end_date="2026-07-01")
        for i in range(8)
    ]
    served = [make_watch(id=f"w{i}", user_id=f"v{i}") for i in range(2)]
    db = FakeDB({"watches": expired + served}, fail_on=fails_watch_patch("w0", "w1"))

    result, _ = run_cycle(db)

    assert all(r["status"] == "expired" for r in db.tables["watches"] if r["id"].startswith("e"))
    assert result["watches_considered"] == 2 and result["watch_errors"] == 2
    assert "2 of 2 served watch(es) failed this cycle (systemic)" in result["errors"]
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1


def test_beyond_horizon_watches_are_out_of_the_systemic_rate():
    # 8 of 10 watches start past the 12-month poll horizon: the cycle never
    # polls them, never writes them and cannot alert them. They must not dilute
    # the rate for the 2 watches it did work for and failed.
    beyond = [
        make_watch(id=f"b{i}", user_id=f"u{i}", start_date="2028-06-01", end_date="2028-06-05")
        for i in range(8)
    ]
    servable = [make_watch(id=f"w{i}", user_id=f"v{i}") for i in range(2)]
    db = FakeDB({"watches": beyond + servable}, fail_on=fails_watch_patch("w0", "w1"))

    result, _ = run_cycle(db)

    assert all(r["status"] == "monitoring" for r in db.tables["watches"])
    assert result["watches_considered"] == 2 and result["watch_errors"] == 2
    assert "2 of 2 served watch(es) failed this cycle (systemic)" in result["errors"]
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1


def test_watches_the_poll_budget_never_reached_are_out_of_the_rate():
    # the time budget cuts the plan short; the watches that were never polled
    # were not served, so they belong in neither side of the rate
    clock = Clock()
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}", campground_id=f"c{i}") for i in range(3)]
    db = FakeDB({"watches": watches}, fail_on=fails_watch_patch("w0", "w1", "w2"))

    result = monitor.run(
        db,
        FakeAPNs(),
        FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        rng=random.Random(0),
        sleep=lambda s: clock.tick(1000),
        now_fn=lambda: NOW,
        monotonic=clock,
        time_budget_seconds=100,
    )

    assert "time budget exhausted: skipped 2 remaining poll(s)" in result["errors"]
    assert result["watches_considered"] == 1  # only the campground actually polled
    assert result["watch_errors"] == 1


def test_failing_to_mark_a_watch_errored_is_systemic():
    # the end-of-cycle status='error' write is the last chance to surface a
    # broken watch; losing it for the whole set must not pass silently
    def fail_on(call):
        if call[0] != "patch" or call[1] != "watches":
            return None
        ids = set(str(call[2]["id"]).partition(".")[2].strip("()").split(","))
        if "w1" in ids and set(call[3]) & {"state_hash", "status"}:
            return postgrest_error(400)
        return None

    db, _ = pool(4, fail_on=fail_on)

    result, _ = run_cycle(db)

    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "monitoring"  # the mark never landed
    assert "watch w1: error-mark:" in result["errors"]
    assert "error-mark: 1 of 1 watch(es) could not be moved" in result["cycle_errors"]
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1
    # the run still reported and pruned before going red
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2


@pytest.mark.parametrize(
    "failed, considered, systemic",
    [
        (0, 100, False),
        (1, 100, False),   # one bad row in a healthy pool: isolated
        (25, 100, False),  # exactly at the rate: not over it
        (26, 100, True),   # over 25%: systemic
        (1, 2, False),     # the floor: a two-watch pool must not cry wolf
        (2, 2, True),      # ...but total breakage is still loud
        (1, 1, False),     # single-watch pool: below the floor, warned only
        (3, 3, True),
    ],
)
def test_systemic_threshold(failed, considered, systemic):
    assert monitor.is_systemic(failed, considered) is systemic


def test_exit_code_and_annotations():
    assert monitor.exit_code({"systemic_failure": False}) == 0
    assert monitor.exit_code({}) == 0
    assert monitor.exit_code({"systemic_failure": True}) == 1
    assert monitor.failure_annotation({"systemic_failure": False}) is None
    line = monitor.failure_annotation(
        {"systemic_failure": True, "errors": "watch w1: boom", "cycle_errors": "prune: nope"}
    )
    assert line.startswith("::error::")
    assert "watch w1: boom" in line and "prune: nope" in line


# --- containment helpers --------------------------------------------------

def test_patch_watches_batches_then_isolates():
    db = FakeDB({"watches": [make_watch(id=f"w{i}") for i in range(3)]})
    assert monitor.patch_watches(db, [], {"status": "expired"}).failures == {}
    assert db.calls == []

    assert monitor.patch_watches(db, ["w0", "w1"], {"status": "paused"}).failures == {}
    assert [c[2]["id"] for c in db.calls_of("patch")] == ["in.(w0,w1)"]

    db.fail_on = fails_watch_patch("w1", columns=("state_hash",))
    db.calls.clear()
    outcome = monitor.patch_watches(db, ["w0", "w1", "w2"], {"state_hash": "h"})
    assert set(outcome.failures) == {"w1"}
    # the per-id write pinned the failure to w1, so it may be errored
    assert outcome.isolated == frozenset({"w1"})
    assert monitor.is_permanent_failure(outcome.failures["w1"]) is True
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w0"]["state_hash"] == "h" and rows["w2"]["state_hash"] == "h"
    assert rows["w1"]["state_hash"] is None

    # a single id is not retried: the batch write was already that one write,
    # and it named the bad row itself
    db.calls.clear()
    outcome = monitor.patch_watches(db, ["w1"], {"state_hash": "h"})
    assert set(outcome.failures) == {"w1"} and len(db.calls_of("patch")) == 1
    assert outcome.isolated == frozenset({"w1"})


def test_large_failing_batch_does_not_fan_out():
    # a batch too big to isolate is systemic anyway; retrying it row by row
    # would spend hundreds of writes to learn the same thing. Nothing pinned
    # the failure to any row, so none of them may be errored.
    ids = [f"w{i}" for i in range(monitor.PER_ID_FALLBACK_MAX + 1)]
    db = FakeDB(fail_on=lambda call: postgrest_error(400))

    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"})

    assert set(outcome.failures) == set(ids)
    assert outcome.isolated == frozenset()
    assert len(db.calls_of("patch")) == 1


def test_transient_batch_failure_does_not_fan_out():
    # a 503 marks nothing errored, so isolating it buys nothing — and dozens of
    # sequential 30 s PATCHes against a struggling Supabase would blow the
    # workflow timeout and kill the run before its summary row and pruning
    ids = [f"w{i}" for i in range(4)]
    db = FakeDB(fail_on=lambda call: postgrest_error(503))

    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"})

    assert set(outcome.failures) == set(ids)
    assert outcome.isolated == frozenset()
    assert len(db.calls_of("patch")) == 1


def slow_rejections(clock, seconds=30, columns=None, watch_ids=("w0", "w1", "w2", "w3")):
    """Reject watch PATCHes, each costing `seconds` — a Supabase degraded into
    burning db.py's httpx timeout on every round-trip."""
    reject = fails_watch_patch(*watch_ids, columns=columns)

    def fail_on(call):
        exc = reject(call)
        if exc is not None:
            clock.tick(seconds)
        return exc

    return fail_on


def test_fanout_stops_when_its_allowance_runs_out():
    # the fan-out is bounded by the time it actually spends: ids it never
    # reached carry the batch failure and stay unattributed, so they are
    # counted but never errored
    ids = [f"w{i}" for i in range(4)]
    clock = Clock()
    db = FakeDB(fail_on=slow_rejections(clock, watch_ids=ids))
    budget = monitor.FanoutBudget(50, deadline=10_000, monotonic=clock)

    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"}, budget=budget)

    assert len(db.calls_of("patch")) == 3  # the batch plus two per-id writes
    assert set(outcome.failures) == set(ids)
    assert outcome.isolated == frozenset({"w0", "w1"})


def test_every_per_id_write_is_charged_to_the_allowance():
    # the allowance is cycle-wide, so a fan-out that spent it must leave nothing
    # for the next site: charging a write only when the *next* one is checked
    # would let every fan-out of the cycle overspend by one full 30 s PATCH
    clock = Clock()
    db = FakeDB(fail_on=slow_rejections(clock))
    budget = monitor.FanoutBudget(50, deadline=10_000, monotonic=clock)

    monitor.patch_watches(db, ["w0", "w1"], {"last_checked_at": "t"}, budget=budget)

    assert len(db.calls_of("patch")) == 3  # the batch plus both per-id writes
    assert budget.remaining <= 0  # two 30 s writes against a 50 s allowance

    db.calls.clear()
    outcome = monitor.patch_watches(db, ["w2", "w3"], {"last_checked_at": "t"}, budget=budget)

    assert len(db.calls_of("patch")) == 1  # the batch only: the allowance is gone
    assert outcome.isolated == frozenset()


def test_fanout_stops_at_its_absolute_deadline():
    # the second cap: however much allowance is left, the fan-out may not run
    # past the instant that still leaves room for the summary row and pruning
    ids = [f"w{i}" for i in range(4)]
    clock = Clock()
    db = FakeDB(fail_on=slow_rejections(clock, watch_ids=ids))
    budget = monitor.FanoutBudget(10_000, deadline=90, monotonic=clock)

    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"}, budget=budget)

    assert len(db.calls_of("patch")) == 3
    assert outcome.isolated == frozenset({"w0", "w1"})


def test_fanout_deadline_fits_inside_the_job_timeout():
    # the arithmetic must close: setup + fan-out deadline + shutdown reserve
    # cannot exceed the workflow's timeout-minutes: 15, and the start jitter
    # lives inside the deadline rather than stacking on top of it
    assert (
        monitor.JOB_SETUP_RESERVE_SECONDS
        + monitor.FANOUT_DEADLINE_SECONDS
        + monitor.SHUTDOWN_RESERVE_SECONDS
    ) <= monitor.JOB_TIMEOUT_SECONDS
    assert (
        monitor.START_JITTER_MAX_SECONDS + monitor.CYCLE_TIME_BUDGET_SECONDS
        > monitor.FANOUT_DEADLINE_SECONDS
    )  # a worst-case jitter + poll phase leaves no fan-out room at all


def test_run_bounds_the_fanout_so_bookkeeping_still_happens():
    # run() wires the allowance into the fan-out: a Supabase slow enough to eat
    # the job timeout one row at a time is cut off, and the cycle still reports
    clock = Clock()
    db, _ = pool(4, fail_on=slow_rejections(clock, columns=("last_checked_at",)))
    apns = FakeAPNs()

    result = monitor.run(
        db, apns, FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        **QUIET,
        monotonic=clock,
        process_started=0.0,
        per_id_fallback_budget_seconds=50,
    )

    checked = [c for c in db.calls_of("patch", "watches") if "last_checked_at" in c[3]]
    assert len(checked) == 3  # the batch plus two per-id writes, then cut off
    # the cycle still served everyone and reached its end-of-cycle bookkeeping
    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2
    assert monitor.exit_code(result) == 1  # every served watch failed: loud


def test_run_anchors_the_fanout_deadline_to_process_start():
    # the start jitter main() sleeps counts against the fan-out, so a run that
    # started late gets no fan-out at all — reaching the summary row and the
    # pruning matters more than isolating one row
    clock = Clock(monitor.FANOUT_DEADLINE_SECONDS + 100)
    db, _ = pool(4, fail_on=fails_watch_patch("w0", "w1", "w2", "w3",
                                              columns=("last_checked_at",)))
    apns = FakeAPNs()

    result = monitor.run(
        db, apns, FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        **QUIET,
        monotonic=clock,
        process_started=0.0,
    )

    checked = [c for c in db.calls_of("patch", "watches") if "last_checked_at" in c[3]]
    assert len(checked) == 1  # the batch only: no room left to fan out
    # nothing was pinned to a row, so nobody is parked in status='error'
    assert all(r["status"] == "monitoring" for r in db.tables["watches"])
    assert sorted(w for w, _ in apns.alerts) == ["w0", "w1", "w2", "w3"]
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2
    assert monitor.exit_code(result) == 1


def test_permanent_vs_transient_classification():
    assert monitor.is_permanent_failure(postgrest_error(400)) is True
    assert monitor.is_permanent_failure(postgrest_error(404)) is True
    assert monitor.is_permanent_failure(postgrest_error(429)) is False
    assert monitor.is_permanent_failure(postgrest_error(503)) is False
    assert monitor.is_permanent_failure(TimeoutError("read timeout")) is False


def test_summarize_exception_keeps_the_rejection_reason():
    exc = postgrest_error(400, "column watches.last_checked_at does not exist")
    # what httpx itself says: a status line, the request URL and a doc link —
    # everything except why the write was refused
    assert "developer.mozilla.org" in str(exc)
    assert "column watches.last_checked_at" not in str(exc)

    detail = monitor.summarize_exception(exc)

    assert "400" in detail
    assert "column watches.last_checked_at does not exist" in detail
    # the boilerplate that used to crowd the reason out of the length cap
    assert "developer.mozilla.org" not in detail and "rest/v1" not in detail
    # and never the credentials the rejected request was sent with
    assert FAKE_SERVICE_KEY not in detail and "apikey" not in detail

    verbose = monitor.summarize_exception(postgrest_error(400, "constraint " * 200))
    assert len(verbose) <= monitor.MAX_ERROR_MESSAGE_CHARS


def test_summarize_exception_falls_back_to_a_non_json_body():
    # a proxy or gateway between us and PostgREST answers in HTML, not JSON
    request = httpx.Request("PATCH", "https://project.supabase.invalid/rest/v1/watches")
    response = httpx.Response(502, request=request, text="upstream connect error")
    exc = httpx.HTTPStatusError("boom", request=request, response=response)

    detail = monitor.summarize_exception(exc)

    assert "502" in detail and "upstream connect error" in detail


def test_recorded_failures_name_the_missing_column():
    # the outage this branch exists for: an operator reading run_summaries.errors
    # must learn *which* column the live DB is missing, not just that a PATCH 400ed
    db, _ = pool(2, fail_on=fails_watch_patch("w0", columns=("last_checked_at",)))

    result, _ = run_cycle(db)

    assert "watch w0: last-checked:" in result["errors"]
    assert "column watches.last_checked_at does not exist" in result["errors"]
    assert FAKE_SERVICE_KEY not in result["errors"]
    assert "developer.mozilla.org" not in result["errors"]
    assert FAKE_SERVICE_KEY not in monitor.error_annotation(result)


def test_error_messages_stay_bounded():
    long = monitor.summarize_exception(ValueError("x " * 500))
    assert len(long) <= monitor.MAX_ERROR_MESSAGE_CHARS
    assert long.startswith("ValueError: x x")
    assert "\n" not in monitor.summarize_exception(ValueError("line\nbreak"))

    # many failing watches: the row lists a bounded sample plus a tally
    count = monitor.MAX_LOGGED_WATCH_ERRORS + 5
    db, _ = pool(count, fail_on=fails_watch_patch(*[f"w{i}" for i in range(count)]))
    result, _ = run_cycle(db)
    assert result["watch_errors"] == count
    listed = [line for line in result["errors"].split("; ") if line.startswith("watch w")]
    assert len(listed) == monitor.MAX_LOGGED_WATCH_ERRORS
    assert "and 5 more watch error(s)" in result["errors"]
