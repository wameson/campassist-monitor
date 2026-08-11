"""Flexible-date / flexible-length watches (Phase 16).

A flexible watch — `date_mode='flexible'` — reuses start_date/end_date as the
search range [start_date, end_date) and fires when ANY consecutive run of at
least `flex_min_nights` nights is fully available inside that range. A fixed
watch is the degenerate single-window case and must stay byte-identical.
"""

import random
from datetime import date

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeHTTP,
    FakeResponse,
    availability_payload,
    make_flex_watch,
    make_watch,
)

QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)


def run_cycle(db, payload, apns=None):
    apns = apns or FakeAPNs()
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    summary = monitor.run(db, apns, http, **QUIET)
    return summary, apns


def avail(*days: str) -> dict:
    """A one-campsite recreation.gov month response, `days` marked Available."""
    return availability_payload({"100": {d: "Available" for d in days}})


# --- unit: the reduction primitives ---------------------------------------

def test_flex_min_nights_reads_fixed_as_none():
    assert monitor.flex_min_nights(make_watch()) is None                    # no columns
    assert monitor.flex_min_nights(make_watch(date_mode="fixed")) is None
    assert monitor.flex_min_nights(make_flex_watch(3)) == 3
    # a flexible watch with no floor falls back to 1 (never over-suppresses)
    assert monitor.flex_min_nights(make_watch(date_mode="flexible")) == 1
    assert monitor.flex_min_nights(
        make_watch(date_mode="flexible", flex_min_nights=0)
    ) == 1


def test_qualifying_nights_keeps_only_long_enough_runs():
    d = date
    days = [d(2026, 8, 10), d(2026, 8, 11), d(2026, 8, 12),  # run of 3
            d(2026, 8, 15),                                    # run of 1
            d(2026, 8, 18), d(2026, 8, 19)]                    # run of 2
    # min 3: only the first run qualifies
    assert monitor.qualifying_nights(days, 3) == {d(2026, 8, 10), d(2026, 8, 11), d(2026, 8, 12)}
    # min 2: the 3-run and the 2-run qualify, the lone night does not
    assert monitor.qualifying_nights(days, 2) == {
        d(2026, 8, 10), d(2026, 8, 11), d(2026, 8, 12), d(2026, 8, 18), d(2026, 8, 19),
    }
    # min 4: nothing is long enough
    assert monitor.qualifying_nights(days, 4) == set()
    assert monitor.qualifying_nights([], 2) == set()


def test_apply_flex_window_is_identity_for_fixed_watch():
    current = {"100": {"campsite_id": "100", "site": "S100", "dates": ["2026-08-10"]}}
    # same object back — the fixed path is byte-identical
    assert monitor.apply_flex_window(current, make_watch()) is current
    # a one-night floor is also a no-op
    assert monitor.apply_flex_window(current, make_flex_watch(1)) is current


# --- fires when a window fits, not when none does -------------------------

def test_flexible_fires_when_a_qualifying_window_opens():
    # range Aug 10–20, want any 3 consecutive nights
    db = FakeDB({"watches": [make_flex_watch(3, start_date="2026-08-10", end_date="2026-08-20")]})
    summary, apns = run_cycle(db, avail("2026-08-12", "2026-08-13", "2026-08-14"))

    assert [w for w, _ in apns.alerts] == ["w1"]
    assert summary["alerts_sent"] == 3
    [(_, openings)] = apns.alerts
    assert sorted(o["date"] for o in openings) == ["2026-08-12", "2026-08-13", "2026-08-14"]


def test_flexible_does_not_fire_when_no_window_fits():
    db = FakeDB({"watches": [make_flex_watch(3, start_date="2026-08-10", end_date="2026-08-20")]})
    # two consecutive + a lone night: no run reaches 3
    summary, apns = run_cycle(db, avail("2026-08-12", "2026-08-13", "2026-08-16"))

    assert apns.alerts == []
    assert summary["alerts_sent"] == 0
    # nothing qualified -> empty shape -> its hash is stored so the cycle is quiet
    assert db.tables["watches"][0]["state_hash"] == monitor.state_hash({})


