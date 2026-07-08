"""Shared fakes and factories for the test suite.

FakeDB implements the same interface as db.SupabaseClient against
in-memory tables, supporting the PostgREST filter subset the monitor
uses (eq, in, lt). FakeHTTP stands in for the recreation.gov client.
No test touches the network or real secrets.
"""

from __future__ import annotations

from datetime import datetime, timezone

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


class FakeDB:
    def __init__(self, tables: dict | None = None):
        self.tables: dict[str, list[dict]] = {t: [] for t in TABLES}
        for name, rows in (tables or {}).items():
            self.tables[name] = [dict(r) for r in rows]
        self.calls: list[tuple] = []

    @property
    def write_count(self) -> int:
        return sum(1 for c in self.calls if c[0] != "select")

    def calls_of(self, op: str, table: str | None = None) -> list[tuple]:
        return [c for c in self.calls if c[0] == op and (table is None or c[1] == table)]

    def select(self, table, params=None):
        self.calls.append(("select", table, params))
        return [dict(r) for r in self.tables[table] if _matches(r, params)]

    def insert(self, table, rows):
        self.calls.append(("insert", table, rows))
        rows = [rows] if isinstance(rows, dict) else rows
        self.tables[table].extend(dict(r) for r in rows)

    def upsert(self, table, rows, on_conflict=None):
        self.calls.append(("upsert", table, rows))
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
        self.calls.append(("patch", table, params, data))
        for row in self.tables[table]:
            if _matches(row, params):
                row.update(data)

    def delete(self, table, params):
        self.calls.append(("delete", table, params))
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
