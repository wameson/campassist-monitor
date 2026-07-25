"""The GoingToCamp conformer: root->child map recursion, the availability
enum, and the normalized shape it must share with recreation.gov.

Fixture provenance — `tests/fixtures/gtc_*.json` are the live responses
captured in the GoingToCamp research report (Alta Lake, resourceLocationId
-2147483647, rootMapId -2147483396, 2026-08-14 to 2026-08-16):

  gtc_root_map.json        verbatim: empty resourceAvailabilities, four child
                           map ids in mapLinkAvailabilities
  gtc_child_map_daily.json child map -2147483334 with getDailyAvailability
                           =true; the first two resources are as captured
                           ([1,1,0] and [1,1,1]), the rest are
                           capture-equivalent rows added to cover the other
                           enum values observed in the wild (3, 5, 7) — the
                           report truncated the real body at "… 35 sites …"
  gtc_child_map_empty.json a well-formed child map serving no resources
  gtc_map_junk_types.json  } degraded bodies, hand-built: nothing in the
  gtc_map_renamed_fields.json } captures was malformed

No test touches the network.
"""

import json
import random
from datetime import date
from pathlib import Path

import pytest

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeGTCHTTP,
    FakeResponse,
    make_gtc_watch,
)
from providers import PROVIDERS, GoingToCampProvider, Provider, provider_for
from providers.going_to_camp import (
    AVAILABILITY_URL,
    BOOKING_URL,
    HOST,
    MAX_CHILD_MAPS,
    InvalidProviderRef,
    parse_map,
    poll_range,
)

TODAY = NOW.date()  # 2026-08-01
QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

GTC = GoingToCampProvider()
FIXTURES = Path(__file__).parent / "fixtures"

ROOT_MAP_ID = -2147483396
RESOURCE_LOCATION_ID = -2147483647
DAILY_CHILD_MAP_ID = -2147483334
CHILD_MAP_IDS = [-2147483639, -2147483638, -2147483465, DAILY_CHILD_MAP_ID]

# The stay make_gtc_watch describes: nights 8/14 and 8/15, check-out 8/16.
START, END = date(2026, 8, 14), date(2026, 8, 16)
POLL_KEY = ("gtc_-2147483647", (RESOURCE_LOCATION_ID, ROOT_MAP_ID, "2026-08-14", "2026-08-16"))


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def park_responder(child=None, root=None):
    """A FakeGTCHTTP responder serving the captured park: the root map, then
    the daily child map, with every other child map empty. `child` and `root`
    override those two responses."""
    root_body = load_fixture("gtc_root_map")
    daily = child if child is not None else FakeResponse(200, load_fixture("gtc_child_map_daily"))
    empty = load_fixture("gtc_child_map_empty")

    def responder(map_id):
        if map_id == ROOT_MAP_ID and root is not None:
            return root
        if map_id == ROOT_MAP_ID:
            return FakeResponse(200, root_body)
        if map_id == DAILY_CHILD_MAP_ID:
            return daily
        return FakeResponse(200, empty)

    return responder


# --- registration ---------------------------------------------------------

def test_registered_for_going_to_camp():
    assert PROVIDERS["going_to_camp"] is not None
    assert isinstance(PROVIDERS["going_to_camp"], Provider)
    assert provider_for(make_gtc_watch()).name == "going_to_camp"


# --- poll plan ------------------------------------------------------------

def test_poll_plan_is_one_key_for_the_whole_park_and_stay():
    # the recursion lives inside poll(), so one watch is one poll unit — and
    # PollKey[0] is the watch's own campground_id, which the cycle's 404-strike
    # bookkeeping and campgrounds_polled telemetry both read
    assert GTC.poll_plan(make_gtc_watch(), TODAY) == [POLL_KEY]

    # two users on the same park and dates plan the identical key, so the
    # cycle's cross-user dedupe collapses them into one recursion
    assert GTC.poll_plan(make_gtc_watch(id="w2", user_id="u2"), TODAY) == [POLL_KEY]


