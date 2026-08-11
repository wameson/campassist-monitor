"""Per-provider poll sharding (fm/monitor-provider-shard).

The poll used to run once per cycle, sharing one CYCLE_TIME_BUDGET_SECONDS across
all three providers — so a going_to_camp-heavy fleet (~11 s/park) could consume
the budget and starve recreation.gov (~2.5-3 s/campground-month) of poll time in
the same run. The fix runs one job per provider, each scoped to its own watches
with its own budget, and moves the once-per-cycle retention prune to a single
owner (the plan job). These tests pin: the scope, the per-provider budgets
closing the job-timeout arithmetic, prune ownership, the labelled summary row,
and the plan job's matrix.

Fully offline: no network, no real secrets.
"""

from __future__ import annotations

import json
import random

import pytest

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeHTTP,
    FakeResponse,
    availability_payload,
    make_gtc_watch,
    make_usedirect_watch,
    make_watch,
    postgrest_error,
)
from providers import PROVIDERS

QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)
OPEN = availability_payload({"100": {"2026-08-10": "Available"}})
EMPTY = availability_payload({})


def rec_watch(**kw) -> dict:
    """A recreation.gov watch with the provider column populated the way a live
    row is (NOT NULL DEFAULT 'recreation_gov') — make_watch omits it."""
    return make_watch(provider="recreation_gov", **kw)


def rec_run(db, payload=OPEN, *, prune=False, apns=None, **kw):
    apns = apns or FakeAPNs()
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    result = monitor.run(
        db, apns, http, provider="recreation_gov", prune=prune, **QUIET, **kw
    )
    return result, apns, http


# --- profiles and the per-job budget arithmetic ----------------------------

def test_every_registered_provider_has_a_poll_profile():
    # a new conformer without a budget profile must fail loudly, not silently
    # poll on the default budget
    assert set(monitor.POLL_PROFILES) == set(PROVIDERS)


def test_provider_budget_arithmetic_closes_for_each_job():
    # the invariant the single-budget test held, now per provider: setup +
    # fan-out deadline + shutdown fits the job timeout, and a worst-case jitter
    # plus a fully spent poll budget still reaches the fan-out
    for name, profile in monitor.POLL_PROFILES.items():
        assert (
            monitor.JOB_SETUP_RESERVE_SECONDS
            + profile.fanout_deadline_seconds
            + monitor.SHUTDOWN_RESERVE_SECONDS
        ) <= profile.job_timeout_seconds, name
        assert (
            monitor.START_JITTER_MAX_SECONDS + profile.time_budget_seconds
            <= profile.fanout_deadline_seconds
        ), name


def test_going_to_camp_gets_a_larger_budget_than_the_cheap_providers():
    # the costly provider (a fleet leans on it) is sized up, not left to share
    gtc = monitor.POLL_PROFILES["going_to_camp"]
    rec = monitor.POLL_PROFILES["recreation_gov"]
    assert gtc.time_budget_seconds > rec.time_budget_seconds
    assert gtc.job_timeout_seconds > rec.job_timeout_seconds


# --- scope: a provider job serves only its own watches ---------------------

def test_run_scoped_to_a_provider_reads_and_serves_only_that_provider():
    db = FakeDB({"watches": [
        rec_watch(id="w-rec", campground_id="232447"),
        make_gtc_watch(id="w-gtc"),
        make_usedirect_watch(id="w-use"),
    ]})

    result, apns, http = rec_run(db)

    # only the recreation campground was ever requested
    assert [r["campground_id"] for r in http.requests] == ["232447"]
    # the other providers' rows are untouched: not polled, not written
    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-gtc"]["state_hash"] is None and rows["w-gtc"]["last_checked_at"] is None
    assert rows["w-use"]["state_hash"] is None and rows["w-use"]["last_checked_at"] is None
    assert rows["w-gtc"]["status"] == "monitoring" and rows["w-use"]["status"] == "monitoring"
    # the recreation watch was served, and the summary counts only its slice
    assert [w for w, _ in apns.alerts] == ["w-rec"]
    assert result["watches_checked"] == 1
    assert result["provider"] == "recreation_gov"


def test_a_provider_jobs_writes_never_reach_another_providers_rows():
    # the core isolation guarantee: the recreation job updates its own rows and
    # leaves going_to_camp's completely alone, whatever it does
    db = FakeDB({"watches": [
        rec_watch(id="w-rec", campground_id="232447", state_hash="stale"),
        make_gtc_watch(id="w-gtc", state_hash="stale"),
    ]})

    rec_run(db)

    rows = {r["id"]: r for r in db.tables["watches"]}
    assert rows["w-rec"]["state_hash"] != "stale"          # served
    assert rows["w-rec"]["last_checked_at"] is not None
    assert rows["w-gtc"]["state_hash"] == "stale"          # untouched
    assert rows["w-gtc"]["last_checked_at"] is None
    # every watches write this run made was scoped to the recreation slice: the
    # per-id lifecycle/last_checked PATCHes name only recreation ids, and the
    # filter-scoped blanket last_checked stamp carries a provider=eq.recreation_gov
    # filter (no id list) so it, too, can never reach the going_to_camp row.
    for call in db.calls_of("patch", "watches"):
        filt = call[2]
        if "id" in filt:
            ids = str(filt["id"]).partition(".")[2].strip("()").split(",")
            assert "w-gtc" not in ids
        else:
            assert filt.get("provider") == "eq.recreation_gov"


