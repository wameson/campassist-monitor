# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- **Spec:** the authoritative design (schema, API contract, anti-blocking + write-budget rules, per-phase checklists) is `PLAN.md` in `~/workspace/firstmate/projects/camp-assist/` — read-only, lives outside this repo. The README summarizes the operator-facing parts.
- **Test:** `pytest` (config in `pytest.ini`; it puts `scripts/` and `tests/` on `sys.path` — modules import as `monitor`, `apns`, `db`, fakes from `tests/helpers.py`). Python 3.12; install with `pip install -r requirements-dev.txt`. The suite must stay fully offline: no network, no real secrets.
- **Sharp edges:** runtime deps are deliberately capped at `httpx[http2]`, `PyJWT`, `cryptography`. A no-change monitor cycle must stay ≤5 Supabase writes (`test_write_budget` enforces it) — don't add per-watch writes. `scripts/*` take injectable HTTP clients/clocks; keep new code testable the same way. Never poll recreation.gov in tests; use `tests/fixtures/`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