def test_poll_plan_skips_what_cannot_be_booked_yet_or_any_more():
    # a stay whose last night has passed is not polled (the cycle expires it)
    assert GTC.poll_plan(
        make_gtc_watch(start_date="2026-07-01", end_date="2026-07-05"), TODAY
    ) == []
    # a stay already under way starts from today, not from its own past start
    [(_, key)] = GTC.poll_plan(
        make_gtc_watch(start_date="2026-07-28", end_date="2026-08-04"), TODAY
    )
    assert key[2:] == ("2026-08-01", "2026-08-04")
    # beyond the 12-month horizon there is nothing to poll yet
    assert GTC.poll_plan(
        make_gtc_watch(start_date="2028-06-01", end_date="2028-06-05"), TODAY
    ) == []
    # a single-day watch still asks for its one night
    [(_, key)] = GTC.poll_plan(
        make_gtc_watch(start_date="2026-08-14", end_date="2026-08-14"), TODAY
    )
    assert key[2:] == ("2026-08-14", "2026-08-14")


def test_poll_range_boundaries():
    assert poll_range(date(2026, 8, 14), date(2026, 8, 16), TODAY) == (
        date(2026, 8, 14), date(2026, 8, 16)
    )
    # the last night is the check-out day minus one: a stay checking out today
    # has no bookable night left
    assert poll_range(date(2026, 7, 30), date(2026, 8, 1), TODAY) is None
    assert poll_range(date(2026, 7, 30), date(2026, 8, 2), TODAY) == (TODAY, date(2026, 8, 2))


# --- polling: root -> child recursion ------------------------------------

def test_poll_recurses_from_the_root_map_into_every_child():
    http = FakeGTCHTTP(park_responder())

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)

    # the root map first, then each child map it named — the root's own
    # resourceAvailabilities are empty, so without the recursion there is
    # nothing at all to report
    assert [r["map_id"] for r in http.requests] == [ROOT_MAP_ID] + CHILD_MAP_IDS
    assert set(parsed) == {
        "-2147483029", "-2147483028", "-2147483027", "-2147483026", "-2147483025",
    }

    # every request went to the one hardcoded endpoint with the documented params
    for request in http.requests:
        assert request["url"] == AVAILABILITY_URL
        assert request["headers"]["User-Agent"] == "UA"
        params = request["params"]
        assert params["resourceLocationId"] == RESOURCE_LOCATION_ID
        assert params["bookingCategoryId"] == 0
        assert params["startDate"] == "2026-08-14"
        assert params["endDate"] == "2026-08-16"
        assert params["isReserving"] == "true"
        assert params["getDailyAvailability"] == "true"
        assert params["partySize"] == 1
        assert params["numEquipment"] == 1
        assert params["equipmentCategoryId"] == -32768


def test_poll_paces_the_recursion_and_charges_it_to_the_cycle_budget():
    # 2-5 GETs per park must not burst: the recursion sleeps between children
    http = FakeGTCHTTP(park_responder())
    sleeps = []
    GTC.poll(http, POLL_KEY, "UA", sleep=sleeps.append)
    assert len(sleeps) == len(CHILD_MAP_IDS)
    assert all(s > 0 for s in sleeps)

    # and once the cycle's time budget is spent mid-recursion the park is a
    # failed unit, not a half-polled one that would read as sites vanishing
    http = FakeGTCHTTP(park_responder())
    calls = iter([False, False, True])
    errors = []
    result = GTC.poll(
        http, POLL_KEY, "UA", sleep=lambda s: None,
        errors=errors, budget_exhausted=lambda: next(calls, True),
    )
    assert result is None
    assert len(http.requests) < 1 + len(CHILD_MAP_IDS)
    assert errors == ["gtc_-2147483647/2026-08-14: time budget exhausted mid-recursion, park not polled"]


def test_availability_zero_is_open_and_every_other_value_is_not():
    http = FakeGTCHTTP(park_responder())

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)

    # element i of the per-night array is startDate + i days
    assert parsed["-2147483029"]["availabilities"] == {
        "2026-08-14": False, "2026-08-15": False, "2026-08-16": True,   # [1,1,0]
    }
    assert parsed["-2147483028"]["availabilities"] == {
        "2026-08-14": False, "2026-08-15": False, "2026-08-16": False,  # [1,1,1]
    }
    assert parsed["-2147483027"]["availabilities"] == {
        "2026-08-14": True, "2026-08-15": True, "2026-08-16": True,     # [0,0,0]
    }
    # 3, 5 and 7 are all observed-but-unconfirmed states: none of them is open
    assert parsed["-2147483026"]["availabilities"] == {
        "2026-08-14": False, "2026-08-15": False, "2026-08-16": False,  # [3,5,7]
    }
    # per-site labels are not served by any endpoint this build can reach, so
    # the site degrades to its resourceId
    assert parsed["-2147483027"]["campsite_id"] == "-2147483027"
    assert parsed["-2147483027"]["site"] == "-2147483027"


