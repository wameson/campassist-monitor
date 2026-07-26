"""Schema-drift preflight: prove the live database still has the schema this
build expects, before the cycle starts.

`supabase/schema.sql` is the fresh-install bootstrap only; an existing database
gets a new column solely from an operator running the matching
`supabase/migrations/NNNN_*.sql` by hand (README "Database migrations"). When
that step is skipped, reads keep working — PostgREST's `select=*` just omits the
missing key and Python tolerates it — so the drift stays invisible until a
*write* names the column and PostgREST answers `400 42703`. That has caused a
multi-day outage once (`supabase/migrations/0001_watches_consecutive_not_found.sql`)
and, in its product-only form, a release the iOS app could not save a watch on
(`0002_watches_provider.sql`).

This module detects that drift and nothing else. It **never** applies DDL, has
no write path of any kind, and issues only `GET`s with `limit=0`, so no row data
— no user ids, no campground values — crosses the wire. The manual apply posture
stands unchanged.

How the probe works: PostgREST validates an explicit `select` list against the
live table while planning, so one column-listing read per table is a schema
assertion that costs zero writes. A missing column comes back as `400` with
PostgREST's `42703` body; a missing relation as `42P01`/`PGRST205`. Because that
body names only **one** offending column, a table that answers `42703` is
re-probed column by column (`_narrow`) so the verdict is read off the *complete*
set of missing columns, never off the first one PostgREST happened to name.

Severity decides what a confirmed drift does, and the rule is the two incidents:
halt when the drift would break the monitor itself (incident 1), warn and keep
monitoring when the monitor provably survives it (incident 2). Anything that is
not a positively-identified missing object — 5xx, 429, timeout, transport error,
a 4xx with a code we do not recognize — proves nothing rather than proving the
schema wrong, because a Supabase blip must never pause cancellation monitoring.

Tables are probed independently, and a blip on one neither erases nor suppresses
a verdict on another. A HALT verdict is monotone: further probing can only reveal
more missing columns, never unmake one already proven absent, and a blip on
`device_tokens` says nothing about a `watches` column the previous probe
positively identified as gone. So a probe failure is *recorded* against its own
table and the pass moves on to the next one; every confirmed drift and every blip
is collected across the whole manifest, and the verdict is reached once, at the
end. On a full Supabase outage that costs one timed-out probe per table (four,
the figure the `monitor` budget comment already carries) on a run where the cycle
would have achieved nothing anyway.

The verdict is therefore narrower than "any transient fails open": a confirmed
HALT halts even when other probes blipped, and the message says the enumerated
set may be incomplete. Fail-open governs everything else — a WARN-only confirmed
set, or no confirmed drift at all — because an incomplete set cannot be
classified as *tolerable* with confidence, and the next run re-checks 30 minutes
later.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from common import capped_line

# HALT — the monitor writes the column, or reads it as required (direct
#        subscript, no fallback), so its absence is a certain write-time 400 or
#        a certain per-watch/per-alert exception. Nothing is gained by starting
#        the cycle: halt before any write and before run() is entered.
# WARN — app/iOS-only, demonstrated read-tolerant, or monitor-untouched. The
#        monitor keeps working, so it keeps monitoring and only says so.
#
# Conservative default: a read column whose tolerance is not *demonstrated* in
# the code classifies HALT. A false halt is loud, safe and recoverable; warning
# past a genuinely-required missing column lets a real outage through opaquely,
# which is the failure this guard exists to prevent.
HALT = "halt"
WARN = "warn"

# {table: {column: (severity, introducing migration or None for bootstrap)}}
#
# This mirrors the **full** expected schema (`supabase/schema.sql`), not merely
# the columns the monitor touches — incident 2 was drift the monitor tolerated
# and the app did not, and a manifest of monitor-critical columns alone would
# have stayed silent through it. `tests/test_preflight.py` asserts this dict and
# `schema.sql` agree in both directions, so the sync guarantee is a red CI test
# rather than runtime DDL parsing.
REQUIRED: dict[str, dict[str, tuple[str, str | None]]] = {
    "watches": {
        # written by the cycle → halt (incident 1's shape)
        "status":                (HALT, None),   # run(): expire, error-mark, strike paths
        "consecutive_not_found": (HALT, "0001_watches_consecutive_not_found.sql"),  # strike path
        "state_hash":            (HALT, None),   # run(): written on a delta
        "last_found_at":         (HALT, None),   # run(): written on a delta
        "last_checked_at":       (HALT, None),   # run(): batched end-of-cycle write
        # read as required — direct subscript, no fallback → also halt
        "id":                    (HALT, None),   # watch["id"] throughout run()
        "user_id":               (HALT, None),   # apns.send_alert: device_tokens lookup
        "campground_id":         (HALT, None),   # run(): the poll key and strike key
        "campground_name":       (HALT, None),   # apns.send_alert: alert body
        "site_ids":              (HALT, None),   # every provider's extract_relevant
        "start_date":            (HALT, None),   # run() expiry, poll_plan, alert body
        "end_date":              (HALT, None),   # run() expiry, poll_plan, alert body
        # app-only, demonstrated read-tolerant, or monitor-untouched → warn
        # (incident 2's shape). Each tolerance is cited below the manifest.
        "provider":              (WARN, "0002_watches_provider.sql"),
        "provider_ref":          (WARN, "0002_watches_provider.sql"),
        "include_ada_only":      (WARN, "0003_watches_include_ada_only.sql"),
        "campground_state":      (WARN, None),
        "created_at":            (WARN, None),
    },
    "device_tokens": {
        "user_id":     (HALT, None),  # written: the dead-token delete filter (apns.send_alert)
        "apns_token":  (HALT, None),  # read as required by apns.send_alert
        "environment": (HALT, None),  # send_alert reads it .get()-tolerantly, but a
                                      # device_tokens table missing a NOT NULL bootstrap
                                      # column is broken enough to stop for: halting is
                                      # the conservative side and every push needs this row
        "updated_at":  (WARN, None),  # monitor never reads or writes it
    },
    "sent_alerts": {
        "watch_id": (HALT, None),   # select filter + upsert key (filter_unalerted, alert_rows)
        "site_id":  (HALT, None),   # subscript read + upsert key
        "date":     (HALT, None),   # subscript read + upsert key
        "sent_at":  (HALT, None),   # written, and the retention prune filters on it
        "id":       (WARN, None),   # monitor never reads or writes it
    },
    "run_summaries": {
        "watches_checked":    (HALT, None),   # inserted in run()'s summary row
        "campgrounds_polled": (HALT, None),   # inserted in run()'s summary row
        "alerts_sent":        (HALT, None),   # inserted in run()'s summary row
        "duration_ms":        (HALT, None),   # inserted in run()'s summary row
        "errors":             (HALT, None),   # inserted in run()'s summary row
        "ran_at":             (HALT, None),   # the retention prune filters on it
        "id":                 (WARN, None),   # monitor never reads or writes it
    },
}

# The confirmation obligation for every WARN-classified column the monitor reads
# at all: the citation proving the fallback. A column that cannot be proven
# tolerant here belongs in the HALT set instead — this is a gate, not advice.
#
#   watches.provider       `str(watch.get("provider") or DEFAULT_PROVIDER)` in
#                          `providers.provider_name` — an absent column reads as the
#                          recreation_gov default, which is exactly how the monitor
#                          stayed green through incident 2.
#   watches.provider_ref   read only through `watch.get("provider_ref")` in
#                          going_to_camp's `provider_ref_ids`; an absent column raises
#                          `InvalidProviderRef` *inside* the designed `unpollable_reason`
#                          path, which errors those watches once and leaves every
#                          recreation_gov watch — i.e. every watch at all, since
#                          `provider` is missing along with it — polled normally.
#   watches.include_ada_only  `bool(watch.get("include_ada_only"))` in going_to_camp's
#                          `extract_relevant`, the one place the ADA-Only exclusion is
#                          gated (recreation_gov never reads it). An absent column reads
#                          `false`, which is exactly what a migrated row carries by
#                          default, so an unmigrated live DB keeps monitoring and keeps
#                          excluding — never halts, never suppresses differently. It
#                          stays WARN only while that read is `.get` with a default; a
#                          read that ever subscripts it belongs in the HALT set instead.
#   watches.campground_state  never read or written by the monitor (schema/iOS only).
#   watches.created_at, device_tokens.updated_at, sent_alerts.id, run_summaries.id
#                          monitor-untouched bootstrap columns; absent from every read
#                          and write path in the cycle.

# PostgREST's own codes for the two things this guard can positively identify.
# Anything else is transient by construction (see the module docstring), which
# mirrors — and narrows — the 4xx-except-429 rule in `monitor.is_permanent_failure`.
MISSING_COLUMN_CODE = "42703"
MISSING_RELATION_CODES = ("42P01", "PGRST205")
SCHEMA_CODES = (MISSING_COLUMN_CODE,) + MISSING_RELATION_CODES

# A PostgREST error code is a short identifier (`42703`, `PGRST205`). Anything
# that does not look like one is not echoed into the log at all.
_CODE_RE = re.compile(r"\A[A-Za-z0-9_]{1,16}\Z")


class Drift(NamedTuple):
    """One missing schema object. `column` is None when the whole table is gone."""

    table: str
    column: str | None
    severity: str
    migration: str | None

    @property
    def label(self) -> str:
        return f"{self.table}.{self.column}" if self.column else f"table {self.table}"


class SchemaCheck(NamedTuple):
    """One probe pass: every drift it positively confirmed, plus one short note
    per table it could not classify (empty when every table answered).

    The two fields are independent on purpose. Confirmed drift survives the
    tables that blipped — that is what stops a blip from un-confirming a missing
    column the monitor writes — while `incomplete` is what keeps a partial
    enumeration from being read as *tolerable*.
    """

    drifts: list[Drift]
    incomplete: tuple[str, ...]


class TransientProbeFailure(Exception):
    """The probe could not reach a verdict — 5xx, 429, timeout, transport error,
    or a 4xx whose code is not a schema code. Never a reason on its own to stop
    the cycle: it cuts the pass short, and only drift already confirmed decides
    whether the run halts.

    It therefore carries that drift (`confirmed`) rather than unwinding past it,
    so a blip on one probe cannot erase a missing column an earlier probe had
    already positively identified.
    """

    def __init__(self, detail: str, confirmed: list[Drift] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.confirmed: list[Drift] = list(confirmed or [])


def _postgrest_code(exc: BaseException) -> str | None:
    """The `code` field of a PostgREST error body, or None.

    Only the response body is read, never the request, whose headers carry the
    service-role key — the same rule `monitor.rejection_reason` follows.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return str(code) if code is not None else None


