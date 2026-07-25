"""APNs HTTP/2 push client (token-based auth, no server).

ES256 JWT signed with the .p8 key: iss = team id, kid = key id.
Apple rejects tokens older than 60 minutes and throttles refreshes
under 20 minutes, so the JWT is cached and refreshed after 50 minutes.
Each device token routes to the sandbox or production APNs host based
on its stored environment; a 410 response means the token is dead and
its device_tokens row is deleted.
"""

from __future__ import annotations

import os
import time

import httpx
import jwt

from providers import provider_for

APNS_HOSTS = {
    "production": "api.push.apple.com",
    "sandbox": "api.sandbox.push.apple.com",
}
JWT_TTL_SECONDS = 50 * 60

# send_alert outcomes: RETRYABLE_FAILURE and CONFIG_FAILURE both mean the
# caller should keep the watch's old state_hash so the alert is retried next
# cycle (a transient outage / an operator-fixable provider or topic fault);
# DELIVERED and PERMANENT_FAILURE both advance it.
#
# The split within the 4xx space is by *who can fix it*. PERMANENT_FAILURE is a
# per-device rejection — a dead device token, a token not valid for this topic,
# a reason code we do not enumerate, or a push URL too malformed to build — that
# no operator action changes, so it is left out of the run's exit-status rate.
# CONFIG_FAILURE is a pool-wide provider/config fault — an expired or wrong
# signing key, or a wrong bundle id — that rotating a credential fixes and that
# would otherwise silence every push, so it *does* count toward the rate.
DELIVERED = "delivered"
PERMANENT_FAILURE = "permanent-failure"
RETRYABLE_FAILURE = "retryable-failure"
CONFIG_FAILURE = "config-failure"

# APNs `reason` strings that name a pool-wide provider/config fault rather than
# one device's dead token (Apple's "Communicating with APNs" reference). Every
# one of these fails identically for every push until an operator rotates a
# credential or fixes the bundle id, so they are rated as the cycle's own health.
CONFIG_FAILURE_REASONS = frozenset({
    "ExpiredProviderToken",
    "InvalidProviderToken",
    "MissingProviderToken",
    "BadTopic",
    "TopicDisallowed",
})


def apns_reason(response) -> str:
    """The APNs `reason` enum from a rejection body (e.g. 'BadDeviceToken'),
    or '' if the body is missing or unparseable. Only the short enum is read —
    never a value that could carry device or user data."""
    try:
        body = response.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        reason = body.get("reason")
        if isinstance(reason, str):
            return reason
    return ""


class APNsClient:
    def __init__(
        self,
        *,
        team_id: str,
        key_id: str,
        bundle_id: str,
        p8_key: str,
        http_client: httpx.Client | None = None,
        clock=time.time,
    ):
        self.team_id = team_id
        self.key_id = key_id
        self.bundle_id = bundle_id
        self._p8_key = p8_key
        self._client = http_client or httpx.Client(http2=True, timeout=10)
        self._clock = clock
        self._jwt: str | None = None
        self._jwt_issued_at = 0.0

    @classmethod
    def from_env(cls) -> "APNsClient":
        return cls(
            team_id=os.environ["APNS_TEAM_ID"],
            key_id=os.environ["APNS_KEY_ID"],
            bundle_id=os.environ["APNS_BUNDLE_ID"],
            p8_key=os.environ["APNS_P8_KEY"],
        )

    def get_jwt(self) -> str:
        now = self._clock()
        if self._jwt is None or now - self._jwt_issued_at >= JWT_TTL_SECONDS:
            self._jwt = jwt.encode(
                {"iss": self.team_id, "iat": int(now)},
                self._p8_key,
                algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self._jwt_issued_at = now
        return self._jwt

    def send(self, apns_token: str, environment: str, payload: dict) -> httpx.Response:
        host = APNS_HOSTS.get(environment, APNS_HOSTS["production"])
        return self._client.post(
            f"https://{host}/3/device/{apns_token}",
            headers={
                "authorization": f"bearer {self.get_jwt()}",
                "apns-topic": self.bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            },
            json=payload,
        )

    def send_alert(
        self, watch: dict, openings: list[dict], db, failures: list[BaseException] | None = None
    ) -> str:
        """Push an availability alert for a watch. Returns a delivery outcome:
        DELIVERED; PERMANENT_FAILURE (a per-device rejection — 410 Unregistered
        with the token row deleted, 400 BadDeviceToken / DeviceTokenNotForTopic
        or any other unenumerated 4xx, no device token, or a user-supplied token
        so malformed the push URL cannot be built); CONFIG_FAILURE (a pool-wide
        provider/config fault — 403 Expired/Invalid/MissingProviderToken, 400
        BadTopic / TopicDisallowed); or RETRYABLE_FAILURE (5xx, 429, or a
        transport-level error).

        A push that did not land appends the *exception* behind it to
        `failures` — the transport error itself, or an HTTPStatusError carrying
        the APNs response. Attribution and rendering are the caller's: nothing
        here names the watch, so an APNs failure cannot put a watch UUID into
        the world-readable run summary."""
        rows = db.select("device_tokens", {"user_id": f"eq.{watch['user_id']}"})
        if not rows:
            return PERMANENT_FAILURE
        token_row = rows[0]
        payload = {
            "aps": {
                "alert": {
                    "title": "Campsite Available!",
                    "body": (
                        f"{watch['campground_name']} · "
                        f"{watch['start_date']} – {watch['end_date']} · "
                        f"{len(openings)} site(s) open"
                    ),
                },
                "sound": "default",
                "badge": 1,
            },
            # The deep link is the watch's provider's to build: each site has
            # its own booking URL shape, and the host is a constant in the
            # provider module (never derived from provider_ref).
            "booking_url": provider_for(watch).booking_url(watch, openings),
            "watch_id": watch["id"],
        }
        try:
            resp = self.send(token_row["apns_token"], token_row.get("environment", "production"), payload)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            if failures is not None:
                failures.append(exc)
            if isinstance(exc, httpx.InvalidURL):
                return PERMANENT_FAILURE
            return RETRYABLE_FAILURE
        if resp.status_code == 200:
            return DELIVERED
        if resp.status_code == 410:
            db.delete("device_tokens", {"user_id": f"eq.{watch['user_id']}"})
            return PERMANENT_FAILURE
        if failures is not None:
            failures.append(
                httpx.HTTPStatusError(
                    f"apns push rejected with {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            return RETRYABLE_FAILURE
        if apns_reason(resp) in CONFIG_FAILURE_REASONS:
            return CONFIG_FAILURE
        return PERMANENT_FAILURE
