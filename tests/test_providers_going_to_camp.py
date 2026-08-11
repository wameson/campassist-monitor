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
  gtc_child_map_horizon.json the same rows with a FOURTH open night appended,
                           so a request that overshoots the poll horizon by one
                           day has a real bookable night to leak (the captured
                           three-element rows are too short to expose it)
  gtc_child_map_nested.json } hand-built drift: a child map that serves a
  gtc_grandchild_map.json   } resource *and* names a child of its own, whose
                            child in turn links back to it. No captured park
                            nests this deep; the pair exists so the traversal
                            is proven to follow the extra level instead of
                            dropping its sites, and to terminate on the cycle
  gtc_map_junk_types.json  } degraded bodies, hand-built: nothing in the
  gtc_map_renamed_fields.json } captures was malformed

The per-site metadata and price fixtures, captured the same way:

  gtc_resources.json       five records from the live
                           /api/resourcelocation/resources?resourceLocationId
                           =-2147483647 body, re-keyed onto the resource ids
                           the map fixtures above use so the two join. Sites
                           "84" (ADA Only = Yes, Service Type 6), "4"
                           (Service Type 3), "28" (Service Type 7) and "13"
                           are verbatim apart from that key; "6" is
                           capture-equivalent, its Service Type moved from 3
                           to 5 (Electric Hook-ups, no water — 74 sites
                           statewide, none of them at Alta Lake), and "13" is
                           stripped back to tent-only equipment with no
                           Service Type, no ADA Only and no capacity, which is
                           the shape a park at the coverage floor answers with.
                           Record "84" keeps its whole captured body — all 28
                           definedAttributes and a photo — so the parse is
                           proven to walk past everything it does not read
  gtc_attribute_filterable.json  four of the 62 live definitions: the two this
                           build decodes (Service Type -32768, ADA Only
                           -32759) plus ADA Accessible and Pad Location, whose
                           enum 0 is "Not Available" rather than "Yes" — the
                           reason an index is never cast to a bool
  gtc_equipment.json       the whole live /api/equipment body, verbatim
  gtc_fee_details_nightly.json     the canonical feeType 1 response, verbatim
  gtc_fee_details_party_size.json  the feeType 7 group-camp response (a price
                           per 25 people, not per night), verbatim

