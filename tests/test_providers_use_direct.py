"""The UseDirect (Tyler / ReserveCalifornia) conformer.

Fully offline: the grid fixture is the probe-captured response shape, no test
touches the network, and the fixed-User-Agent regression (the captain's ruling
that camply's per-request UA rotation must not be copied) is asserted directly.
"""

import random
from datetime import date
from pathlib import Path

import pytest

import monitor
import providers.use_direct as ud
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeResponse,
    FakeUseDirectHTTP,
    load_fixture,
    make_usedirect_watch,
)
from providers import PROVIDERS, Provider, provider_for
from providers.use_direct import (
    TENANTS,
    FacilityTooLarge,
    UnpollableFacility,
    UseDirectProvider,
    parse_campground_id,
    parse_grid,
    poll_range,
)

TODAY = NOW.date()  # 2026-08-01
QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

UD = UseDirectProvider()
CA_GRID_URL = (
    "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com"
    "/rdr/search/grid"
)
FIXED_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/604.1"

# The probe's park: ReserveCalifornia facility 377. campground_id 'ca_377',
# watch 2026-08-10..2026-08-12, so the poll unit is ('ca_377', ('2026-08-10',
# '2026-08-11')) — nights 10 and 11, the check-out day 12 dropped downstream.
KEY = ("ca_377", ("2026-08-10", "2026-08-11"))


def grid_response():
    return FakeResponse(200, load_fixture("usedirect_grid"))


# --- registry and the shared contract -------------------------------------

def test_registered_under_use_direct():
    assert PROVIDERS["use_direct"].name == "use_direct"
    assert isinstance(PROVIDERS["use_direct"], Provider)  # the whole seam, not just poll()
    assert provider_for(make_usedirect_watch()).name == "use_direct"


def test_campground_id_is_the_wire_contract_and_matches_the_regex():
    # The two values the app builds to: provider raw value, and the
    # '<tenant>_<facilityId>' campground_id. The id MUST match CAMPGROUND_ID_RE,
    # so the namespacing separator is an underscore, never a colon — the colon
    # form once silently killed every GoingToCamp watch.
    watch = make_usedirect_watch()
    assert watch["provider"] == "use_direct"
    assert monitor.CAMPGROUND_ID_RE.fullmatch(watch["campground_id"])
    # every key the plan produces keeps campground_id as PollKey[0], intact
    (key,) = UD.poll_plan(watch, TODAY)
    assert key == KEY
    assert monitor.CAMPGROUND_ID_RE.fullmatch(key[0])
    # the colon form the contract forbids does NOT match — the monitor would
    # error it before it ever reached this provider
    assert monitor.CAMPGROUND_ID_RE.fullmatch("ca:377") is None


def test_parse_campground_id_splits_tenant_and_facility():
    tenant, facility_id = parse_campground_id("ca_377")
    assert tenant is TENANTS["ca"]
    assert facility_id == 377


# --- poll: URL, body, and the fixed User-Agent ----------------------------

def test_poll_posts_the_right_url_and_body():
    http = FakeUseDirectHTTP(lambda fid: grid_response())

    parsed = UD.poll(http, KEY, FIXED_UA)

    assert parsed == parse_grid(load_fixture("usedirect_grid"))
    [request] = http.requests
    assert request["method"] == "POST"
    assert request["url"] == CA_GRID_URL
    # the grid body is the probe-verified shape; EndDate is the last NIGHT
    # (inclusive), in MM-DD-YYYY
    assert request["json"] == {
        "FacilityId": 377,
        "StartDate": "08-10-2026",
        "EndDate": "08-11-2026",
        "UnitSort": "orderby",
        "InSeasonOnly": True,
        "WebOnly": True,
    }
    assert request["headers"]["Content-Type"] == "application/json"


def test_the_user_agent_is_fixed_never_randomised():
    # The captain's ruling: camply rotates a random Chrome UA on this exact
    # availability POST as fingerprint evasion, and that must NOT be copied. The
    # conformer uses the one session UA the cycle hands it, verbatim — the same
    # on the first try and on every backoff retry — and invents none of its own.
    attempts = {"n": 0}

    def responder(fid):
        attempts["n"] += 1
        return FakeResponse(200 if attempts["n"] > 1 else 403)

    http = FakeUseDirectHTTP(responder)
    UD.poll(http, KEY, FIXED_UA, sleep=lambda s: None)

    assert len(http.requests) == 2  # one 403, then the retry that succeeded
    sent = {r["headers"]["User-Agent"] for r in http.requests}
    assert sent == {FIXED_UA}  # identical across the retry — never regenerated

    # And structurally: this module imports no User-Agent generator and no
    # `random`, so it cannot rotate even by accident.
    source = Path(ud.__file__).read_text()
    assert "import random" not in source
    assert "fake_useragent" not in source and "UserAgent(" not in source


