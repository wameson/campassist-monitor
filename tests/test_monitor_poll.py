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


@pytest.mark.parametrize("bad_id", ["123\n456", "232447?injected=1", "232447#frag", "a/b", ""])
def test_invalid_campground_id_skipped(bad_id):
    # a malformed campground_id never reaches the HTTP client: the entry is
    # skipped with an error recorded, and the rest of the cycle proceeds
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
    bad = next(w for w in db.tables["watches"] if w["id"] == "w-bad")
    assert bad["state_hash"] is None  # old hash kept; nothing hashed from a skipped poll
    assert bad["last_checked_at"] is not None


@pytest.mark.parametrize(
    "fixture", ["normal", "missing_campsites", "renamed_fields", "junk_types", "not_a_dict"]
)
def test_parser_defensive(fixture):
    # every fixture parses without raising and yields a dict
    result = monitor.parse_availability(load_fixture(f"availability_{fixture}"))
    assert isinstance(result, dict)
    for entry in result.values():
        assert set(entry) == {"campsite_id", "site", "availabilities"}


def test_parser_fixture_contents():
    normal = monitor.parse_availability(load_fixture("availability_normal"))
    assert normal["100"]["site"] == "042"
    assert normal["100"]["availabilities"]["2026-08-10"] == "Available"
    assert normal["101"]["availabilities"] == {"2026-08-10": "Reserved", "2026-08-11": "Available"}

    # campsites key missing entirely -> empty parse, no crash
    assert monitor.parse_availability(load_fixture("availability_missing_campsites")) == {}

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

    # whole body not a dict -> empty parse
    assert monitor.parse_availability(load_fixture("availability_not_a_dict")) == {}
