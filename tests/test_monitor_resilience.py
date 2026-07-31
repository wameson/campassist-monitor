"""Per-watch failure containment and the threshold-gated run status.

A single watch's rejected write once aborted the whole cycle for days: no
watch was polled, nobody was alerted, no run summary was written, and the
schedule still looked green. These tests pin down both halves of the fix —
one watch's failure stays contained (A), and breakage broad enough to be
systemic still fails the run loudly (B).
"""

import functools
import random
import time

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
    # the persisted, world-readable row is sanitized: a per-run ordinal, no UUID
    assert "watch #1: last-checked:" in result["errors"]
    assert "watch w1" not in result["errors"]
    # the operator-only annotation keeps the UUID + full reason
    assert "watch w1: last-checked:" in result["errors_detail"]
    annotation = monitor.error_annotation(result)
    assert annotation.startswith("::warning::") and "watch w1: last-checked:" in annotation
    assert monitor.failure_annotation(result) is None


def apns_failure(status=503, body=None):
    """What APNsClient records for a push APNs rejected: an HTTPStatusError
    carrying the response, with no mention of the watch it was pushing for."""
    request = httpx.Request("POST", "https://api.push.apple.com/3/device/devicetoken")
    response = httpx.Response(status, request=request, json=body or {"reason": "TooManyRequests"})
    return httpx.HTTPStatusError(f"apns push rejected with {status}", request=request, response=response)


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
    assert "watch #1: process:" in result["errors"]  # sanitized persisted row
    assert "watch w0: process:" in result["errors_detail"]  # full operator log
    assert monitor.exit_code(result) == 0


def test_transient_failure_does_not_error_the_watch():
    # a 503 is an outage, not a broken watch: it is recorded and retried next
    # cycle rather than parked in status='error' where a user must fix it
    db, _ = pool(4, fail_on=fails_watch_patch("w1", status=503))

    result, _ = run_cycle(db)

    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "monitoring"
    assert row["state_hash"] is None  # keeps the old hash, so it re-alerts
    assert not [c for c in db.calls_of("patch", "watches") if c[3].get("status") == "error"]
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
    # (that write is the one carrying the write-rejected reason)
    assert len([c for c in db.calls_of("patch", "watches") if "status" in c[3]]) == 1
    assert not [
        c for c in db.calls_of("patch", "watches")
        if c[3].get("error_reason") == monitor.ERROR_REASON_WRITE_REJECTED
    ]

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
    assert not [c for c in db.calls_of("patch", "watches") if c[3].get("status") == "error"]

    # recorded and counted, but isolated — the run stays green
    assert result["watch_errors"] == 1 and result["watches_checked"] == 4
    assert "watch #1: strike-count:" in result["errors"]
    assert "watch w1: strike-count:" in result["errors_detail"]
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
    assert "watch #1: strike-count:" in result["errors"]
    assert "watch w1: strike-count:" in result["errors_detail"]
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
    db, _ = pool(4, fail_on=fails_watch_patch("w0", "w1", "w2", "w3"))

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


def test_time_budget_skip_makes_the_run_systemic():
    # A cycle that runs out of poll budget mid-plan knowingly leaves parks
    # unpolled: their watches do not fire this cycle. That is a completed miss,
    # not a transient the next cycle heals, so the run must go red instead of
    # reporting itself healthy — the silent-green skip was a latent correctness
    # defect (fleet grows past one cycle's capacity, monitor quietly stops
    # polling cold parks, every run stays green). No watch is errored: an
    # overrun is a cycle condition, never a fault pinned to a watch's own row.
    clock = Clock()
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}", campground_id=f"c{i}") for i in range(3)]
    db = FakeDB({"watches": watches})

    result = monitor.run(
        db,
        FakeAPNs(),
        FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        rng=random.Random(0),
        sleep=lambda s: clock.tick(1000),  # the first inter-request delay blows the budget
        now_fn=lambda: NOW,
        monotonic=clock,
        time_budget_seconds=100,
    )

    # the skip count is surfaced like the errored-watch census, and the run is red
    assert result["polls_skipped"] == 2
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1
    # it reaches the operator failure channel, not just the world-readable warning
    assert "time budget exhausted" in result["cycle_errors"]
    assert monitor.failure_annotation(result) is not None
    assert "time budget exhausted" in result["errors"] and "skipped 2" in result["errors"]
    # a cycle condition never errors an individual watch's own row
    assert all(r["status"] == "monitoring" for r in db.tables["watches"])
    # end-of-cycle bookkeeping still ran, and no extra writes were added
    assert len(db.calls_of("insert", "run_summaries")) == 1