def _status_of(exc: BaseException) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _schema_code(exc: BaseException) -> str | None:
    """The schema code this failure positively identifies, or None if the
    failure is transient. A schema code only counts on a 4xx that is not a 429;
    a 42703 body attached to a 503 is a blip wearing a schema code's clothes."""
    status = _status_of(exc)
    if status is None or not (400 <= status < 500) or status == 429:
        return None
    code = _postgrest_code(exc)
    return code if code in SCHEMA_CODES else None


def _table_severity(table: str) -> str:
    """A missing table takes the highest severity among its columns."""
    return HALT if any(sev == HALT for sev, _ in REQUIRED[table].values()) else WARN


def _probe(db, table: str, columns) -> None:
    """One schema assertion: `limit=0` returns no rows, but PostgREST still
    validates the select list while planning, so a missing column rejects."""
    db.select(table, {"select": ",".join(columns), "limit": "0"})


def _narrow(db, table: str) -> list[Drift]:
    """Enumerate *every* missing column of a table that answered `42703`.

    PostgREST names one offending column per response, so the combined probe can
    prove a table healthy but cannot classify a drifted one: if a WARN and a HALT
    column are both absent and the WARN one is reported first, trusting the
    combined answer would warn-and-continue past a monitor-written column that is
    genuinely gone — incident 1, waved through. So each column is probed on its
    own and the table is judged on the complete set.

    A transient failure ends *this table's* enumeration — the rest of the
    manifest is still probed — but the columns already proven missing travel out
    on the exception: an incomplete set cannot be read as *tolerable*, yet a HALT
    column it did enumerate stays confirmed.
    """
    missing: list[Drift] = []
    for column, (severity, migration) in REQUIRED[table].items():
        try:
            _probe(db, table, [column])
        except Exception as exc:
            code = _schema_code(exc)
            if code == MISSING_COLUMN_CODE:
                missing.append(Drift(table, column, severity, migration))
                continue
            if code in MISSING_RELATION_CODES:
                return [Drift(table, None, _table_severity(table), None)]
            raise TransientProbeFailure(_transient_detail(exc), missing) from exc
    return missing


