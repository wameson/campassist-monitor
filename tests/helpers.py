"""Shared fakes and factories for the test suite.

FakeDB implements the same interface as db.SupabaseClient against
in-memory tables, supporting the PostgREST filter subset the monitor
uses (eq, in, lt, gte), and can be given a `fail_on` hook that raises for
chosen calls the way a real PostgREST rejection would. FakeHTTP stands
in for the recreation.gov client and FakeGTCHTTP for the GoingToCamp one
(which addresses maps by query parameter rather than by path). No test
touches the network or real secrets.
"""

from __future__ import annotations

import inspect
import json as jsonlib
from datetime import datetime, timezone
from pathlib import Path

import httpx

from apns import DELIVERED
from providers.going_to_camp import ATTRIBUTES_URL, EQUIPMENT_URL, RESOURCES_URL

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
# make_watch's default updated_at: well before any test cycle's watermark, so a
# watch is picked up by the read-reduction's "edited since last cycle" read only
# when a test sets a recent value on purpose (the edited/unpaused-watch cases).
OLD_UPDATED_AT = "2020-01-01T00:00:00+00:00"

TABLES = (
    "watches", "device_tokens", "sent_alerts", "alert_history", "run_summaries",
    "poll_units",
)
# The monitoring_plan view's columns (== monitor.PLAN_COLUMNS): a derived,
# read-only DISTINCT over the monitoring watches, which FakeDB.select computes.
_PLAN_COLUMNS = ("provider", "campground_id", "provider_ref", "start_date", "end_date")

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """One captured response body from tests/fixtures/."""
    return jsonlib.loads((FIXTURES / f"{name}.json").read_text())


# PostgREST params that steer the query rather than filter rows; FakeDB.select
# applies these itself so keyset pagination (order + limit + id=gt.<cursor>)
# behaves like the real gateway. `select` is a projection column list, handled
# separately too.
_CONTROL_PARAMS = ("order", "limit", "offset", "select")


def _matches(row: dict, params: dict | None) -> bool:
    for col, expr in (params or {}).items():
        if col in _CONTROL_PARAMS:
            continue
        op, _, arg = str(expr).partition(".")
        val = str(row.get(col))
        if op == "eq":
            if val != arg:
                return False
        elif op == "in":
            if val not in arg.strip("()").split(","):
                return False
        elif op == "lt":
            if not val < arg:
                return False
        elif op == "gt":
            if not val > arg:
                return False
        elif op == "gte":
            if not val >= arg:
                return False
        else:
            raise NotImplementedError(f"FakeDB filter op {op!r}")
    return True


FAKE_SERVICE_KEY = "fake-service-role-key"


