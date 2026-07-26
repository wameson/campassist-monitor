"""Schema-drift preflight guard (scripts/preflight.py).

Every case is offline: `FakeDB`'s `fail_on` hook stands in for PostgREST, which
validates an explicit `select` list while planning and rejects an unknown column
with `400 42703` — naming exactly **one** offending column per response, which
is the behaviour the narrowing pass exists to work around. The two production
incidents are replayed directly: `watches.provider`/`provider_ref` missing (the
monitor tolerated it, so the guard must too) and a monitor-written column
missing (the monitor could not, so the guard must stop the run).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

import preflight
from helpers import FAKE_SERVICE_KEY, FakeDB, postgrest_error

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"


def schema_db(missing: dict[str, set[str]] | None = None, *, report_first: str | None = None):
    """A FakeDB that answers select-list probes the way PostgREST does.

    `missing` names the columns absent from the live table. A probe whose select
    list touches any of them is rejected with `42703` naming **one** column —
    `report_first` picks which, so a test can force the WARN column to be the one
    PostgREST reports and prove a HALT column cannot hide behind it.
    """
    missing = {t: set(cols) for t, cols in (missing or {}).items()}

    def fail_on(call):
        op, table, params = call[0], call[1], call[2]
        if op != "select":
            return None
        requested = [c for c in str((params or {}).get("select", "")).split(",") if c]
        absent = [c for c in requested if c in missing.get(table, set())]
        if not absent:
            return None
        named = report_first if report_first in absent else absent[0]
        return postgrest_error(400, f"column {table}.{named} does not exist")

    return FakeDB(fail_on=fail_on)


def error_db(exc: BaseException):
    """A FakeDB whose every probe fails with one given exception."""
    return FakeDB(fail_on=lambda call: exc if call[0] == "select" else None)


def http_error(status: int, code: str = "PGRST100", message: str = "unrecognized"):
    """A PostgREST rejection carrying a code the guard must not read as drift."""
    request = httpx.Request("GET", "https://project.supabase.invalid/rest/v1/watches")
    response = httpx.Response(status, request=request, json={"code": code, "message": message})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError(f"status {status} is not an error status")


def run_preflight(db):
    """Run the guard, capturing what it would print. Returns (lines, exit_code),
    with exit_code None when the cycle was allowed to proceed."""
    lines: list[str] = []
    try:
        preflight.preflight(db, emit=lines.append)
    except SystemExit as exc:
        return lines, exc.code
    return lines, None


# --- clean -----------------------------------------------------------------

def test_preflight_clean():
    assert preflight.check_schema(schema_db()) == []

    db = schema_db()
    lines, code = run_preflight(db)
    assert lines == [] and code is None

    # One combined probe per table, zero rows requested, nothing else.
    probes = db.calls_of("select")
    assert [c[1] for c in probes] == list(preflight.REQUIRED)
    for _, table, params in probes:
        assert params["limit"] == "0"
        assert params["select"].split(",") == list(preflight.REQUIRED[table])


# --- incident 2, replayed: product-only drift warns and keeps monitoring ----

def test_preflight_detects_missing_column():
    db = schema_db({"watches": {"provider", "provider_ref"}}, report_first="provider")

    drifts = preflight.check_schema(db)
    assert {d.label for d in drifts} == {"watches.provider", "watches.provider_ref"}
    assert {d.severity for d in drifts} == {preflight.WARN}

    lines, code = run_preflight(db)
    assert code is None, "the monitor tolerates this drift; the cycle must still run"
    (line,) = lines
    assert line.startswith("::warning::")
    # PostgREST named one column; the narrowing pass must have found both.
    assert "watches.provider," in line and "watches.provider_ref" in line
    assert "supabase/migrations/0002_watches_provider.sql" in line


# --- incident 1, replayed: monitor-written drift stops the run --------------

@pytest.mark.parametrize("column", ["consecutive_not_found", "status"])
def test_preflight_missing_written_column_hard_fails(column):
    db = schema_db({"watches": {column}})

    lines, code = run_preflight(db)
    assert code == 1
    (line,) = lines
    assert line.startswith("::error::")
    assert f"watches.{column}" in line
    if column == "consecutive_not_found":
        assert "supabase/migrations/0001_watches_consecutive_not_found.sql" in line
    assert "No watches were polled this run." in line


def test_preflight_missing_table_halts():
    def fail_on(call):
        if call[0] == "select" and call[1] == "sent_alerts":
            return http_error(404, code="42P01", message="relation does not exist")
        return None

    lines, code = run_preflight(FakeDB(fail_on=fail_on))
    assert code == 1
    assert "table sent_alerts" in lines[0] and lines[0].startswith("::error::")


# --- the masking hole: a HALT column must never hide behind a WARN one ------

def test_preflight_mixed_severity_halts():
    db = schema_db({"watches": {"provider", "status"}}, report_first="provider")

    lines, code = run_preflight(db)
    assert code == 1, "a missing monitor-written column must not be masked by a tolerated one"
    (line,) = lines
    assert line.startswith("::error::")
    assert "watches.provider" in line and "watches.status" in line
    assert "supabase/migrations/0002_watches_provider.sql" in line


# --- transient: a Supabase blip never stops the cycle -----------------------

@pytest.mark.parametrize(
    "exc",
    [
        # A 5xx, even one carrying a schema code in its body (postgrest_error
        # always emits 42703): status decides first, so this stays transient.
        postgrest_error(503, "column watches.status does not exist"),
        postgrest_error(429, "column watches.status does not exist"),
        httpx.ConnectError("connection refused"),
        http_error(400, code="PGRST100"),  # 4xx with an unrecognized code
    ],
    ids=["503", "429", "transport", "unrecognized-4xx"],
)
def test_preflight_transient_does_not_hard_fail(exc):
    db = error_db(exc)
    with pytest.raises(preflight.TransientProbeFailure):
        preflight.check_schema(db)

    lines, code = run_preflight(db)
    assert code is None
    (line,) = lines
    assert line.startswith("::warning::") and "inconclusive" in line


def test_preflight_transient_during_narrowing_does_not_hard_fail():
    """An incomplete enumeration cannot be classified in either direction, so
    the guard fails open rather than halting on a set it could not finish."""
    seen: list[str] = []

    def fail_on(call):
        if call[0] != "select" or call[1] != "watches":
            return None
        seen.append(call[2]["select"])
        if len(seen) == 1:
            return postgrest_error(400, "column watches.status does not exist")
        return postgrest_error(503, "unavailable")

    lines, code = run_preflight(FakeDB(fail_on=fail_on))
    assert code is None
    assert lines[0].startswith("::warning::") and "inconclusive" in lines[0]


# --- invariants -------------------------------------------------------------

def test_preflight_performs_no_writes():
    for db in (
        schema_db(),
        schema_db({"watches": {"provider", "status"}}),
        error_db(postgrest_error(503, "unavailable")),
    ):
        run_preflight(db)
        assert db.write_count == 0


def test_preflight_message_is_publishable():
    halt = preflight.drift_message(
        preflight.check_schema(schema_db({"watches": {"status", "provider"}}))
    )
    warn = preflight.drift_message(
        preflight.check_schema(schema_db({"watches": {"provider", "provider_ref"}}))
    )
    transient, _ = run_preflight(error_db(postgrest_error(503, "boom")))

    known = {f"{t}.{c}" for t, cols in preflight.REQUIRED.items() for c in cols}
    for message in (halt, warn, *transient):
        assert FAKE_SERVICE_KEY not in message
        assert "Bearer" not in message and "apikey" not in message
        assert "supabase.invalid" not in message  # no request URL, no project ref
        # Every dotted token in the message is a manifest identifier or a .sql file.
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.]+", message):
            token = token.rstrip(".")  # sentence punctuation, not part of the identifier
            assert token in known or token.endswith(".sql"), token


# --- the CI sync guard ------------------------------------------------------

def parse_schema_sql(text: str) -> dict[str, list[str]]:
    """Dependency-free CREATE TABLE parser, test-only by design: regex-parsing
    DDL at runtime would turn a formatting change into a red production run, so
    the manifest is explicit and this parser only holds it to schema.sql in CI."""
    # Strip `--` line comments first: they carry commas and braces that would
    # otherwise be mistaken for column separators.
    stripped = "\n".join(line.split("--")[0] for line in text.splitlines())

    tables: dict[str, list[str]] = {}
    for match in re.finditer(r"CREATE TABLE\s+(\w+)\s*\(", stripped):
        start = match.end()
        depth, end = 1, start
        while depth:
            if stripped[end] == "(":
                depth += 1
            elif stripped[end] == ")":
                depth -= 1
                if not depth:
                    break
            end += 1
        body, depth, item, items = stripped[start:end], 0, "", []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                items.append(item)
                item = ""
            else:
                item += ch
        items.append(item)

        constraints = {"UNIQUE", "CHECK", "PRIMARY", "FOREIGN", "CONSTRAINT", "EXCLUDE"}
        columns = []
        for entry in items:
            word = entry.split()[0] if entry.split() else ""
            if not word or word.split("(")[0].upper() in constraints:
                continue
            columns.append(word)
        tables[match.group(1)] = columns
    return tables


def test_preflight_manifest_matches_schema_sql():
    parsed = parse_schema_sql(SCHEMA_SQL.read_text())

    # Sanity first, so a parser that silently matched nothing fails loudly
    # instead of agreeing with an empty manifest.
    assert set(parsed) == {"watches", "device_tokens", "sent_alerts", "run_summaries"}
    assert sum(len(c) for c in parsed.values()) >= 25
    assert all(len(c) >= 4 for c in parsed.values())

    for table, columns in parsed.items():
        assert set(columns) == set(preflight.REQUIRED[table]), table
    assert set(parsed) == set(preflight.REQUIRED)


def test_every_manifest_column_is_classified():
    for table, columns in preflight.REQUIRED.items():
        for column, (severity, migration) in columns.items():
            assert severity in (preflight.HALT, preflight.WARN), f"{table}.{column}"
            assert migration is None or migration.endswith(".sql"), f"{table}.{column}"
            if migration:
                assert (SCHEMA_SQL.parent / "migrations" / migration).exists(), migration


# --- wiring -----------------------------------------------------------------

def wire_main(monkeypatch, order: list[str], preflight_hook):
    """monitor.main() with everything but the ordering stubbed out."""
    import monitor

    def hook(db):
        order.append("preflight")
        preflight_hook()

    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: "db"))
    monkeypatch.setattr(monitor.APNsClient, "from_env", classmethod(lambda cls: "apns"))
    monkeypatch.setattr(monitor, "preflight", hook)
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: order.append("sleep"))
    monkeypatch.setattr(monitor, "run", lambda *a, **k: (order.append("run"), {})[1])
    return monitor


def test_main_preflights_before_jitter_and_never_enters_run(monkeypatch):
    """Halting drift must cost no Actions minutes and reach no write path."""
    order: list[str] = []

    def halt():
        raise SystemExit(1)

    monitor = wire_main(monkeypatch, order, halt)
    with pytest.raises(SystemExit) as exc:
        monitor.main()
    assert exc.value.code == 1
    assert order == ["preflight"], "no jitter sleep, no cycle"


def test_main_proceeds_when_preflight_is_clean(monkeypatch):
    order: list[str] = []
    monitor = wire_main(monkeypatch, order, lambda: None)
    with pytest.raises(SystemExit) as exc:
        monitor.main()
    assert exc.value.code == 0
    assert order == ["preflight", "sleep", "run"]
