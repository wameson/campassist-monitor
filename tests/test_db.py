"""SupabaseClient maps each operation to exactly one PostgREST request."""

import json

import httpx
import pytest

from db import SupabaseClient


@pytest.fixture
def client_and_requests():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    client = SupabaseClient(
        "https://project.supabase.co",
        "service-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return client, requests


def test_request_mapping(client_and_requests):
    client, requests = client_and_requests

    assert client.select("watches", {"status": "eq.monitoring"}) == []
    client.insert("run_summaries", {"watches_checked": 0})
    client.upsert("sent_alerts", [{"site_id": "100"}], on_conflict="watch_id,site_id,date")
    client.patch("watches", {"id": "eq.w1"}, {"state_hash": "abc"})
    client.delete("sent_alerts", {"sent_at": "lt.2026-06-01"})

    methods = [(r.method, r.url.path) for r in requests]
    assert methods == [
        ("GET", "/rest/v1/watches"),
        ("POST", "/rest/v1/run_summaries"),
        ("POST", "/rest/v1/sent_alerts"),
        ("PATCH", "/rest/v1/watches"),
        ("DELETE", "/rest/v1/sent_alerts"),
    ]

    select, insert, upsert, patch, delete = requests
    assert select.url.params["status"] == "eq.monitoring"
    assert select.headers["apikey"] == "service-key"
    assert select.headers["authorization"] == "Bearer service-key"
    assert insert.headers["prefer"] == "return=minimal"
    assert upsert.headers["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert upsert.url.params["on_conflict"] == "watch_id,site_id,date"
    assert json.loads(patch.content) == {"state_hash": "abc"}
    assert patch.url.params["id"] == "eq.w1"
    assert delete.url.params["sent_at"] == "lt.2026-06-01"


def test_error_raises(client_and_requests):
    def handler(request):
        return httpx.Response(500)

    client = SupabaseClient(
        "https://project.supabase.co",
        "service-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.select("watches")
