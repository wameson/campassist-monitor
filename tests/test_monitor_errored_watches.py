"""The errored-watch population: reported every cycle, with a recorded reason.

`status='error'` is terminal — an errored watch is absent from the only query
the cycle runs and no code path writes the status back — so a cycle serving one
watch of four used to read exactly like a clean cycle serving all four. Three of
five watches once sat dead for two days behind a wall of green runs with
`errors: null`, and nothing anywhere recorded *why* they had been errored.

Two guarantees are pinned here: every cycle reports how many watches are errored
(count on the world-readable row, per-reason breakdown operator-only), and every
one of the four error sites records a machine-readable reason on the watch —
without letting a live database that has not had 0004 applied yet cost the cycle
anything.
"""

import random

import pytest

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeGTCHTTP,
    FakeHTTP,
    FakeResponse,
    availability_payload,
    make_gtc_watch,
    make_watch,
    postgrest_error,
)

QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)
OPEN_PAYLOAD = availability_payload({"100": {"2026-08-10": "Available"}})
QUIET_PAYLOAD = availability_payload({"100": {"2026-08-10": "Reserved"}})


def errored_watch(watch_id, reason, **overrides):
    return make_watch(id=watch_id, status="error", error_reason=reason, **overrides)


def run_cycle(db, payload=OPEN_PAYLOAD):
    apns = FakeAPNs()
    return monitor.run(db, apns, FakeHTTP(lambda cg: FakeResponse(200, payload)), **QUIET), apns


# --- the census -----------------------------------------------------------

def test_the_errored_population_is_reported_every_cycle():
    # the incident's after-the-fact half: two dead watches, one healthy one, and
    # a run that used to say nothing at all
    db = FakeDB({"watches": [
        make_watch(id="w-ok", user_id="u1"),
        errored_watch("w-dead", monitor.ERROR_REASON_INVALID_CAMPGROUND_ID, user_id="u2"),
        errored_watch("w-gone", monitor.ERROR_REASON_CAMPGROUND_NOT_FOUND, user_id="u3"),
    ]})

    result, apns = run_cycle(db)

    assert result["watches_errored"] == 2
    # the count reaches the world-readable row, so silence and health stop
    # looking the same from the app
    assert "2 watch(es) in status='error', not monitored" in result["errors"]
    assert db.tables["run_summaries"][-1]["errors"] == result["errors"]
    # the per-reason breakdown is operator-only
    breakdown = "errored watches by reason: "
    assert breakdown not in (result["errors"] or "")
    assert (
        breakdown
        + f"{monitor.ERROR_REASON_CAMPGROUND_NOT_FOUND}: 1, "
        + f"{monitor.ERROR_REASON_INVALID_CAMPGROUND_ID}: 1"
    ) in result["errors_detail"]

    # a census, not a verdict: the healthy watch is served and the run stays
    # green — an errored watch is a standing fact, not this cycle's failure
    assert [watch_id for watch_id, _ in apns.alerts] == ["w-ok"]
    assert result["watch_errors"] == 0 and result["systemic_failure"] is False
    assert monitor.exit_code(result) == 0

    # and it costs one select and no writes: the errored rows are never touched
    assert db.calls_of("select", "watches")[1][2] == {"status": "eq.error"}
    assert not [c for c in db.calls_of("patch", "watches") if "w-dead" in str(c[2])]


def test_a_clean_pool_reports_an_empty_population():
    db = FakeDB({"watches": [make_watch()]})

    result, _ = run_cycle(db, QUIET_PAYLOAD)

    assert result["watches_errored"] == 0
    assert result["errors"] is None  # nothing errored: nothing to say


def test_a_row_errored_before_the_column_existed_counts_as_unrecorded():
    # 0004 is backfill-free by design, so rows errored by an older build carry
    # no reason. The census must report that honestly rather than guess a cause.
    db = FakeDB({"watches": [make_watch(id="w-old", status="error")]})

    result, _ = run_cycle(db)

    assert result["watches_errored"] == 1
    assert f"{monitor.ERROR_REASON_UNRECORDED}: 1" in result["errors_detail"]