def test_all_polls_completing_stays_green():
    # The companion to the above: when the budget is not exhausted nothing is
    # skipped, polls_skipped is 0, and a fully-polled cycle stays green exactly
    # as before — the fix must not turn a healthy cycle red.
    clock = Clock()
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}", campground_id=f"c{i}") for i in range(3)]
    db = FakeDB({"watches": watches})

    result = monitor.run(
        db,
        FakeAPNs(),
        FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        rng=random.Random(0),
        sleep=lambda s: clock.tick(1),  # every poll fits inside the budget
        now_fn=lambda: NOW,
        monotonic=clock,
        time_budget_seconds=100,
    )

    assert result["polls_skipped"] == 0
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0
    assert "time budget exhausted" not in (result["errors"] or "")


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
    # the persisted row carries the sanitized aggregate (no UUID); the per-watch
    # error-mark detail is operator-only
    assert "error-mark: 1 of 1 watch(es) could not be moved" in result["errors"]
    assert "watch w1: error-mark:" not in result["errors"]
    assert "watch w1: error-mark:" in result["errors_detail"]
    assert "error-mark: 1 of 1 watch(es) could not be moved" in result["cycle_errors"]
    # the persisted verdict label matches the exit code: an error-mark failure
    # flips the run systemic, so the row must not read "(isolated)"
    assert "(systemic)" in result["errors"] and "(isolated)" not in result["errors"]
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
    # since the jitter came down to 20 s, a worst-case jitter + a fully spent
    # poll budget still reaches the fan-out with room left, where the 240 s
    # jitter used to arrive past the deadline with none
    assert (
        monitor.START_JITTER_MAX_SECONDS + monitor.CYCLE_TIME_BUDGET_SECONDS
        <= monitor.FANOUT_DEADLINE_SECONDS
    )


def test_worst_case_jitter_and_poll_phase_still_reaches_the_fanout():
    # the behavioural half of the arithmetic above, through run() rather than
    # through the constants: anchor process start at the worst case a cycle can
    # reach the fan-out with — the full start jitter, then a fully spent poll
    # budget — and w1's rejected row is still isolated per-id. At the 240 s
    # jitter this was, the same worst case arrived past FANOUT_DEADLINE_SECONDS
    # and the whole batch went down together.
    def worst_case(jitter):
        db, _ = pool(4, fail_on=fails_watch_patch("w1", columns=("last_checked_at",)))
        apns = FakeAPNs()
        result = monitor.run(
            db, apns, FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
            **QUIET,
            monotonic=Clock(jitter + monitor.CYCLE_TIME_BUDGET_SECONDS),
            process_started=0.0,
        )
        checked = [c for c in db.calls_of("patch", "watches") if "last_checked_at" in c[3]]
        rows = {r["id"]: r for r in db.tables["watches"]}
        return result, checked, rows

    result, checked, rows = worst_case(monitor.START_JITTER_MAX_SECONDS)
    assert len(checked) == 5  # the batch, then one per-id write per row
    assert rows["w1"]["status"] == "error"  # w1 alone, pinned to its own row
    assert all(rows[f"w{i}"]["last_checked_at"] is not None for i in (0, 2, 3))
    assert monitor.exit_code(result) == 0  # the other three were served: contained

    # what the old jitter bought at the same worst case: batch only, so the one
    # bad row is never identified and the other three go unchecked with it
    _, checked_at_240, rows_at_240 = worst_case(240.0)
    assert len(checked_at_240) == 1
    assert all(rows_at_240[f"w{i}"]["last_checked_at"] is None for i in range(4))


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
    # everything since process start — the preflight, the start jitter, the poll
    # phase — counts against the fan-out, so a run that got there late enough
    # gets no fan-out at all: reaching the summary row and the pruning matters
    # more than isolating one row. The 20 s jitter can no longer cause that on
    # its own (see test_fanout_deadline_fits_inside_the_job_timeout); a slow
    # Supabase or a stalled preflight still can, which is what this pins.
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

    # the sanitized persisted row still names the missing column (the useful
    # part), just without the watch UUID or PostgREST details/hint
    assert "watch #1: last-checked:" in result["errors"]
    assert "column watches.last_checked_at does not exist" in result["errors"]
    assert "watch w0" not in result["errors"]
    assert FAKE_SERVICE_KEY not in result["errors"]
    assert "developer.mozilla.org" not in result["errors"]
    # the operator-only channel keeps the full detail, still never the key
    assert "watch w0: last-checked:" in result["errors_detail"]
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
    listed = [line for line in result["errors"].split("; ") if line.startswith("watch #")]
    assert len(listed) == monitor.MAX_LOGGED_WATCH_ERRORS
    assert "and 5 more watch error(s)" in result["errors"]


