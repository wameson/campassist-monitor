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

import json as jsonlib
from datetime import datetime, timezone
from pathlib import Path

import httpx

from apns import DELIVERED
from providers.going_to_camp import ATTRIBUTES_URL, EQUIPMENT_URL, RESOURCES_URL

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

TABLES = ("watches", "device_tokens", "sent_alerts", "alert_history", "run_summaries")

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """One captured response body from tests/fixtures/."""
    return jsonlib.loads((FIXTURES / f"{name}.json").read_text())


def _matches(row: dict, params: dict | None) -> bool:
    for col, expr in (params or {}).items():
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
    def __init__(self, tables: dict | None = None, fail_on=None):
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLES}
        for name, rows in (tables or {}).items():
            self.tables[name] = [dict(r) for r in rows]
        self.calls: list[tuple] = []
        # fail_on(call_tuple) -> exception to raise, or None to let it through
        self.fail_on = fail_on or (lambda call: None)

    def _guard(self, call: tuple) -> None:
        """Raise for an injected fault after recording the attempt — a real
        client also issues the request before learning it was rejected."""
        exc = self.fail_on(call)
        if exc is not None:
            raise exc

    @property
    def write_count(self) -> int:
        return sum(1 for c in self.calls if c[0] != "select")

    def calls_of(self, op: str, table: str | None = None) -> list[tuple]:
        return [c for c in self.calls if c[0] == op and (table is None or c[1] == table)]

    def select(self, table, params=None):
        call = ("select", table, params)
        self.calls.append(call)
        self._guard(call)
        return [dict(r) for r in self.tables[table] if _matches(r, params)]

    def insert(self, table, rows):
        call = ("insert", table, rows)
        self.calls.append(call)
        self._guard(call)
        rows = [rows] if isinstance(rows, dict) else rows
        self.tables[table].extend(dict(r) for r in rows)

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
        for row in self.tables[table]:
            if _matches(row, params):
                row.update(data)

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
    }
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
