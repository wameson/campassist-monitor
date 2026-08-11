"""APNs HTTP/2 push client (token-based auth, no server).

ES256 JWT signed with the .p8 key: iss = team id, kid = key id.
Apple rejects tokens older than 60 minutes and throttles refreshes
under 20 minutes, so the JWT is cached and refreshed after 50 minutes.
Each device token routes to the sandbox or production APNs host based
on its stored environment; a 410 (Unregistered), or a 400 naming the
token itself as dead, means the token is dead and its device_tokens
row is deleted.
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

# APNs `reason` strings on a 400 that name *this device's token* as dead rather
# than a transient or pool-wide fault: the token is invalid as of now and will
# never deliver, so its row is pruned (the app re-registers a fresh token on its
# next launch). A 410 Unregistered is the canonical form of the same signal and
# always prunes regardless of body; these cover the 400 restatement Apple can
# return for a token that is bad for this environment/topic pairing.
DEAD_TOKEN_REASONS = frozenset({
    "Unregistered",
    "BadDeviceToken",
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

    def _send_to_token(
        self, watch: dict, token_row: dict, payload: dict, db,
        collected: list[tuple[str, BaseException]],
    ) -> str:
        """Push to one device token and classify the single-device outcome:
        DELIVERED; PERMANENT_FAILURE (a per-device rejection — a dead device
        token whose row is pruned [410 Unregistered, or a 400 naming the token
        itself: BadDeviceToken / Unregistered], a 400 DeviceTokenNotForTopic or
        any other unenumerated 4xx, or a user-supplied token so malformed the
        push URL cannot be built); CONFIG_FAILURE (a pool-wide provider/config
        fault — 403 Expired/Invalid/MissingProviderToken, 400 BadTopic /
        TopicDisallowed); or RETRYABLE_FAILURE (5xx, 429, or a transport-level
        error). Any push that did not land appends its (outcome, exception) pair
        to `collected` — send_alert aggregates across a user's tokens and only
        then decides which of those exceptions reach the caller's failures list
        (a per-device rejection is suppressed once a sibling device delivered)."""
        try:
            resp = self.send(token_row["apns_token"], token_row.get("environment", "production"), payload)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            if isinstance(exc, httpx.InvalidURL):
                outcome = PERMANENT_FAILURE
            else:
                outcome = RETRYABLE_FAILURE
            collected.append((outcome, exc))
            return outcome
        if resp.status_code == 200:
            return DELIVERED
        reason = apns_reason(resp)
        if resp.status_code == 410 or (
            resp.status_code == 400 and reason in DEAD_TOKEN_REASONS
        ):
            # Prune only THIS dead (user_id, apns_token) row so it is not retried
            # and stops counting as a live recipient. Keyed on both columns (the
            # composite PK since migration 0012), so one dead device can never
            # wipe out the same user's other, live device tokens — and no other
            # user's row is touched. No extra per-watch write on a no-change
            # cycle (a cycle only reaches here when it has an alert to send).
            db.delete(
                "device_tokens",
                {"user_id": f"eq.{watch['user_id']}", "apns_token": f"eq.{token_row['apns_token']}"},
            )
            # A 410 is a clean prune, not an operator-facing failure, so it is
            # reported to neither audience. A 400 rejection is still collected
            # below for parity with every other 4xx (unrated all the same).
            if resp.status_code == 410:
                return PERMANENT_FAILURE
        if resp.status_code == 429 or resp.status_code >= 500:
            outcome = RETRYABLE_FAILURE
        elif reason in CONFIG_FAILURE_REASONS:
            outcome = CONFIG_FAILURE
        else:
            outcome = PERMANENT_FAILURE
        collected.append((
            outcome,
            httpx.HTTPStatusError(
                f"apns push rejected with {resp.status_code}",
                request=resp.request,
                response=resp,
            ),
        ))
        return outcome

    def send_alert(
        self, watch: dict, openings: list[dict], db, failures: list[BaseException] | None = None
    ) -> str:
        """Push an availability alert for a watch, fanning out to EVERY device
        token the user has registered (device_tokens is keyed on the composite
        (user_id, apns_token) since migration 0012, so a user can hold more than
        one). Returns one aggregated delivery outcome for the whole fan-out:

        - DELIVERED    if at least one token returned 200 — the opening reached a
                       device, so the caller marks sent_alerts and advances the
                       state_hash (a device that missed it is covered by APNs
                       store-and-forward, and re-alerting the whole set on the
                       next opening would spam the ones that did get it).
        - CONFIG_FAILURE  else, if any token hit a pool-wide provider/config
                       fault — an operator must rotate a credential or fix the
                       bundle id; rated, and the hash is kept so it retries.
        - RETRYABLE_FAILURE  else, if any token was 5xx/429/transport — keep the
                       hash and retry the whole set next cycle.
        - PERMANENT_FAILURE  else (no tokens at all, or every token was a
                       per-device rejection) — advance the hash so dead tokens
                       are not retried forever; unrated.

        Fan-out is atomic inside this one call, so an opening is still one alert
        event deduped once by (watch_id, site_id, date); it is simply delivered
        to N devices. A dead token is pruned per (user_id, apns_token), never by
        user_id, so cleaning up one device never silences another.

        Each push that did not land contributes the *exception* behind it to
        `failures` — the transport error itself, or an HTTPStatusError carrying
        the APNs response — regardless of the aggregate, so a per-device
        rejection stays visible to the operator even on a DELIVERED fan-out
        (e.g. a dead sibling token pruned while the live phone got the push).
        Reporting is decoupled from rating: whether a reported failure counts
        toward the exit-status rate is the caller's decision, made on the
        aggregate outcome (only RETRYABLE_FAILURE / CONFIG_FAILURE are rated), so
        surfacing a per-device rejection here can never redden a delivered watch.
        The one rejection never reported is the 410 clean-prune, which
        `_send_to_token` declines to collect at all. Attribution and rendering
        are the caller's: nothing here names the watch, so an APNs failure cannot
        put a watch UUID into the world-readable run summary."""
        rows = db.select("device_tokens", {"user_id": f"eq.{watch['user_id']}"})
        if not rows:
            return PERMANENT_FAILURE
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
        collected: list[tuple[str, BaseException]] = []
        outcomes = [
            self._send_to_token(watch, token_row, payload, db, collected)
            for token_row in rows
        ]
        # Aggregate by priority: any delivery wins; otherwise the most
        # operator-actionable still-open outcome (config > retryable) wins over a
        # plain per-device rejection.
        if DELIVERED in outcomes:
            aggregate = DELIVERED
        elif CONFIG_FAILURE in outcomes:
            aggregate = CONFIG_FAILURE
        elif RETRYABLE_FAILURE in outcomes:
            aggregate = RETRYABLE_FAILURE
        else:
            aggregate = PERMANENT_FAILURE
        # Report every collected exception to the caller, whatever the
        # aggregate. A sibling token's per-device rejection stays visible on a
        # DELIVERED fan-out so the operator can see a dead device was pruned;
        # rating is the caller's separate decision (it rates on the aggregate
        # outcome, not on the presence of a failure), so a delivered watch is
        # reported-but-unrated rather than counted as systemic. The only
        # rejection never here is the 410 clean-prune, which _send_to_token does
        # not collect.
        if failures is not None:
            failures.extend(exc for _, exc in collected)
        return aggregate
