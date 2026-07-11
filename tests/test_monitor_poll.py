"""Exponential backoff and defensive parsing of recreation.gov JSON."""

import json
import random
from datetime import date
from pathlib import Path

import pytest

import monitor
from helpers import NOW, FakeAPNs, FakeDB, FakeHTTP, FakeResponse, availability_payload, make_watch

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_backoff():
    # 429 forever -> retries after 2s, 4s, 8s, then gives up for the cycle
    http = FakeHTTP(lambda cg: FakeResponse(429))
    sleeps, errors = [], []
    result = monitor.poll_with_backoff(
        http, "111", date(2026, 8, 1), "UA", sleep=sleeps.append, errors=errors
    )
    assert result is None
    assert sleeps == [2, 4, 8]
    assert len(http.requests) == 4
    assert errors == ["111/2026-08-01: HTTP 429"]

    # transient 503 -> recovers on retry
    responses = iter([FakeResponse(503), FakeResponse(200, availability_payload({}))])
    http = FakeHTTP(lambda cg: next(responses))
    sleeps = []
    result = monitor.poll_with_backoff(http, "111", date(2026, 8, 1), "UA", sleep=sleeps.append)
    assert result == {} and sleeps == [2]

    # non-retryable status -> single attempt, no sleeps
    http = FakeHTTP(lambda cg: FakeResponse(404))
    sleeps = []
    assert monitor.poll_with_backoff(http, "111", date(2026, 8, 1), "UA", sleep=sleeps.append) is None
    assert sleeps == [] and len(http.requests) == 1

    # one campground blocked does not abort the run: the other watch still alerts
    def responder(cg):
        if cg == "111":
            return FakeResponse(429)
        return FakeResponse(200, availability_payload({"100": {"2026-08-10": "Available"}}))

    db = FakeDB({
        "watches": [
            make_watch(id="w-blocked", campground_id="111"),
            make_watch(id="w-ok", user_id="u2", campground_id="222"),
        ],
        "device_tokens": [{"user_id": "u2", "apns_token": "tok", "environment": "production"}],
    })
    apns = FakeAPNs()
    summary = monitor.run(
        db, apns, FakeHTTP(responder), rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW
    )
    assert [w for w, _ in apns.alerts] == ["w-ok"]
    assert "HTTP 429" in summary["errors"]
    # the blocked watch keeps its old hash so the next run re-evaluates it
    blocked = next(w for w in db.tables["watches"] if w["id"] == "w-blocked")
    assert blocked["state_hash"] is None