def test_composed_error_line_respects_the_cap():
    # summarize_exception caps its own body, but the callers prepend a prefix
    # ("watch #N: context: "), so a long PostgREST message could push the whole
    # persisted line past the bound. The cap must hold for the composed line.
    long_message = "column watches." + "x" * 400 + " does not exist"

    def fail_on(call):
        if (
            call[0] == "patch" and call[1] == "watches"
            and "last_checked_at" in call[3]
            and "w0" in str(call[2]["id"])
        ):
            return postgrest_error(400, long_message)
        return None

    db, _ = pool(2, fail_on=fail_on)

    result, _ = run_cycle(db)

    persisted_lines = result["errors"].split("; ")
    detail_lines = result["errors_detail"].split("; ")
    assert any(line.startswith("watch #1: last-checked:") for line in persisted_lines)
    assert all(len(line) <= monitor.MAX_ERROR_MESSAGE_CHARS for line in persisted_lines)
    assert all(len(line) <= monitor.MAX_ERROR_MESSAGE_CHARS for line in detail_lines)


# --- Phase 2, finding 1: only a row-pinned failure errors the watch --------

@pytest.mark.parametrize("op", ["select", "upsert"])
def test_sent_alerts_failure_does_not_error_the_watch(op):
    # a sent_alerts schema drift surfaces inside the per-watch block (the
    # cooldown select or the dedup upsert), but it is table-scoped — not
    # evidence this user's watch row is broken — so the watch is recorded and
    # counted, yet never moved to status='error'. Only w1 has a matching opening
    # and so is the only watch that reaches sent_alerts at all.
    watches = [
        make_watch(id="w1", user_id="u1"),
        *[make_watch(id=f"w{i}", user_id=f"u{i}", site_ids=["absent"]) for i in (0, 2, 3)],
    ]
    db = FakeDB(
        {"watches": watches},
        fail_on=lambda call: (
            postgrest_error(400, "column sent_alerts.site_id does not exist")
            if call[0] == op and call[1] == "sent_alerts" else None
        ),
    )

    result, _ = run_cycle(db)

    w1 = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert w1["status"] == "monitoring"  # finding 1: not the watch's fault
    assert not [c for c in db.calls_of("patch", "watches") if c[3].get("status") == "error"]
    # the watch was polled — only table-scoped work failed — so it is still
    # stamped rather than left looking unchecked to its user
    assert w1["last_checked_at"] is not None
    if op == "select":
        # the cooldown is unknown, so the old hash is kept and the watch
        # re-evaluates next cycle
        assert w1["state_hash"] is None
    # but the failure is still recorded and counted (isolated rate -> green)
    assert result["watch_errors"] == 1
    assert "watch #1: alert:" in result["errors"]
    assert "watch w1: alert:" in result["errors_detail"]
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0
    # the three watches with no opening were unaffected
    assert all(
        r["status"] == "monitoring" for r in db.tables["watches"] if r["id"] != "w1"
    )