def postgrest_error(
    status: int = 400,
    message: str = "column watches.x does not exist",
    *,
    code: str = "42703",
    details=None,
    hint=None,
):
    """An httpx.HTTPStatusError shaped like a PostgREST rejection, i.e. what
    db.SupabaseClient's raise_for_status() raises on a bad write — raised by
    httpx itself, so the exception's own message is the generic status line and
    doc link a real client produces, and `message` is reachable only through the
    response body. The request carries service-key headers like the real one, so
    a rendering that leaked them would show up in the tests. `details`/`hint`
    stand in for the fields PostgREST populates on a constraint violation, where
    `details` echoes the offending key values. `code` defaults to PostgreSQL's
    missing-column `42703`; PostgREST answers a column named in a write *body*
    with its own `PGRST204` instead, which the monitor's write path also has to
    recognize."""
    request = httpx.Request(
        "PATCH",
        "https://project.supabase.invalid/rest/v1/watches?id=in.%28w0,w1%29",
        headers={"apikey": FAKE_SERVICE_KEY, "Authorization": f"Bearer {FAKE_SERVICE_KEY}"},
    )
    response = httpx.Response(
        status,
        request=request,
        json={"code": code, "message": message, "details": details, "hint": hint},
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError(f"status {status} is not an error status")


class FakeDB:
    def __init__(
        self, tables: dict | None = None, fail_on=None, *, project=False, max_rows=None
    ):
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLES}
        for name, rows in (tables or {}).items():
            self.tables[name] = [dict(r) for r in rows]
        self.calls: list[tuple] = []
        # (table, rows_returned) per successful select — the egress read-reduction
        # tests sum the `watches` rows a cycle actually pulled over the wire.
        self.reads: list[tuple[str, int]] = []
        # fail_on(call_tuple) -> exception to raise, or None to let it through
        self.fail_on = fail_on or (lambda call: None)
        # When True, a select carrying a `select=col,col` projection returns only
        # those columns (as real PostgREST does), so a cycle that reads a column
        # its projection omits fails loudly in tests instead of silently seeing a
        # `.get()` default. Off by default to keep other tests' rows intact.
        self.project = project
        # Emulates PostgREST's `max-rows` cap: every select returns at most this
        # many rows regardless of the requested limit, so a test can prove the
        # paginated read still returns the whole set instead of a truncated one.
        self.max_rows = max_rows

    def _guard(self, call: tuple) -> None:
        """Raise for an injected fault after recording the attempt — a real
        client also issues the request before learning it was rejected.

        A fail_on that declares a second parameter is handed `self` too, so it can
        model a filter-scoped write faithfully — e.g. the blanket last_checked
        stamp fails only if a still-'monitoring' bad row is in its scope. One-arg
        fail_ons (the common `lambda call: …`) are called unchanged."""
        try:
            params = len(inspect.signature(self.fail_on).parameters)
        except (TypeError, ValueError):
            params = 1
        exc = self.fail_on(call, self) if params >= 2 else self.fail_on(call)
        if exc is not None:
            raise exc

    @property
    def write_count(self) -> int:
        return sum(1 for c in self.calls if c[0] != "select")

    def calls_of(self, op: str, table: str | None = None) -> list[tuple]:
        return [c for c in self.calls if c[0] == op and (table is None or c[1] == table)]

    def _monitoring_plan_rows(self) -> list[dict]:
        """The monitoring_plan view: a DISTINCT projection of the monitoring
        watches to the poll-planning columns, computed on the fly the way the real
        view is (PostgREST exposes a view as a selectable relation)."""
        seen: set = set()
        rows: list[dict] = []
        for w in self.tables["watches"]:
            if str(w.get("status")) != "monitoring":
                continue
            row = {c: w.get(c) for c in _PLAN_COLUMNS}
            key = jsonlib.dumps(row, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        return rows

    def select(self, table, params=None):
        call = ("select", table, params)
        self.calls.append(call)
        self._guard(call)
        params = params or {}
        source = self._monitoring_plan_rows() if table == "monitoring_plan" else self.tables[table]
        rows = [dict(r) for r in source if _matches(r, params)]
        order = params.get("order")
        if order:
            column = str(order).split(".")[0]
            rows.sort(key=lambda r: str(r.get(column)))
        offset = params.get("offset")
        if offset is not None:
            rows = rows[int(offset):]
        limit = params.get("limit")
        if limit is not None:
            rows = rows[: int(limit)]
        if self.max_rows is not None:
            rows = rows[: self.max_rows]
        projection = params.get("select")
        if self.project and projection and projection != "*":
            columns = [c for c in str(projection).split(",") if c]
            rows = [{c: r[c] for c in columns if c in r} for r in rows]
        self.reads.append((table, len(rows)))
        return rows

    def insert(self, table, rows):
        call = ("insert", table, rows)
        self.calls.append(call)
        self._guard(call)
        rows = [rows] if isinstance(rows, dict) else rows
        stored = []
        for r in rows:
            r = dict(r)
            # Mirror the DEFAULT now() the real schema stamps on these columns, so
            # a cross-cycle read that depends on them (the read-reduction's
            # watermark reads run_summaries.ran_at) behaves like PostgREST.
            if table == "run_summaries" and "ran_at" not in r:
                r["ran_at"] = NOW.isoformat()
            stored.append(r)
        self.tables[table].extend(stored)

    def upsert(self, table, rows, on_conflict=None):
        call = ("upsert", table, rows)
        self.calls.append(call)
        self._guard(call)
        rows = [rows] if isinstance(rows, dict) else rows
        keys = on_conflict.split(",") if on_conflict else None
        for row in rows:
            existing = None
            if keys:
                existing = next(
                    (e for e in self.tables[table]
                     if all(str(e.get(k)) == str(row.get(k)) for k in keys)),
                    None,
                )
            if existing is not None:
                existing.update(row)
            else:
                self.tables[table].append(dict(row))

    def patch(self, table, params, data):
        call = ("patch", table, params, data)
        self.calls.append(call)
        self._guard(call)
        matched = 0
        for row in self.tables[table]:
            if _matches(row, params):
                row.update(data)
                matched += 1
        return matched  # mirrors PostgREST's count=exact Content-Range total

    def delete(self, table, params):
        call = ("delete", table, params)
        self.calls.append(call)
        self._guard(call)
        self.tables[table] = [r for r in self.tables[table] if not _matches(r, params)]


class FakeAPNs:
    def __init__(
        self,
        result: str = DELIVERED,
        failure: BaseException | None = None,
        responder=None,
    ):
        self.alerts: list[tuple[str, list[dict]]] = []
        self.result = result
        # what a real client records for a push that did not land: the
        # exception behind it, never a message naming the watch
        self.failure = failure
        # optional responder(watch) -> (result, failure) for per-watch outcomes,
        # e.g. one dead device token in an otherwise healthy pool
        self.responder = responder

    def send_alert(self, watch, openings, db, failures=None) -> str:
        self.alerts.append((watch["id"], openings))
        result, failure = (
            self.responder(watch) if self.responder else (self.result, self.failure)
        )
        if failures is not None and failure is not None:
            failures.append(failure)
        return result


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHTTP:
    """recreation.gov stand-in; responder(campground_id) -> FakeResponse."""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []

    def get(self, url, params=None, headers=None):
        campground_id = url.rstrip("/").split("/")[-2]
        self.requests.append(
            {"url": url, "campground_id": campground_id, "params": params, "headers": headers}
        )
        return self.responder(campground_id)


class FakeGTCHTTP:
    """GoingToCamp stand-in; responder(map_id) -> FakeResponse.

    Unlike recreation.gov, which names the campground in the URL path, every
    GoingToCamp availability call goes to the same URL and picks its map with
    the `mapId` query parameter — so that is what the responder is keyed on.

    A successful poll also reads the park's resource catalog and the two
    vocabulary tables it decodes through, which is where the real site labels
    come from. Those go by URL rather than by map, and default to the captured
    fixtures so a poll behaves like the live API; `responses` overrides any of
    them (a `FakeResponse`, or None to make that URL unreachable). The fee
    endpoint is POST-only and reached only by an explicit `responses` entry,
    so no test can post to it by accident.
    """

    def __init__(self, responder, responses: dict | None = None):
        self.responder = responder
        self.responses = {
            RESOURCES_URL: FakeResponse(200, load_fixture("gtc_resources")),
            ATTRIBUTES_URL: FakeResponse(200, load_fixture("gtc_attribute_filterable")),
            EQUIPMENT_URL: FakeResponse(200, load_fixture("gtc_equipment")),
        }
        self.responses.update(responses or {})
        self.requests: list[dict] = []

    def _record(self, method, url, params, headers, body=None) -> None:
        self.requests.append({
            "method": method,
            "url": url,
            "map_id": (params or {}).get("mapId"),
            "params": params,
            "headers": headers,
            "json": body,
        })

    def get(self, url, params=None, headers=None):
        self._record("GET", url, params, headers)
        if url in self.responses:
            canned = self.responses[url]
            if canned is None:
                return FakeResponse(404)
            return canned
        return self.responder((params or {}).get("mapId"))

    def post(self, url, params=None, headers=None, json=None):
        self._record("POST", url, params, headers, json)
        if url not in self.responses:
            raise AssertionError(f"unexpected POST to {url}")
        return self.responses[url] or FakeResponse(404)


class FakeUseDirectHTTP:
    """UseDirect stand-in; responder(facility_id) -> FakeResponse.

    Every availability read is a single POST to the tenant's grid URL, keyed on
    the `FacilityId` in the JSON body, so that is what the responder is keyed on.
    There is deliberately no GET path — the availability read is a POST and
    nothing else — and each POST's headers and body are recorded so a test can
    assert the fixed User-Agent, the exact request shape, and that no host but
    the pinned tenant one is ever reached.
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []

    def post(self, url, params=None, headers=None, json=None):
        facility_id = (json or {}).get("FacilityId")
        self.requests.append({
            "method": "POST",
            "url": url,
            "facility_id": facility_id,
            "params": params,
            "headers": headers,
            "json": json,
        })
        return self.responder(facility_id)


def make_usedirect_watch(**overrides) -> dict:
    """A use_direct watch on the park the probe captured: ReserveCalifornia
    facility 377, campground_id 'ca_377' ('ca' = the one wired tenant), 2026-08-10
    to 2026-08-12 (nights 10 and 11; 12 is the check-out day the cycle drops)."""
    watch = make_watch(
        campground_id="ca_377",
        campground_name="Anza-Borrego Desert SP — Middle Section",
        campground_state="CA",
        provider="use_direct",
    )
    watch.update(overrides)
    return watch


def make_watch(**overrides) -> dict:
    watch = {
        "id": "w1",
        "user_id": "u1",
        "campground_id": "232447",
        "campground_name": "Upper Pines",
        "campground_state": "CA",
        "site_ids": [],
        "start_date": "2026-08-10",
        "end_date": "2026-08-12",
        "status": "monitoring",
        "state_hash": None,
        "consecutive_not_found": 0,
        "created_at": None,
        "last_checked_at": None,
        "last_found_at": None,
        "updated_at": OLD_UPDATED_AT,
    }
    watch.update(overrides)
    return watch


def make_flex_watch(min_nights: int, max_nights: int | None = None, **overrides) -> dict:
    """A flexible-date watch (Phase 16): date_mode='flexible', with
    start_date/end_date reused as the search range bounds. `max_nights` defaults
    to `min_nights` (a fixed-length flexible window)."""
    watch = make_watch(
        date_mode="flexible",
        flex_min_nights=min_nights,
        flex_max_nights=max_nights if max_nights is not None else min_nights,
    )
    watch.update(overrides)
    return watch


def make_gtc_watch(**overrides) -> dict:
    """A going_to_camp watch on the park and dates the fixtures were captured
    from: Alta Lake (resourceLocationId -2147483647, rootMapId -2147483396),
    2026-08-14 to 2026-08-16 — the exact request in the research report, so the
    fixtures' three-element per-night arrays are the ones it really answers
    with (nights 8/14 and 8/15, plus the check-out day 8/16)."""
    watch = make_watch(
        campground_id="gtc_-2147483647",
        campground_name="Alta Lake",
        campground_state="WA",
        start_date="2026-08-14",
        end_date="2026-08-16",
        provider="going_to_camp",
        provider_ref={
            "resource_location_id": -2147483647,
            "map_id": -2147483396,
        },
    )
    watch.update(overrides)
    return watch


def availability_payload(sites: dict[str, dict[str, str]]) -> dict:
    """Build a recreation.gov month response: {campsite_id: {date: status}}."""
    return {
        "campsites": {
            cs_id: {
                "campsite_id": cs_id,
                "site": f"S{cs_id}",
                "loop": "A",
                "availabilities": {f"{d}T00:00:00Z": status for d, status in dates.items()},
            }
            for cs_id, dates in sites.items()
        },
        "count": len(sites),
    }