class FakeClock:
    """Deterministic monotonic clock; sleeping advances it."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_backoff_stops_when_budget_exhausted():
    # 429 forever, but the cycle budget runs out mid-backoff: the remaining
    # retry (and its 8s sleep) is skipped and the entry gives up early
    clock = FakeClock()
    http = FakeHTTP(lambda cg: FakeResponse(429))
    sleeps, errors = [], []

    def sleep(s):
        sleeps.append(s)
        clock.sleep(s)

    result = monitor.poll_with_backoff(
        http, "111", date(2026, 8, 1), "UA",
        sleep=sleep, errors=errors,
        budget_exhausted=lambda: clock.monotonic() >= 5,
    )
    assert result is None
    assert sleeps == [2, 4]  # third backoff skipped: budget hit at t=6
    assert len(http.requests) == 3
    assert errors == ["111/2026-08-01: HTTP 429"]


def test_time_budget_cycle_reaches_bookkeeping():
    # sustained blocking with slow (timeout-length) responses: once the
    # budget is spent, remaining polls are skipped and the cycle still
    # performs its bookkeeping writes
    clock = FakeClock()

    def responder(cg):
        clock.sleep(20)  # each request runs to the httpx timeout
        return FakeResponse(429)

    watches = [
        make_watch(id=f"w{i}", user_id=f"u{i}", campground_id=str(111 + i))
        for i in range(10)
    ]
    db = FakeDB({"watches": watches})
    http = FakeHTTP(responder)

    summary = monitor.run(
        db, FakeAPNs(), http,
        rng=random.Random(0), sleep=clock.sleep, now_fn=lambda: NOW,
        monotonic=clock.monotonic, time_budget_seconds=120,
    )

    # far fewer than the 40 requests full backoff on 10 entries would make
    assert len(http.requests) < 10
    assert "time budget exhausted" in summary["errors"]
    assert "skipped" in summary["errors"]
    # bookkeeping still ran: batched last_checked_at PATCH, run_summaries
    # INSERT, and both retention DELETEs
    assert len(db.calls_of("patch", "watches")) == 1
    assert len(db.calls_of("insert", "run_summaries")) == 1
    assert len(db.calls_of("delete")) == 2
    rows = db.tables["watches"]
    # telemetry reflects only what was actually attempted: skipped watches
    # keep their old last_checked_at and are not counted as checked
    checked_rows = [r for r in rows if r["last_checked_at"] is not None]
    assert 0 < len(checked_rows) < len(rows)
    assert summary["watches_checked"] == len(checked_rows)
    assert summary["campgrounds_polled"] == len({r["campground_id"] for r in http.requests})
    # nothing polled successfully -> every watch keeps its old hash for retry
    assert all(r["state_hash"] is None for r in rows)


@pytest.mark.parametrize("bad_id", ["123\n456", "232447?injected=1", "232447#frag", "a/b", ""])
def test_invalid_campground_id_errored(bad_id):
    # a malformed campground_id never reaches the HTTP client: the watch
    # transitions to status='error' with an error recorded once, and the
    # rest of the cycle proceeds
    db = FakeDB({
        "watches": [
            make_watch(id="w-bad", campground_id=bad_id),
            make_watch(id="w-ok", user_id="u2", campground_id="222"),
        ],
        "device_tokens": [{"user_id": "u2", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2026-08-10": "Available"}})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    apns = FakeAPNs()

    summary = monitor.run(db, apns, http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

    assert {r["campground_id"] for r in http.requests} == {"222"}
    assert [w for w, _ in apns.alerts] == ["w-ok"]
    assert "invalid campground_id" in summary["errors"]
    assert summary["watches_checked"] == 1
    bad = next(w for w in db.tables["watches"] if w["id"] == "w-bad")
    assert bad["status"] == "error"  # out of the monitoring pool for good
    assert bad["state_hash"] is None
    assert bad["last_checked_at"] is None  # errored before bookkeeping

    # next cycle: the errored watch is excluded, so the error does not recur
    summary = monitor.run(db, FakeAPNs(), http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)
    assert summary["errors"] is None or "invalid campground_id" not in summary["errors"]
    assert summary["watches_checked"] == 1
    assert {r["campground_id"] for r in http.requests} == {"222"}


def test_unrecognized_200_body_is_failed_month():
    # a 200 whose body has no recognizable campsites dict is treated like a
    # poll error: no retry, an error recorded for the campground/month, and
    # the watch keeps its old state_hash instead of advancing to "empty"
    db = FakeDB({"watches": [make_watch(state_hash="old-hash")]})
    http = FakeHTTP(lambda cg: FakeResponse(200, load_fixture("availability_missing_campsites")))
    sleeps = []

    summary = monitor.run(
        db, FakeAPNs(), http, rng=random.Random(0), sleep=sleeps.append, now_fn=lambda: NOW
    )

    assert len(http.requests) == 1
    assert sleeps == []
    assert "232447/2026-08-01: unrecognized response body" in summary["errors"]
    watch = db.tables["watches"][0]
    assert watch["state_hash"] == "old-hash"
    assert watch["last_checked_at"] is not None
    assert monitor.error_annotation(summary) is not None


def test_degraded_entry_shape_is_failed_month():
    # campsites is non-empty but no entry carries a recognizable
    # availabilities dict (renamed field / non-dict campsites): the month is
    # a failed poll like an unrecognized body, not authoritative empty — an
    # error is recorded and the watch keeps its old state_hash
    payload = {
        "campsites": {
            "100": {
                "campsite_id": "100",
                "site": "042",
                "availability": {"2026-08-10T00:00:00Z": "Available"},
            },
            "101": "this campsite is a string, not an object",
        },
        "count": 2,
    }
    assert monitor.parse_availability(payload) is None

    db = FakeDB({"watches": [make_watch(state_hash="old-hash")]})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))

    summary = monitor.run(
        db, FakeAPNs(), http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW
    )

    assert "232447/2026-08-01: unrecognized response body" in summary["errors"]
    assert db.tables["watches"][0]["state_hash"] == "old-hash"
    assert monitor.error_annotation(summary) is not None


def test_empty_availabilities_dicts_are_authoritative():
    # a well-formed response whose campsites all have genuinely empty
    # availabilities dicts is authoritative, not a failed month
    payload = {
        "campsites": {
            "100": {"campsite_id": "100", "site": "042", "availabilities": {}},
        },
        "count": 1,
    }
    parsed = monitor.parse_availability(payload)
    assert parsed == {
        "100": {"campsite_id": "100", "site": "042", "availabilities": {}}
    }


QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)


def test_persistent_404_errors_watch_after_three_cycles():
    # a syntactically valid campground id that keeps 404ing accumulates one
    # strike per cycle and moves the watch to status='error' on the third
    db = FakeDB({"watches": [make_watch()]})

    for cycle in (1, 2):
        summary = monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)
        row = db.tables["watches"][0]
        assert row["status"] == "monitoring"
        assert row["consecutive_not_found"] == cycle
        assert "HTTP 404" in summary["errors"]

    summary = monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)
    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["consecutive_not_found"] == 3
    assert "consecutive cycles" in summary["errors"]
    assert summary["watches_checked"] == 0  # errored before bookkeeping

    # next cycle: the errored watch is out of the monitoring pool for good
    http = FakeHTTP(lambda cg: FakeResponse(404))
    summary = monitor.run(db, FakeAPNs(), http, **QUIET)
    assert http.requests == []
    assert summary["errors"] is None


def test_404_strikes_reset_on_successful_poll():
    db = FakeDB({"watches": [make_watch()]})
    for _ in range(2):
        monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)
    assert db.tables["watches"][0]["consecutive_not_found"] == 2

    # a successful poll resets the strike count with one recovery write
    ok = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))
    db.calls.clear()
    monitor.run(db, FakeAPNs(), ok, **QUIET)
    row = db.tables["watches"][0]
    assert row["status"] == "monitoring"
    assert row["consecutive_not_found"] == 0
    reset_patches = [
        c for c in db.calls_of("patch", "watches") if "consecutive_not_found" in c[3]
    ]
    assert len(reset_patches) == 1

    # steady state after recovery: no strike writes at all
    db.calls.clear()
    monitor.run(db, FakeAPNs(), ok, **QUIET)
    assert not any(
        "consecutive_not_found" in c[3] for c in db.calls_of("patch", "watches")
    )

    # the counter starts over: a fresh 404 is strike one, not strike three
    monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(404)), **QUIET)
    row = db.tables["watches"][0]
    assert row["consecutive_not_found"] == 1 and row["status"] == "monitoring"


def test_wellformed_empty_body_is_authoritative():
    # a well-formed empty campsites dict is authoritative no-availability:
    # no error, and the hash advances away from the old state
    db = FakeDB({"watches": [make_watch(state_hash="old-hash")]})
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload({})))

    summary = monitor.run(
        db, FakeAPNs(), http, rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW
    )

    assert summary["errors"] is None
    watch = db.tables["watches"][0]
    assert watch["state_hash"] == monitor.state_hash({})


@pytest.mark.parametrize("fixture", ["normal", "renamed_fields", "junk_types"])
def test_parser_defensive(fixture):
    # every fixture with a recognizable campsites dict parses without
    # raising and yields a dict
    result = monitor.parse_availability(load_fixture(f"availability_{fixture}"))
    assert isinstance(result, dict)
    for entry in result.values():
        assert set(entry) == {"campsite_id", "site", "availabilities"}


@pytest.mark.parametrize("fixture", ["missing_campsites", "not_a_dict"])
def test_parser_unrecognized_body(fixture):
    # no recognizable campsites dict -> None (not authoritative), while a
    # well-formed empty campsites dict is an authoritative empty parse
    assert monitor.parse_availability(load_fixture(f"availability_{fixture}")) is None
    assert monitor.parse_availability({"campsites": {}}) == {}


def test_parser_fixture_contents():
    normal = monitor.parse_availability(load_fixture("availability_normal"))
    assert normal["100"]["site"] == "042"
    assert normal["100"]["availabilities"]["2026-08-10"] == "Available"
    assert normal["101"]["availabilities"] == {"2026-08-10": "Reserved", "2026-08-11": "Available"}

    # campsites key missing entirely -> unrecognized shape, no crash
    assert monitor.parse_availability(load_fixture("availability_missing_campsites")) is None

    # renamed fields degrade to a partial parse: the intact campsite survives,
    # the renamed one falls back to its key with no dates
    renamed = monitor.parse_availability(load_fixture("availability_renamed_fields"))
    assert renamed["201"]["availabilities"] == {"2026-08-10": "Available"}
    assert renamed["200"] == {"campsite_id": "200", "site": "200", "availabilities": {}}

    # junk value types are skipped field-by-field
    junk = monitor.parse_availability(load_fixture("availability_junk_types"))
    assert set(junk) == {"300", "303"}  # non-dict campsites dropped
    assert junk["300"]["availabilities"] == {"2026-08-10": "Available"}  # bad dates/statuses dropped
    assert junk["300"]["campsite_id"] == "300" and junk["300"]["site"] == "300"
    assert junk["303"]["availabilities"] == {}  # availabilities-as-list ignored

    # whole body not a dict -> unrecognized shape
    assert monitor.parse_availability(load_fixture("availability_not_a_dict")) is None