def test_poll_fails_the_unit_rather_than_reporting_a_park_half_polled():
    # one child map failing means the merged result would be missing sites, so
    # the whole unit fails and the cycle keeps the old state_hash
    http = FakeGTCHTTP(park_responder(child=FakeResponse(500)))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14: HTTP 500"]

    # the root map failing stops the recursion before it starts
    http = FakeGTCHTTP(park_responder(root=FakeResponse(503)))
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None) is None
    assert [r["map_id"] for r in http.requests] == [ROOT_MAP_ID] * 4  # 3 retries


def test_only_the_root_maps_404_strikes_the_watch():
    # a park that does not exist -> the 404-strike lifecycle, keyed on the
    # watch's own campground_id
    http = FakeGTCHTTP(park_responder(root=FakeResponse(404)))
    not_found = set()
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, not_found=not_found) is None
    assert not_found == {"gtc_-2147483647"}

    # a child map that has gone missing is API drift inside a park that
    # answered: the unit fails, but the watch is not struck
    http = FakeGTCHTTP(park_responder(child=FakeResponse(404)))
    not_found = set()
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, not_found=not_found) is None
    assert not_found == set()


def test_a_park_that_fans_out_past_the_cap_is_failed_not_polled():
    root = {
        "mapId": ROOT_MAP_ID,
        "resourceAvailabilities": {},
        "mapLinkAvailabilities": {str(-i): [7] for i in range(1, MAX_CHILD_MAPS + 2)},
    }
    http = FakeGTCHTTP(lambda map_id: FakeResponse(200, root))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    # only the root was fetched: one park cannot spend the whole cycle
    assert len(http.requests) == 1
    assert "child maps exceeds" in errors[0]


# --- defensive parsing ----------------------------------------------------

def test_parse_map_reads_the_captured_bodies():
    sites, children = parse_map(load_fixture("gtc_root_map"), START, END)
    assert sites == {}
    assert children == CHILD_MAP_IDS

    sites, children = parse_map(load_fixture("gtc_child_map_daily"), START, END)
    assert children == []
    assert sites["-2147483025"]["availabilities"] == {
        "2026-08-14": True, "2026-08-15": False, "2026-08-16": True,
    }


def test_parse_map_degrades_instead_of_crashing():
    # junk entries inside a recognizable body are skipped, not fatal
    sites, children = parse_map(load_fixture("gtc_map_junk_types"), START, END)
    # "0" as a string, a bool, null, a bare string, an int, and a missing field
    # are all "not available"; only the real integer 0 is open — and it lands
    # past the requested range, so it is dropped rather than misdated
    assert sites["-2147483029"]["availabilities"] == {
        "2026-08-14": False, "2026-08-15": False, "2026-08-16": False,
    }
    # a resource whose value is not a per-night array contributes nothing
    assert set(sites) == {"-2147483029", "-2147483026"}
    assert sites["-2147483026"]["availabilities"] == {}
    # an unaddressable child map id is skipped, the usable one is kept
    assert children == [-2147483639]


def test_parse_map_refuses_a_body_it_does_not_recognize():
    # neither half of the documented body -> unrecognized, never "no sites"
    assert parse_map(load_fixture("gtc_map_renamed_fields"), START, END) is None
    assert parse_map({}, START, END) is None
    assert parse_map([], START, END) is None
    assert parse_map(None, START, END) is None
    assert parse_map("<html>Azure WAF</html>", START, END) is None
    # a non-empty resourceAvailabilities in which no value is a per-night array
    # is a changed entry shape, not an empty park
    assert parse_map(
        {"resourceAvailabilities": {"-1": {"availability": 0}}}, START, END
    ) is None
    # but a genuinely empty park parses as authoritative
    assert parse_map({"resourceAvailabilities": {}}, START, END) == ({}, [])