# --- parse: the captured response into the shared shape -------------------

def test_captured_response_parses_into_the_shared_shape():
    parsed = parse_grid(load_fixture("usedirect_grid"))

    # site identity is the UnitId; the label is the grid's own Name; a night is
    # open iff its slice IsFree is true
    assert parsed["5043"] == {
        "campsite_id": "5043",
        "site": "Campsite #29",
        "availabilities": {"2026-08-10": True, "2026-08-11": False, "2026-08-12": True},
    }
    # fully taken (a reservation, a block) — every night false
    assert parsed["5044"]["availabilities"] == {
        "2026-08-10": False, "2026-08-11": False, "2026-08-12": False,
    }
    # the accessible unit is parsed like any other and carries its label — this
    # build applies no ADA exclusion, so it is never dropped here
    assert parsed["5051"]["site"] == "Campsite #37 (accessible)"
    assert parsed["5051"]["availabilities"] == {
        "2026-08-10": True, "2026-08-11": True, "2026-08-12": True,
    }


def test_unknown_availability_is_taken_not_open():
    # Fail-closed for AVAILABILITY: a slice that does not POSITIVELY say IsFree
    # is true — missing, non-boolean, a held/blocked slice — is taken, so a
    # false opening never fires.
    raw = {"Facility": {"Units": {"9": {"UnitId": 9, "Name": "S9", "Slices": {
        "2026-08-10T00:00:00": {"Date": "2026-08-10"},                 # no IsFree
        "2026-08-11T00:00:00": {"Date": "2026-08-11", "IsFree": "true"},  # string, not bool
        "2026-08-12T00:00:00": {"Date": "2026-08-12", "IsFree": True},    # the only open one
    }}}}}
    assert parse_grid(raw)["9"]["availabilities"] == {
        "2026-08-10": False, "2026-08-11": False, "2026-08-12": True,
    }


def test_unrecognized_bodies_are_none_never_empty_availability():
    # The challenge-page defence: a body that is not the expected grid shape is a
    # FAILED poll (None → keep old hash, retry), never parsed as an empty park.
    assert parse_grid(None) is None
    assert parse_grid({"Message": "queued", "RedirectUrl": "..."}) is None  # wrong JSON
    assert parse_grid({"Facility": {"Units": None}}) is None                # no Units dict
    # a non-empty Units in which no entry has a Slices dict = the entry shape
    # changed → unrecognized, not empty
    assert parse_grid({"Facility": {"Units": {"9": {"UnitId": 9}}}}) is None
    # but a well-formed EMPTY facility is authoritative "nothing open"
    assert parse_grid({"Facility": {"Units": {}}}) == {}


# --- extract_relevant: the watch's open state -----------------------------

def test_extract_relevant_returns_the_shared_shape_and_drops_the_checkout_day():
    watch = make_usedirect_watch()  # 2026-08-10..2026-08-12 → nights 10, 11
    availability = {KEY: parse_grid(load_fixture("usedirect_grid"))}

    current = UD.extract_relevant(availability, watch, TODAY)

    # 5043: night 10 open, 11 taken, 12 is the check-out day (dropped)
    # 5044: fully taken → absent
    # 5051 (accessible): 10 and 11 open, 12 dropped — NOT suppressed for ADA
    assert current == {
        "5043": {"campsite_id": "5043", "site": "Campsite #29", "dates": ["2026-08-10"]},
        "5051": {
            "campsite_id": "5051",
            "site": "Campsite #37 (accessible)",
            "dates": ["2026-08-10", "2026-08-11"],
        },
    }


def test_extract_relevant_narrows_to_site_ids_by_stable_unit_id():
    watch = make_usedirect_watch(site_ids=["5051"])
    availability = {KEY: parse_grid(load_fixture("usedirect_grid"))}
    narrowed = UD.extract_relevant(availability, watch, TODAY)
    assert set(narrowed) == {"5051"}


def test_extract_relevant_is_none_when_the_facility_failed_to_poll():
    watch = make_usedirect_watch()
    assert UD.extract_relevant({KEY: None}, watch, TODAY) is None


