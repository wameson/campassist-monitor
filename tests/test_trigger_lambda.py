"""Offline tests for the AWS trigger Lambda (PLAN.md Phase 17, Path A).

Fully offline: no network, no real secrets, no AWS. `boto3` is never imported —
`read_token`/`dispatch` take an injected fake SSM client and a fake HTTP poster,
and the `handler` tests stub `_ssm_client` (so the lazy `import boto3` never
runs) and the poster (so `urllib` never opens a socket).

The two behaviours these tests pin are the reason Path A uses a Lambda at all:
- a non-204 dispatch **raises** so the invocation is flagged and the alarm sees
  it — the regression guard for the silent-miss failure class; and
- the PAT read from SSM is **never logged** — a leaked token in CloudWatch would
  be a real incident.
"""

from __future__ import annotations

import io
import logging
import urllib.error

import pytest

import trigger_lambda
from trigger_lambda import DEFAULT_SSM_PARAM, DISPATCH_URL, DispatchError


# --- fakes ------------------------------------------------------------------

SECRET_TOKEN = "ghp_fake_do_not_log_0123456789ABCDEF"  # noqa: S105 (test-only fake)


class FakeSSM:
    """Minimal stand-in for boto3's SSM client: get_parameter over a dict."""

    def __init__(self, store: dict[str, str]):
        self.store = store
        self.with_decryption = None
        self.calls = 0
        self.requested: list[str] = []

    def get_parameter(self, Name, WithDecryption):  # noqa: N803 (boto3 casing)
        self.calls += 1
        self.with_decryption = WithDecryption
        self.requested.append(Name)
        if Name not in self.store:
            raise KeyError(f"ParameterNotFound: {Name}")
        return {"Parameter": {"Name": Name, "Value": self.store[Name]}}


class FakeGitHubResponse:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


class FakeGitHubHTTP:
    """GitHub API stand-in; records every POST so the request can be asserted."""

    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text
        self.requests: list[dict] = []

    def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json})
        return FakeGitHubResponse(self.status_code, self.text)


def _ssm_with_token(token=SECRET_TOKEN, param_name=DEFAULT_SSM_PARAM) -> FakeSSM:
    return FakeSSM({param_name: token})


# --- successful dispatch ----------------------------------------------------

def test_successful_dispatch_returns_204_and_posts_ref_main():
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=204)

    status = trigger_lambda.dispatch(ssm, http)

    assert status == 204
    assert len(http.requests) == 1
    req = http.requests[0]
    assert req["url"] == DISPATCH_URL
    assert req["json"] == {"ref": "main"}
    # The PAT is read (decrypted) from SSM at invoke and passed as a Bearer token.
    assert ssm.with_decryption is True, "the PAT must be decrypted (SecureString/KMS)"
    assert req["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"


def test_dispatch_reads_the_configured_ssm_param(monkeypatch):
    monkeypatch.setenv("GITHUB_PAT_SSM_PARAM", "/prod/monitor/pat")
    ssm = _ssm_with_token(param_name="/prod/monitor/pat")
    http = FakeGitHubHTTP(status_code=204)

    trigger_lambda.dispatch(ssm, http)  # param name read from env

    assert ssm.requested == ["/prod/monitor/pat"]


def test_successful_dispatch_logs_the_status(caplog):
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=204)

    with caplog.at_level(logging.INFO, logger="trigger_lambda"):
        trigger_lambda.dispatch(ssm, http)

    # Every attempt is logged with its HTTP status — the inspectable record.
    assert any("204" in r.getMessage() for r in caplog.records)


# --- failed dispatch: must raise, never silently succeed --------------------

@pytest.mark.parametrize("status", [401, 404, 422, 500])
def test_failed_dispatch_raises_so_lambda_records_an_invocation_error(status, caplog):
    """The regression test for the silent-failure class: a non-204 must raise,
    so the invocation is marked failed and the CloudWatch alarm can see it."""
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=status, text="github error body")

    with caplog.at_level(logging.INFO, logger="trigger_lambda"):
        with pytest.raises(DispatchError) as excinfo:
            trigger_lambda.dispatch(ssm, http)

    # It must not have quietly returned a success value.
    assert str(status) in str(excinfo.value)
    # The failed attempt is still logged with its status before raising.
    assert any(str(status) in r.getMessage() for r in caplog.records)


