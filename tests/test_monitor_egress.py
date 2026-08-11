"""The per-cycle `watches` read-reduction (egress).

The dominant Supabase egress term was one `watches` row per monitoring watch,
every cycle, growing linearly with the fleet. The reduction still polls every
unit (a quiet unit is polled so an opening on it is noticed) but reads back the
WATCH rows only where a change, edit, strike, or expiry can matter. These tests
pin down the two things that must both hold: a quiet cycle reads (almost) no
watch rows, AND every opening — including one a watch was EDITED into on a unit
whose availability never changed — is still detected and alerted.
"""

import random

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
RESERVED = availability_payload({"100": {"2026-08-10": "Reserved"}})
OPEN = availability_payload({"100": {"2026-08-10": "Available"}})


def run_cycle(db, payload, apns=None):
    apns = apns or FakeAPNs()
    result = monitor.run(db, apns, FakeHTTP(lambda cg: FakeResponse(200, payload)), **QUIET)
    return result, apns


def watch_rows_read(db) -> int:
    """The number of `watches` rows this cycle actually pulled over the wire —
    the egress term the reduction targets. Excludes the tiny monitoring_plan and
    poll_units reads, which are sublinear in the fleet by construction."""
    return sum(n for table, n in db.reads if table == "watches")


def seed_and_arm(db, payload):
    """Run the seeding cycle, then clear the recorders so the next cycle's reads
    and writes are measured on their own."""
    run_cycle(db, payload)
    db.calls.clear()
    db.reads.clear()


def test_quiet_cycle_reads_no_watch_rows():
    # 100 campers on one shared campground+month. After seeding, an unchanged
    # unit means an unchanged state for every one of them, so a quiet cycle must
    # read back ZERO watch rows — the whole point of the reduction — while still
    # polling the unit and stamping last_checked.
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(100)]
    db = FakeDB({"watches": watches})

    seed_and_arm(db, RESERVED)
    summary, apns = run_cycle(db, RESERVED)

    assert watch_rows_read(db) == 0            # no per-watch egress on a quiet cycle
    assert apns.alerts == []
    assert summary["watches_checked"] == 100   # every watch is still monitored/stamped
    assert summary["campgrounds_polled"] == 1  # and the unit was still polled
    # the plan came from the deduped view, not a full watches read
    assert any(t == "monitoring_plan" for t, _ in db.reads)


def test_quiet_cycle_egress_is_flat_as_the_fleet_grows():
    # the structural claim: rows read on a quiet cycle does not grow with the
    # number of watches on a shared unit. 10 campers and 500 campers both read 0.
    def quiet_rows(n):
        db = FakeDB({"watches": [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(n)]})
        seed_and_arm(db, RESERVED)
        run_cycle(db, RESERVED)
        return watch_rows_read(db)

    assert quiet_rows(10) == 0
    assert quiet_rows(500) == 0


