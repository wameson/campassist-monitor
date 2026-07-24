"""Shared fakes and factories for the test suite.

FakeDB implements the same interface as db.SupabaseClient against
in-memory tables, supporting the PostgREST filter subset the monitor
uses (eq, in, lt), and can be given a `fail_on` hook that raises for
chosen calls the way a real PostgREST rejection would. FakeHTTP stands
in for the recreation.gov client. No test touches the network or real
secrets.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from apns import DELIVERED

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

TABLES = ("watches", "device_tokens", "sent_alerts", "run_summaries")


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
        else:
            raise NotImplementedError(f"FakeDB filter op {op!r}")
    return True


FAKE_SERVICE_KEY = "fake-service-role-key"


def postgrest_error(
    status: int = 400,
    message: str = "column watches.x does not exist",
    *,
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
    `details` echoes the offending key values."""
    request = httpx.Request(
        "PATCH",
        "https://project.supabase.invalid/rest/v1/watches?id=in.%28w0,w1%29",
        headers={"apikey": FAKE_SERVICE_KEY, "Authorization": f"Bearer {FAKE_SERVICE_KEY}"},
    )
    response = httpx.Response(
        status,
        request=request,
        json={"code": "42703", "message": message, "details": details, "hint": hint},
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
    def __init__(self, result: str = DELIVERED):
        self.alerts: list[tuple[str, list[dict]]] = []
        self.result = result

    def send_alert(self, watch, openings, db, errors=None) -> str:
        self.alerts.append((watch["id"], openings))
        return self.result


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