def _unreproduced_detail(table: str) -> str:
    """A `42703` no manifest column reproduces: the table rejected the combined
    select list, then answered every single-column probe. The two answers
    contradict each other (a column dropped and re-added between them, or a
    `42703` raised for something other than a select-list column), so the pass
    is inconclusive for that table rather than clean."""
    return capped_line(
        f"{table} rejected the column list with {MISSING_COLUMN_CODE}, no column reproduced it"
    )


def _check_table(db, table: str, columns) -> SchemaCheck:
    """One table's verdict. Raises TransientProbeFailure when the probe could not
    classify it, carrying whatever the narrowing pass had already confirmed."""
    try:
        _probe(db, table, columns)
    except Exception as exc:
        code = _schema_code(exc)
        if code in MISSING_RELATION_CODES:
            return SchemaCheck([Drift(table, None, _table_severity(table), None)], ())
        if code != MISSING_COLUMN_CODE:
            raise TransientProbeFailure(_transient_detail(exc)) from exc
        missing = _narrow(db, table)
        return SchemaCheck(missing, () if missing else (_unreproduced_detail(table),))
    return SchemaCheck([], ())


def check_schema(db) -> SchemaCheck:
    """Probe the live schema: every missing object it confirmed, and one note per
    table it could not classify.

    One combined `GET` per table (four total, zero writes). The per-column
    narrowing pass fires only on real drift — on a run that is already halting or
    warning — so it cannot inflate a healthy run's budget.

    Every table is probed whatever the ones before it answered. A probe failure
    that is not a positively-identified missing object is recorded against its own
    table and the pass continues, because a blip on one table can neither un-prove
    drift an earlier table confirmed nor hide drift a later one would have: they
    are independent assertions. That costs at worst one timed-out probe per table,
    and only while Supabase is broken enough that the cycle would achieve nothing.
    """
    drifts: list[Drift] = []
    incomplete: list[str] = []
    for table, columns in REQUIRED.items():
        try:
            checked = _check_table(db, table, columns)
        except TransientProbeFailure as blip:
            drifts.extend(blip.confirmed)
            incomplete.append(capped_line(f"{table}: {blip.detail}"))
            continue
        drifts.extend(checked.drifts)
        incomplete.extend(checked.incomplete)
    return SchemaCheck(drifts, tuple(incomplete))