def test_opening_is_detected_and_alerted_after_the_reduction():
    # correctness beats savings: when the unit's availability actually changes,
    # its rows ARE read back and the opening is alerted, exactly as before.
    db = FakeDB({
        "watches": [make_watch(id="w1")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    seed_and_arm(db, RESERVED)  # nothing open yet, unit hash stored

    summary, apns = run_cycle(db, OPEN)  # the site opens: the unit changes

    assert [w for w, _ in apns.alerts] == ["w1"]
    assert summary["alerts_sent"] == 1
    assert watch_rows_read(db) >= 1  # the changed unit's rows were read back


def test_edited_watch_on_an_unchanged_unit_still_alerts():
    # the correctness hole the captain flagged: an unchanged UNIT does not imply
    # an unchanged WATCH. Site 100 is open and STAYS open (the unit never
    # changes). A watch that wanted a different site is edited to accept any site
    # — on the unchanged unit — and must still be read (via the updated_at
    # watermark) and alerted, not skipped until the availability happens to move.
    db = FakeDB({
        "watches": [make_watch(id="w1", site_ids=["999"])],  # wants a site that is not open
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    seed_and_arm(db, OPEN)  # site 100 open, but w1 wants 999 -> no match, hash seeded

    # the user edits the watch to accept any site; the trigger would bump
    # updated_at, so model that here. The unit's availability is byte-identical.
    db.tables["watches"][0]["site_ids"] = []
    db.tables["watches"][0]["updated_at"] = monitor.iso_now(NOW)

    summary, apns = run_cycle(db, OPEN)

    # the unit did NOT change (no poll_units upsert), yet the edit was caught and
    # the already-open site was alerted — the hole is closed.
    assert not db.calls_of("upsert", "poll_units")
    assert [w for w, _ in apns.alerts] == ["w1"]
    assert summary["alerts_sent"] == 1


def test_unedited_watch_on_an_unchanged_unit_is_not_re_read():
    # the flip side, so the edit test above is not vacuous: with no edit and no
    # availability change, the watch is NOT re-read and NOT re-alerted.
    db = FakeDB({
        "watches": [make_watch(id="w1")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    run_cycle(db, OPEN)  # cycle 1: opening alerted once, hash + unit stored
    db.calls.clear()
    db.reads.clear()

    summary, apns = run_cycle(db, OPEN)  # cycle 2: nothing changed, nobody edited

    assert watch_rows_read(db) == 0
    assert apns.alerts == []


def test_falls_back_to_the_full_read_before_the_migrations_are_applied():
    # pre-migration correctness: with the monitoring_plan view and poll_units
    # table absent (unapplied 0010/0011), the cycle must behave exactly as before
    # the reduction — plan from a full watches read and detect the opening.
    def fail_on(call):
        if call[0] == "select" and call[1] in ("monitoring_plan", "poll_units"):
            return postgrest_error(404, "relation does not exist", code="42P01")
        return None

    db = FakeDB({
        "watches": [make_watch(id="w1")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    }, fail_on=fail_on)

    # cycle 1 seeds the hash via the full-read path; cycle 2 sees the opening
    run_cycle(db, RESERVED)
    summary, apns = run_cycle(db, OPEN)

    assert [w for w, _ in apns.alerts] == ["w1"]
    assert summary["alerts_sent"] == 1
    assert monitor.exit_code(summary) == 0  # a missing optimization object never reddens


def test_invalid_campground_created_after_seeding_is_still_errored():
    # invalid/unpollable watches are not scoped into the process read (their id
    # can carry URL-unsafe characters and is never needed there); instead the
    # updated_at watermark reads a newly written one back and errors it, so a
    # malformed watch is never left silently 'monitoring'.
    db = FakeDB({"watches": [make_watch(id="w1")]})
    seed_and_arm(db, RESERVED)

    db.tables["watches"].append(
        make_watch(id="w2", user_id="u2", campground_id="a/b", updated_at=monitor.iso_now(NOW))
    )
    summary, _ = run_cycle(db, RESERVED)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w2"]["status"] == "error"
    assert rows["w2"]["error_reason"] == monitor.ERROR_REASON_INVALID_CAMPGROUND_ID


def test_plan_view_is_paginated_past_a_max_rows_cap():
    # the plan view is offset-paginated (it has no unique id to keyset on), so a
    # PostgREST max-rows cap cannot silently truncate the unit list and leave
    # campgrounds unpolled — the same silent-skip guard the watches read carries.
    watches = [
        make_watch(id=f"w{i:04d}", user_id=f"u{i}", campground_id=str(1000 + i))
        for i in range(250)
    ]
    db = FakeDB({"watches": watches}, max_rows=100)  # every GET truncated to 100

    rows = monitor.read_plan_rows(db)

    assert len({r["campground_id"] for r in rows}) == 250


def test_cycle_polls_every_distinct_unit_and_diffs_them_past_a_max_rows_cap():
    # end to end under the cap: 250 distinct campgrounds are all polled the first
    # cycle (view + poll_units chunked read both paginate), and an opening on one
    # of them is detected on the next quiet cycle.
    watches = [
        make_watch(id=f"w{i:04d}", user_id=f"u{i}", campground_id=str(1000 + i))
        for i in range(250)
    ]
    db = FakeDB(
        {
            "watches": watches,
            "device_tokens": [{"user_id": f"u{i}", "apns_token": "t", "environment": "production"}
                              for i in range(250)],
        },
        max_rows=100,
    )
    seed_and_arm(db, RESERVED)

    # one campground opens; every other unit is unchanged
    def responder(cg):
        return FakeResponse(200, OPEN if cg == "1007" else RESERVED)

    apns = FakeAPNs()
    summary = monitor.run(db, apns, FakeHTTP(responder), **QUIET)

    assert summary["campgrounds_polled"] == 250          # nothing truncated
    assert [w for w, _ in apns.alerts] == ["w0007"]      # the one opening, alerted


def test_updated_at_absence_falls_back_without_reddening():
    # the watermark column (0009) missing must also fall back, not halt: the
    # process read rejects on updated_at and the cycle reads the full monitoring
    # set instead. The opening is still detected.
    def fail_on(call):
        if call[0] == "select" and call[1] == "watches":
            params = call[2] or {}
            if "updated_at" in params:
                return postgrest_error(400, "column watches.updated_at does not exist")
        return None

    db = FakeDB({
        "watches": [make_watch(id="w1")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    }, fail_on=fail_on)

    run_cycle(db, RESERVED)
    summary, apns = run_cycle(db, OPEN)

    assert [w for w, _ in apns.alerts] == ["w1"]
    assert monitor.exit_code(summary) == 0
