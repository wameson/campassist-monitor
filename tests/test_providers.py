"""The provider seam: routing by `watches.provider`, and the recreation.gov
conformer producing exactly what the cycle used to compute inline."""

import random
from datetime import date

import pytest

import monitor
from helpers import NOW, FakeAPNs, FakeDB, FakeHTTP, FakeResponse, availability_payload, make_watch
from providers import DEFAULT_PROVIDER, PROVIDERS, Provider, provider_for, provider_name
from providers.recreation_gov import RecreationGovProvider, months_for_watch, parse_availability

TODAY = NOW.date()
QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

REC_GOV = RecreationGovProvider()


# --- registry and routing -------------------------------------------------

def test_registry_is_keyed_by_conformer_name():
    for name, provider in PROVIDERS.items():
        assert provider.name == name
        assert isinstance(provider, Provider)  # the whole seam, not just poll()
    # only recreation.gov conforms so far; going_to_camp arrives in step 3
    assert set(PROVIDERS) == {"recreation_gov"}
    assert DEFAULT_PROVIDER in PROVIDERS


def test_provider_name_defaults_to_recreation_gov():
    # watches.provider is NOT NULL DEFAULT 'recreation_gov', so a row (or a
    # fixture) without the column is a recreation.gov watch
    assert provider_name(make_watch()) == "recreation_gov"
    assert provider_name(make_watch(provider=None)) == "recreation_gov"
    assert provider_name(make_watch(provider="going_to_camp")) == "going_to_camp"


def test_provider_for_routes_and_refuses_the_unknown():
    assert provider_for(make_watch()).name == "recreation_gov"
    assert provider_for(make_watch(provider="recreation_gov")).name == "recreation_gov"
    # a row this build has no conformer for must never fall back to another
    # provider's poller — callers filter on PROVIDERS first
    with pytest.raises(KeyError):
        provider_for(make_watch(provider="going_to_camp"))


# --- the recreation.gov conformer ----------------------------------------

def test_poll_plan_is_one_key_per_month():
    watch = make_watch(start_date="2026-08-30", end_date="2026-09-02")
    assert REC_GOV.poll_plan(watch, TODAY) == [
        ("232447", date(2026, 8, 1)),
        ("232447", date(2026, 9, 1)),
    ]
    # PollKey[0] is the watch's own campground_id, and the months are the ones
    # the cycle used to compute for itself
    assert [key[1] for key in REC_GOV.poll_plan(watch, TODAY)] == months_for_watch(
        date(2026, 8, 30), date(2026, 9, 2), TODAY
    )
    # nothing pollable yet -> no keys (the cycle reads this as "not served")
    assert REC_GOV.poll_plan(make_watch(start_date="2028-06-01", end_date="2028-06-05"), TODAY) == []


def test_poll_fetches_the_key_and_parses_it():
    payload = availability_payload({"100": {"2026-08-10": "Available"}})
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))

    parsed = REC_GOV.poll(http, ("232447", date(2026, 8, 1)), "UA")

    assert parsed == parse_availability(payload)
    [request] = http.requests
    assert request["url"] == (
        "https://www.recreation.gov/api/camps/availability/campground/232447/month"
    )
    assert request["params"] == {"start_date": "2026-08-01T00:00:00.000Z"}
    assert request["headers"]["User-Agent"] == "UA"


def test_poll_reports_failure_the_way_the_cycle_expects():
    # a failed key is None (the cycle then keeps the watch's old state_hash),
    # a 404 lands in not_found (the 404-strike lifecycle), and the error line
    # is already capped for the world-readable summary
    errors, not_found = [], set()
    http = FakeHTTP(lambda cg: FakeResponse(404))

    assert REC_GOV.poll(
        http, ("232447", date(2026, 8, 1)), "UA",
        sleep=lambda s: None, errors=errors, not_found=not_found,
    ) is None
    assert errors == ["232447/2026-08-01: HTTP 404"]
    assert not_found == {"232447"}


