"""AWS trigger Lambda: fire monitor.yml on an exact schedule (PLAN.md Phase 17, Path A).

This is the trigger hop the plan describes — **not** an attempt to run the poll
itself inside Lambda. That earlier plan (and the dead shim it needed) was killed
by the GoingToCamp WAF with an 8×403 and has since been removed from the repo;
this function never touches GoingToCamp, and its only network peer is
`api.github.com`. It does exactly three things:

1. **Read the GitHub PAT from SSM Parameter Store** (`SecureString`,
   `WithDecryption=True`) at invoke — an injected-client pattern, so the whole
   thing stays testable offline with a fake SSM client.
2. **POST `workflow_dispatch`** to the single hardcoded GitHub endpoint with
   `{"ref": "main"}`, telling GitHub Actions to run the (unchanged) poll.
3. **Log the HTTP status of every attempt**, and treat GitHub's **204** as
   success and **anything else** as a failure that raises — so the invocation is
   marked failed, the `Invocations`/error CloudWatch alarm can see it, and the
   *silent-miss* failure class (a trigger that quietly stops firing) is loud.

Why raising matters: the whole reason Path A uses a Lambda instead of a direct
EventBridge API-destination call is that this system's failure mode is *silent
missing* — no run happens, so no Actions failure email is ever sent. A swallowed
non-204 would look green and page no one; re-raising is the loud-failure contract
that turns a missed trigger into a visible invocation error.

**No third-party imports.** The HTTP POST goes through the standard library
(`urllib`), and `boto3` is provided by the Lambda `python3.12` runtime and
imported lazily inside `_ssm_client()`. So this file has **zero** bundled
dependencies: the operator deploys it as a single file (console inline editor or
a one-file zip), no wheel build, no packaging step, and the runtime dependency
cap (`httpx[http2]`, `PyJWT`, `cryptography`) is untouched — it isn't in the poll
package at all. The offline suite injects a fake SSM client and a fake HTTP
poster, so neither `boto3` nor the network is ever touched under pytest.

**SSRF posture:** every URL here is a module constant. The owner/repo, the
workflow file, the API host, and the `ref` are all hardcoded — nothing is
derived from `event`, from SSM, or from any other client-writable value.
"""

from __future__ import annotations

import json as jsonlib
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
# Lambda's python runtime pre-configures a root handler; make sure our INFO
# status line actually reaches CloudWatch regardless of the runtime's default
# level. This is the whole point of the design — every attempt must be logged.
logger.setLevel(logging.INFO)

# The one repo this trigger may ever dispatch. Hardcoded, never derived from the
# invocation event or from SSM — see the SSRF note in the module docstring.
OWNER_REPO = "wameson/campassist-monitor"
WORKFLOW_FILE = "monitor.yml"
# "Create a workflow dispatch event" REST endpoint — the only URL this Lambda hits.
DISPATCH_URL = (
    f"https://api.github.com/repos/{OWNER_REPO}"
    f"/actions/workflows/{WORKFLOW_FILE}/dispatches"
)
# The branch the poll runs from. A constant, not client input.
DEFAULT_REF = "main"

# GitHub answers a successful workflow_dispatch with 204 No Content.
SUCCESS_STATUS = 204

# SSM SecureString holding the fine-grained PAT (single repo, Actions: Read and
# write). Operator-overridable per-deploy via env, but never derived from the
# event. Documented in README "Scheduling".
DEFAULT_SSM_PARAM = "/campassist-monitor/github-dispatch-pat"

HTTP_TIMEOUT_SECONDS = 15


class DispatchError(RuntimeError):
    """A non-204 dispatch. Raised so Lambda records an invocation error and the
    heartbeat/error alarm fires — never swallowed, or the trigger fails silent."""


class _Response:
    """The `(status_code, text)` slice `dispatch` needs, produced from either a
    2xx `urlopen` result or the `HTTPError` urllib raises for a 4xx/5xx (which is
    itself a readable response). The fake HTTP client in the tests matches it."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class UrllibPoster:
    """Default HTTP poster over the standard library, so the Lambda needs no
    third-party dependency. Exposes the same `post(url, headers, json)` seam an
    httpx.Client would, which is what the offline tests substitute a fake for."""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS):
        self._timeout = timeout

    def post(self, url, headers, json) -> _Response:
        data = jsonlib.dumps(json).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return _Response(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx: HTTPError is a response too. Capture its status and body
            # so dispatch() can log the status and surface the body for diagnosis.
            body = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
            return _Response(exc.code, body)


def ssm_param_name() -> str:
    """The SSM parameter name holding the PAT, overridable per-deploy via env."""
    return os.environ.get("GITHUB_PAT_SSM_PARAM", DEFAULT_SSM_PARAM)


def read_token(ssm, param_name: str | None = None) -> str:
    """Fetch and decrypt the GitHub PAT from SSM.

    `ssm` is any object exposing `get_parameter(Name=..., WithDecryption=True)`
    the way boto3's SSM client does; the test suite passes a fake. The returned
    value is a secret and must never be logged.
    """
    param_name = ssm_param_name() if param_name is None else param_name
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def dispatch(ssm, http, *, param_name: str | None = None, ref: str = DEFAULT_REF) -> int:
    """Read the PAT, POST the workflow_dispatch, log the status, return it.

    `http` is any object exposing `post(url, headers=..., json=...)` — the real
    `UrllibPoster` in production, a fake in the tests. Logs the HTTP status of
    the attempt, then returns it on 204 or raises `DispatchError` on anything
    else. The token lives only in the Authorization header — it is never logged,
    and a GitHub error body cannot contain it, so the body is safe to surface.
    """
    token = read_token(ssm, param_name)
    resp = http.post(
        DISPATCH_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            # GitHub rejects requests with no User-Agent.
            "User-Agent": "campassist-monitor-trigger",
        },
        json={"ref": ref},
    )
    status = resp.status_code
    # Every attempt is logged — this is the inspectable record the silent-miss
    # design is built around. The URL and status only; never the token.
    logger.info("workflow_dispatch POST %s -> HTTP %s", DISPATCH_URL, status)
    if status != SUCCESS_STATUS:
        # Body cannot carry the token; include a bounded slice to explain the
        # 401/404/422 the operator will otherwise have to guess at.
        body = (resp.text or "")[:500]
        raise DispatchError(
            f"workflow_dispatch to {OWNER_REPO} failed: HTTP {status} (expected "
            f"{SUCCESS_STATUS}); body: {body!r}"
        )
    return status


def _ssm_client():
    """boto3 SSM client — provided by the Lambda runtime, imported lazily so it
    is never bundled and the offline suite (which injects a fake) never needs it."""
    import boto3  # provided by the Lambda runtime; never in requirements/zip

    return boto3.client("ssm")


def handler(event, context):
    """Lambda entrypoint. Wired in the console as `trigger_lambda.handler`."""
    status = dispatch(_ssm_client(), UrllibPoster())
    return {"ok": True, "status": status}
