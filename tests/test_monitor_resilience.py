"""Per-watch failure containment and the threshold-gated run status.

A single watch's rejected write once aborted the whole cycle for days: no
watch was polled, nobody was alerted, no run summary was written, and the
schedule still looked green. These tests pin down both halves of the fix —
one watch's failure stays contained (A), and breakage broad enough to be
systemic still fails the run loudly (B).
"""

import random

import pytest

import monitor
from helpers import (
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
    assert "1 of 4 watch(es) failed this cycle (isolated)" in result["errors"]
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
    assert "4 of 4 watch(es) failed this cycle (systemic)" in result["errors"]

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
    assert monitor.patch_watches(db, [], {"status": "expired"}) == {}
    assert db.calls == []

    assert monitor.patch_watches(db, ["w0", "w1"], {"status": "paused"}) == {}
    assert [c[2]["id"] for c in db.calls_of("patch")] == ["in.(w0,w1)"]

    db.fail_on = fails_watch_patch("w1", columns=("state_hash",))
    db.calls.clear()
    failures = monitor.patch_watches(db, ["w0", "w1", "w2"], {"state_hash": "h"})
    assert set(failures) == {"w1"}
    assert monitor.is_permanent_failure(failures["w1"]) is True
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w0"]["state_hash"] == "h" and rows["w2"]["state_hash"] == "h"
    assert rows["w1"]["state_hash"] is None

    # a single id is not retried: the batch write was already that one write
    db.calls.clear()
    failures = monitor.patch_watches(db, ["w1"], {"state_hash": "h"})
    assert set(failures) == {"w1"} and len(db.calls_of("patch")) == 1


def test_large_failing_batch_does_not_fan_out():
    # a batch too big to isolate is systemic anyway; retrying it row by row
    # would spend hundreds of writes to learn the same thing
    ids = [f"w{i}" for i in range(monitor.PER_ID_FALLBACK_MAX + 1)]
    db = FakeDB(fail_on=lambda call: postgrest_error(400))

    failures = monitor.patch_watches(db, ids, {"last_checked_at": "t"})

    assert set(failures) == set(ids)
    assert len(db.calls_of("patch")) == 1


def test_permanent_vs_transient_classification():
    assert monitor.is_permanent_failure(postgrest_error(400)) is True
    assert monitor.is_permanent_failure(postgrest_error(404)) is True
    assert monitor.is_permanent_failure(postgrest_error(429)) is False
    assert monitor.is_permanent_failure(postgrest_error(503)) is False
    assert monitor.is_permanent_failure(TimeoutError("read timeout")) is False


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