def test_the_census_never_republishes_a_client_written_value():
    # `watches` is a table its owner can write, so both the campground_id and
    # the error_reason on an errored row are user-supplied text. Neither channel
    # may echo either: the row gets a count, the operator gets a bucket.
    db = FakeDB({"watches": [
        errored_watch(
            "w-bad",
            "look-at-me' OR 1=1 --",
            campground_id="hostile-value-4b1c",
        ),
    ]})

    result, _ = run_cycle(db)

    both = f"{result['errors']} {result['errors_detail']}"
    assert "look-at-me" not in both
    assert "hostile-value-4b1c" not in both
    assert f"{monitor.ERROR_REASON_OTHER}: 1" in result["errors_detail"]


def test_a_failed_census_does_not_cost_the_cycle():
    # the census is visibility, never a dependency: if the extra select is
    # rejected the watches are still polled, alerted and stamped
    def fail_on(call):
        if call[0] == "select" and call[1] == "watches" and call[2] == {"status": "eq.error"}:
            return postgrest_error(503, "upstream connect error")
        return None

    db = FakeDB({"watches": [make_watch()]}, fail_on=fail_on)

    result, apns = run_cycle(db)

    assert [watch_id for watch_id, _ in apns.alerts] == ["w1"]
    assert result["watches_errored"] is None
    assert "errored-watch census unavailable" in result["errors"]
    assert "errored-watch census: HTTPStatusError: 503" in result["errors_detail"]
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0


def test_the_census_costs_the_write_budget_nothing():
    # the no-change budget is ≤5 writes whatever else is in the table
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(20)]
    errored = [
        errored_watch(f"e{i}", monitor.ERROR_REASON_WRITE_REJECTED, user_id=f"v{i}")
        for i in range(20)
    ]
    db = FakeDB({"watches": watches + errored})
    run_cycle(db, QUIET_PAYLOAD)  # seeds state_hash

    db.calls.clear()
    result, apns = run_cycle(db, QUIET_PAYLOAD)

    assert apns.alerts == [] and db.write_count <= 5
    assert len(db.calls_of("patch")) == 1  # the batched last_checked_at write alone
    assert result["watches_errored"] == 20


# --- the recorded reason, one test per error site -------------------------

def test_a_malformed_campground_id_records_its_reason():
    db = FakeDB({"watches": [make_watch(id="w-bad", campground_id="gtc:-2147483625")]})

    run_cycle(db)

    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["error_reason"] == monitor.ERROR_REASON_INVALID_CAMPGROUND_ID


def test_an_unreadable_provider_ref_records_its_reason():
    db = FakeDB({"watches": [
        make_gtc_watch(id="w-bad", provider_ref={"resource_location_id": -2147483647}),
    ]})

    monitor.run(db, FakeAPNs(), FakeGTCHTTP(lambda map_id: FakeResponse(404)), **QUIET)

    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["error_reason"] == monitor.ERROR_REASON_UNREADABLE_PROVIDER_REF


def test_a_persistently_404ing_campground_records_its_reason():
    # the one genuinely permanent cause, and the one that must stay terminal
    db = FakeDB({"watches": [
        make_watch(id="w-404", consecutive_not_found=monitor.NOT_FOUND_ERROR_THRESHOLD - 1),
    ]})

    monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)

    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["error_reason"] == monitor.ERROR_REASON_CAMPGROUND_NOT_FOUND
    assert row["consecutive_not_found"] == monitor.NOT_FOUND_ERROR_THRESHOLD


def test_a_rejected_watch_write_records_its_reason():
    # the retry-worthy one: this watch's own state_hash write was rejected the
    # way a missing column rejects, which recovers the moment it is applied
    def fail_on(call):
        if call[0] == "patch" and call[1] == "watches" and "state_hash" in call[3]:
            return postgrest_error(400, "column watches.state_hash does not exist")
        return None

    db = FakeDB({"watches": [make_watch(id="w1")]}, fail_on=fail_on)

    result, _ = run_cycle(db)

    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["error_reason"] == monitor.ERROR_REASON_WRITE_REJECTED
    assert result["watch_errors"] == 1


