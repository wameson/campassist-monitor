"""Poll-plan dedupe, jitter bounds, and backend-owned watch expiry."""

import random
from datetime import date, datetime, timezone

import monitor
from helpers import NOW, FakeAPNs, FakeDB, FakeHTTP, FakeResponse, availability_payload, make_watch
from providers import recreation_gov

TODAY = NOW.date()


def test_dedupe_poll_plan():
    # 3 watches, same campground, all within August -> one poll entry
    watches = [
        make_watch(id="w1", start_date="2026-08-10", end_date="2026-08-12"),
        make_watch(id="w2", user_id="u2", start_date="2026-08-01", end_date="2026-08-05"),
        make_watch(id="w3", user_id="u3", start_date="2026-08-20", end_date="2026-08-25"),
    ]
    assert monitor.dedupe_poll_plan(watches, TODAY) == [("232447", date(2026, 8, 1))]

    # different month -> separate entry
    watches.append(make_watch(id="w4", start_date="2026-09-03", end_date="2026-09-05"))
    assert monitor.dedupe_poll_plan(watches, TODAY) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]

    # a watch spanning a month boundary needs both months; already-planned
    # months stay deduplicated
    watches.append(make_watch(id="w5", start_date="2026-08-30", end_date="2026-09-02"))
    assert monitor.dedupe_poll_plan(watches, TODAY) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]

    # different campground -> separate entry
    watches.append(make_watch(id="w6", campground_id="255555", start_date="2026-08-10", end_date="2026-08-12"))
    assert ("255555", date(2026, 8, 1)) in monitor.dedupe_poll_plan(watches, TODAY)


def test_poll_plan_clamped_to_today():
    # in-progress watch spanning June–September: fully-past months are skipped
    watches = [make_watch(start_date="2026-06-20", end_date="2026-09-05")]
    assert monitor.dedupe_poll_plan(watches, date(2026, 8, 15)) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]


def test_poll_plan_clamped_to_horizon():
    # far-future end_date: the plan never extends past 12 months from today
    watches = [make_watch(start_date="2026-08-10", end_date="2100-01-01")]
    plan = monitor.dedupe_poll_plan(watches, TODAY)
    assert plan[0] == ("232447", date(2026, 8, 1))
    assert plan[-1] == ("232447", date(2027, 8, 1))
    assert len(plan) == 13


