"""Pre-release scaling fixes (see the scale/cost analysis):

1. the batched last_checked_at PATCH is chunked so its URL cannot outgrow a
   gateway URI limit as the fleet grows,
2. the per-cycle watches read is column-scoped and paginated so PostgREST's
   max-rows cap cannot silently truncate it,
3. alert_history is pruned on the shared retention window, and
4. the per-user active-watch cap is enforced backend-side (a DB trigger),
   defined identically in schema.sql and migrations/0008.

Fully offline: no network, no real secrets.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import monitor
from helpers import (
    NOW,
    FakeAPNs,
    FakeDB,
    FakeHTTP,
    FakeResponse,
    availability_payload,
    make_flex_watch,
    make_gtc_watch,
    make_usedirect_watch,
    make_watch,
    postgrest_error,
)
from providers import provider_for, unpollable_reason

QUIET = dict(rng=random.Random(0), sleep=lambda s: None, now_fn=lambda: NOW)
EMPTY = availability_payload({})


def quiet_cycle(db, payload=EMPTY, apns=None):
    apns = apns or FakeAPNs()
    http = FakeHTTP(lambda cg: FakeResponse(200, payload))
    return monitor.run(db, apns, http, **QUIET), apns


def patch_id_lists(db):
    """The set of watch ids in every batched `id=in.(…)` PATCH FakeDB recorded."""
    lists = []
    for call in db.calls_of("patch", "watches"):
        expr = str(call[2].get("id", ""))
        if expr.startswith("in.("):
            lists.append(expr[len("in.("):-1].split(","))
    return lists


# --- 1. chunk the oversized last_checked_at PATCH --------------------------

def test_patch_watches_chunks_ids_into_bounded_batches():
    # 400 ids is far past a single 8 KB URL of real UUIDs, but each chunk fits.
    ids = [f"w{i:04d}" for i in range(400)]
    db = FakeDB({"watches": [make_watch(id=i, user_id=f"u{i}") for i in ids]})

    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"})

    batches = patch_id_lists(db)
    assert len(batches) == 3  # ceil(400 / 150)
    assert all(len(b) <= monitor.PATCH_ID_CHUNK_MAX for b in batches)
    # every id written exactly once across the chunks, none dropped or duplicated
    written = [i for b in batches for i in b]
    assert sorted(written) == sorted(ids) and len(written) == len(set(written))
    assert outcome.failures == {}

    # the cliff this closes: 400 real UUIDs in one id=in.(…) URL blow nginx's
    # 8 KB default (~39 URL-encoded bytes each), while one 150-id chunk does not.
    assert 400 * 39 > 8192 >= monitor.PATCH_ID_CHUNK_MAX * 39


def test_patch_watches_is_a_single_request_within_one_chunk():
    ids = [f"w{i}" for i in range(monitor.PATCH_ID_CHUNK_MAX)]  # exactly the chunk size
    db = FakeDB({"watches": [make_watch(id=i, user_id=f"u{i}") for i in ids]})

    monitor.patch_watches(db, ids, {"last_checked_at": "t"})

    assert len(db.calls_of("patch", "watches")) == 1


def test_a_bad_row_in_one_chunk_is_still_isolated():
    # chunking must not lose the per-id fan-out that errors a genuinely bad row.
    ids = [f"w{i}" for i in range(monitor.PATCH_ID_CHUNK_MAX + 5)]

    def fail_on(call):
        if call[0] == "patch" and "eq.w0" in str(call[2].get("id", "")):
            return postgrest_error(400, "column watches.x does not exist")
        # the first chunk (an id=in list) is permanently rejected, forcing fan-out
        if call[0] == "patch" and str(call[2].get("id", "")).startswith("in.(w0,"):
            return postgrest_error(400, "boom")
        return None

    db = FakeDB({"watches": [make_watch(id=i, user_id=f"u{i}") for i in ids]}, fail_on=fail_on)
    # small chunk so the failing batch is <= PER_ID_FALLBACK_MAX and does fan out
    outcome = monitor.patch_watches(db, ids, {"last_checked_at": "t"}, chunk_size=10)

    assert "w0" in outcome.isolated


def test_cycle_chunks_the_last_checked_write_at_scale():
    # 400 watches on one campground+month -> 1 poll unit, 400 served watches:
    # the single unchunked PATCH this replaces is what breaks below 100 users.
    watches = [make_watch(id=f"w{i:04d}", user_id=f"u{i}") for i in range(400)]
    db = FakeDB({"watches": watches})

    summary, _ = quiet_cycle(db)

    assert summary["watches_checked"] == 400
    # the per-id last_checked writes (exclude the filter-scoped blanket stamp)
    last_checked = [
        call for call in db.calls_of("patch", "watches")
        if "last_checked_at" in call[3] and "id" in call[2]
    ]
    assert len(last_checked) == 3  # chunked, not one giant URL
    assert all(
        len(str(c[2]["id"])[len("in.("):-1].split(",")) <= monitor.PATCH_ID_CHUNK_MAX
        for c in last_checked
    )
    # and every watch really was stamped
    assert all(w["last_checked_at"] is not None for w in db.tables["watches"])


# --- 2. scope + paginate the watches read ----------------------------------

def test_paginated_select_reads_all_rows_past_a_max_rows_cap():
    watches = [make_watch(id=f"w{i:04d}", user_id=f"u{i}") for i in range(250)]
    db = FakeDB({"watches": watches}, max_rows=100)  # server truncates every GET to 100

    rows = monitor.paginated_select(db, "watches", {"status": "eq.monitoring"}, page_size=100)

    assert len(rows) == 250
    assert sorted(r["id"] for r in rows) == sorted(w["id"] for w in watches)


def test_read_monitoring_watches_projects_the_scoped_columns():
    db = FakeDB({"watches": [make_watch()]})

    monitor.read_monitoring_watches(db)

    projected = [c for c in db.calls_of("select", "watches") if "select" in (c[2] or {})]
    assert projected, "the scoped read must send a select= projection"
    assert projected[0][2]["select"] == ",".join(monitor.WATCH_READ_COLUMNS)
    # the projection must not name a column the census depends on tolerating
    assert "error_reason" not in monitor.WATCH_READ_COLUMNS


def test_read_monitoring_watches_falls_back_to_star_on_a_drifted_column():
    # a live DB missing a WARN column the projection names 400s the whole read;
    # the reader must retry with the tolerant select=* rather than abort.
    def fail_on(call):
        if call[0] == "select" and call[1] == "watches" and "select" in (call[2] or {}):
            return postgrest_error(400, "column watches.provider does not exist", code="42703")
        return None

    watches = [make_watch(id="w1"), make_watch(id="w2", user_id="u2")]
    db = FakeDB({"watches": watches}, fail_on=fail_on)

    rows = monitor.read_monitoring_watches(db)

    assert sorted(r["id"] for r in rows) == ["w1", "w2"]
    fallback = [c for c in db.calls_of("select", "watches") if "select" not in (c[2] or {})]
    assert fallback, "must fall back to an unprojected select=* read"


def test_read_monitoring_watches_reraises_a_non_drift_failure():
    def fail_on(call):
        if call[0] == "select" and call[1] == "watches":
            return postgrest_error(503, "upstream connect error", code=None)
        return None

    db = FakeDB({"watches": [make_watch()]}, fail_on=fail_on)
    try:
        monitor.read_monitoring_watches(db)
    except Exception as exc:  # a transient failure must not be swallowed as drift
        assert monitor.is_missing_column_rejection(exc) is False
    else:
        raise AssertionError("a non-drift read failure must propagate")


def test_cycle_polls_every_watch_past_a_max_rows_cap():
    # the silent-skip proof: 250 watches, a server that truncates reads to 100.
    # Without pagination the cycle would poll only 100 and look green.
    watches = [make_watch(id=f"w{i:04d}", user_id=f"u{i}") for i in range(250)]
    db = FakeDB({"watches": watches}, max_rows=100)

    summary, _ = quiet_cycle(db)

    assert summary["watches_checked"] == 250


def test_a_projected_recreation_gov_and_flex_cycle_still_alerts():
    # project=True drops every column the projection omits, so a cycle that read
    # one it needs would fail here. It alerts and stamps normally instead.
    db = FakeDB(
        {
            "watches": [make_watch(id="w1"), make_flex_watch(2, id="w2", user_id="u2")],
            "device_tokens": [
                {"user_id": "u1", "apns_token": "t1", "environment": "production"},
                {"user_id": "u2", "apns_token": "t2", "environment": "production"},
            ],
        },
        project=True,
    )
    payload = availability_payload(
        {"100": {"2026-08-10": "Available", "2026-08-11": "Available"}}
    )

    summary, apns = quiet_cycle(db, payload)

    assert summary["watches_checked"] == 2
    assert {w for w, _ in apns.alerts} == {"w1", "w2"}
    assert all(w["state_hash"] for w in db.tables["watches"])
    assert summary["watch_errors"] == 0


def test_every_provider_reads_only_scoped_columns():
    # A watch stripped to WATCH_READ_COLUMNS must still poll_plan (and, for the
    # config-carrying providers, resolve pollable) — proof the projection names
    # every field a provider reads off the row.
    today = date(2026, 8, 1)
    for full in (make_watch(), make_flex_watch(2), make_gtc_watch(), make_usedirect_watch()):
        projected = {c: full[c] for c in monitor.WATCH_READ_COLUMNS if c in full}
        provider = provider_for(projected)
        assert provider.poll_plan(projected, today) == provider.poll_plan(full, today)
    # going_to_camp reads provider_ref/include_ada_only through unpollable_reason
    gtc = make_gtc_watch()
    projected_gtc = {c: gtc[c] for c in monitor.WATCH_READ_COLUMNS if c in gtc}
    assert unpollable_reason(projected_gtc) is None


# --- 3. alert_history retention prune ---------------------------------------

def test_alert_history_is_pruned_on_the_retention_window():
    old = monitor.iso_now(NOW - timedelta(days=monitor.RETENTION_DAYS + 5))
    recent = monitor.iso_now(NOW - timedelta(days=1))
    db = FakeDB({
        "alert_history": [
            {"id": "h-old", "watch_id": "w1", "campground_name": "Upper Pines",
             "start_date": "2026-06-01", "end_date": "2026-06-03", "site_count": 1,
             "delivered_at": old},
            {"id": "h-new", "watch_id": "w1", "campground_name": "Upper Pines",
             "start_date": "2026-08-10", "end_date": "2026-08-12", "site_count": 1,
             "delivered_at": recent},
        ],
    })

    quiet_cycle(db)

    kept = {r["id"] for r in db.tables["alert_history"]}
    assert "h-old" not in kept and "h-new" in kept
    pruned = db.calls_of("delete", "alert_history")
    assert len(pruned) == 1 and pruned[0][2]["delivered_at"].startswith("lt.")


def test_alert_history_prune_failure_is_reported_but_never_reddens_the_run():
    # alert_history is WARN-classified in preflight.REQUIRED: a database missing
    # 0005 has no table to prune, and that must keep a quiet cycle GREEN, exactly
    # like the insert path. The failure still surfaces in the run summary errors.
    def fail_on(call):
        if call[0] == "delete" and call[1] == "alert_history":
            return postgrest_error(404, "relation \"alert_history\" does not exist")
        return None

    db = FakeDB({"watches": [make_watch()]}, fail_on=fail_on)
    summary, _ = quiet_cycle(db)

    assert summary["systemic_failure"] is False
    assert monitor.exit_code(summary) == 0
    assert "alert_history prune" in (summary["errors"] or "")


def test_sent_alerts_prune_failure_reddens_the_run():
    # Contrast with alert_history above: sent_alerts is a HALT table preflight
    # guarantees exists, so its prune failing is genuine breakage and stays rated.
    def fail_on(call):
        if call[0] == "delete" and call[1] == "sent_alerts":
            return postgrest_error(503, "upstream connect error")
        return None

    db = FakeDB({"watches": [make_watch()]}, fail_on=fail_on)
    summary, _ = quiet_cycle(db)

    assert summary["systemic_failure"] is True
    assert monitor.exit_code(summary) == 1


# --- 4. per-user active-watch cap (backend trigger) ------------------------

SCHEMA_SQL = (Path(__file__).resolve().parents[1] / "supabase" / "schema.sql").read_text()
MIGRATION_0008 = (
    Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "0008_watches_user_cap.sql"
).read_text()


def test_watch_cap_is_defined_identically_in_schema_and_migration():
    for sql in (SCHEMA_SQL, MIGRATION_0008):
        # the cap number the captain chose, and the active-status set it counts
        assert ">= 20" in sql
        assert "status IN ('monitoring','paused')" in sql
        # a legible, app-recognisable failure (hint token + a client-error code)
        assert "WATCH_CAP_EXCEEDED" in sql
        assert "check_violation" in sql
        # fires on entry into the active set, never on an in-set update, so the
        # monitor's own writes are never blocked
        assert "BEFORE INSERT OR UPDATE" in sql
        assert "TG_OP = 'INSERT' OR OLD.status NOT IN ('monitoring','paused')" in sql
        assert "CREATE TRIGGER watch_cap" in sql


def test_watch_cap_migration_is_idempotent():
    assert "CREATE OR REPLACE FUNCTION enforce_watch_cap" in MIGRATION_0008
    assert "DROP TRIGGER IF EXISTS watch_cap ON watches" in MIGRATION_0008
