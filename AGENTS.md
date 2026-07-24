# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- **Spec:** the authoritative design (schema, API contract, anti-blocking + write-budget rules, per-phase checklists) is `PLAN.md` in `~/workspace/firstmate/projects/camp-assist/` — read-only, lives outside this repo. The README summarizes the operator-facing parts.
- **Test:** `pytest` (config in `pytest.ini`; it puts `scripts/` and `tests/` on `sys.path` — modules import as `monitor`, `apns`, `db`, fakes from `tests/helpers.py`). Python 3.12; install with `pip install -r requirements-dev.txt`. The suite must stay fully offline: no network, no real secrets.
- **Sharp edges:** runtime deps are deliberately capped at `httpx[http2]`, `PyJWT`, `cryptography`. A no-change monitor cycle must stay ≤5 Supabase writes (`test_write_budget` enforces it) — don't add per-watch writes. `scripts/*` take injectable HTTP clients/clocks; keep new code testable the same way. Never poll recreation.gov in tests; use `tests/fixtures/`.
- **Cycle resilience:** per-watch work in `monitor.run` is contained — one watch's failure must never abort the cycle, and end-of-cycle bookkeeping (`run_summaries`, pruning) must always run. New watch writes go through `patch_watches` (batched, per-id fallback) and new failure paths through `record_failures`, so they count toward the threshold that decides the run's exit status. Two invariants are easy to break: a watch is moved to `status='error'` **only** for a failure a write pinned to its own row (`PatchOutcome.isolated` / `isolated_failure`) — table-scoped `sent_alerts`/APNs failures use `unattributed_failure` and must not error the watch; and `run_summaries.errors` is **world-readable**, so it gets the `safe=True` rendering of `summarize_exception` (no watch UUIDs, no PostgREST `details`/`hint`) while the operator-only Action annotation gets the full rendering via `errors_detail`. Keep loud failures loud: systemic breakage must still exit non-zero (README "Errors and run status").
- **Schema changes:** editing `supabase/schema.sql` only affects fresh installs. Existing live DBs drift unless you also add an idempotent `supabase/migrations/NNNN_<desc>.sql` (see README "Database migrations"). Migrations are applied **manually** in the Supabase SQL editor by design — CI does not run them.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