def test_failure_does_not_return_a_success_sentinel():
    """Belt-and-suspenders: assert the failure path cannot fall through to a
    normal return the way a swallowed error would."""
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=500)

    with pytest.raises(DispatchError):
        result = trigger_lambda.dispatch(ssm, http)
        # Unreachable — but if the raise is ever removed, this makes the silent
        # success loud instead of passing.
        assert result is None, "a failed dispatch must never return"


# --- the token is read from SSM at invoke and never logged ------------------

def test_token_is_never_logged_on_success(caplog):
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=204)

    with caplog.at_level(logging.DEBUG, logger="trigger_lambda"):
        trigger_lambda.dispatch(ssm, http)

    assert ssm.calls == 1, "the token must be read from SSM at invoke"
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_TOKEN not in combined, "a leaked token in CloudWatch is an incident"


def test_token_is_never_logged_on_failure(caplog):
    """A failed dispatch logs and raises — neither the log nor the raised
    exception message may carry the token."""
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=401, text="Bad credentials")

    with caplog.at_level(logging.DEBUG, logger="trigger_lambda"):
        with pytest.raises(DispatchError) as excinfo:
            trigger_lambda.dispatch(ssm, http)

    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_TOKEN not in combined
    assert SECRET_TOKEN not in str(excinfo.value)


# --- the stdlib poster (no third-party dep) ---------------------------------

class _FakeUrlopenResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_urllib_poster_returns_status_and_text_on_2xx(monkeypatch):
    """The real poster (production HTTP path) maps a 2xx urlopen result to the
    (status_code, text) slice dispatch expects — exercised without a socket."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return _FakeUrlopenResp(204, "")

    monkeypatch.setattr(trigger_lambda.urllib.request, "urlopen", fake_urlopen)

    resp = trigger_lambda.UrllibPoster(timeout=9).post(
        DISPATCH_URL, headers={"User-Agent": "x"}, json={"ref": "main"}
    )

    assert (resp.status_code, resp.text) == (204, "")
    assert captured["method"] == "POST"
    assert captured["url"] == DISPATCH_URL
    assert captured["timeout"] == 9


def test_urllib_poster_maps_httperror_to_a_response(monkeypatch):
    """A 4xx/5xx surfaces from urllib as HTTPError, which is itself a response;
    the poster must capture its status and body rather than let it escape, so
    dispatch can log the status and raise its own DispatchError."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            DISPATCH_URL, 422, "Unprocessable", hdrs={}, fp=io.BytesIO(b"no trigger")
        )

    monkeypatch.setattr(trigger_lambda.urllib.request, "urlopen", fake_urlopen)

    resp = trigger_lambda.UrllibPoster().post(
        DISPATCH_URL, headers={}, json={"ref": "main"}
    )

    assert resp.status_code == 422
    assert "no trigger" in resp.text


# --- handler wiring ---------------------------------------------------------

def test_handler_uses_injected_ssm_and_poster(monkeypatch):
    """End-to-end through handler with a fake SSM (so boto3 is never imported)
    and a fake poster (so urllib never opens a socket), proving the wiring
    returns success on 204."""
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=204)
    monkeypatch.setattr(trigger_lambda, "_ssm_client", lambda: ssm)
    monkeypatch.setattr(trigger_lambda, "UrllibPoster", lambda *a, **k: http)

    result = trigger_lambda.handler({}, None)

    assert result == {"ok": True, "status": 204}
    assert http.requests[0]["json"] == {"ref": "main"}


def test_handler_propagates_failure_as_invocation_error(monkeypatch):
    """A non-204 must surface out of handler as a raised error, so Lambda flags
    the invocation and the alarm fires."""
    ssm = _ssm_with_token()
    http = FakeGitHubHTTP(status_code=422, text="no workflow_dispatch trigger")
    monkeypatch.setattr(trigger_lambda, "_ssm_client", lambda: ssm)
    monkeypatch.setattr(trigger_lambda, "UrllibPoster", lambda *a, **k: http)

    with pytest.raises(DispatchError):
        trigger_lambda.handler({}, None)
