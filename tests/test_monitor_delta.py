"""Delta detection, alert cooldown, write budget, and retention pruning."""

import random
from datetime import timedelta

import monitor
from helpers import NOW, FakeAPNs, FakeDB, FakeHTTP, FakeResponse, availability_payload, make_watch

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