def test_dedup_write_failure_does_not_republish_the_alert_every_cycle():
    # the push landed but the sent_alerts dedup row was rejected. With neither a
    # dedup row nor a stored state_hash, filter_unalerted would find nothing to
    # suppress and the identical push would go out again every cycle until the
    # drift is fixed — so the state_hash write still runs. The watch keeps
    # monitoring (a sent_alerts drift is not its fault) and simply does not
    # re-alert these openings.
    db = FakeDB(
        {"watches": [make_watch(id="w1", user_id="u1")]},
        fail_on=lambda call: (
            postgrest_error(400, "column sent_alerts.site_id does not exist")
            if call[0] == "upsert" and call[1] == "sent_alerts" else None
        ),
    )

    result, apns = run_cycle(db)

    assert len(apns.alerts) == 1 and result["alerts_sent"] == 1  # the push went out
    assert db.tables["sent_alerts"] == []  # ...and nothing recorded that it did
    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "monitoring"
    assert row["state_hash"] is not None and row["last_found_at"] is not None
    assert "watch #1: alert:" in result["errors"]
    assert monitor.exit_code(result) == 0

    # next cycle: same openings, still no dedup row, and no second push
    _, apns_next = run_cycle(db)
    assert apns_next.alerts == []


def test_permanent_apns_rejection_is_reported_outside_the_rate():
    # a single non-410 4xx such as BadDeviceToken is one user's dead device
    # token, not an operational fault. It is still recorded and surfaced, but
    # counting it toward the systemic rate would turn a whole scheduled run red
    # over something no operator can fix — and one dead token in an otherwise
    # healthy pool does not trip the pool-wide backstop either.
    db, _ = pool(4)
    apns = FakeAPNs(
        responder=lambda w: (
            (monitor.PERMANENT_FAILURE, apns_failure(400, {"reason": "BadDeviceToken"}))
            if w["id"] == "w0"
            else (monitor.DELIVERED, None)
        )
    )

    result, _ = run_cycle(db, apns=apns)

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert result["watch_errors"] == 1  # reported...
    assert "watch #1: alert:" in persisted and "400" in persisted
    assert "BadDeviceToken" not in persisted  # sanitized like every other path
    assert "BadDeviceToken" in monitor.error_annotation(result)
    # ...but out of the rate, so the schedule stays green
    assert "0 of 4 served watch(es) failed this cycle (isolated)" in persisted
    assert "plus 1 outside the rate" in persisted
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0
    # a push given up on still advances the hash: retrying it is futile
    assert all(r["state_hash"] is not None for r in db.tables["watches"])


def test_pool_wide_config_fault_exits_nonzero():
    # every push 403s on an expired provider token: an operator-fixable,
    # pool-wide fault delivering zero alerts. Unlike a dead device token it is
    # rated, so a signing-key outage turns the run red instead of green.
    db, _ = pool(4)
    apns = FakeAPNs(
        result=monitor.CONFIG_FAILURE,
        failure=apns_failure(403, {"reason": "ExpiredProviderToken"}),
    )

    result, _ = run_cycle(db, apns=apns)

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert result["watch_errors"] == 4
    assert "4 of 4 served watch(es) failed this cycle (systemic)" in persisted
    assert "403" in persisted
    assert "ExpiredProviderToken" not in persisted  # sanitized like every path
    assert "ExpiredProviderToken" in monitor.error_annotation(result)
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1
    # a config fault keeps the old hash so the alert retries once the key is
    # rotated, and never errors a watch (no user's row is broken)
    rows = db.tables["watches"]
    assert all(r["state_hash"] is None for r in rows)
    assert all(r["status"] == "monitoring" for r in rows)