def test_scoped_read_falls_back_when_the_provider_column_is_absent():
    # on a database without 0002 the server-side provider filter is unusable, so
    # the read falls back to select=* and filters by provider_name, which
    # .get()-defaults an absent column to recreation_gov — a pre-0002 DB has only
    # recreation.gov watches, and the recreation job must still serve them
    def reject_projection(call):
        if call[0] == "select" and call[1] == "watches":
            params = call[2] or {}
            if "provider" in str(params.get("select", "")) or "provider" in params:
                return postgrest_error(400, "column watches.provider does not exist")
        return None

    # make_watch omits the provider column, standing in for a pre-0002 row
    db = FakeDB({"watches": [make_watch(id="w0"), make_watch(id="w1")]}, fail_on=reject_projection)
    watches = monitor.read_monitoring_watches(db, "recreation_gov")
    assert sorted(w["id"] for w in watches) == ["w0", "w1"]
    # and the other providers' jobs correctly get nothing from that same DB
    assert monitor.read_monitoring_watches(db, "going_to_camp") == []


# --- prune ownership -------------------------------------------------------

def test_a_poll_job_never_prunes():
    db = FakeDB({"watches": [rec_watch(id="w-rec", campground_id="232447")]})
    rec_run(db, EMPTY, prune=False)
    assert db.calls_of("delete") == []


def test_per_provider_steady_state_stays_within_two_writes():
    # a no-change per-provider cycle: 1 batched last_checked_at PATCH + 1
    # run_summaries INSERT, and NO prune deletes (the plan job owns those)
    db = FakeDB({"watches": [
        rec_watch(id=f"w{i}", user_id=f"u{i}", campground_id="232447") for i in range(20)
    ]})
    rec_run(db, EMPTY, prune=False)  # seed state_hash
    db.calls.clear()
    rec_run(db, EMPTY, prune=False)
    assert db.write_count <= 2
    assert len(db.calls_of("patch")) == 1
    assert len(db.calls_of("insert")) == 1
    assert db.calls_of("delete") == []


def test_prune_retention_deletes_the_three_tables_once():
    old = monitor.iso_now(NOW - __import__("datetime").timedelta(days=40))
    recent = monitor.iso_now(NOW - __import__("datetime").timedelta(days=1))
    db = FakeDB({
        "sent_alerts": [
            {"id": "s-old", "watch_id": "w1", "site_id": "1", "date": "2026-06-01", "sent_at": old},
            {"id": "s-new", "watch_id": "w1", "site_id": "1", "date": "2026-07-30", "sent_at": recent},
        ],
        "run_summaries": [
            {"id": "r-old", "ran_at": old}, {"id": "r-new", "ran_at": recent},
        ],
        "alert_history": [
            {"id": "h-old", "delivered_at": old}, {"id": "h-new", "delivered_at": recent},
        ],
    })

    rated, warn = monitor.prune_retention(db, NOW)

    assert rated == {} and warn is None
    assert len(db.calls_of("delete")) == 3
    assert [r["id"] for r in db.tables["sent_alerts"]] == ["s-new"]
    assert [r["id"] for r in db.tables["run_summaries"]] == ["r-new"]
    assert [r["id"] for r in db.tables["alert_history"]] == ["h-new"]


def test_prune_retention_contains_each_failure_by_scope():
    def fail(call):
        if call[0] == "delete" and call[1] == "sent_alerts":
            return postgrest_error(400, "sent_alerts boom")
        if call[0] == "delete" and call[1] == "alert_history":
            return postgrest_error(400, "alert_history absent", code="PGRST205")
        return None

    db = FakeDB({}, fail_on=fail)
    rated, warn = monitor.prune_retention(db, NOW)

    # sent_alerts is a HALT table: its failure is rated; run_summaries still ran
    assert set(rated) == {"sent_alerts prune"}
    # alert_history is WARN: reported via `warn`, never in the rated set
    assert warn is not None
    assert len(db.calls_of("delete")) == 3  # one failure never stops the others


# --- the labelled summary row ---------------------------------------------

def test_scoped_summary_row_is_labelled_with_its_provider():
    db = FakeDB({"watches": [rec_watch(id="w-rec", campground_id="232447")]})
    rec_run(db, EMPTY, prune=False)
    rows = db.tables["run_summaries"]
    assert len(rows) == 1 and rows[0]["provider"] == "recreation_gov"


