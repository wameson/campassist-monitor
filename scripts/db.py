"""Thin Supabase PostgREST client using the service-role key.

The service-role key bypasses RLS, so this client must only ever run
server-side (GitHub Actions). Every method maps to exactly one HTTP
request; the monitor's write budget (<=5 writes per no-change cycle)
is accounted at this layer.
"""

from __future__ import annotations

import os

import httpx


class SupabaseClient:
    def __init__(self, url: str, service_key: str, http_client: httpx.Client | None = None):
        self._base = url.rstrip("/") + "/rest/v1"
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self._client = http_client or httpx.Client(timeout=30)

    @classmethod
    def from_env(cls) -> "SupabaseClient":
        return cls(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    def select(self, table: str, params: dict | None = None) -> list[dict]:
        resp = self._client.get(
            f"{self._base}/{table}",
            params=params or {},
            headers={**self._headers, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()

    def count(self, table: str, params: dict | None = None) -> int | None:
        """The exact number of rows matching `params`, read as metadata only: a
        GET with `limit=0` so no rows cross the wire and `Prefer: count=exact` so
        PostgREST still reports the full total in `Content-Range`. Returns None
        when the gateway did not report a total. Lets a systemic-vs-isolated
        determination divide by the whole active fleet without a full-fleet row
        read."""
        resp = self._client.get(
            f"{self._base}/{table}",
            params={**(params or {}), "limit": 0},
            headers={
                **self._headers,
                "Accept": "application/json",
                "Prefer": "count=exact",
            },
        )
        resp.raise_for_status()
        return _content_range_total(resp.headers.get("content-range"))

    def insert(self, table: str, rows: dict | list[dict]) -> None:
        resp = self._client.post(
            f"{self._base}/{table}",
            json=rows,
            headers={**self._headers, "Prefer": "return=minimal"},
        )
        resp.raise_for_status()

    def upsert(self, table: str, rows: dict | list[dict], on_conflict: str | None = None) -> None:
        params = {"on_conflict": on_conflict} if on_conflict else {}
        resp = self._client.post(
            f"{self._base}/{table}",
            params=params,
            json=rows,
            headers={**self._headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        resp.raise_for_status()

    def patch(self, table: str, params: dict, data: dict) -> int | None:
        """PATCH `data` onto the rows `params` selects. Returns the number of rows
        the write actually matched (from PostgREST's `Content-Range` header, asked
        for via `count=exact`), or None when the gateway did not report one.

        The count lets a filter-scoped write — e.g. the cycle's blanket
        `last_checked_at` stamp of every monitoring watch — report how many rows it
        covered without a separate read. Still exactly one HTTP request, so the
        write budget is unchanged; `count=exact` only adds a server-side COUNT over
        the same filter. Existing callers ignore the return value."""
        resp = self._client.patch(
            f"{self._base}/{table}",
            params=params,
            json=data,
            headers={**self._headers, "Prefer": "return=minimal,count=exact"},
        )
        resp.raise_for_status()
        return _content_range_total(resp.headers.get("content-range"))

    def delete(self, table: str, params: dict) -> None:
        resp = self._client.delete(
            f"{self._base}/{table}",
            params=params,
            headers={**self._headers, "Prefer": "return=minimal"},
        )
        resp.raise_for_status()


def _content_range_total(header: str | None) -> int | None:
    """The total after the `/` in a PostgREST `Content-Range` (`0-99/100`, or
    `*/100` for a count-only response), or None when the header is missing or the
    total is unknown (`*`)."""
    if not header or "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None