def test_poll_treats_an_undecodable_body_as_a_failed_unit():
    http = FakeGTCHTTP(park_responder(child=FakeResponse(200, ValueError("no json"))))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14: invalid JSON"]

    http = FakeGTCHTTP(park_responder(child=FakeResponse(200, {"nope": 1})))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14: unrecognized response body"]


# --- normalization to the shared shape ------------------------------------

def test_extract_relevant_returns_the_shape_recreation_gov_returns():
    http = FakeGTCHTTP(park_responder())
    watch = make_gtc_watch()
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}

    current = GTC.extract_relevant(availability, watch, TODAY)

    # exactly {site_id: {"campsite_id", "site", "dates": [iso…]}} — 8/16 is the
    # check-out day, so a site open only then (-2147483029) is not an opening
    assert current == {
        "-2147483027": {
            "campsite_id": "-2147483027",
            "site": "-2147483027",
            "dates": ["2026-08-14", "2026-08-15"],
        },
        "-2147483025": {
            "campsite_id": "-2147483025",
            "site": "-2147483025",
            "dates": ["2026-08-14"],
        },
    }
    # the cycle hashes and alerts on this shape without knowing which provider
    # produced it, so it must be structurally identical to recreation.gov's
    for site in current.values():
        assert set(site) == {"campsite_id", "site", "dates"}
        assert all(isinstance(d, str) for d in site["dates"])
    assert monitor.available_sites(current) == [
        {"campsite_id": "-2147483025", "site": "-2147483025", "date": "2026-08-14"},
        {"campsite_id": "-2147483027", "site": "-2147483027", "date": "2026-08-14"},
        {"campsite_id": "-2147483027", "site": "-2147483027", "date": "2026-08-15"},
    ]


def test_extract_relevant_narrows_to_site_ids_and_keeps_the_old_hash_on_failure():
    http = FakeGTCHTTP(park_responder())
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}

    narrowed = GTC.extract_relevant(
        availability, make_gtc_watch(site_ids=["-2147483025"]), TODAY
    )
    assert set(narrowed) == {"-2147483025"}

    # a park that failed to poll -> None, so the cycle keeps the old hash and
    # retries next run rather than reading the gap as "nothing available"
    assert GTC.extract_relevant({POLL_KEY: None}, make_gtc_watch(), TODAY) is None
    # nothing planned for this watch yet -> also None
    assert GTC.extract_relevant(
        {}, make_gtc_watch(start_date="2028-06-01", end_date="2028-06-05"), TODAY
    ) is None


# --- provider_ref: identifiers only, and never a host ---------------------

def test_the_host_is_a_constant_a_hostile_provider_ref_cannot_move():
    hostile = {
        "resource_location_id": RESOURCE_LOCATION_ID,
        "map_id": ROOT_MAP_ID,
        "host": "evil.invalid",
        "url": "https://evil.invalid/api/availability/map",
        "base_url": "//evil.invalid",
        "scheme": "http",
    }
    watch = make_gtc_watch(provider_ref=hostile)
    http = FakeGTCHTTP(park_responder())

    for key in GTC.poll_plan(watch, TODAY):
        GTC.poll(http, key, "UA", sleep=lambda s: None)

    assert http.requests
    for request in http.requests:
        assert request["url"] == AVAILABILITY_URL
        assert request["url"].startswith(f"https://{HOST}/")
        assert "evil.invalid" not in json.dumps(request, default=str)

    booking = GTC.booking_url(watch, [])
    assert booking.startswith(f"https://{HOST}/") and "evil.invalid" not in booking


def test_a_malformed_provider_ref_is_this_watchs_own_error_not_a_crash():
    for ref in (
        None,
        {},
        "not an object",
        [{"resource_location_id": 1, "map_id": 2}],
        {"resource_location_id": -1},                       # no map_id
        {"map_id": -1},                                     # no resource_location_id
        {"resource_location_id": -1, "map_id": None},
        {"resource_location_id": -1, "map_id": True},       # a bool is not an id
        {"resource_location_id": -1, "map_id": "; DROP"},
        {"resource_location_id": -1, "map_id": "1" * 40},   # unbounded literal
        {"resource_location_id": -1, "map_id": 1.5},
        {"resource_location_id": -1, "map_id": {"a": 1}},
    ):
        watch = make_gtc_watch(provider_ref=ref)
        # poll_dispatch runs outside the cycle's per-watch containment, so
        # planning must never raise — it simply plans nothing
        assert GTC.poll_plan(watch, TODAY) == []
        # extract_relevant runs inside it, so that is where the failure lands
        with pytest.raises(InvalidProviderRef):
            GTC.extract_relevant({}, watch, TODAY)

    # a string identifier (what a JSON client may well write) is accepted
    numeric_strings = make_gtc_watch(provider_ref={
        "resource_location_id": "-2147483647", "map_id": "-2147483396",
    })
    assert GTC.poll_plan(numeric_strings, TODAY) == [POLL_KEY]