No test touches the network.
"""

import json
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeGTCHTTP,
    FakeResponse,
    load_fixture,
    make_gtc_watch,
    make_watch,
)
from providers import (
    PROVIDERS,
    GoingToCampProvider,
    Provider,
    provider_for,
    unpollable_reason,
)
from providers.going_to_camp import (
    ADA_ONLY_DEF,
    ATTRIBUTES_URL,
    AVAILABILITY_URL,
    AVAILABLE,
    BOOKING_URL,
    ELECTRIC_SERVICE_TYPES,
    EMPTY_VOCABULARY,
    EQUIPMENT_URL,
    FEE_DETAILS_URL,
    HOST,
    MAX_CHILD_MAPS,
    RESOURCES_URL,
    SERVICE_TYPE_DEF,
    WATER_SERVICE_TYPES,
    InvalidProviderRef,
    ParkTooLarge,
    Vocabulary,
    clear_metadata_cache,
    fee_details,
    horizon_date,
    parse_attribute_vocabulary,
    parse_equipment_vocabulary,
    parse_fee_details,
    parse_map,
    parse_resources,
    poll_range,
)

TODAY = NOW.date()  # 2026-08-01
QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)

GTC = GoingToCampProvider()

ROOT_MAP_ID = -2147483396
RESOURCE_LOCATION_ID = -2147483647
DAILY_CHILD_MAP_ID = -2147483334
NESTING_CHILD_MAP_ID = -2147483639
GRANDCHILD_MAP_ID = -2147483300
CHILD_MAP_IDS = [NESTING_CHILD_MAP_ID, -2147483638, -2147483465, DAILY_CHILD_MAP_ID]

# The stay make_gtc_watch describes: nights 8/14 and 8/15, check-out 8/16.
START, END = date(2026, 8, 14), date(2026, 8, 16)
POLL_KEY = ("gtc_-2147483647", (RESOURCE_LOCATION_ID, ROOT_MAP_ID, "2026-08-14", "2026-08-16"))


def map_requests(http):
    """Just the availability calls. A poll that found sites also reads the
    park catalog and the two vocabulary tables (see the label tests), and
    those are not part of the map recursion being asserted on."""
    return [r for r in http.requests if r["url"] == AVAILABILITY_URL]


def park_responder(child=None, root=None, maps=None):
    """A FakeGTCHTTP responder serving the captured park: the root map, then
    the daily child map, with every other child map empty. `child` and `root`
    override those two responses; `maps` overrides any map by id."""
    root_body = load_fixture("gtc_root_map")
    daily = child if child is not None else FakeResponse(200, load_fixture("gtc_child_map_daily"))
    empty = load_fixture("gtc_child_map_empty")

    def responder(map_id):
        if maps is not None and map_id in maps:
            return maps[map_id]
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


def test_the_requested_range_is_clamped_to_the_horizon_not_just_gated_by_it():
    # a stay that starts inside the horizon and runs years past it must not be
    # requested in full: the same clamp recreation.gov applies to its last month
    horizon = horizon_date(TODAY)
    assert horizon == date(2027, 8, 1)
    # the requested end IS the last night served (parse_map keys element i to
    # startDate + i days and keeps the one at endDate), so the clamp must land
    # exactly ON the horizon: a day later and the horizon night's successor —
    # a genuine bookable night, not a check-out day — would be hashed on
    assert poll_range(date(2027, 7, 25), date(2029, 1, 1), TODAY) == (
        date(2027, 7, 25), horizon
    )
    # the check-out day of an in-horizon stay is still requested verbatim
    assert poll_range(date(2027, 7, 25), date(2027, 7, 28), TODAY) == (
        date(2027, 7, 25), date(2027, 7, 28)
    )
    # a stay ending exactly one day past the horizon is clamped to it too: its
    # last bookable night IS the horizon, and its check-out day is out of reach
    assert poll_range(date(2027, 7, 25), horizon + timedelta(days=1), TODAY) == (
        date(2027, 7, 25), horizon
    )
    # and the poll key the cycle dedupes on carries the clamped range, so a
    # multi-year watch costs the same 2-5 GETs as any other
    [(_, key)] = GTC.poll_plan(
        make_gtc_watch(start_date="2027-07-25", end_date="2029-01-01"), TODAY
    )
    assert key[2:] == ("2027-07-25", "2027-08-01")


def test_a_watch_straddling_the_horizon_is_served_on_its_in_horizon_nights():
    # opted in to the ADA-only sites so the boundary is what is under test:
    # -2147483029's only in-range open night IS the horizon night, which makes
    # it the sharpest evidence here, and the exclusion would otherwise drop it
    watch = make_gtc_watch(
        start_date="2027-07-30", end_date="2029-01-01", include_ada_only=True
    )
    [key] = GTC.poll_plan(watch, TODAY)
    # a body one night LONGER than the clamped range: if the request overshot
    # the horizon by a day, that fourth (open) night would show up below in
    # every site's dates — and two sites that have nothing in range would
    # appear at all — so an off-by-one in either direction fails here
    http = FakeGTCHTTP(park_responder(
        child=FakeResponse(200, load_fixture("gtc_child_map_horizon"))
    ))

    parsed = GTC.poll(http, key, "UA", sleep=lambda s: None)
    current = GTC.extract_relevant({key: parsed}, watch, TODAY)

    # nothing past the horizon was even asked for
    assert map_requests(http)
    for request in map_requests(http):
        assert request["params"]["startDate"] == "2027-07-30"
        assert request["params"]["endDate"] == "2027-08-01"
    # so no out-of-horizon night can reach the state hash or an alert
    assert current == {
        "-2147483029": {
            "campsite_id": "-2147483029",
            "site": "84",
            "dates": ["2027-08-01"],
        },
        "-2147483027": {
            "campsite_id": "-2147483027",
            "site": "6",
            "dates": ["2027-07-30", "2027-07-31", "2027-08-01"],
        },
        "-2147483025": {
            "campsite_id": "-2147483025",
            "site": "13",
            "dates": ["2027-07-30", "2027-08-01"],
        },
    }


# --- polling: root -> child recursion ------------------------------------

def test_poll_recurses_from_the_root_map_into_every_child():
    http = FakeGTCHTTP(park_responder())

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)

    # the root map first, then each child map it named — the root's own
    # resourceAvailabilities are empty, so without the recursion there is
    # nothing at all to report
    assert [r["map_id"] for r in map_requests(http)] == [ROOT_MAP_ID] + CHILD_MAP_IDS
    assert set(parsed) == {
        "-2147483029", "-2147483028", "-2147483027", "-2147483026", "-2147483025",
    }

    # every request went to the one hardcoded endpoint with the documented params
    for request in map_requests(http):
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
    # four child maps, then the park catalog and the two vocabulary tables the
    # labels decode through — every one of the seven paced, none of them burst
    assert len(sleeps) == len(CHILD_MAP_IDS) + 3
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
    assert len(map_requests(http)) < 1 + len(CHILD_MAP_IDS)
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
    # the availability body names no site, so campsite_id stays the resourceId
    # (it is the join key everything else uses) while `site` carries the real
    # label the park catalog gave it
    assert parsed["-2147483027"]["campsite_id"] == "-2147483027"
    assert parsed["-2147483027"]["site"] == "6"


def test_poll_follows_a_park_that_nests_deeper_than_one_level():
    # one level down is all the live API needs, but a park that nests deeper
    # must not have its deeper sites silently dropped from a "successful"
    # merged result — that is the false delta the failed-unit rule exists for
    maps = {
        NESTING_CHILD_MAP_ID: FakeResponse(200, load_fixture("gtc_child_map_nested")),
        GRANDCHILD_MAP_ID: FakeResponse(200, load_fixture("gtc_grandchild_map")),
    }
    http = FakeGTCHTTP(park_responder(maps=maps))

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)

    fetched = [r["map_id"] for r in map_requests(http)]
    assert fetched == [ROOT_MAP_ID] + CHILD_MAP_IDS + [GRANDCHILD_MAP_ID]
    # the grandchild's own site is in the merged result, not missing from it
    assert "-2147483023" in parsed
    assert parsed["-2147483023"]["availabilities"] == {
        "2026-08-14": True, "2026-08-15": False, "2026-08-16": True,
    }
    assert parsed["-2147483024"]["availabilities"]["2026-08-14"] is True
    # the grandchild links back to its own parent: a cycle in the map graph
    # terminates instead of being polled forever
    assert fetched.count(NESTING_CHILD_MAP_ID) == 1


def test_the_fan_out_cap_bounds_the_whole_park_however_deep_it_nests():
    # a park whose deeper levels together exceed the cap is stopped at the
    # moment they are discovered, not polled to the bottom
    deep = {
        "mapId": NESTING_CHILD_MAP_ID,
        "resourceAvailabilities": {},
        "mapLinkAvailabilities": {str(-i): [7] for i in range(1, MAX_CHILD_MAPS + 1)},
    }
    http = FakeGTCHTTP(park_responder(maps={NESTING_CHILD_MAP_ID: FakeResponse(200, deep)}))

    with pytest.raises(ParkTooLarge):
        GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)
    # the root's 4 children plus the MAX_CHILD_MAPS this one names is past the
    # cap, so the traversal stopped there rather than spending a GET on each
    assert [r["map_id"] for r in http.requests] == [ROOT_MAP_ID, NESTING_CHILD_MAP_ID]


def test_poll_fails_the_unit_rather_than_reporting_a_park_half_polled():
    # one child map failing means the merged result would be missing sites, so
    # the whole unit fails and the cycle keeps the old state_hash — and the line
    # says it was a child map, which is the failure that does NOT strike the
    # watch (see the 404 test below)
    http = FakeGTCHTTP(park_responder(child=FakeResponse(500)))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14 child map: HTTP 500"]

    # the root map failing stops the recursion before it starts, and reads
    # differently from the child failure above
    http = FakeGTCHTTP(park_responder(root=FakeResponse(503)))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14: HTTP 503"]
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


def over_cap_root():
    """A root map naming one child more than the safety cap allows."""
    return {
        "mapId": ROOT_MAP_ID,
        "resourceAvailabilities": {},
        "mapLinkAvailabilities": {str(-i): [7] for i in range(1, MAX_CHILD_MAPS + 2)},
    }


def test_a_park_past_the_safety_cap_raises_rather_than_failing_the_unit_quietly():
    # MAX_CHILD_MAPS is a safety cap, not a tuning knob: a park past it fails
    # identically every cycle, so returning None (which means "transient, keep
    # the old hash and retry") would leave its watches monitoring, unalerted and
    # looking healthy forever. It is a fault for an operator instead
    http = FakeGTCHTTP(lambda map_id: FakeResponse(200, over_cap_root()))
    errors = []
    with pytest.raises(ParkTooLarge) as caught:
        GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors)
    # only the root was fetched: one park cannot spend the whole cycle
    assert len(http.requests) == 1
    assert f"safety cap of {MAX_CHILD_MAPS}" in str(caught.value)
    # the operator gets counts and the validated campground id, and the
    # world-readable row gets the sanitized rendering: a type name alone
    assert monitor.summarize_exception(caught.value, safe=True) == "ParkTooLarge"


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
    assert errors == ["gtc_-2147483647/2026-08-14 child map: invalid JSON"]

    http = FakeGTCHTTP(park_responder(child=FakeResponse(200, {"nope": 1})))
    errors = []
    assert GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors) is None
    assert errors == ["gtc_-2147483647/2026-08-14 child map: unrecognized response body"]

    # the root's own undecodable body is labelled as the root's
    http = FakeGTCHTTP(park_responder(root=FakeResponse(200, {"nope": 1})))
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
            "site": "6",
            "dates": ["2026-08-14", "2026-08-15"],
        },
        "-2147483025": {
            "campsite_id": "-2147483025",
            "site": "13",
            "dates": ["2026-08-14"],
        },
    }
    # the cycle hashes and alerts on this shape without knowing which provider
    # produced it, so it must be structurally identical to recreation.gov's
    for site in current.values():
        assert set(site) == {"campsite_id", "site", "dates"}
        assert all(isinstance(d, str) for d in site["dates"])
    assert monitor.available_sites(current) == [
        {"campsite_id": "-2147483025", "site": "13", "date": "2026-08-14"},
        {"campsite_id": "-2147483027", "site": "6", "date": "2026-08-14"},
        {"campsite_id": "-2147483027", "site": "6", "date": "2026-08-15"},
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


def test_site_ids_match_the_resource_id_and_never_the_display_label():
    # The catalog label is display-only, and it reverts to the resourceId
    # whenever the catalog fetch fails: matching on it would mean a labelled
    # watch matched every site on a healthy cycle and none at all on a degraded
    # one, suppressing openings with nothing the user could see. Only the stable
    # identifier selects, which is what the per-site selection phase must store.
    http = FakeGTCHTTP(park_responder())
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}
    assert availability[POLL_KEY]["-2147483025"]["site"] == "13"  # the label is there

    assert GTC.extract_relevant(
        availability, make_gtc_watch(site_ids=["13", "84"]), TODAY
    ) == {}

    narrowed = GTC.extract_relevant(
        availability, make_gtc_watch(site_ids=["-2147483025"]), TODAY
    )
    assert set(narrowed) == {"-2147483025"}


# --- per-site selection: what the client persists vs. what the user reads --
#
# The app stores the stable `resource_id` in `site_ids` and shows the catalog
# label. These three hold the two apart end to end: the id selects, the label
# is what the alert says, and a cycle that could not read the label still
# alerts (the id is what identifies a site, so a missing label never fails the
# match closed).

def test_a_watch_on_one_resource_id_alerts_on_that_site_under_its_label():
    http = FakeGTCHTTP(park_responder())
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}

    current = GTC.extract_relevant(
        availability, make_gtc_watch(site_ids=["-2147483027"]), TODAY
    )

    # only the named site, however many others the park has open
    assert current == {
        "-2147483027": {
            "campsite_id": "-2147483027",
            "site": "6",
            "dates": ["2026-08-14", "2026-08-15"],
        },
    }
    # and what the alert carries is the catalog's label, never the resourceId
    openings = monitor.available_sites(current)
    assert [o["site"] for o in openings] == ["6", "6"]
    # while the dedup rows stay keyed on the id, which does not move with it
    assert [r["site_id"] for r in monitor.alert_rows(make_gtc_watch(), openings, NOW)] == [
        "-2147483027",
        "-2147483027",
    ]


def test_an_any_open_watch_is_untouched_by_the_selection_path():
    http = FakeGTCHTTP(park_responder())
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}

    every_open = GTC.extract_relevant(availability, make_gtc_watch(), TODAY)

    assert set(every_open) == {"-2147483027", "-2147483025"}

    absent = make_gtc_watch()
    del absent["site_ids"]  # the shape a row read without the column has
    # an empty list, an explicit null and an absent column are all the same
    # undirected watch: none of them may make the whole park look selected
    for watch in (absent, make_gtc_watch(site_ids=None), make_gtc_watch(site_ids=[])):
        assert GTC.extract_relevant(availability, watch, TODAY) == every_open


def test_a_selection_still_matches_when_the_label_could_not_be_resolved():
    # The catalog is cosmetic (see `test_a_catalog_this_cycle_cannot_read_never
    # _fails_the_unit`), so on a cycle that could not read it every `site`
    # reverts to the resourceId. A selection matches anyway: it was persisted
    # against the id, which is exactly why the label is not part of the match.
    http = FakeGTCHTTP(park_responder(), responses={RESOURCES_URL: FakeResponse(500)})
    availability = {POLL_KEY: GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)}
    assert availability[POLL_KEY]["-2147483027"]["site"] == "-2147483027"

    current = GTC.extract_relevant(
        availability, make_gtc_watch(site_ids=["-2147483027"]), TODAY
    )

    assert current == {
        "-2147483027": {
            "campsite_id": "-2147483027",
            "site": "-2147483027",  # degraded display, never a dropped opening
            "dates": ["2026-08-14", "2026-08-15"],
        },
    }


# --- the park catalog: labels, ADA Only, capacity, equipment, hookups -----

def catalog(vocabulary=None):
    """The captured park catalog, decoded through the captured vocabulary."""
    if vocabulary is None:
        vocabulary = Vocabulary(
            parse_attribute_vocabulary(load_fixture("gtc_attribute_filterable")),
            parse_equipment_vocabulary(load_fixture("gtc_equipment")),
        )
    return parse_resources(load_fixture("gtc_resources"), vocabulary)


def test_the_catalog_describes_each_site_from_the_captured_body():
    described = catalog()
    assert set(described) == {
        "-2147483029", "-2147483028", "-2147483027", "-2147483026", "-2147483025",
    }

    # site "84": the real label, the platform's own ADA-Only flag, capacity,
    # and equipment resolved to names through /api/equipment
    ada = described["-2147483029"]
    assert ada.label == "84"
    assert ada.ada_only is True
    assert (ada.min_capacity, ada.max_capacity) == (1, 8)
    assert ada.service_type == "Electrical Water Hook-up"
    assert ada.equipment[:3] == ("1 Tent", "2 Tents", "1 Van/Camper")
    assert ada.allows_tent is True and ada.allows_rv is True
    assert ada.max_tents == 2

    # and the same attribute reading No is False, not absent — the two are
    # different answers and a filter must be able to tell them apart
    assert described["-2147483028"].ada_only is False
    assert described["-2147483028"].label == "4"


def test_electric_and_water_are_read_as_two_sets_not_one():
    described = catalog()
    # Service Type 6 and 7 carry both utilities
    assert (described["-2147483029"].electric, described["-2147483029"].water) == (True, True)
    assert (described["-2147483026"].electric, described["-2147483026"].water) == (True, True)
    # 3 (Standard - No Hook-ups) carries neither
    assert (described["-2147483028"].electric, described["-2147483028"].water) == (False, False)
    # and 5 is the case that makes this two sets: electric, no water. Folding
    # water into electric is right 98.8% of the time and wrong on exactly the
    # sites someone filtering for water would be asking about
    electric_only = described["-2147483027"]
    assert electric_only.service_type == "Electric Hook-ups"
    assert electric_only.electric is True
    assert electric_only.water is False
    assert ELECTRIC_SERVICE_TYPES != WATER_SERVICE_TYPES


def test_a_tent_only_site_is_told_apart_from_an_rv_one():
    tents = catalog()["-2147483025"]
    assert tents.equipment == ("1 Tent", "2 Tents", "3 Tents")
    assert tents.allows_tent is True
    assert tents.allows_rv is False
    assert tents.max_tents == 3


def test_a_field_the_platform_did_not_publish_reads_unknown_never_no():
    # coverage runs to 0% at a handful of tiny parks, so "absent" has to be
    # distinguishable from "no": a filter built on these fields must fail open,
    # because a silently suppressed opening is one the user cannot see
    sparse = catalog()["-2147483025"]
    assert sparse.ada_only is None
    assert sparse.service_type is None
    assert sparse.electric is None and sparse.water is None
    assert sparse.min_capacity is None and sparse.max_capacity is None
    # its label is still there: the one field at 100% coverage
    assert sparse.label == "13"


def test_a_yes_no_enum_is_resolved_and_never_cast_from_its_index():
    # enum 0 is "Yes" on ADA Only and "Not Available" on Pad Location, so an
    # index this build could not decode answers unknown rather than a guess
    vocabulary = load_fixture("gtc_attribute_filterable")
    assert vocabulary[str(ADA_ONLY_DEF)]["values"][0]["localizedValues"][0][
        "displayName"] == "Yes"
    assert vocabulary["-32753"]["values"][0]["localizedValues"][0][
        "displayName"] == "Not Available"

    undecodable = catalog(EMPTY_VOCABULARY)
    assert undecodable["-2147483029"].ada_only is None
    assert undecodable["-2147483029"].service_type is None
    # and the fields that need no vocabulary still parse
    assert undecodable["-2147483029"].label == "84"
    assert undecodable["-2147483029"].max_capacity == 8


def test_the_pinned_attribute_ids_still_mean_what_this_build_says():
    # ADA_ONLY_DEF and SERVICE_TYPE_DEF are magic negative ints, and the
    # electric/water sets are enum indices of the second one. Held against the
    # captured vocabulary so a renumbering upstream is a red test rather than a
    # field that silently stops decoding
    raw = load_fixture("gtc_attribute_filterable")
    assert raw[str(ADA_ONLY_DEF)]["localizedValues"][0]["displayName"] == "ADA Only"
    assert raw[str(SERVICE_TYPE_DEF)]["localizedValues"][0]["displayName"] == "Service Type"

    labels = parse_attribute_vocabulary(raw)[SERVICE_TYPE_DEF]
    assert {v: labels[v] for v in sorted(ELECTRIC_SERVICE_TYPES)} == {
        5: "Electric Hook-ups",
        6: "Electrical Water Hook-up",
        7: "Electrical Water Sewer Hook-up",
    }
    assert all("Water" in labels[v] for v in WATER_SERVICE_TYPES)
    assert "Water" not in labels[5]
    assert parse_attribute_vocabulary(raw)[ADA_ONLY_DEF] == {0: "Yes", 1: "No"}


def test_the_equipment_vocabulary_flattens_both_categories():
    names = parse_equipment_vocabulary(load_fixture("gtc_equipment"))
    assert names[-32768] == "1 Tent"
    assert names[-32766] == "3 Tents"
    assert names[-32759] == "1 RV/Trailer up to 30'"
    # the ids collide across namespaces — sub-equipment -32759 is an
    # RV/Trailer, attribute definition -32759 is ADA Only — which is why only
    # the resolved name is ever reasoned about
    assert names[-32759] != "ADA Only"
    # the "Group" category is flattened in alongside "Equipment"
    assert names[-32761] == "Tents"


def test_parse_resources_degrades_instead_of_crashing():
    vocabulary = Vocabulary(
        parse_attribute_vocabulary(load_fixture("gtc_attribute_filterable")),
        parse_equipment_vocabulary(load_fixture("gtc_equipment")),
    )
    # a body that is not a catalog at all is unrecognized, never "no sites"
    for body in (None, [], "<html>Azure WAF</html>", {"-1": "not a record"}):
        assert parse_resources(body, vocabulary) is None
    # a genuinely empty park parses as authoritative
    assert parse_resources({}, vocabulary) == {}
    # a record missing every field this build reads still yields a row, so the
    # site is never dropped out of the join
    [bare] = parse_resources({"-1": {}}, vocabulary).values()
    assert bare.label is None and bare.equipment == ()
    assert bare.ada_only is None and bare.max_tents is None


# --- the site label in the availability shape -----------------------------

def test_the_label_replaces_the_resource_id_and_falls_back_when_it_cannot():
    # the nested fixtures serve -2147483024 and -2147483023, which the captured
    # catalog does not describe: an id present in availability but absent from
    # the catalog keeps the resourceId rather than dropping out of the poll
    maps = {
        NESTING_CHILD_MAP_ID: FakeResponse(200, load_fixture("gtc_child_map_nested")),
        GRANDCHILD_MAP_ID: FakeResponse(200, load_fixture("gtc_grandchild_map")),
    }
    http = FakeGTCHTTP(park_responder(maps=maps))

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)

    assert parsed["-2147483029"]["site"] == "84"
    assert parsed["-2147483024"]["site"] == "-2147483024"
    # campsite_id is the join key everything downstream uses, so it never moves
    assert parsed["-2147483029"]["campsite_id"] == "-2147483029"

    # the catalog was read from the module's own URL, with only the identifier
    # already bounded on its way out of provider_ref in the query
    [request] = [r for r in http.requests if r["url"] == RESOURCES_URL]
    assert request["method"] == "GET"
    assert request["params"] == {"resourceLocationId": RESOURCE_LOCATION_ID}
    assert request["headers"]["User-Agent"] == "UA"


def test_a_catalog_this_cycle_cannot_read_never_fails_the_unit():
    # labels are cosmetic: a park whose catalog 500s is still polled, hashed
    # and alerted on, with the resourceId fallback and one reported line
    http = FakeGTCHTTP(park_responder(), responses={RESOURCES_URL: FakeResponse(500)})
    errors = []

    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors)

    assert parsed is not None
    assert parsed["-2147483029"]["site"] == "-2147483029"
    assert errors == ["gtc_-2147483647/2026-08-14 site metadata: HTTP 500"]
    # and it cost exactly one request: a cosmetic read gets no retry, so a dead
    # catalog endpoint cannot spend the budget the availability polls need
    assert len([r for r in http.requests if r["url"] == RESOURCES_URL]) == 1

    # the same for a body it cannot recognize
    clear_metadata_cache()
    http = FakeGTCHTTP(park_responder(), responses={RESOURCES_URL: FakeResponse(200, {"-1": 5})})
    errors = []
    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors)
    assert parsed["-2147483029"]["site"] == "-2147483029"
    assert errors == [
        "gtc_-2147483647/2026-08-14 site metadata: unrecognized response body"
    ]

    # a bare JSON `null` is a 200 that carries no body at all: it reports like
    # every other unreadable shape rather than passing for a failed request and
    # costing the operator the line
    clear_metadata_cache()
    http = FakeGTCHTTP(park_responder(), responses={RESOURCES_URL: FakeResponse(200, None)})
    errors = []
    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None, errors=errors)
    assert parsed["-2147483029"]["site"] == "-2147483029"
    assert errors == ["gtc_-2147483647/2026-08-14 site metadata: unexpected response body"]

    # and a vocabulary it cannot read only costs the fields that decode through
    # it: the label needs none, so it still lands
    clear_metadata_cache()
    http = FakeGTCHTTP(park_responder(), responses={ATTRIBUTES_URL: FakeResponse(503)})
    parsed = GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)
    assert parsed["-2147483029"]["site"] == "84"


def test_the_catalog_and_vocabulary_are_read_once_per_process():
    # one extra GET per park per cycle, not per poll unit: two watches on the
    # same park over different stays share the catalog, and every park in the
    # cycle shares the vocabulary
    http = FakeGTCHTTP(park_responder())
    other_stay = (POLL_KEY[0], (RESOURCE_LOCATION_ID, ROOT_MAP_ID, "2026-09-01", "2026-09-03"))

    GTC.poll(http, POLL_KEY, "UA", sleep=lambda s: None)
    GTC.poll(http, other_stay, "UA", sleep=lambda s: None)

    for url in (RESOURCES_URL, ATTRIBUTES_URL, EQUIPMENT_URL):
        assert len([r for r in http.requests if r["url"] == url]) == 1, url


# --- the ADA-Only exclusion -----------------------------------------------
#
# The captured catalog marks resource -2147483029 ("84") ADA Only = Yes. Its
# only open night in the captured availability body is the third one, 8/16 —
# the check-out day of the stay every test above uses, and therefore not an
# opening at all — so these tests watch one night longer, to 8/17, which makes
# that night a real opening for the filter to have something to drop.

ADA_STAY = dict(start_date="2026-08-14", end_date="2026-08-17")
ADA_POLL_KEY = (
    "gtc_-2147483647", (RESOURCE_LOCATION_ID, ROOT_MAP_ID, "2026-08-14", "2026-08-17")
)
ADA_ONLY_ID = "-2147483029"


def ada_stay_availability(http=None):
    """The captured park polled over the ADA stay, as the cycle hands it to
    `extract_relevant`."""
    if http is None:
        http = FakeGTCHTTP(park_responder())
    return {ADA_POLL_KEY: GTC.poll(http, ADA_POLL_KEY, "UA", sleep=lambda s: None)}


def test_an_ada_only_site_is_not_an_opening_for_a_watch_that_did_not_ask():
    availability = ada_stay_availability()
    # the platform published the flag, so the poll carries it through
    assert availability[ADA_POLL_KEY][ADA_ONLY_ID]["ada_only"] is True
    assert availability[ADA_POLL_KEY][ADA_ONLY_ID]["availabilities"]["2026-08-16"] is True

    watch = make_gtc_watch(**ADA_STAY)
    # the column is absent from this watch entirely — a live DB that has not
    # had 0003 applied — and reads as the same `false` a migrated row carries
    assert "include_ada_only" not in watch

    current = GTC.extract_relevant(availability, watch, TODAY)

    assert ADA_ONLY_ID not in current
    # and nothing else is dropped: "6" is ADA Only = No, "13" carries no
    # ADA Only attribute at all, and both are still openings
    assert set(current) == {"-2147483027", "-2147483025"}
    # an explicit false reads identically to the absent column
    assert GTC.extract_relevant(
        availability, make_gtc_watch(include_ada_only=False, **ADA_STAY), TODAY
    ) == current


def test_the_same_site_is_an_opening_for_a_watch_that_opted_in():
    availability = ada_stay_availability()

    current = GTC.extract_relevant(
        availability, make_gtc_watch(include_ada_only=True, **ADA_STAY), TODAY
    )

    assert current[ADA_ONLY_ID] == {
        "campsite_id": ADA_ONLY_ID,
        "site": "84",  # the catalog's own label, as for every other site
        "dates": ["2026-08-16"],
    }
    assert set(current) == {ADA_ONLY_ID, "-2147483027", "-2147483025"}


def test_a_watch_that_named_an_ada_only_site_still_matches_it():
    # The exclusion exists to keep an undirected watch quiet, not to overrule a
    # deliberate choice: a watch whose site_ids names this resource alerts on
    # it with the opt-in off, which is what proves the filter runs after the
    # wanted match rather than in front of it.
    availability = ada_stay_availability()
    watch = make_gtc_watch(site_ids=[ADA_ONLY_ID], **ADA_STAY)
    assert "include_ada_only" not in watch

    current = GTC.extract_relevant(availability, watch, TODAY)

    assert set(current) == {ADA_ONLY_ID}
    assert current[ADA_ONLY_ID]["dates"] == ["2026-08-16"]


def test_metadata_this_cycle_cannot_read_excludes_nothing():
    # Fail open in both halves of the decode. A suppressed opening is invisible
    # to the user — nothing tells them the catalog 500'd — while a surplus one
    # is only the noise the monitor already makes today, so an unreadable
    # catalog (or an unreadable vocabulary, which leaves the Yes/No enum
    # undecodable) filters nothing at all.
    for responses in (
        {RESOURCES_URL: FakeResponse(500)},
        {ATTRIBUTES_URL: FakeResponse(503)},
    ):
        clear_metadata_cache()
        http = FakeGTCHTTP(park_responder(), responses=responses)
        availability = ada_stay_availability(http)

        assert "ada_only" not in availability[ADA_POLL_KEY][ADA_ONLY_ID], responses
        current = GTC.extract_relevant(availability, make_gtc_watch(**ADA_STAY), TODAY)
        assert ADA_ONLY_ID in current, responses


def test_an_ada_only_site_opening_and_closing_never_churns_the_state_hash():
    # The exclusion runs before the caller hashes this shape, so an ADA-only
    # site going bookable and back is not a delta for an undirected watch: no
    # phantom `watches` PATCH, no `sent_alerts` row, no push with nothing in it.
    excluded, opted_in = [], []
    for availability_enum in (AVAILABLE, 1):  # bookable, then taken
        clear_metadata_cache()
        body = load_fixture("gtc_child_map_daily")
        body["resourceAvailabilities"][ADA_ONLY_ID][2]["availability"] = availability_enum
        availability = ada_stay_availability(
            FakeGTCHTTP(park_responder(child=FakeResponse(200, body)))
        )
        for hashes, watch in (
            (excluded, make_gtc_watch(**ADA_STAY)),
            (opted_in, make_gtc_watch(include_ada_only=True, **ADA_STAY)),
        ):
            hashes.append(
                monitor.state_hash(GTC.extract_relevant(availability, watch, TODAY))
            )

    assert excluded[0] == excluded[1]
    # the watch that asked for these sites still sees the change, so the
    # stability above is the exclusion and not a hash that ignores the site
    assert opted_in[0] != opted_in[1]


# --- per-night price ------------------------------------------------------

def test_fee_details_reads_the_nightly_rate_over_the_one_read_only_post():
    http = FakeGTCHTTP(
        park_responder(),
        responses={FEE_DETAILS_URL: FakeResponse(200, load_fixture("gtc_fee_details_nightly"))},
    )

    price = fee_details(http, -2147482979, date(2026, 8, 15), "UA", sleep=lambda s: None)

    assert price == Decimal("46.0")
    # a raw JSON decimal, kept as one for a caller to format — never a float to
    # round or a string to match on
    assert isinstance(price, Decimal)

    [request] = [r for r in http.requests if r["url"] == FEE_DETAILS_URL]
    # the endpoint answers 405 to a GET, which is why this one request is a
    # POST; it stays a read — an empty body, no cart, no cookie, no token
    assert request["method"] == "POST"
    assert request["json"] == []
    assert request["url"].startswith(f"https://{HOST}/")
    assert request["params"] == {"resourceId": -2147482979, "startDate": "2026-08-15"}


def test_fee_details_declines_to_price_what_is_not_a_nightly_rate():
    # a group camp answers feeType 7 — a price per 25 people. Rendering that as
    # a nightly rate would be wrong, so no price is the honest answer
    assert parse_fee_details(load_fixture("gtc_fee_details_party_size")) is None
    assert parse_fee_details({"resultCode": 0, "resourceFeeDetails": []}) is None
    for body in (None, [], "<html>", {"resourceFeeDetails": "nope"}):
        assert parse_fee_details(body) is None
    # a nightly entry alongside one this build does not price still reads
    assert parse_fee_details({"resourceFeeDetails": [
        {"feeTotal": 12.5, "feeType": 0},
        {"feeTotal": 31.00000, "feeType": 1},
    ]}) == Decimal("31.0")
    # a total that is not a number at all is not a price
    assert parse_fee_details({"resourceFeeDetails": [
        {"feeTotal": "46.00", "feeType": 1},
    ]}) is None


def test_fee_details_refuses_an_identifier_that_could_grow_the_query():
    http = FakeGTCHTTP(park_responder(), responses={FEE_DETAILS_URL: FakeResponse(500)})
    for bad in ("-2147482979", None, True, 1.5):
        with pytest.raises(TypeError):
            fee_details(http, bad, date(2026, 8, 15), "UA", sleep=lambda s: None)
    with pytest.raises(ValueError):
        fee_details(http, 10 ** 19, date(2026, 8, 15), "UA", sleep=lambda s: None)
    assert not [r for r in http.requests if r["url"] == FEE_DETAILS_URL]

    # a failed price read is None, never an exception the caller has to catch
    errors = []
    assert fee_details(
        http, -2147482979, date(2026, 8, 15), "UA", sleep=lambda s: None, errors=errors
    ) is None
    assert errors == ["going_to_camp fee details: HTTP 500"]


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
    # every URL this provider reaches — availability, the park catalog, the two
    # vocabulary tables — is a constant in its own module, on the one host
    for request in http.requests:
        assert request["url"] in {
            AVAILABILITY_URL, RESOURCES_URL, ATTRIBUTES_URL, EQUIPMENT_URL,
        }
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
        # jsonb numbers are arbitrary-precision, so the number form is bounded
        # by magnitude exactly as the string form is by length — neither may
        # grow the query string this provider sends to the park
        {"resource_location_id": -1, "map_id": 10 ** 19},
        {"resource_location_id": -1, "map_id": -(10 ** 5000)},
        {"resource_location_id": 10 ** 19, "map_id": -1},
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

    # a string identifier (what a JSON client may well write) is accepted, and
    # so is the largest identifier the string form has always allowed
    numeric_strings = make_gtc_watch(provider_ref={
        "resource_location_id": "-2147483647", "map_id": "-2147483396",
    })
    assert GTC.poll_plan(numeric_strings, TODAY) == [POLL_KEY]
    at_the_bound = make_gtc_watch(provider_ref={
        "resource_location_id": 10 ** 19 - 1, "map_id": ROOT_MAP_ID,
    })
    assert GTC.poll_plan(at_the_bound, TODAY) != []


def test_an_unusable_ref_is_reported_as_permanently_unpollable():
    # the cycle asks the conformer whether a watch can ever be polled, so a
    # ref it cannot read is a lifecycle matter rather than a failure to repeat
    # every cycle; the reason names the field and nothing the client wrote
    assert unpollable_reason(make_gtc_watch()) is None
    assert unpollable_reason(make_gtc_watch(provider_ref={})) == (
        "provider_ref.resource_location_id is missing"
    )
    assert unpollable_reason(
        make_gtc_watch(provider_ref={"resource_location_id": -1, "map_id": "s3cret"})
    ) == "provider_ref.map_id is not an integer identifier"
    # a provider with no client-written config of its own does not implement the
    # hook, and its watches are never permanently unpollable
    assert unpollable_reason(make_watch()) is None


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
    # the push says "Site 6" / "Site 13", not the platform's internal resourceId
    assert {o["site"] for o in openings} == {"6", "13"}
    row = db.tables["watches"][0]
    assert row["status"] == "monitoring"
    assert row["state_hash"]
    assert summary["watches_checked"] == 1
    assert summary["campgrounds_polled"] == 1  # one park, however many maps
    assert summary["errors"] is None
    assert monitor.exit_code(summary) == 0


def test_a_watch_with_an_unusable_provider_ref_is_errored_once_and_converges():
    # a going_to_camp row without the identifiers can never poll, so it gets the
    # same one-off status='error' a malformed campground_id gets — otherwise it
    # sits 'monitoring' forever, never alerting, while its user sees nothing
    # wrong and every cycle republishes the same error line
    db = FakeDB({
        "watches": [
            make_gtc_watch(
                id="w-bad",
                provider_ref={"resource_location_id": -2147483647, "map_id": "s3cret"},
            ),
            make_gtc_watch(id="w-ok", user_id="u2"),
        ],
        "device_tokens": [{"user_id": "u2", "apns_token": "tok", "environment": "production"}],
    })
    apns = FakeAPNs()

    summary = monitor.run(db, apns, FakeGTCHTTP(park_responder()), **QUIET)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-bad"]["status"] == "error"
    # the healthy watch alongside it is still polled and alerted
    assert rows["w-ok"]["status"] == "monitoring"
    assert [watch_id for watch_id, _ in apns.alerts] == ["w-ok"]
    # the world-readable row gets the count alone; the field at fault is
    # operator-only, and neither channel republishes what the client wrote
    assert summary["errors"] == (
        "1 watch(es) their provider cannot poll, errored and skipped"
    )
    assert "provider_ref.map_id" in summary["errors_detail"]
    assert "s3cret" not in summary["errors_detail"]
    # one bad row is not this cycle's health: the run stays green
    assert monitor.exit_code(summary) == 0

    # and it converges: errored, it leaves the pool, so the next cycle neither
    # writes it again nor reports it again
    db.calls.clear()
    steady = monitor.run(db, FakeAPNs(), FakeGTCHTTP(park_responder()), **QUIET)

    assert not [c for c in db.calls if c[0] == "patch" and "w-bad" in str(c)]
    # converged, but not silent: the errored watch is now reported as a
    # population count, with the reason it recorded operator-only
    assert steady["errors"] == "1 watch(es) in status='error', not monitored"
    assert steady["watches_errored"] == 1
    assert f"{monitor.ERROR_REASON_UNREADABLE_PROVIDER_REF}: 1" in steady["errors_detail"]
    assert db.write_count <= 5


def test_a_pool_wide_unpollable_condition_goes_red_without_erroring_the_pool():
    # the shared cause this guards against: a client shipping the wrong key
    # names writes them on every row it creates. That is the operator's to fix,
    # and users must never have to recreate their watches over it — so the pool
    # stays 'monitoring' and the run goes red instead
    db = FakeDB({"watches": [
        make_gtc_watch(
            id=f"w{i}",
            user_id=f"u{i}",
            provider_ref={"resourceLocationId": -2147483647, "mapId": -2147483396},
        )
        for i in range(4)
    ]})

    summary = monitor.run(db, FakeAPNs(), FakeGTCHTTP(park_responder()), **QUIET)

    assert [r["status"] for r in db.tables["watches"]] == ["monitoring"] * 4
    # no watch is errored or otherwise status-changed — the only write is the
    # filter-scoped blanket freshness stamp (last_checked_at), which cannot
    # exclude these still-'monitoring' rows. The pool is left intact.
    assert not [c for c in db.calls if c[0] == "patch" and "status" in c[3]]
    # loud: red, with the count on the world-readable row and the field at
    # fault operator-only, exactly as the isolated case renders it
    assert monitor.exit_code(summary) == 1
    assert (
        "4 of 4 watch(es) their provider cannot poll: a shared cause, "
        "left monitoring for an operator"
    ) in summary["errors"]
    assert "provider_ref.resource_location_id" in summary["errors_detail"]
    assert "provider_ref" not in summary["errors"]


def test_a_park_past_the_fan_out_cap_cannot_masquerade_as_a_healthy_watch():
    # the whole point of raising: the cycle contains the fault, keeps the old
    # hash like any failed unit, leaves the watch alone — and still exits red,
    # rather than reporting one error line a cycle forever on a green run
    db = FakeDB({"watches": [make_gtc_watch()]})
    http = FakeGTCHTTP(lambda map_id: FakeResponse(200, over_cap_root()))

    summary = monitor.run(db, FakeAPNs(), http, **QUIET)

    row = db.tables["watches"][0]
    assert row["status"] == "monitoring"  # not this user's fault to fix
    assert row["state_hash"] is None      # no false "everything vanished" delta
    assert monitor.exit_code(summary) == 1
    assert "poll going_to_camp: ParkTooLarge" in summary["errors"]
    assert f"safety cap of {MAX_CHILD_MAPS}" in summary["cycle_errors"]
    # and the cycle still reached its own bookkeeping past the raise
    assert db.tables["run_summaries"]


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