def test_ada_is_never_suppressed_for_either_value_of_include_ada_only():
    # Unlike going_to_camp, this conformer applies no ADA exclusion and does not
    # read include_ada_only — IsAda here is not established to mean "reserved", so
    # suppressing on it would hide bookable sites. The accessible unit alerts
    # whatever the column says (and whether or not it is present at all).
    availability = {KEY: parse_grid(load_fixture("usedirect_grid"))}
    for watch in (
        make_usedirect_watch(),                        # column absent
        make_usedirect_watch(include_ada_only=False),
        make_usedirect_watch(include_ada_only=True),
    ):
        current = UD.extract_relevant(availability, watch, TODAY)
        assert "5051" in current, watch.get("include_ada_only")


# --- poll_range: the inclusive night span ---------------------------------

def test_poll_range_is_the_inclusive_night_span():
    # last night is end-1 for a real stay; EndDate is that night, inclusive
    assert poll_range(date(2026, 8, 10), date(2026, 8, 12), TODAY) == (
        date(2026, 8, 10), date(2026, 8, 11),
    )
    # a single-night watch (end == start) polls that one night
    assert poll_range(date(2026, 8, 10), date(2026, 8, 10), TODAY) == (
        date(2026, 8, 10), date(2026, 8, 10),
    )
    # a stay whose last night has passed is not polled
    assert poll_range(date(2026, 7, 20), date(2026, 8, 1), TODAY) is None
    # a stay wholly beyond the 12-month horizon is not polled yet
    assert poll_range(date(2028, 6, 1), date(2028, 6, 5), TODAY) is None


# --- unpollable: poll_plan never raises, extract_relevant does ------------

@pytest.mark.parametrize("campground_id", ["ca_abc", "zz_377", "ca377", "ca_"])
def test_poll_plan_never_raises_on_an_unusable_watch(campground_id):
    # poll_plan runs OUTSIDE per-watch containment (poll_dispatch), so an
    # unresolvable campground_id must plan nothing rather than raise.
    watch = make_usedirect_watch(campground_id=campground_id)
    assert UD.poll_plan(watch, TODAY) == []


@pytest.mark.parametrize("campground_id", ["ca_abc", "zz_377", "ca377", "ca_"])
def test_extract_relevant_surfaces_the_unusable_watch(campground_id):
    # extract_relevant runs INSIDE containment, so it is where the failure
    # surfaces as this watch's own.
    watch = make_usedirect_watch(campground_id=campground_id)
    with pytest.raises(UnpollableFacility):
        UD.extract_relevant({}, watch, TODAY)


def test_unpollable_reason_names_the_field_never_the_value():
    # The reason reaches the operator channel, so it names only the field at
    # fault — never the client-written tenant key or facility id.
    unknown_tenant = UD.unpollable_reason(make_usedirect_watch(campground_id="zz_377"))
    assert unknown_tenant is not None
    assert "zz" not in unknown_tenant and "377" not in unknown_tenant

    bad_facility = UD.unpollable_reason(make_usedirect_watch(campground_id="ca_wat"))
    assert bad_facility is not None
    assert "wat" not in bad_facility

    # a resolvable watch is pollable
    assert UD.unpollable_reason(make_usedirect_watch()) is None


# --- transient vs permanent: None versus raise ----------------------------

def test_transient_failures_return_none_and_feed_the_lifecycle():
    # a retryable status exhausts backoff → None (keep old hash, retry), with a
    # capped error line for the world-readable summary
    errors = []
    http = FakeUseDirectHTTP(lambda fid: FakeResponse(403))
    assert UD.poll(http, KEY, FIXED_UA, sleep=lambda s: None, errors=errors) is None
    assert errors == ["ca_377/2026-08-10: HTTP 403"]

    # a 404 additionally records the campground for the 404-strike lifecycle
    errors, not_found = [], set()
    http = FakeUseDirectHTTP(lambda fid: FakeResponse(404))
    assert UD.poll(
        http, KEY, FIXED_UA, sleep=lambda s: None, errors=errors, not_found=not_found
    ) is None
    assert errors == ["ca_377/2026-08-10: HTTP 404"]
    assert not_found == {"ca_377"}

    # invalid JSON on a 200 is a non-retryable failed unit, not empty availability
    errors = []
    http = FakeUseDirectHTTP(lambda fid: FakeResponse(200, ValueError("not json")))
    assert UD.poll(http, KEY, FIXED_UA, sleep=lambda s: None, errors=errors) is None
    assert errors == ["ca_377/2026-08-10: invalid JSON"]