def test_the_error_for_a_bad_ref_never_republishes_the_offending_value():
    # run_summaries.errors is world-readable; the sanitized rendering of an
    # exception with no response is its type name alone, and the message the
    # operator log gets names only the field, never what the client wrote
    watch = make_gtc_watch(provider_ref={"resource_location_id": -1, "map_id": "s3cret"})
    with pytest.raises(InvalidProviderRef) as caught:
        GTC.extract_relevant({}, watch, TODAY)
    assert "s3cret" not in str(caught.value)
    assert monitor.summarize_exception(caught.value, safe=True) == "InvalidProviderRef"


# --- booking deep link ----------------------------------------------------

def test_booking_url_opens_the_parks_booking_search_for_the_stay():
    openings = [{"campsite_id": "-2147483027", "site": "-2147483027", "date": "2026-08-14"}]

    assert GTC.booking_url(make_gtc_watch(), openings) == (
        "https://washington.goingtocamp.com/create-booking/results"
        "?mapId=-2147483396"
        "&bookingCategoryId=0"
        "&startDate=2026-08-14"
        "&endDate=2026-08-16"
        "&isReserving=true"
        "&equipmentId=-32768"
        "&partySize=1"
        "&resourceLocationId=-2147483647"
    )
    # the SPA takes no resourceId preselect, so the link is park-and-dates and
    # does not vary with which site opened
    assert GTC.booking_url(make_gtc_watch(), []) == GTC.booking_url(make_gtc_watch(), openings)
    assert BOOKING_URL.startswith(f"https://{HOST}/")


# --- inside the cycle -----------------------------------------------------

def test_the_cycle_serves_a_going_to_camp_watch_end_to_end():
    db = FakeDB({
        "watches": [make_gtc_watch()],
        "device_tokens": [{"user_id": "u1", "apns_token": "tok", "environment": "production"}],
    })
    apns = FakeAPNs()
    http = FakeGTCHTTP(park_responder())

    summary = monitor.run(db, apns, http, **QUIET)

    [(watch_id, openings)] = apns.alerts
    assert watch_id == "w1"
    assert {o["site"] for o in openings} == {"-2147483027", "-2147483025"}
    row = db.tables["watches"][0]
    assert row["status"] == "monitoring"
    assert row["state_hash"]
    assert summary["watches_checked"] == 1
    assert summary["campgrounds_polled"] == 1  # one park, however many maps
    assert summary["errors"] is None
    assert monitor.exit_code(summary) == 0


def test_one_park_is_one_recursion_however_many_watchers():
    # child-map recursion costs more GETs than recreation.gov's single call, so
    # the cross-user dedupe matters more here: 100 watches on one park is still
    # one root plus its children, and the no-change write budget is unmoved
    def quiet_park():  # one park, nothing bookable in it
        return FakeGTCHTTP(
            park_responder(child=FakeResponse(200, load_fixture("gtc_child_map_empty")))
        )

    db = FakeDB({"watches": [make_gtc_watch(id=f"w{i}", user_id=f"u{i}") for i in range(100)]})

    http = quiet_park()
    monitor.run(db, FakeAPNs(), http, **QUIET)  # seeds state_hash on every watch
    assert len(http.requests) == 1 + len(CHILD_MAP_IDS)  # 5 GETs, not 500

    db.calls.clear()
    summary = monitor.run(db, FakeAPNs(), quiet_park(), **QUIET)

    assert db.write_count <= 5  # the budget PLAN.md sets for a no-change cycle
    assert summary["watches_checked"] == 100
    assert summary["campgrounds_polled"] == 1
