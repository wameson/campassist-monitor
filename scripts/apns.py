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

APNS_HOSTS = {
    "production": "api.push.apple.com",
    "sandbox": "api.sandbox.push.apple.com",
}
JWT_TTL_SECONDS = 50 * 60
BOOKING_URL_TEMPLATE = "https://www.recreation.gov/camping/campsites/{campsite_id}"

# send_alert outcomes: RETRYABLE_FAILURE means the caller should keep the
# watch's old state_hash so the alert is retried next cycle; DELIVERED and
# PERMANENT_FAILURE both advance it.
DELIVERED = "delivered"
PERMANENT_FAILURE = "permanent-failure"
RETRYABLE_FAILURE = "retryable-failure"


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
        self, watch: dict, openings: list[dict], db, errors: list[str] | None = None
    ) -> str:
        """Push an availability alert for a watch. Returns a delivery outcome:
        DELIVERED, PERMANENT_FAILURE (410 Unregistered — token row deleted —
        other 4xx, or no device token), or RETRYABLE_FAILURE (5xx, 429, or a
        transport-level error, recorded into `errors`)."""
        rows = db.select("device_tokens", {"user_id": f"eq.{watch['user_id']}"})
        if not rows:
            return PERMANENT_FAILURE
        token_row = rows[0]
        first = openings[0]
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
            "booking_url": BOOKING_URL_TEMPLATE.format(campsite_id=first["campsite_id"]),
            "watch_id": watch["id"],
        }
        try:
            resp = self.send(token_row["apns_token"], token_row.get("environment", "production"), payload)
        except httpx.HTTPError as exc:
            if errors is not None:
                errors.append(f"apns {watch['id']}: {exc!r}")
            return RETRYABLE_FAILURE
        if resp.status_code == 200:
            return DELIVERED
        if resp.status_code == 410:
            db.delete("device_tokens", {"user_id": f"eq.{watch['user_id']}"})
            return PERMANENT_FAILURE
        if errors is not None:
            errors.append(f"apns {watch['id']}: HTTP {resp.status_code}")
        if resp.status_code == 429 or resp.status_code >= 500:
            return RETRYABLE_FAILURE
        return PERMANENT_FAILURE
