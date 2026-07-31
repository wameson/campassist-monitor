"""Offline tests for the AWS Lambda entrypoint shim (PLAN.md Phase 17).

Fully offline: no network, no real secrets, no AWS. `boto3` is never imported —
`load_secrets` takes an injected fake SSM client, and every handler test stubs
`_ensure_secrets` so the lazy `import boto3` inside it never runs.
"""

from __future__ import annotations

import os

import pytest

import lambda_function
from lambda_function import SECRET_ENV_VARS


# --- fakes ------------------------------------------------------------------

class FakeSSM:
    """Minimal stand-in for boto3's SSM client: get_parameters over a dict."""

    def __init__(self, store: dict[str, str]):
        self.store = store
        self.with_decryption = None
        self.calls = 0

    def get_parameters(self, Names, WithDecryption):  # noqa: N803 (boto3 casing)
        self.calls += 1
        self.with_decryption = WithDecryption
        params = [
            {"Name": n, "Value": self.store[n]} for n in Names if n in self.store
        ]
        invalid = [n for n in Names if n not in self.store]
        return {"Parameters": params, "InvalidParameters": invalid}


@pytest.fixture(autouse=True)
def _clean_secret_env():
    """Keep secret env vars from leaking between tests or into the real env."""
    saved = {k: os.environ.get(k) for k in SECRET_ENV_VARS}
    for k in SECRET_ENV_VARS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --- secret loading ---------------------------------------------------------

def test_load_secrets_maps_ssm_names_to_env_vars():
    prefix = "/campassist-monitor/"
    ssm = FakeSSM({prefix + name: f"val-{name}" for name in SECRET_ENV_VARS})

    applied = lambda_function.load_secrets(ssm, prefix)

    assert ssm.with_decryption is True, "secrets must be decrypted (KMS)"
    for name in SECRET_ENV_VARS:
        assert os.environ[name] == f"val-{name}"
    assert set(applied) == set(SECRET_ENV_VARS)
    # The value never appears in the returned map.
    assert all(v == "<set>" for v in applied.values())


def test_load_secrets_uses_configurable_prefix(monkeypatch):
    monkeypatch.setenv("SSM_PARAM_PREFIX", "/prod/monitor/")
    ssm = FakeSSM({f"/prod/monitor/{name}": name for name in SECRET_ENV_VARS})

    lambda_function.load_secrets(ssm)  # prefix read from env

    assert os.environ["SUPABASE_URL"] == "SUPABASE_URL"


def test_load_secrets_raises_on_missing_parameter():
    prefix = "/campassist-monitor/"
    store = {prefix + name: name for name in SECRET_ENV_VARS}
    del store[prefix + "APNS_P8_KEY"]  # one missing
    ssm = FakeSSM(store)

    with pytest.raises(RuntimeError, match="APNS_P8_KEY"):
        lambda_function.load_secrets(ssm, prefix)


# --- exit-code translation --------------------------------------------------

def test_zero_exit_returns_normally():
    def fake_main(*, process_started):
        raise SystemExit(0)

    assert lambda_function.invoke(fake_main, process_started=1.0) == {
        "ok": True,
        "exit_code": 0,
    }


def test_none_exit_is_success():
    def fake_main(*, process_started):
        raise SystemExit()  # code is None → clean

    assert lambda_function.invoke(fake_main, process_started=1.0)["ok"] is True


def test_nonzero_exit_raises_a_real_error():
    """The loud-failure contract: a systemic run must not look green."""

    def fake_main(*, process_started):
        raise SystemExit(1)

    with pytest.raises(RuntimeError):
        lambda_function.invoke(fake_main, process_started=1.0)


def test_preflight_halt_style_exit_raises():
    def fake_main(*, process_started):
        raise SystemExit(2)  # any non-zero, incl. preflight HALT's SystemExit(1)

    with pytest.raises(RuntimeError):
        lambda_function.invoke(fake_main, process_started=1.0)


# --- warm-container freshness (the regression review caught) ----------------

def _stub_monitor_internals(monkeypatch, captured):
    """Stub monitor's I/O and capture the process_started that reaches run()."""
    import monitor

    monkeypatch.setattr(monitor.SupabaseClient, "from_env", classmethod(lambda cls: "db"))
    monkeypatch.setattr(monitor.APNsClient, "from_env", classmethod(lambda cls: "apns"))
    monkeypatch.setattr(monitor, "preflight", lambda db: None)
    monkeypatch.setattr(monitor.time, "sleep", lambda seconds: None)

    def fake_run(db, apns, http, *, process_started=None, **kwargs):
        captured.append(process_started)
        return {}

    monkeypatch.setattr(monitor, "run", fake_run)
    return monitor


def test_second_invocation_forwards_a_fresh_anchor_through_to_run(monkeypatch):
    """A warm-container second call must reach run() with a *newer* anchor.

    This fails if someone reverts to rebinding the module global instead of
    forwarding process_started: run()'s bound default would stay stale and both
    invocations would carry the same import-time value.
    """
    captured: list[float] = []
    _stub_monitor_internals(monkeypatch, captured)
    import monitor

    lambda_function.invoke(monitor.main, process_started=1000.0)
    lambda_function.invoke(monitor.main, process_started=1000.5)

    assert captured == [1000.0, 1000.5]
    assert captured[1] > captured[0], "warm-container anchor must advance"
    # A global-rebind revert would leave run()'s bound default (the import-time
    # PROCESS_STARTED) in place instead of the forwarded values.
    assert monitor.PROCESS_STARTED not in captured


def test_handler_reads_a_fresh_monotonic_each_invocation(monkeypatch):
    """End-to-end through handler: two calls, two increasing anchors."""
    monkeypatch.setattr(lambda_function, "_ensure_secrets", lambda: None)

    ticks = iter([2000.0, 2001.0])
    monkeypatch.setattr(lambda_function, "_monotonic", lambda: next(ticks))

    seen: list[float] = []
    import monitor

    def fake_main(*, process_started):
        seen.append(process_started)
        raise SystemExit(0)

    monkeypatch.setattr(monitor, "main", fake_main)

    lambda_function.handler({}, None)
    lambda_function.handler({}, None)

    assert seen == [2000.0, 2001.0]
    assert seen[1] > seen[0]