def test_monolithic_summary_row_omits_provider():
    # the whole-fleet run writes no provider label, so its row (and every row
    # predating 0013) is byte-identical to before and needs no migration
    db = FakeDB({"watches": [make_watch(id="w0", campground_id="232447")]})
    monitor.run(db, FakeAPNs(), FakeHTTP(lambda cg: FakeResponse(200, EMPTY)), **QUIET)
    assert "provider" not in db.tables["run_summaries"][0]


def test_summary_provider_label_is_drift_tolerant():
    # a database without 0013 rejects the provider column; the insert drops it and
    # retries, so the row is still written and the run is not reddened
    def reject_provider(call):
        if call[0] == "insert" and call[1] == "run_summaries" and "provider" in call[2]:
            return postgrest_error(
                400, "Could not find the 'provider' column of 'run_summaries'", code="PGRST204"
            )
        return None

    db = FakeDB({"watches": [rec_watch(id="w-rec", campground_id="232447")]}, fail_on=reject_provider)
    result, _, _ = rec_run(db, EMPTY, prune=False)

    assert result["systemic_failure"] is False
    rows = db.tables["run_summaries"]
    assert len(rows) == 1 and "provider" not in rows[0]  # written, minus the label
    assert len(db.calls_of("insert", "run_summaries")) == 2  # rejected, then retried


# --- providers_with_watches (the plan job's matrix input) ------------------

def test_providers_with_watches_lists_only_present_registered_providers():
    db = FakeDB({"watches": [
        rec_watch(id="w-rec"),
        make_gtc_watch(id="w-gtc"),
        make_watch(id="w-future", provider="some_future_site"),  # unregistered
    ]})
    assert monitor.providers_with_watches(db) == ["going_to_camp", "recreation_gov"]


def test_providers_with_watches_empty_fleet_is_empty():
    assert monitor.providers_with_watches(FakeDB({"watches": []})) == []


def test_providers_with_watches_falls_back_to_all_on_a_read_blip():
    # a plan-side read failure must never silently drop a provider's whole poll,
    # so it runs every provider rather than none
    def boom(call):
        if call[0] == "select" and call[1] == "watches":
            return postgrest_error(503, "upstream", code="XXUNK")
        return None

    db = FakeDB({"watches": [rec_watch(id="w0")]}, fail_on=boom)
    assert monitor.providers_with_watches(db) == sorted(PROVIDERS)


def test_providers_with_watches_tolerates_a_missing_provider_column():
    # 0002 unapplied: the projected read is rejected, the retry drops the column,
    # and every row reads as recreation_gov
    def reject_provider_projection(call):
        if call[0] == "select" and call[1] == "watches":
            if "provider" in str((call[2] or {}).get("select", "")):
                return postgrest_error(400, "column watches.provider does not exist")
        return None

    db = FakeDB({"watches": [make_watch(id="w0"), make_watch(id="w1")]},
                fail_on=reject_provider_projection)
    assert monitor.providers_with_watches(db) == ["recreation_gov"]


# --- the plan job entrypoint ----------------------------------------------

def test_plan_main_emits_matrix_and_prunes(monkeypatch, tmp_path):
    db = FakeDB({"watches": [rec_watch(id="w-rec"), make_gtc_watch(id="w-gtc")]})
    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: db))
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    monitor.plan_main()  # clean prune → no SystemExit

    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["run_poll"] == "true"
    matrix = json.loads(lines["matrix"])
    entries = {e["provider"]: e["timeout"] for e in matrix["include"]}
    assert set(entries) == {"going_to_camp", "recreation_gov"}  # not use_direct: no watches
    # timeouts come from the profiles (minutes), so the job and its budget agree
    assert entries["going_to_camp"] == int(monitor.POLL_PROFILES["going_to_camp"].job_timeout_seconds // 60)
    assert entries["recreation_gov"] == 15
    assert len(db.calls_of("delete")) == 3  # the plan job owns the prune


def test_plan_main_empty_fleet_disables_polling(monkeypatch, tmp_path):
    db = FakeDB({"watches": []})
    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: db))
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    monitor.plan_main()

    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["run_poll"] == "false"
    assert json.loads(lines["matrix"]) == {"include": []}
    assert len(db.calls_of("delete")) == 3  # pruning still runs on an empty fleet


def test_plan_main_reddens_on_a_rated_prune_failure(monkeypatch, tmp_path):
    def fail(call):
        if call[0] == "delete" and call[1] == "run_summaries":
            return postgrest_error(400, "run_summaries boom")
        return None

    db = FakeDB({"watches": [rec_watch(id="w-rec")]}, fail_on=fail)
    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: db))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))

    with pytest.raises(SystemExit) as exc:
        monitor.plan_main()
    assert exc.value.code == 1


def test_plan_main_stays_green_on_only_a_warn_prune_failure(monkeypatch, tmp_path):
    # a missing alert_history table (0005 unapplied) is reported, never rated
    def fail(call):
        if call[0] == "delete" and call[1] == "alert_history":
            return postgrest_error(400, "alert_history absent", code="PGRST205")
        return None

    db = FakeDB({"watches": [rec_watch(id="w-rec")]}, fail_on=fail)
    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: db))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))

    monitor.plan_main()  # no SystemExit
