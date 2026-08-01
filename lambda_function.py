"""AWS Lambda entrypoint shim for the campsite monitor (PLAN.md Phase 17).

The business logic does not move: `scripts/monitor.py` and its providers,
budgets, containment and exit-status rules run exactly as they do under GitHub
Actions. This shim only adapts the `python scripts/monitor.py` CLI contract to
Lambda's `handler(event, context)` contract, with three thin responsibilities:

1. **Load secrets before the monitor reads them.** The monitor reads the same
   six values the Actions workflow passes (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
   `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_P8_KEY`) via
   `*.from_env()`. Here they live in SSM Parameter Store as `SecureString`s; we
   fetch and decrypt them into `os.environ` once per container, cached across
   warm invocations so `kms:Decrypt` stays deep under the free tier.

2. **Forward a *fresh* process-start anchor per invocation.** `monitor.run()`
   binds `process_started` as a default argument evaluated *once at import*, and
   the fan-out deadline is anchored to it. On a warm container the module is not
   re-imported, so rebinding `monitor.PROCESS_STARTED` would NOT help — `run()`'s
   bound default stays stale and the deadline lands in the past, skipping per-id
   error isolation. The fix is to forward `process_started=time.monotonic()`
   through `main()` into `run()` every invocation. (Regression-tested.)

3. **Translate the exit code into a Lambda result.** `main()` always ends in
   `raise SystemExit(exit_code(result))` (and preflight HALT raises
   `SystemExit(1)` before that). `SystemExit` is a `BaseException`; uncaught it
   would make Lambda report *every* invocation — including the healthy
   `SystemExit(0)` — as an error. So: code 0 → return normally (success);
   non-zero → re-raise as a real exception so the invocation is marked failed →
   `Errors` metric → alarm → SNS. A swallowed non-zero code would silently undo
   this repo's loud-failure contract.

`boto3` is provided by the Lambda `python3.12` runtime, so it is imported lazily
inside `_ensure_secrets()`: the offline test suite injects a fake SSM client and
never triggers the import, and the deployment zip never bundles it — the runtime
dependency cap (`httpx[http2]`, `PyJWT`, `cryptography`) is untouched.
"""

from __future__ import annotations

import os
import time

# The env var names the GitHub Actions workflow passes today. Each one's value
# lives in SSM at `<prefix><NAME>`; the prefix is the single naming convention
# the operator must match when creating the SecureString parameters. (This
# deploy path is now orphaned/dormant — see PLAN.md Phase 17.) Values never
# live in the repo or in IaC.
SECRET_ENV_VARS = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "APNS_KEY_ID",
    "APNS_TEAM_ID",
    "APNS_BUNDLE_ID",
    "APNS_P8_KEY",
)

DEFAULT_SSM_PREFIX = "/campassist-monitor/"

# Injectable clock seam: tests patch this to assert warm-invocation freshness
# deterministically without perturbing the `time` module the monitor uses.
_monotonic = time.monotonic

_secrets_loaded = False


def ssm_prefix() -> str:
    """The SSM parameter-name prefix, overridable per-deploy via env."""
    return os.environ.get("SSM_PARAM_PREFIX", DEFAULT_SSM_PREFIX)


def load_secrets(ssm, prefix: str | None = None) -> dict[str, str]:
    """Fetch the six SecureString parameters and set them in `os.environ`.

    `ssm` is any object exposing `get_parameters(Names=..., WithDecryption=True)`
    the way boto3's SSM client does; the test suite passes a fake. Returns the
    env-var names it applied (never the secret values). Raises if any parameter
    is missing, so a half-configured deploy fails loudly instead of running with
    only some of its secrets.
    """
    prefix = ssm_prefix() if prefix is None else prefix
    names = [prefix + name for name in SECRET_ENV_VARS]
    resp = ssm.get_parameters(Names=names, WithDecryption=True)
    invalid = resp.get("InvalidParameters") or []
    if invalid:
        raise RuntimeError(f"missing SSM parameters: {sorted(invalid)}")
    values = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    applied: dict[str, str] = {}
    for name in SECRET_ENV_VARS:
        os.environ[name] = values[prefix + name]
        applied[name] = "<set>"  # never surface the value
    return applied


def _ensure_secrets() -> None:
    """Load secrets from SSM once per container (cached across warm calls)."""
    global _secrets_loaded
    if _secrets_loaded:
        return
    import boto3  # provided by the Lambda runtime; never in requirements/zip

    load_secrets(boto3.client("ssm"))
    _secrets_loaded = True


def invoke(main, process_started: float):
    """Run one monitor cycle via `main` and translate its exit into a result.

    Exit code 0 (or `None`) → return a summary dict (successful invocation);
    non-zero → re-raise as a real exception so Lambda flags the invocation
    failed. This is the single most important line of the shim: a swallowed
    non-zero code looks green and pages no one.
    """
    try:
        main(process_started=process_started)
    except SystemExit as exc:
        code = exc.code
        if code:  # truthy → non-zero int (or non-None); 0 and None are success
            raise RuntimeError(f"monitor cycle exited non-zero: {code!r}") from exc
        return {"ok": True, "exit_code": 0}
    # main() always raises SystemExit in practice; a plain return is still a
    # clean cycle, so treat it as success rather than inventing a failure.
    return {"ok": True, "exit_code": 0}


def handler(event, context):
    """Lambda entrypoint. Wired in the console as `lambda_function.handler`."""
    _ensure_secrets()
    import monitor  # imported only after secrets are in env

    return invoke(monitor.main, process_started=_monotonic())
