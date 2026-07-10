"""CampAssist availability monitor — one cycle per GitHub Actions run.

Cycle: jittered start → read active watches → expire past-date watches →
error watches with invalid campground ids →
dedupe poll plan by (campground_id, month) → poll recreation.gov politely
(one browser UA per run, shuffled order, 1.2–2.8 s gaps, exponential
backoff, all under a per-cycle time budget) → delta-detect per watch via
state_hash → APNs alert with (site, date) dedup + 6 h cooldown → batched
last_checked_at write → one run_summaries row → 30-day retention pruning.

Write budget (see PLAN.md): a cycle with no availability changes performs
at most 5 DB writes regardless of watch count.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from apns import DELIVERED, RETRYABLE_FAILURE, APNsClient
from db import SupabaseClient

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

AVAILABILITY_URL = "https://www.recreation.gov/api/camps/availability/campground/{campground_id}/month"

START_JITTER_MAX_SECONDS = 240.0
INTER_REQUEST_DELAY_RANGE = (1.2, 2.8)
BACKOFF_DELAYS_SECONDS = [2, 4, 8]
# Keeps jitter (≤240 s) + polling + one in-flight request (≤20 s) + the
# bookkeeping writes inside the workflow's 15-minute timeout even under
# sustained 403/429 blocking.
CYCLE_TIME_BUDGET_SECONDS = 480.0
# Fixed westmost-US offset (UTC-8, no DST) for deriving "today": same-night
# openings at US campgrounds stay alertable during US evening hours after
# UTC midnight.
WESTMOST_US_OFFSET = timezone(timedelta(hours=-8))
RETRYABLE_STATUS = {403, 429}
ALERT_COOLDOWN_HOURS = 6
RETENTION_DAYS = 30
POLL_HORIZON_MONTHS = 12
CAMPGROUND_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


# --- jitter ---------------------------------------------------------------

def start_delay(rng: random.Random) -> float:
    """Random run-start delay to desynchronize from the exact cron tick."""
    return rng.uniform(0, START_JITTER_MAX_SECONDS)


def inter_request_delay(rng: random.Random) -> float:
    """Humanized gap between recreation.gov requests."""
    return rng.uniform(*INTER_REQUEST_DELAY_RANGE)


# --- dates ----------------------------------------------------------------

def as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def iso_now(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def monitor_today(now: datetime) -> date:
    """'Today' for the poll plan, alert date filter, and watch expiry."""
    return now.astimezone(WESTMOST_US_OFFSET).date()


def horizon_month(today: date) -> date:
    """First-of-month containing today + POLL_HORIZON_MONTHS: the last
    month the poll plan may include."""
    years, month0 = divmod(today.month - 1 + POLL_HORIZON_MONTHS, 12)
    return date(today.year + years, month0 + 1, 1)


def months_for_watch(start: date, end: date, today: date) -> list[date]:
    """First-of-month dates covering the stay's remaining nights — one API
    call each. The check-out day's month is not polled, and months entirely
    in the past or beyond the polling horizon (today + POLL_HORIZON_MONTHS)
    are skipped; a watch wholly beyond the horizon yields no months until
    the horizon reaches it."""
    last_night = end - timedelta(days=1) if end > start else start
    last_month = min(last_night, horizon_month(today))
    months = []
    cur = max(start, today).replace(day=1)
    while cur <= last_month:
        months.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)
    return months


def date_in_watch(d: date, start: date, end: date) -> bool:
    """Nights of the stay: check-out day availability is irrelevant."""
    if end > start:
        return start <= d < end
    return d == start


# --- poll plan ------------------------------------------------------------

def dedupe_poll_plan(watches: list[dict], today: date) -> list[tuple[str, date]]:
    """One (campground_id, month) entry per unique pair across ALL users."""
    plan = set()
    for watch in watches:
        start = as_date(watch["start_date"])
        end = as_date(watch["end_date"])
        for month in months_for_watch(start, end, today):
            plan.add((str(watch["campground_id"]), month))
    return sorted(plan)


# --- recreation.gov -------------------------------------------------------

def parse_availability(raw) -> dict[str, dict]:
    """Defensively parse a recreation.gov month-availability response.

    Missing or renamed fields degrade to a partial parse — never a crash.
    Returns {campsite_id: {"campsite_id", "site", "availabilities": {date: status}}}.
    """
    sites: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return sites
    campsites = raw.get("campsites")
    if not isinstance(campsites, dict):
        return sites
    for cs_key, cs in campsites.items():
        if not isinstance(cs, dict):
            continue
        availabilities = cs.get("availabilities")
        dates: dict[str, str] = {}
        if isinstance(availabilities, dict):
            for date_str, status in availabilities.items():
                if not isinstance(status, str):
                    continue
                try:
                    d = as_date(date_str)
                except (ValueError, TypeError):
                    continue
                dates[d.isoformat()] = status
        campsite_id = cs.get("campsite_id", cs_key)
        site = cs.get("site")
        sites[str(cs_key)] = {
            "campsite_id": str(campsite_id),
            "site": site if isinstance(site, str) else str(cs_key),
            "availabilities": dates,
        }
    return sites


def poll_with_backoff(
    http: httpx.Client,
    campground_id: str,
    month: date,
    user_agent: str,
    *,
    sleep=time.sleep,
    errors: list[str] | None = None,
    budget_exhausted=lambda: False,
) -> dict[str, dict] | None:
    """GET one campground-month with exponential backoff on 403/429/5xx.

    Retries after 2 s, 4 s, 8 s, then gives up for this cycle (returns
    None) so the rest of the run continues. Once budget_exhausted()
    reports the cycle's time budget is spent, remaining retries and their
    backoff sleeps are skipped.
    """
    url = AVAILABILITY_URL.format(campground_id=campground_id)
    params = {"start_date": f"{month.isoformat()}T00:00:00.000Z"}
    headers = {"User-Agent": user_agent, "Accept": "application/json"}

    for attempt in range(len(BACKOFF_DELAYS_SECONDS) + 1):
        try:
            resp = http.get(url, params=params, headers=headers)
            status = resp.status_code
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            status = None
            failure = f"{campground_id}/{month.isoformat()}: {exc!r}"
        if status == 200:
            try:
                return parse_availability(resp.json())
            except ValueError:
                failure = f"{campground_id}/{month.isoformat()}: invalid JSON"
                break
        if status is not None:
            failure = f"{campground_id}/{month.isoformat()}: HTTP {status}"
            if not (status in RETRYABLE_STATUS or status >= 500):
                break
        if attempt < len(BACKOFF_DELAYS_SECONDS):
            if budget_exhausted():
                break
            sleep(BACKOFF_DELAYS_SECONDS[attempt])
    if errors is not None:
        errors.append(failure)
    return None


# --- delta detection ------------------------------------------------------

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def state_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def extract_relevant(availability: dict, watch: dict, today: date) -> dict[str, dict] | None:
    """Current open-site state relevant to one watch, or None if any of
    the watch's months failed to poll this cycle (keep old hash, retry
    next run rather than hashing partial data). Past nights are excluded:
    they are unbookable, so they count toward neither the hash nor alerts.
    A watch wholly beyond the polling horizon has no pollable months yet,
    so it also returns None; a watch straddling the horizon is hashed on
    its in-horizon months alone."""
    start = as_date(watch["start_date"])
    end = as_date(watch["end_date"])
    wanted = {str(s) for s in (watch.get("site_ids") or [])}

    months = months_for_watch(start, end, today)
    if not months:
        return None

    merged: dict[str, dict] = {}
    for month in months:
        parsed = availability.get((str(watch["campground_id"]), month))
        if parsed is None:
            return None
        for cs_id, cs in parsed.items():
            entry = merged.setdefault(
                cs_id, {"campsite_id": cs["campsite_id"], "site": cs["site"], "dates": {}}
            )
            entry["dates"].update(cs["availabilities"])

    current: dict[str, dict] = {}
    for cs_id, cs in merged.items():
        if wanted and not ({cs_id, cs["campsite_id"], cs["site"]} & wanted):
            continue
        open_dates = sorted(
            d
            for d, status in cs["dates"].items()
            if status == "Available"
            and as_date(d) >= today
            and date_in_watch(as_date(d), start, end)
        )
        if open_dates:
            current[cs_id] = {
                "campsite_id": cs["campsite_id"],
                "site": cs["site"],
                "dates": open_dates,
            }
    return current


def available_sites(current: dict[str, dict]) -> list[dict]:
    openings = [
        {"campsite_id": cs["campsite_id"], "site": cs["site"], "date": d}
        for cs in current.values()
        for d in cs["dates"]
    ]
    openings.sort(key=lambda o: (o["date"], o["site"]))
    return openings


# --- alert dedup ----------------------------------------------------------

def filter_unalerted(
    db, watch: dict, openings: list[dict], now: datetime, cooldown_hours: int = ALERT_COOLDOWN_HOURS
) -> list[dict]:
    """Drop openings whose (site, date) was alerted within the cooldown."""
    if not openings:
        return []
    rows = db.select("sent_alerts", {"watch_id": f"eq.{watch['id']}"})
    cutoff = now - timedelta(hours=cooldown_hours)
    recent = {
        (str(r["site_id"]), as_date(r["date"]).isoformat())
        for r in rows
        if parse_timestamp(r["sent_at"]) > cutoff
    }
    return [o for o in openings if (o["campsite_id"], o["date"]) not in recent]


def alert_rows(watch: dict, openings: list[dict], now: datetime) -> list[dict]:
    return [
        {
            "watch_id": watch["id"],
            "site_id": o["campsite_id"],
            "date": o["date"],
            "sent_at": iso_now(now),
        }
        for o in openings
    ]


# --- main cycle -----------------------------------------------------------

def run(
    db,
    apns,
    http,
    *,
    rng: random.Random | None = None,
    sleep=time.sleep,
    now_fn=lambda: datetime.now(timezone.utc),
    monotonic=time.monotonic,
    time_budget_seconds: float = CYCLE_TIME_BUDGET_SECONDS,
) -> dict:
    rng = rng or random.Random()
    started = monotonic()
    deadline = started + time_budget_seconds

    def budget_exhausted() -> bool:
        return monotonic() >= deadline

    errors: list[str] = []
    now = now_fn()
    today = monitor_today(now)

    watches = db.select("watches", {"status": "eq.monitoring"})

    # Backend-owned lifecycle: expire past-date watches (one batched write)
    expired_ids = {w["id"] for w in watches if as_date(w["end_date"]) < today}
    if expired_ids:
        db.patch(
            "watches",
            {"id": f"in.({','.join(sorted(str(i) for i in expired_ids))})"},
            {"status": "expired"},
        )
    active = [w for w in watches if w["id"] not in expired_ids]

    # Backend-owned lifecycle: watches with malformed campground ids can
    # never poll successfully, so they move to status='error' once (one
    # batched write, failure case only) instead of re-erroring every cycle.
    invalid = [w for w in active if not CAMPGROUND_ID_RE.fullmatch(str(w["campground_id"]))]
    if invalid:
        for campground_id in sorted({str(w["campground_id"]) for w in invalid}):
            errors.append(f"{campground_id!r}: invalid campground_id, skipped")
        db.patch(
            "watches",
            {"id": f"in.({','.join(sorted(str(w['id']) for w in invalid))})"},
            {"status": "error"},
        )
        invalid_watch_ids = {w["id"] for w in invalid}
        active = [w for w in active if w["id"] not in invalid_watch_ids]

    plan = dedupe_poll_plan(active, today)
    rng.shuffle(plan)
    session_ua = rng.choice(USER_AGENTS)  # one UA per run, rotated across runs

    availability: dict[tuple[str, date], dict | None] = {}
    for i, (campground_id, month) in enumerate(plan):
        if budget_exhausted():
            errors.append(
                f"time budget exhausted: skipped {len(plan) - i} remaining poll(s)"
            )
            break
        availability[(campground_id, month)] = poll_with_backoff(
            http, campground_id, month, session_ua,
            sleep=sleep, errors=errors, budget_exhausted=budget_exhausted,
        )
        if i < len(plan) - 1 and not budget_exhausted():
            sleep(inter_request_delay(rng))

    alerts_sent = 0
    for watch in active:
        current = extract_relevant(availability, watch, today)
        if current is None:
            continue  # poll failed for this watch's months; keep old hash
        new_hash = state_hash(current)
        if new_hash == watch.get("state_hash"):
            continue
        openings = available_sites(current)
        fresh = filter_unalerted(db, watch, openings, now)
        delivered = False
        if fresh:
            outcome = apns.send_alert(watch, fresh, db, errors=errors)
            if outcome == RETRYABLE_FAILURE:
                continue  # keep old hash so the alert is retried next cycle
            if outcome == DELIVERED:
                db.upsert("sent_alerts", alert_rows(watch, fresh, now), on_conflict="watch_id,site_id,date")
                alerts_sent += len(fresh)
                delivered = True
        db.patch(
            "watches",
            {"id": f"eq.{watch['id']}"},
            {"state_hash": new_hash, **({"last_found_at": iso_now(now)} if delivered else {})},
        )

    if active:
        db.patch(
            "watches",
            {"id": f"in.({','.join(sorted(str(w['id']) for w in active))})"},
            {"last_checked_at": iso_now(now)},
        )

    summary = {
        "watches_checked": len(active),
        "campgrounds_polled": len({cg for cg, _ in plan}),
        "alerts_sent": alerts_sent,
        "duration_ms": int((monotonic() - started) * 1000),
        "errors": "; ".join(errors) or None,
    }
    db.insert("run_summaries", summary)

    retention_cutoff = iso_now(now - timedelta(days=RETENTION_DAYS))
    db.delete("sent_alerts", {"sent_at": f"lt.{retention_cutoff}"})
    db.delete("run_summaries", {"ran_at": f"lt.{retention_cutoff}"})
    return summary


def error_annotation(summary: dict) -> str | None:
    """GitHub Actions warning annotation when the cycle recorded errors,
    surfacing them in the run history while the exit code stays 0 so
    scheduled runs remain green."""
    if summary.get("errors"):
        return f"::warning::monitor completed with errors: {summary['errors']}"
    return None


def main() -> None:
    rng = random.Random()
    delay = start_delay(rng)
    print(f"start jitter: sleeping {delay:.0f}s", flush=True)
    time.sleep(delay)

    db = SupabaseClient.from_env()
    apns = APNsClient.from_env()
    with httpx.Client(http2=True, timeout=20, follow_redirects=True) as http:
        summary = run(db, apns, http, rng=rng)
    print(json.dumps(summary), flush=True)
    warning = error_annotation(summary)
    if warning:
        print(warning, flush=True)


if __name__ == "__main__":
    main()