def test_pool_wide_apns_wipeout_trips_the_backstop():
    # even a per-device reason we deliberately leave unrated (BadDeviceToken)
    # cannot yield a silent green outage when it wipes out nearly every served
    # push: the pool-wide backstop fires regardless of per-reason rating, so an
    # unenumerated 4xx that silences the whole pool still exits non-zero.
    db, _ = pool(4)
    apns = FakeAPNs(
        result=monitor.PERMANENT_FAILURE,
        failure=apns_failure(400, {"reason": "BadDeviceToken"}),
    )

    result, _ = run_cycle(db, apns=apns)

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert result["watch_errors"] == 4
    # no failure is rated, yet the backstop makes the run systemic
    assert "0 of 4 served watch(es) failed this cycle (systemic)" in persisted
    assert "plus 4 outside the rate" in persisted
    assert "BadDeviceToken" not in persisted  # still sanitized
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1


def test_state_hash_write_failure_still_errors_the_watch():
    # the counterpart: a failure of the one write pinned to the watch's own row
    # (its state_hash PATCH) is isolated, so a permanent rejection still surfaces
    # the watch as broken — finding 1 narrows attribution, it does not remove it
    db, _ = pool(4, fail_on=fails_watch_patch("w1", columns=("state_hash",)))

    result, _ = run_cycle(db)

    row = next(r for r in db.tables["watches"] if r["id"] == "w1")
    assert row["status"] == "error"
    assert "watch #1: process:" in result["errors"]
    assert monitor.exit_code(result) == 0


# --- Phase 2, finding 2: persisted label matches the exit code -------------

def test_prune_failure_makes_the_persisted_label_systemic():
    # one isolated watch failure alone is green, but a retention-prune failure
    # (owned by no watch) flips the run systemic. Pruning now runs before the
    # summary INSERT, so the persisted row is labelled to match the exit code —
    # never "(isolated)" on a run that exits 1.
    def fail_on(call):
        if call[0] == "patch" and call[1] == "watches" and "last_checked_at" in call[3]:
            ids = set(str(call[2]["id"]).partition(".")[2].strip("()").split(","))
            if "w1" in ids:
                return postgrest_error(400, "column watches.last_checked_at does not exist")
        if call[0] == "delete" and call[1] == "sent_alerts":
            return postgrest_error(400, "relation sent_alerts does not exist")
        return None

    db, _ = pool(4, fail_on=fail_on)

    result, _ = run_cycle(db)

    assert result["watch_errors"] == 1  # 1 of 4: isolated on its own
    assert "sent_alerts prune" in result["cycle_errors"]  # but a cycle failure
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1
    # the persisted row's verdict label matches the exit code
    assert "(systemic)" in result["errors"] and "(isolated)" not in result["errors"]
    assert "(systemic)" in db.tables["run_summaries"][-1]["errors"]
    # the summary row and both prunes were still attempted before going red
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2


# --- Phase 2, finding 3: run_summaries.errors is sanitized -----------------

def test_summarize_exception_safe_drops_details_and_hint():
    exc = postgrest_error(
        400,
        "duplicate key value violates unique constraint \"sent_alerts_key\"",
        details="Key (watch_id, site_id, date)=(SECRET-UUID-1234, 100, 2026-08-10) already exists.",
        hint="some internal hint",
    )
    full = monitor.summarize_exception(exc)
    assert "duplicate key value" in full
    assert "SECRET-UUID-1234" in full and "some internal hint" in full

    safe = monitor.summarize_exception(exc, safe=True)
    assert "400" in safe and "duplicate key value" in safe  # status + column kept
    assert "SECRET-UUID-1234" not in safe  # details (echoed key values) dropped
    assert "some internal hint" not in safe  # hint dropped
    assert "Key (watch_id" not in safe


