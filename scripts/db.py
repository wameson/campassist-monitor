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

    def patch(self, table: str, params: dict, data: dict) -> None:
        resp = self._client.patch(
            f"{self._base}/{table}",
            params=params,
            json=data,
            headers={**self._headers, "Prefer": "return=minimal"},
        )
        resp.raise_for_status()

    def delete(self, table: str, params: dict) -> None:
        resp = self._client.delete(
            f"{self._base}/{table}",
            params=params,
            headers={**self._headers, "Prefer": "return=minimal"},
        )
        resp.raise_for_status()