def test_a_facility_over_the_safety_cap_raises_rather_than_returning_none():
    # The single-POST analogue of going_to_camp's ParkTooLarge: a response with
    # more units than the safety cap is a PERMANENT fault (it recurs identically),
    # so parse_grid RAISES — the cycle goes red instead of leaving the park
    # unserved behind a green exit — rather than returning a transient None.
    huge = {"Facility": {"Units": {
        str(i): {"UnitId": i, "Name": f"S{i}", "Slices": {}}
        for i in range(ud.MAX_UNITS_PER_FACILITY + 1)
    }}}
    with pytest.raises(FacilityTooLarge):
        parse_grid(huge)
    # and it propagates out of poll (the cycle contains it as a cycle failure)
    http = FakeUseDirectHTTP(lambda fid: FakeResponse(200, huge))
    with pytest.raises(FacilityTooLarge):
        UD.poll(http, KEY, FIXED_UA, sleep=lambda s: None)


# --- SSRF: the host is pinned, never client-derived -----------------------

def test_host_is_never_derived_from_client_data():
    # A hostile campground_id tenant selects no host (unknown tenant → unpollable,
    # no request) and provider_ref is not read at all, so neither can steer the
    # request away from the pinned California host.
    hostile = {"host": "evil.invalid", "url": "https://evil.invalid/api"}
    watch = make_usedirect_watch(provider_ref=hostile)
    http = FakeUseDirectHTTP(lambda fid: grid_response())

    UD.poll(http, KEY, FIXED_UA)

    assert http.requests and all(r["url"] == CA_GRID_URL for r in http.requests)
    assert "evil.invalid" not in UD.booking_url(watch, [])

    # an unknown tenant key never falls back to California's host — it is
    # unpollable, so no request is made for it at all
    assert UD.poll_plan(make_usedirect_watch(campground_id="evilhost_1"), TODAY) == []


def test_tenant_registry_is_per_tenant_with_no_default_fallback():
    # Per-tenant, like GoingToCamp's per-system vocabulary: California's host is
    # California's alone, and an unregistered tenant borrows nothing.
    assert TENANTS["ca"].grid_url == CA_GRID_URL
    assert "tylerapp.com" in TENANTS["ca"].base
    with pytest.raises(UnpollableFacility):
        parse_campground_id("nv_1")  # Nevada not wired → no host, not CA's


# --- through the cycle: a real alert, and the write budget ----------------

def usedirect_db():
    return FakeDB({
        "watches": [make_usedirect_watch()],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })


def test_cycle_polls_alerts_and_stays_green():
    db = usedirect_db()
    apns = FakeAPNs()
    http = FakeUseDirectHTTP(lambda fid: grid_response())

    summary = monitor.run(db, apns, http, **QUIET)

    # the one POST went to the pinned California host for facility 377
    assert [r["url"] for r in http.requests] == [CA_GRID_URL]
    assert http.requests[0]["facility_id"] == 377
    # the alert carried the open sites (5043 night 10; 5051 nights 10 and 11),
    # the accessible one included
    [(watch_id, openings)] = apns.alerts
    assert watch_id == "w1"
    assert {o["campsite_id"] for o in openings} == {"5043", "5051"}
    assert summary["campgrounds_polled"] == 1
    assert summary["systemic_failure"] is False
    assert monitor.exit_code(summary) == 0


def test_no_change_cycle_stays_within_the_write_budget():
    db = usedirect_db()
    http = FakeUseDirectHTTP(lambda fid: grid_response())

    monitor.run(db, FakeAPNs(), http, **QUIET)  # seeds state_hash + first alert

    db.calls.clear()
    apns = FakeAPNs()
    monitor.run(db, apns, http, **QUIET)

    # steady state: same availability → no alert, ≤5 writes (the no-change budget
    # PLAN.md sets); the conformer itself performs no writes at all
    assert apns.alerts == []
    assert db.write_count <= 5


def test_an_unresolvable_campground_id_is_errored_once_not_polled():
    # A watch whose campground_id resolves to no tenant is permanently
    # unpollable, so the cycle errors it once (unreadable_provider_ref) and never
    # polls it — the unpollable_reason lifecycle, exercised end to end.
    db = FakeDB({
        "watches": [make_usedirect_watch(id="w-bad", campground_id="zz_1")],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    http = FakeUseDirectHTTP(lambda fid: grid_response())

    monitor.run(db, FakeAPNs(), http, **QUIET)

    assert http.requests == []  # never polled
    row = db.tables["watches"][0]
    assert row["status"] == "error"
    assert row["error_reason"] == monitor.ERROR_REASON_UNREADABLE_PROVIDER_REF
