"""Poll-plan dedupe, jitter bounds, and backend-owned watch expiry."""

import random
from datetime import date

import monitor
from helpers import NOW, FakeAPNs, FakeDB, FakeHTTP, FakeResponse, availability_payload, make_watch


def test_dedupe_poll_plan():
    # 3 watches, same campground, all within August -> one poll entry
    watches = [
        make_watch(id="w1", start_date="2026-08-10", end_date="2026-08-12"),
        make_watch(id="w2", user_id="u2", start_date="2026-08-01", end_date="2026-08-05"),
        make_watch(id="w3", user_id="u3", start_date="2026-08-20", end_date="2026-08-25"),
    ]
    assert monitor.dedupe_poll_plan(watches) == [("232447", date(2026, 8, 1))]

    # different month -> separate entry
    watches.append(make_watch(id="w4", start_date="2026-09-03", end_date="2026-09-05"))
    assert monitor.dedupe_poll_plan(watches) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]

    # a watch spanning a month boundary needs both months; already-planned
    # months stay deduplicated
    watches.append(make_watch(id="w5", start_date="2026-08-30", end_date="2026-09-02"))
    assert monitor.dedupe_poll_plan(watches) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]

    # different campground -> separate entry
    watches.append(make_watch(id="w6", campground_id="255555", start_date="2026-08-10", end_date="2026-08-12"))
    assert ("255555", date(2026, 8, 1)) in monitor.dedupe_poll_plan(watches)


def test_jitter_bounds():
    rng = random.Random(42)
    starts = [monitor.start_delay(rng) for _ in range(2000)]
    gaps = [monitor.inter_request_delay(rng) for _ in range(2000)]

    assert all(0 <= s <= 240 for s in starts)
    assert all(1.2 <= g <= 2.8 for g in gaps)
    # the whole range is actually used, not a constant
    assert min(starts) < 30 and max(starts) > 210
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