def test_flexible_drops_the_out_of_window_night_but_keeps_the_run():
    # a qualifying 3-run PLUS a stray open night: only the run alerts
    db = FakeDB({"watches": [make_flex_watch(3, start_date="2026-08-10", end_date="2026-08-20")]})
    summary, apns = run_cycle(
        db, avail("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-18")
    )
    [(_, openings)] = apns.alerts
    assert sorted(o["date"] for o in openings) == ["2026-08-10", "2026-08-11", "2026-08-12"]
    assert "2026-08-18" not in [o["date"] for o in openings]


# --- min/max-nights variants ----------------------------------------------

def test_min_nights_two_fires_on_a_two_night_run():
    db = FakeDB({"watches": [make_flex_watch(2, 4, start_date="2026-08-10", end_date="2026-08-20")]})
    _, apns = run_cycle(db, avail("2026-08-15", "2026-08-16"))
    [(_, openings)] = apns.alerts
    assert sorted(o["date"] for o in openings) == ["2026-08-15", "2026-08-16"]


def test_min_nights_two_does_not_fire_on_a_single_night():
    db = FakeDB({"watches": [make_flex_watch(2, 4, start_date="2026-08-10", end_date="2026-08-20")]})
    _, apns = run_cycle(db, avail("2026-08-15"))
    assert apns.alerts == []


def test_max_nights_does_not_narrow_the_alert_set():
    # a 5-night run with max 3: every night still qualifies (a 3-night window
    # covers each of them), so max is advisory and does not suppress.
    days = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    db = FakeDB({"watches": [make_flex_watch(2, 3, start_date="2026-08-10", end_date="2026-08-20")]})
    _, apns = run_cycle(db, avail(*days))
    [(_, openings)] = apns.alerts
    assert sorted(o["date"] for o in openings) == days


# --- delta detection: fire once per newly-available window ----------------

def test_flexible_delta_does_not_re_alert_an_unchanged_window():
    db = FakeDB({"watches": [make_flex_watch(3, start_date="2026-08-10", end_date="2026-08-20")]})
    payload = avail("2026-08-12", "2026-08-13", "2026-08-14")

    _, apns = run_cycle(db, payload)
    assert len(apns.alerts) == 1
    stored = db.tables["watches"][0]["state_hash"]

    # unchanged qualifying window -> hash matches -> no second alert
    db.calls.clear()
    _, apns = run_cycle(db, payload)
    assert apns.alerts == []
    assert db.tables["watches"][0]["state_hash"] == stored
    assert db.calls_of("select", "sent_alerts") == []

    # the run grows by a night -> hash changes -> only the new night fires
    # (the first three are within the alert cooldown)
    grown = avail("2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15")
    _, apns = run_cycle(db, grown)
    [(_, openings)] = apns.alerts
    assert [o["date"] for o in openings] == ["2026-08-15"]
    assert db.tables["watches"][0]["state_hash"] != stored


# --- write budget with flexible watches -----------------------------------

def test_flexible_write_budget():
    watches = [
        make_flex_watch(3, id=f"w{i}", user_id=f"u{i}",
                        start_date="2026-08-10", end_date="2026-08-20")
        for i in range(100)
    ]
    db = FakeDB({"watches": watches})
    payload = avail("2026-08-12", "2026-08-13")  # a 2-run: below the 3-night floor

    run_cycle(db, payload)  # seeds state_hash on every watch (nothing qualifies)

    db.calls.clear()
    _, apns = run_cycle(db, payload)
    assert apns.alerts == []
    assert db.write_count <= 5
    assert len(db.calls_of("patch")) == 1     # one batched last_checked_at PATCH
    assert len(db.calls_of("insert")) == 1    # run_summaries
    assert len(db.calls_of("delete")) == 3    # retention