def _transient_detail(exc: BaseException) -> str:
    """A short, publishable account of a probe failure: the exception type, the
    status, and the PostgREST code when it looks like one. Deliberately not
    `str(exc)` — httpx spends that on the full request URL and a doc link."""
    parts = [type(exc).__name__]
    status = _status_of(exc)
    if status is not None:
        parts.append(str(status))
    code = _postgrest_code(exc)
    if code and _CODE_RE.match(code):
        parts.append(code)
    return capped_line(" ".join(parts))


def _remedy(drifts: list[Drift]) -> str:
    """The exact file(s) an operator must apply, named in the message so one
    apply pass fixes everything the run found."""
    files = sorted({d.migration for d in drifts if d.migration})
    bootstrap = any(d.migration is None for d in drifts)
    if files:
        listed = ", ".join(f"supabase/migrations/{f}" for f in files)
        if bootstrap:
            listed += " (and restore the remaining object(s) from supabase/schema.sql)"
    else:
        listed = "the missing object(s) from supabase/schema.sql"
    return f'Apply {listed} in the Supabase SQL editor (README "Database migrations")'


def _unclassified(incomplete) -> str:
    """The tables the pass could not classify, one short note each."""
    return capped_line("; ".join(incomplete))


def drift_message(drifts: list[Drift], *, incomplete=()) -> str:
    """One GitHub Actions annotation naming every missing object and its remedy.

    Identifiers and migration filenames only — all of them already published in
    `supabase/schema.sql`. No row data, no user-supplied values, no credentials;
    `incomplete` carries only table names and short probe-failure details.
    """
    halting = sorted(d.label for d in drifts if d.severity == HALT)
    missing = ", ".join(sorted(d.label for d in drifts))
    cache_hint = (
        "If the migration is already applied, PostgREST may still be caching the "
        "old schema — run NOTIFY pgrst, 'reload schema'."
    )
    cut_short = (
        f"Some tables could not be classified ({_unclassified(incomplete)}), so more "
        "objects may be missing than are listed here. "
        if incomplete
        else ""
    )
    if halting:
        return (
            f"::error::schema drift — {missing} missing from the live database. "
            f"The monitor writes or requires {', '.join(halting)}. {cut_short}"
            f"{_remedy(drifts)}, then re-run. "
            f"No watches were polled this run. {cache_hint}"
        )
    return (
        f"::warning::schema drift — {missing} missing from the live database. "
        f"The monitor tolerates this; the iOS app may not. {cut_short}"
        f"{_remedy(drifts)}. "
        f"Monitoring continued normally this run. {cache_hint}"
    )


def _print(line: str) -> None:
    print(line, flush=True)


def preflight(db, *, emit=_print) -> None:
    """Run the guard. Returns normally when the cycle may proceed.

    The whole manifest is probed first and the verdict reached once, so no
    table's answer depends on another's. Raises SystemExit(1) — before the
    cycle's first write and without entering run() — only for a
    positively-identified missing object the monitor writes or reads as required,
    including one confirmed on a pass where other tables blipped: that verdict
    cannot be undone by a probe failure elsewhere, and continuing walks into the
    write-time 400 this guard exists to stop. Drift the monitor provably
    tolerates, and a pass that confirmed nothing halting, print a warning and
    return: halting on those would turn the hours-long window between merging a
    migration and an operator applying it into zero cancellation monitoring,
    inverting the blast radius of the very drift this guard was built for.
    """
    checked = check_schema(db)
    if checked.drifts:
        emit(drift_message(checked.drifts, incomplete=checked.incomplete))
        if any(d.severity == HALT for d in checked.drifts):
            raise SystemExit(1)
        return
    if checked.incomplete:
        emit(
            f"::warning::schema preflight inconclusive ({_unclassified(checked.incomplete)}) "
            "— proceeding with the cycle; a pass that confirmed no halting drift never "
            "stops monitoring."
        )