# --- WARN-class tolerance: 0004 not applied yet ---------------------------

def rejects_the_reason_column(call):
    """PostgREST's answer to a write naming a column the live DB does not have —
    exactly what an unapplied 0004 does to the status='error' write."""
    if call[0] == "patch" and call[1] == "watches" and "error_reason" in call[3]:
        return postgrest_error(400, "column watches.error_reason does not exist")
    return None


def test_a_missing_error_reason_column_still_errors_the_watch():
    # WARN, and it has to be earned: an unapplied migration must not stop the
    # lifecycle, or it would park a watch in the exact failing-forever state the
    # column exists to make visible
    db = FakeDB(
        {"watches": [
            make_watch(id="w-bad", campground_id="gtc:-2147483625"),
            make_watch(id="w-ok", user_id="u2", campground_id="222"),
        ]},
        fail_on=rejects_the_reason_column,
    )

    result, apns = run_cycle(db)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-bad"]["status"] == "error"  # errored anyway
    assert "error_reason" not in rows["w-bad"]  # just not annotated
    # the rest of the cycle is untouched: the healthy watch is served and the
    # run stays green, with nothing recorded against the errored watch
    assert [watch_id for watch_id, _ in apns.alerts] == ["w-ok"]
    assert result["watch_errors"] == 0
    assert result["systemic_failure"] is False and monitor.exit_code(result) == 0
    assert len(db.calls_of("insert", "run_summaries")) == 1


def test_the_reason_column_is_dropped_for_the_rest_of_a_drifted_cycle():
    # one rejection is enough to learn the column is gone: the second error site
    # of the same cycle writes without it rather than paying another round trip
    db = FakeDB(
        {"watches": [
            make_watch(id="w-bad", campground_id="gtc:-2147483625"),
            make_watch(
                id="w-404",
                user_id="u2",
                campground_id="222",
                consecutive_not_found=monitor.NOT_FOUND_ERROR_THRESHOLD - 1,
            ),
        ]},
        fail_on=rejects_the_reason_column,
    )

    monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-bad"]["status"] == "error" and rows["w-404"]["status"] == "error"
    attempted = [c for c in db.calls_of("patch", "watches") if "error_reason" in c[3]]
    assert len(attempted) == 1


@pytest.mark.parametrize(
    "exc",
    [
        postgrest_error(400, "null value in column \"status\" violates not-null constraint"),
        postgrest_error(503, "upstream connect error"),
    ],
    ids=["names-another-column", "transient"],
)
def test_only_a_missing_column_is_retried_without_the_reason(exc):
    # a rejection that does not name *this* column — a constraint violation, a
    # Supabase blip — would fail identically the second time, so it is recorded
    # as-is rather than costing the cycle a doomed extra write
    db = FakeDB(
        {"watches": [make_watch(id="w-bad", campground_id="gtc:-2147483625")]},
        fail_on=lambda call: (
            exc if call[0] == "patch" and call[1] == "watches" else None
        ),
    )

    result, _ = run_cycle(db)

    assert len(db.calls_of("patch", "watches")) == 1
    assert db.tables["watches"][0]["status"] == "monitoring"  # the write never landed
    assert result["watch_errors"] == 1  # recorded, not swallowed


def test_rejects_missing_column_reads_only_the_response_body():
    # the discriminator itself: a 42703 naming this column, and nothing else
    column = monitor.ERROR_REASON_COLUMN
    assert monitor.rejects_missing_column(
        postgrest_error(400, f"column watches.{column} does not exist"), column
    )
    # a different column's 42703 is not this drift
    assert not monitor.rejects_missing_column(
        postgrest_error(400, "column watches.state_hash does not exist"), column
    )
    # and a schema code on a transient status proves nothing
    assert not monitor.rejects_missing_column(
        postgrest_error(503, f"column watches.{column} does not exist"), column
    )
    assert not monitor.rejects_missing_column(ValueError("no response to read"), column)