def test_extract_relevant_returns_the_shared_shape():
    watch = make_watch()  # 2026-08-10 -> 2026-08-12, so nights 10 and 11
    availability = {
        ("232447", date(2026, 8, 1)): parse_availability(availability_payload({
            "100": {
                "2026-08-10": "Available",
                "2026-08-11": "Available",
                "2026-08-12": "Available",  # check-out day: not a night
            },
            "101": {"2026-08-10": "Reserved"},
        })),
    }

    assert REC_GOV.extract_relevant(availability, watch, TODAY) == {
        "100": {"campsite_id": "100", "site": "S100", "dates": ["2026-08-10", "2026-08-11"]},
    }

    # site_ids narrows to the wanted sites
    both_open = {
        ("232447", date(2026, 8, 1)): parse_availability(availability_payload({
            "100": {"2026-08-10": "Available"},
            "101": {"2026-08-10": "Available"},
        })),
    }
    narrowed = REC_GOV.extract_relevant(both_open, make_watch(site_ids=["101"]), TODAY)
    assert set(narrowed) == {"101"}

    # a month that failed to poll -> None, so the cycle keeps the old hash
    assert REC_GOV.extract_relevant({("232447", date(2026, 8, 1)): None}, watch, TODAY) is None


def test_booking_url_points_at_the_first_opening():
    openings = [{"campsite_id": "100", "site": "S100", "date": "2026-08-10"}]
    assert REC_GOV.booking_url(make_watch(), openings) == (
        "https://www.recreation.gov/camping/campsites/100"
    )


# --- routing inside the cycle --------------------------------------------

def test_cycle_never_polls_a_watch_of_another_provider():
    # a going_to_camp row (step 3) must not be touched by the recreation.gov
    # provider: not polled, not written, not counted — and not an error on the
    # watch either, since a newer client may write rows this build cannot serve
    db = FakeDB({
        "watches": [
            make_watch(id="w-rec", campground_id="232447"),
            make_watch(id="w-gtc", user_id="u2", campground_id="999999",
                       provider="going_to_camp", provider_ref={"resource_location_id": -1}),
        ],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    payload = availability_payload({"100": {"2026-08-10": "Available"}})
    apns = FakeAPNs()
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))

    summary = monitor.run(db, apns, http, **QUIET)

    assert {r["campground_id"] for r in http.requests} == {"232447"}
    assert [w for w, _ in apns.alerts] == ["w-rec"]
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-gtc"]["status"] == "monitoring"
    assert rows["w-gtc"]["state_hash"] is None
    assert rows["w-gtc"]["last_checked_at"] is None
    assert summary["watches_checked"] == 1
    assert "provider this build does not serve" in summary["errors"]
    # unserveable rows are the operator's signal to ship the conformer, not a
    # failing cycle: they stay out of the systemic rate and keep the run green
    assert summary["systemic_failure"] is False
    assert monitor.exit_code(summary) == 0


def test_provider_ref_never_reaches_a_request_url():
    # provider_ref is client-writable and carries identifiers only: a host or
    # URL smuggled into it must not steer a single request (SSRF)
    hostile = {"host": "evil.invalid", "url": "https://evil.invalid/api"}
    db = FakeDB({
        "watches": [make_watch(provider="recreation_gov", provider_ref=hostile)],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    http = FakeHTTP(lambda cg: FakeResponse(200, availability_payload(
        {"100": {"2026-08-10": "Available"}}
    )))

    monitor.run(db, FakeAPNs(), http, **QUIET)

    assert http.requests
    for request in http.requests:
        assert request["url"].startswith(
            "https://www.recreation.gov/api/camps/availability/campground/"
        )
        assert "evil.invalid" not in request["url"]
    openings = [{"campsite_id": "100", "site": "S100", "date": "2026-08-10"}]
    booking = provider_for(make_watch(provider_ref=hostile)).booking_url(
        make_watch(provider_ref=hostile), openings
    )
    assert booking.startswith("https://www.recreation.gov/") and "evil.invalid" not in booking
