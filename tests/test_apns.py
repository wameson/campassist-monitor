"""APNs client: JWT caching, headers, environment routing, 410 cleanup."""

import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import apns
from helpers import FakeDB, make_watch

TEAM_ID = "TESTTEAM12"
KEY_ID = "TESTKEY456"
BUNDLE_ID = "com.example.campassist"


@pytest.fixture(scope="module")
def signing_key():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def make_client(pem, handler=None, clock=None):
    handler = handler or (lambda request: httpx.Response(200))
    return apns.APNsClient(
        team_id=TEAM_ID,
        key_id=KEY_ID,
        bundle_id=BUNDLE_ID,
        p8_key=pem,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock or (lambda: 1_000_000.0),
    )


OPENINGS = [
    {"campsite_id": "100", "site": "042", "date": "2026-08-10"},
    {"campsite_id": "101", "site": "043", "date": "2026-08-11"},
]


def token_db(environment="production"):
    return FakeDB({"device_tokens": [
        {"user_id": "u1", "apns_token": "devicetoken", "environment": environment},
    ]})


def test_apns_jwt(signing_key):
    key, pem = signing_key
    now = [1_000_000.0]
    client = make_client(pem, clock=lambda: now[0])

    token = client.get_jwt()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == KEY_ID
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == TEAM_ID
    assert claims["iat"] == 1_000_000

    # cached within the run
    assert client.get_jwt() is token
    now[0] += 49 * 60
    assert client.get_jwt() is token

    # refreshed after 50 minutes
    now[0] += 2 * 60
    refreshed = client.get_jwt()
    assert refreshed != token
    assert jwt.decode(refreshed, key.public_key(), algorithms=["ES256"])["iat"] == 1_000_000 + 51 * 60


def test_apns_410_cleanup(signing_key):
    _, pem = signing_key
    client = make_client(pem, handler=lambda request: httpx.Response(410, json={"reason": "Unregistered"}))
    db = token_db()

    delivered = client.send_alert(make_watch(), OPENINGS, db)

    assert delivered is False
    assert db.tables["device_tokens"] == []
    assert db.calls_of("delete", "device_tokens") == [
        ("delete", "device_tokens", {"user_id": "eq.u1"})
    ]


@pytest.mark.parametrize(
    "environment, expected_host",
    [("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")],
)
def test_env_routing(signing_key, environment, expected_host):
    _, pem = signing_key
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    client = make_client(pem, handler=handler)
    assert client.send_alert(make_watch(), OPENINGS, token_db(environment)) is True

    [request] = requests
    assert request.url.host == expected_host
    assert request.url.path == "/3/device/devicetoken"


def test_apns_headers(signing_key):
    key, pem = signing_key
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    client = make_client(pem, handler=handler)
    client.send_alert(make_watch(), OPENINGS, token_db())

    [request] = requests
    assert request.headers["apns-topic"] == BUNDLE_ID
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    bearer = request.headers["authorization"]
    assert bearer.startswith("bearer ")
    jwt.decode(bearer.removeprefix("bearer "), key.public_key(), algorithms=["ES256"])

    payload = json.loads(request.content)
    assert payload["booking_url"] == "https://www.recreation.gov/camping/campsites/100"
    assert payload["watch_id"] == "w1"
    alert = payload["aps"]["alert"]
    assert alert["title"] == "Campsite Available!"
    assert "Upper Pines" in alert["body"] and "2 site(s) open" in alert["body"]


def test_send_alert_without_token_row(signing_key):
    _, pem = signing_key
    client = make_client(pem, handler=lambda request: pytest.fail("must not push without a token"))
    assert client.send_alert(make_watch(), OPENINGS, FakeDB()) is False