def test_persisted_row_omits_uuids_and_key_values_the_annotation_keeps():
    # run_summaries is world-readable (RLS `USING (true)`); a constraint
    # violation's `details` echoes another user's key values, so the persisted
    # row must drop the watch UUID, details and hint, while the operator-only
    # Action annotation keeps them.
    secret_details = (
        "Key (watch_id, site_id, date)=(SECRET-UUID-1234, 100, 2026-08-10) already exists."
    )

    def fail_on(call):
        if call[0] == "patch" and call[1] == "watches" and "last_checked_at" in call[3]:
            ids = set(str(call[2]["id"]).partition(".")[2].strip("()").split(","))
            if "w0" in ids:
                return postgrest_error(
                    400,
                    "duplicate key value violates unique constraint",
                    details=secret_details,
                    hint="internal hint",
                )
        return None

    db, _ = pool(2, fail_on=fail_on)

    result, _ = run_cycle(db)

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert persisted == result["errors"]  # what every app client can read
    for secret in ("SECRET-UUID-1234", "watch w0", secret_details, "internal hint"):
        assert secret not in persisted
    # still enough to act on: the ordinal and the constraint that was violated
    assert "watch #1: last-checked:" in persisted
    assert "duplicate key value violates unique constraint" in persisted

    # the operator-only annotation carries the full detail
    annotation = monitor.error_annotation(result)
    assert "watch w0" in annotation
    assert "SECRET-UUID-1234" in annotation and "internal hint" in annotation


# --- Phase 2, test-clock fix: fan-out deadline on the injected clock --------

def test_fanout_deadline_can_anchor_to_this_runs_own_clock():
    # process_started=None asks for the anchor a caller on a clock of its own
    # needs: the deadline must live on the injected clock, not on the module's
    # real-time PROCESS_STARTED default, or the cap is an instant on a different
    # timeline the fake clock never reaches and is silently disabled. Allowance
    # is left huge so only the deadline can bind.
    clock = Clock()  # starts at 0, the same reading run() takes as `started`
    db, _ = pool(4, fail_on=slow_rejections(clock, seconds=40, columns=("last_checked_at",)))

    result = monitor.run(
        db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        **QUIET,
        monotonic=clock,
        process_started=None,  # anchor to this run's own reading of that clock
        per_id_fallback_budget_seconds=10_000,
        fanout_deadline_seconds=90,
    )

    checked = [c for c in db.calls_of("patch", "watches") if "last_checked_at" in c[3]]
    # batch + two per-id writes (t=40, t=80), then t=120 >= 90 cuts it off. A
    # deadline anchored to the real clock would never bind and all four would run.
    assert len(checked) == 3
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert monitor.exit_code(result) == 1


def test_default_fanout_anchor_holds_for_a_wrapped_real_clock(monkeypatch):
    # the anchor is the caller's to choose, never inferred from whether the
    # clock object *is* time.monotonic: a caller that wraps or instruments the
    # real clock is still on the process's own timeline, so it must keep the
    # process-start anchor that makes the start jitter count against the fan-out
    # instead of silently getting a fresh deadline from run() entry.
    real, seen = monitor.FanoutBudget, {}

    def recording(allowance, deadline, monotonic):
        seen["deadline"] = deadline
        return real(allowance, deadline, monotonic)

    monkeypatch.setattr(monitor, "FanoutBudget", recording)
    db, _ = pool(1)

    monitor.run(
        db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(200, OPEN_PAYLOAD)),
        **QUIET,
        monotonic=functools.partial(time.monotonic),
    )

    assert seen["deadline"] == monitor.PROCESS_STARTED + monitor.FANOUT_DEADLINE_SECONDS


# --- Phase 2 follow-up: the sanitized row holds for every failure path ------

