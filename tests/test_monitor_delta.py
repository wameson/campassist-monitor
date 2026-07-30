"""Delta detection, alert cooldown, write budget, and retention pruning."""

import random
from datetime import timedelta

import apns as apns_module
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


def run_cycle(db, payload, apns=None):
    apns = apns or FakeAPNs()
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    summary = monitor.run(db, apns, http, **QUIET)
    return summary, apns


def test_delta_detection():
    db = FakeDB({
        "watches": [make_watch()],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    open_aug10 = availability_payload({"100": {"2026-08-10": "Available", "2026-08-11": "Reserved"}})

    # first sighting of an opening -> alert
    summary, apns = run_cycle(db, open_aug10)
    assert [w for w, _ in apns.alerts] == ["w1"]
    assert summary["alerts_sent"] == 1
    stored_hash = db.tables["watches"][0]["state_hash"]
    assert stored_hash and db.tables["watches"][0]["last_found_at"] is not None

    # unchanged availability -> hash matches -> no alert, no sent_alerts lookup
    db.calls.clear()
    summary, apns = run_cycle(db, open_aug10)
    assert apns.alerts == []
    assert summary["alerts_sent"] == 0
    assert db.tables["watches"][0]["state_hash"] == stored_hash
    assert db.calls_of("select", "sent_alerts") == []

    # a new opening appears -> hash changes -> alert for the new (site, date)
    both_open = availability_payload({"100": {"2026-08-10": "Available", "2026-08-11": "Available"}})
    summary, apns = run_cycle(db, both_open)
    assert summary["alerts_sent"] == 1
    [(watch_id, openings)] = apns.alerts
    assert watch_id == "w1"
    # 2026-08-10 was already alerted minutes ago (cooldown); only the new date fires
    assert [o["date"] for o in openings] == ["2026-08-11"]
    assert db.tables["watches"][0]["state_hash"] != stored_hash


def test_alert_cooldown():
    def seeded_db(sent_hours_ago):
        return FakeDB({
            "watches": [make_watch(state_hash="stale-hash")],
            "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
            "sent_alerts": [{
                "id": "a1",
                "watch_id": "w1",
                "site_id": "100",
                "date": "2026-08-10",
                "sent_at": monitor.iso_now(NOW - timedelta(hours=sent_hours_ago)),
            }],
        })

    payload = availability_payload({"100": {"2026-08-10": "Available"}})

    # alerted 3 h ago -> suppressed, but the new state_hash is still stored
    db = seeded_db(3)
    summary, apns = run_cycle(db, payload)
    assert apns.alerts == [] and summary["alerts_sent"] == 0
    assert db.tables["watches"][0]["state_hash"] != "stale-hash"

    # alerted 7 h ago -> cooldown elapsed -> re-alerted, sent_at refreshed
    db = seeded_db(7)
    summary, apns = run_cycle(db, payload)
    assert [w for w, _ in apns.alerts] == ["w1"] and summary["alerts_sent"] == 1
    [alert_row] = db.tables["sent_alerts"]  # upsert refreshed, not duplicated
    assert alert_row["sent_at"] == monitor.iso_now(NOW)


def test_retryable_apns_failure_keeps_hash_and_retries():
    def seeded_db():
        return FakeDB({
            "watches": [make_watch(state_hash="stale-hash")],
            "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
        })

    payload = availability_payload({"100": {"2026-08-10": "Available"}})

    # retryable failure: old hash kept, nothing recorded as sent
    db = seeded_db()
    summary, apns = run_cycle(db, payload, FakeAPNs(result=apns_module.RETRYABLE_FAILURE))
    assert [w for w, _ in apns.alerts] == ["w1"] and summary["alerts_sent"] == 0
    row = db.tables["watches"][0]
    assert row["state_hash"] == "stale-hash"
    assert row["last_found_at"] is None and row["last_checked_at"] is not None
    assert db.tables["sent_alerts"] == []

    # next cycle: hash still differs -> the same alert fires and delivers
    summary, apns = run_cycle(db, payload)
    assert [w for w, _ in apns.alerts] == ["w1"] and summary["alerts_sent"] == 1
    assert db.tables["watches"][0]["state_hash"] != "stale-hash"
    assert len(db.tables["sent_alerts"]) == 1


# --- server-truth Alert History (alert_history) ---------------------------
#
# One alert_history row is written per DELIVERED push, in the same step and on
# the same condition (delivered) as watches.last_found_at, so the app can render
# history from the same fact the "Site Found" badge is derived from. These tests
# pin: a delivered alert writes exactly one row with the right fields; a
# non-delivered or suppressed alert writes none; and a failing history write is
# purely additive — it never blocks the row's own writes or reddens the run.

def _delivery_db():
    return FakeDB({
        "watches": [make_watch(state_hash="stale-hash")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })


def test_delivered_alert_writes_one_history_row():
    db = _delivery_db()
    payload = availability_payload({"100": {"2026-08-10": "Available", "2026-08-11": "Available"}})

    summary, apns = run_cycle(db, payload)
    assert summary["alerts_sent"] == 2

    # exactly one history row, in the same cycle last_found_at was set
    watch = db.tables["watches"][0]
    assert watch["last_found_at"] is not None
    assert len(db.tables["alert_history"]) == 1
    row = db.tables["alert_history"][0]
    assert row["watch_id"] == "w1"
    assert row["campground_name"] == "Upper Pines"
    # dates are the watch's DATE strings verbatim — no timezone shift (Issue 1)
    assert row["start_date"] == "2026-08-10"
    assert row["end_date"] == "2026-08-12"
    # site_count is the fresh openings this push announced
    assert row["site_count"] == 2
    assert row["delivered_at"] == monitor.iso_now(NOW)


def test_history_row_is_written_only_when_last_found_at_is():
    # No-change cycle, cooldown-suppressed, and both APNs failures all leave
    # last_found_at unset — and must therefore leave alert_history untouched.

    # (a) no delta -> no push, no history
    db = FakeDB({
        "watches": [make_watch()],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    quiet = availability_payload({"100": {"2026-08-10": "Reserved"}})
    run_cycle(db, quiet)          # seeds hash
    run_cycle(db, quiet)          # unchanged
    assert db.tables["alert_history"] == []

    # (b) opening within the cooldown -> push suppressed, no history
    db = FakeDB({
        "watches": [make_watch(state_hash="stale-hash")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
        "sent_alerts": [{
            "id": "a1", "watch_id": "w1", "site_id": "100", "date": "2026-08-10",
            "sent_at": monitor.iso_now(NOW - timedelta(hours=1)),
        }],
    })
    summary, apns = run_cycle(db, availability_payload({"100": {"2026-08-10": "Available"}}))
    assert apns.alerts == [] and summary["alerts_sent"] == 0
    assert db.tables["alert_history"] == []

    # (c) retryable and (d) permanent APNs failures -> delivered is False, no history
    for result in (apns_module.RETRYABLE_FAILURE, apns_module.PERMANENT_FAILURE):
        db = _delivery_db()
        payload = availability_payload({"100": {"2026-08-10": "Available"}})
        summary, _ = run_cycle(db, payload, FakeAPNs(result=result))
        assert summary["alerts_sent"] == 0
        assert db.tables["watches"][0]["last_found_at"] is None
        assert db.tables["alert_history"] == []


def test_failed_history_write_is_additive_and_does_not_redden_the_run():
    # A rejected alert_history insert (e.g. an unapplied 0005) must not block the
    # watch's own state_hash/last_found_at write, and must not turn the run red:
    # the write is contained, non-blocking and UNRATED.
    def fail_history_insert(call):
        if call[0] == "insert" and call[1] == "alert_history":
            return postgrest_error(400, "relation \"alert_history\" does not exist", code="PGRST205")
        return None

    db = FakeDB(
        {
            "watches": [make_watch(state_hash="stale-hash")],
            "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
        },
        fail_on=fail_history_insert,
    )
    payload = availability_payload({"100": {"2026-08-10": "Available"}})

    summary, apns = run_cycle(db, payload)

    # the push still landed and the badge fact was still written
    assert [w for w, _ in apns.alerts] == ["w1"]
    watch = db.tables["watches"][0]
    assert watch["last_found_at"] is not None
    assert watch["state_hash"] != "stale-hash"
    assert db.tables["alert_history"] == []          # the insert failed
    assert len(db.tables["sent_alerts"]) == 1         # dedup row still written
    # the failure is still reported (so an unapplied migration is visible)...
    assert summary["watch_errors"] == 1
    # ...but it is unrated and non-erroring: the watch is not moved to error and
    # the run stays green.
    assert db.tables["watches"][0]["status"] == "monitoring"
    assert summary["systemic_failure"] is False
    assert monitor.exit_code(summary) == 0


def test_permanent_apns_failure_advances_hash():
    db = FakeDB({
        "watches": [make_watch(state_hash="stale-hash")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2026-08-10": "Available"}})

    summary, apns = run_cycle(db, payload, FakeAPNs(result=apns_module.PERMANENT_FAILURE))
    assert summary["alerts_sent"] == 0
    row = db.tables["watches"][0]
    assert row["state_hash"] != "stale-hash" and row["last_found_at"] is None
    assert db.tables["sent_alerts"] == []

    # no retry: unchanged availability next cycle stays silent
    summary, apns = run_cycle(db, payload)
    assert apns.alerts == [] and summary["alerts_sent"] == 0


def test_write_budget():
    # 100 watches, one shared campground+month -> 1 API call per cycle
    watches = [make_watch(id=f"w{i}", user_id=f"u{i}") for i in range(100)]
    db = FakeDB({"watches": watches})
    payload = availability_payload({"100": {"2026-08-10": "Reserved"}})

    run_cycle(db, payload)  # seeds state_hash on every watch

    # steady state: no availability changes -> <=5 writes total
    db.calls.clear()
    summary, apns = run_cycle(db, payload)
    assert apns.alerts == []
    assert db.write_count <= 5
    # exactly: 1 batched last_checked_at PATCH + 1 run_summaries INSERT + 2 retention DELETEs
    assert len(db.calls_of("patch")) == 1
    assert len(db.calls_of("insert")) == 1
    assert len(db.calls_of("delete")) == 2
    assert summary["watches_checked"] == 100
    assert summary["campgrounds_polled"] == 1


def test_error_annotation():
    # errors surface as a GitHub Actions warning annotation; quiet runs emit none
    assert monitor.error_annotation({"errors": None}) is None
    line = monitor.error_annotation({"errors": "111/2026-08-01: HTTP 429"})
    assert line.startswith("::warning::")
    assert "111/2026-08-01: HTTP 429" in line


def test_retention():
    old = monitor.iso_now(NOW - timedelta(days=40))
    recent = monitor.iso_now(NOW - timedelta(days=1))
    db = FakeDB({
        "sent_alerts": [
            {"id": "a-old", "watch_id": "w1", "site_id": "100", "date": "2026-06-01", "sent_at": old},
            {"id": "a-new", "watch_id": "w1", "site_id": "100", "date": "2026-07-30", "sent_at": recent},
        ],
        "run_summaries": [
            {"id": "r-old", "ran_at": old, "watches_checked": 1},
            {"id": "r-new", "ran_at": recent, "watches_checked": 1},
        ],
    })
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))

    # also exercises the empty-state path: zero watches must not crash
    summary = monitor.run(db, FakeAPNs(), http, **QUIET)
    assert summary["watches_checked"] == 0 and http.requests == []

    assert [r["id"] for r in db.tables["sent_alerts"]] == ["a-new"]
    kept = {r.get("id") for r in db.tables["run_summaries"]}
    assert "r-old" not in kept and "r-new" in kept
    # plus this run's own summary row
    assert len(db.tables["run_summaries"]) == 2