def test_watch_entirely_beyond_horizon_polls_nothing():
    # stay starts >12 months out: no polls, no error, watch stays monitoring
    # and becomes pollable once the horizon reaches it
    db = FakeDB({"watches": [make_watch(id="w-far", start_date="2028-06-01", end_date="2028-06-05")]})
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))

    summary = monitor.run(db, FakeAPNs(), http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

    assert http.requests == []
    row = db.tables["watches"][0]
    assert row["status"] == "monitoring"
    assert row["state_hash"] is None  # delta detection deferred, not seeded
    assert row["last_checked_at"] is None  # zero pollable months: not "checked"
    assert summary["watches_checked"] == 0
    assert summary["errors"] is None


def test_horizon_clamped_watch_detects_in_horizon_openings():
    # stay straddles the horizon (Jul–Sep 2027, horizon month = Aug 2027):
    # only in-horizon months are polled, and their openings still alert
    db = FakeDB({
        "watches": [make_watch(start_date="2027-07-20", end_date="2027-09-10")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2027-08-03": "Available"}})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    apns = FakeAPNs()

    summary = monitor.run(db, apns, http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

    assert sorted(r["params"]["start_date"] for r in http.requests) == [
        "2027-07-01T00:00:00.000Z",
        "2027-08-01T00:00:00.000Z",
    ]
    assert summary["alerts_sent"] == 1
    [(watch_id, openings)] = apns.alerts
    assert watch_id == "w1" and [o["date"] for o in openings] == ["2027-08-03"]
    assert db.tables["watches"][0]["state_hash"] == monitor.state_hash(
        {"100": {"campsite_id": "100", "site": "S100", "dates": ["2027-08-03"]}}
    )


def test_months_exclude_checkout_day():
    # stay ending on the 1st: last night is Aug 31, September is never polled
    assert recreation_gov.months_for_watch(date(2026, 8, 28), date(2026, 9, 1), TODAY) == [date(2026, 8, 1)]
    # single-date convention (start == end) still polls its month
    assert recreation_gov.months_for_watch(date(2026, 9, 1), date(2026, 9, 1), TODAY) == [date(2026, 9, 1)]


def test_in_progress_watch_ignores_past_dates():
    # started in June, ends Aug 5: only August is polled, and a past date
    # showing "Available" neither alerts nor enters the state hash
    db = FakeDB({
        "watches": [make_watch(start_date="2026-06-20", end_date="2026-08-05")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2026-06-25": "Available", "2026-08-02": "Reserved"}})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    apns = FakeAPNs()

    summary = monitor.run(db, apns, http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

    assert [r["params"]["start_date"] for r in http.requests] == ["2026-08-01T00:00:00.000Z"]
    assert apns.alerts == [] and summary["alerts_sent"] == 0
    assert db.tables["watches"][0]["state_hash"] == monitor.state_hash({})


def test_same_night_alertable_after_utc_midnight():
    # 04:00 UTC on Aug 11 is still the evening of Aug 10 in the westmost US
    # offset (UTC-8): a same-night Aug 10 opening must still alert, and the
    # watch whose last night is Aug 10 must not expire yet
    us_evening = datetime(2026, 8, 11, 4, 0, 0, tzinfo=timezone.utc)
    assert monitor.monitor_today(us_evening) == date(2026, 8, 10)

    db = FakeDB({
        "watches": [make_watch(start_date="2026-08-10", end_date="2026-08-11")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2026-08-10": "Available"}})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    apns = FakeAPNs()

    summary = monitor.run(
        db, apns, http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: us_evening
    )

    assert db.tables["watches"][0]["status"] == "monitoring"
    assert summary["alerts_sent"] == 1
    [(watch_id, openings)] = apns.alerts
    assert watch_id == "w1" and [o["date"] for o in openings] == ["2026-08-10"]


def test_jitter_bounds():
    rng = random.Random(42)
    starts = [monitor.start_delay(rng) for _ in range(2000)]
    gaps = [monitor.inter_request_delay(rng) for _ in range(2000)]

    # 20 s, not the 240 s this was: a job has to stay inside GitHub's 1-minute
    # billing floor, and the desync it buys is only needed at all because the
    # trigger is moving to an exact-wall-clock external cron (monitor.py).
    assert monitor.START_JITTER_MAX_SECONDS == 20.0
    assert all(0 <= s <= 20 for s in starts)
    assert all(1.2 <= g <= 2.8 for g in gaps)
    # the whole range is actually used, not a constant
    assert min(starts) < 2.5 and max(starts) > 17.5
    assert min(gaps) < 1.35 and max(gaps) > 2.65

    # run() sleeps between consecutive recreation.gov requests within bounds
    db = FakeDB({"watches": [
        make_watch(id="w1", campground_id="111"),
        make_watch(id="w2", campground_id="222"),
        make_watch(id="w3", campground_id="333"),
    ]})
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))
    sleeps = []
    monitor.run(db, FakeAPNs(), http, rng=random.Random(1), sleep=sleeps.append, now_fn=lambda: NOW)
    assert len(sleeps) == 2  # 3 requests -> 2 inter-request gaps, no trailing sleep
    assert all(1.2 <= s <= 2.8 for s in sleeps)


def test_expiry():
    stale = make_watch(id="w-old", campground_id="111", start_date="2026-07-01", end_date="2026-07-20")
    fresh = make_watch(id="w-new", campground_id="222", start_date="2026-08-10", end_date="2026-08-12")
    db = FakeDB({"watches": [stale, fresh]})
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))

    summary = monitor.run(db, FakeAPNs(), http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-old"]["status"] == "expired"
    assert rows["w-new"]["status"] == "monitoring"

    # expired watch excluded from polling and from the checked count
    assert {r["campground_id"] for r in http.requests} == {"222"}
    assert summary["watches_checked"] == 1
    assert rows["w-new"]["last_checked_at"] is not None
    assert rows["w-old"]["last_checked_at"] is None