@pytest.mark.parametrize(
    "failure, kept, operator_only",
    [
        (apns_failure(429), "429", "TooManyRequests"),
        (apns_failure(503), "503", "TooManyRequests"),
        (
            httpx.ConnectError("connection refused by 17.188.1.1"),
            "ConnectError",
            "17.188.1.1",
        ),
    ],
)
def test_apns_delivery_failure_never_persists_a_watch_uuid(failure, kept, operator_only):
    # an undelivered push is recorded like any other unattributed per-watch
    # failure: counted, ordinalized in the world-readable row, and full only in
    # the operator annotation. It must never write a watch UUID into
    # run_summaries.errors, which every app client can read.
    db, watches = pool(4)
    apns = FakeAPNs(result=monitor.RETRYABLE_FAILURE, failure=failure)

    result, _ = run_cycle(db, apns=apns)

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert persisted == result["errors"]
    for watch in watches:
        assert watch["id"] not in persisted
    assert FAKE_SERVICE_KEY not in persisted
    assert operator_only not in persisted
    assert "watch #1: alert:" in persisted and kept in persisted

    # the operator-only channel keeps the UUID and the full reason
    assert "watch w0: alert:" in result["errors_detail"]
    assert operator_only in monitor.error_annotation(result)

    # counted against the served set, but no watch is blamed for an APNs outage
    assert result["watch_errors"] == 4
    rows = db.tables["watches"]
    assert all(r["status"] == "monitoring" for r in rows)
    # a retryable push keeps the old hash and still stamps last_checked_at:
    # the watch was polled, only the delivery failed
    assert all(r["state_hash"] is None for r in rows)
    assert all(r["last_checked_at"] is not None for r in rows)
    # four of four served watches failed: an APNs outage stays loud
    assert monitor.exit_code(result) == 1


def test_prune_only_failure_persists_a_sanitized_systemic_row():
    # a healthy cycle whose only failure is a rejected retention prune exits 1.
    # The row it leaves behind must say so — a NULL errors column on a red run
    # is the "looked green" hole this work closes.
    secret_details = "Key (id)=(SECRET-UUID-1234) is still referenced."

    def fail_on(call):
        if call[0] == "delete" and call[1] == "sent_alerts":
            return postgrest_error(
                400,
                "relation sent_alerts does not exist",
                details=secret_details,
                hint="internal hint",
            )
        return None

    db, _ = pool(4, fail_on=fail_on)

    result, _ = run_cycle(db)

    assert result["watch_errors"] == 0  # no watch is to blame
    assert result["systemic_failure"] is True and monitor.exit_code(result) == 1

    persisted = db.tables["run_summaries"][-1]["errors"]
    assert persisted is not None and persisted == result["errors"]
    assert "0 of 4 served watch(es) failed this cycle (systemic)" in persisted
    assert "sent_alerts prune:" in persisted
    assert "relation sent_alerts does not exist" in persisted  # still actionable
    for secret in ("SECRET-UUID-1234", secret_details, "internal hint", FAKE_SERVICE_KEY):
        assert secret not in persisted

    # the operator annotation keeps the full PostgREST reason
    annotation = monitor.failure_annotation(result)
    assert "SECRET-UUID-1234" in annotation and "internal hint" in annotation


def test_summarize_exception_safe_drops_a_non_http_message():
    # an exception with no response to read carries whatever message its raiser
    # built — often the row value that upset it — so the persisted rendering
    # keeps the type name alone
    exc = ValueError("invalid date '2026-13-01' for watch SECRET-UUID-1234")

    full = monitor.summarize_exception(exc)
    assert full == "ValueError: invalid date '2026-13-01' for watch SECRET-UUID-1234"

    assert monitor.summarize_exception(exc, safe=True) == "ValueError"


def test_error_mark_aggregate_is_annotated_once():
    # the aggregate is built once and travels one channel per audience: the
    # operator ::error:: line joins errors_detail and cycle_errors, so a copy in
    # both would print the same sentence twice
    def fail_on(call):
        if call[0] != "patch" or call[1] != "watches":
            return None
        ids = set(str(call[2]["id"]).partition(".")[2].strip("()").split(","))
        if "w1" in ids and set(call[3]) & {"state_hash", "status"}:
            return postgrest_error(400)
        return None

    db, _ = pool(4, fail_on=fail_on)

    result, _ = run_cycle(db)

    sentence = "error-mark: 1 of 1 watch(es) could not be moved to status='error'"
    assert monitor.failure_annotation(result).count(sentence) == 1
    assert result["errors"].count(sentence) == 1  # still in the persisted row
    assert sentence in result["cycle_errors"]  # and still drives the verdict
    assert result["systemic_failure"] is True
