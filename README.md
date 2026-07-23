# campassist-monitor

CampAssist backend: centralized recreation.gov availability monitor.
A GitHub Actions cron job (every 30 minutes, in this **private** repo) polls the
recreation.gov availability API for all users' watches — deduplicated to one API
call per unique (campground, month) per cycle — detects new openings via state
hashes, and sends APNs push notifications with a direct booking link. Supabase
(free tier) is the shared database; there is no server.

See the CampAssist `PLAN.md` (Phase 1) for the full design: write budget,
anti-blocking rules, and the recreation.gov API contract.

## Layout

| Path | Purpose |
|---|---|
| `scripts/monitor.py` | One monitoring cycle: jittered polling, dedupe, delta detection, alert cooldown, watch lifecycle (expiry + erroring), per-watch failure containment + threshold-gated exit status, retention pruning, run summary |
| `scripts/apns.py` | APNs HTTP/2 client (ES256 JWT auth, sandbox/production routing, 410 token cleanup) |
| `scripts/db.py` | Thin Supabase PostgREST client (service-role key) |
| `supabase/schema.sql` | Fresh-install schema + RLS policies — paste into the Supabase SQL editor for a **new** DB |
| `supabase/migrations/` | Ordered, idempotent SQL applied **by hand** to keep **existing** DBs in sync (see [Database migrations](#database-migrations)) |
| `.github/workflows/monitor.yml` | 30-minute cron + manual `workflow_dispatch` |
| `.github/workflows/keepalive.yml` | Monthly bot commit so GitHub never auto-disables the scheduled workflow (60-day rule) |
| `.github/workflows/ci.yml` | pytest on every PR and push to `main` (ubuntu) |
| `tests/` | Offline pytest suite — fakes and fixtures only, no network or secrets |

## Setup

### 1. Supabase

1. Create a free project at [supabase.com](https://supabase.com) (any region).
2. Open **SQL Editor**, paste the entire contents of [`supabase/schema.sql`](supabase/schema.sql), and run it once. This creates `watches`, `device_tokens`, `sent_alerts`, and `run_summaries` with row-level security enabled.
3. Enable **Anonymous Sign-In**: Dashboard → **Authentication → Sign In / Providers → Allow anonymous sign-ins** → toggle on. The iOS app signs every device in anonymously so `auth.uid()` is real and the RLS policies work.
4. Collect two values for the secrets below:
   - **Project URL** (Settings → API → Project URL) → `SUPABASE_URL`
   - **service_role key** (Settings → API → Project API keys → `service_role`, secret) → `SUPABASE_SERVICE_KEY`. This key bypasses RLS — it must only ever live in GitHub Actions Secrets, never in the app.

### 2. APNs key (Apple Developer Program, $99/yr)

1. [developer.apple.com](https://developer.apple.com) → **Certificates, Identifiers & Profiles → Keys** → create a key with **Apple Push Notifications service (APNs)** enabled.
2. Download the `.p8` file (one-time download) and note the **Key ID**.
3. Your **Team ID** is under Membership; the **bundle id** is the iOS app's identifier (e.g. `com.example.campassist`).

### 3. GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions** → add all six:

| Secret | Value |
|---|---|
| `SUPABASE_URL` | Supabase project URL, e.g. `https://abcdefgh.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase `service_role` key |
| `APNS_KEY_ID` | APNs key ID (10 chars) |
| `APNS_TEAM_ID` | Apple Developer team ID (10 chars) |
| `APNS_BUNDLE_ID` | iOS app bundle id (used as `apns-topic`) |
| `APNS_P8_KEY` | Full contents of the `.p8` file, including the `BEGIN/END PRIVATE KEY` lines |

### 4. First run

Actions → **Monitor Campsites** → **Run workflow**. A run with zero watches
completes cleanly and writes one `run_summaries` row. Scheduled runs then fire
every 30 minutes (GitHub adds its own 0–5 min cron jitter; the script adds a
random 0–4 min start delay on top by design).

## Database migrations

`supabase/schema.sql` is only the **fresh-install bootstrap** — running it creates a
new database with the current schema. It does **not** update a database that already
exists. When a column or object is added to `schema.sql` later, an already-live DB
(created from an earlier version) never receives it, and the monitor's writes start
failing (a column present in `schema.sql` but missing from the pre-existing live DB
once caused a multi-day PATCH-400 outage). Migrations close that gap.

- **Where:** `supabase/migrations/` holds ordered, numbered files (`0001_<desc>.sql`,
  `0002_<desc>.sql`, …). Each is **idempotent** (`ADD COLUMN IF NOT EXISTS`,
  `CREATE INDEX IF NOT EXISTS`, …), so re-running an already-applied one is a no-op.
- **Applying them (manual):** in the Supabase **SQL Editor**, run each
  `supabase/migrations/*.sql` that has not yet been applied to that database, in
  numeric order. Because they are idempotent, running the whole directory in order is
  always safe if you are unsure which are outstanding. Do this after pulling schema
  changes and before the next monitor run.
- **CI does not run migrations** — this is deliberate for now. There is no automation;
  a human applies them against Supabase by hand. (Fresh installs still just run
  `schema.sql` once, as in Setup above.)
- **Adding a migration when you change the schema:** update `supabase/schema.sql` (so
  fresh installs get the change) **and** add a new `supabase/migrations/NNNN_<desc>.sql`
  with the next number, using idempotent DDL (so existing DBs get the same change).
  Keep the two in sync — every additive change to `schema.sql` needs a matching
  migration.

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The test suite is fully offline: Supabase, APNs, and recreation.gov are all
faked. CI runs the same suite on every PR and push to `main`.

## Operating notes

- **Write budget:** a cycle with no availability changes performs ≤5 Supabase
  writes (1 batched `last_checked_at` PATCH, 1 `run_summaries` INSERT, 2
  retention DELETEs) regardless of watch count — enforced by `test_write_budget`.
- **Politeness / anti-blocking:** one rotating browser User-Agent per run,
  randomized campground order, 1.2–2.8 s inter-request delays, exponential
  backoff (2 s → 4 s → 8 s, then skip the campground for this cycle).
- **Time budget:** polling (including backoff retries) stops once an
  8-minute per-cycle budget is spent, keeping every run — even under
  sustained 403/429 blocking — inside the workflow's 15-minute timeout.
  Skipped campgrounds are simply retried next cycle; skipped watches keep
  their old `last_checked_at`, and the run summary counts only what was
  actually polled.
- **Poll horizon:** each watch's months are clamped to today through
  today + 12 months; "today" uses a fixed UTC-8 offset so same-night
  openings at US campgrounds stay alertable during US evening hours after
  UTC midnight. A watch entirely beyond the horizon is polled once the
  horizon reaches it.
- **Watch lifecycle:** the backend expires watches whose end date has
  passed (`status='expired'`) and errors watches with malformed campground
  ids (`status='error'`, once), whose campground has 404ed for 3
  consecutive cycles (typo or delisted campground; any successful poll
  resets the count), or whose own database writes are permanently rejected
  (see Failure containment) — the app never has to clean these up.
- **Failure containment:** a failure that belongs to one watch — a rejected
  write, a malformed row — is caught, recorded, and skipped; it never
  aborts the cycle. The other watches are still polled and alerted, and the
  run still writes its `run_summaries` row and prunes. Batched watch writes
  (`id=in.(…)`) are attempted as one write, as the write budget assumes.
  Only a *permanently* rejected batch (a PostgREST 4xx other than 429:
  missing column, constraint violation) falls back to one write per id, so a
  single unwritable row cannot silently drop everyone else's update. A
  transient batch failure (429, 5xx, timeout, transport error) is never
  fanned out: it errors nothing, so isolating it buys nothing, while dozens
  of sequential 30-second PATCHes against a struggling Supabase would blow
  the workflow's 15-minute timeout and kill the run before its summary row
  and pruning. For the same reason the fan-out stops at
  `PER_ID_FALLBACK_BUDGET_SECONDS` past the poll budget, and batches larger
  than `PER_ID_FALLBACK_MAX` skip it entirely.

  A watch moves to `status='error'` only when the failure was **pinned to
  that row** — a single-watch write, a per-id fallback write, or its own
  per-watch processing — and looks permanent. A batch failure nobody
  attributed to a specific row is recorded and counted but never errors a
  watch: users must not have to recreate a watch over a failure that was
  never shown to be theirs. A transient failure likewise leaves the watch
  `monitoring` to retry next cycle. Strike-count (`consecutive_not_found`)
  writes are pure bookkeeping: a rejected one is recorded, but the watch is
  still delta-checked, alerted, and stamped `last_checked_at`.
- **Alert delivery:** an APNs 5xx/429 or transient transport error keeps
  the watch's old `state_hash` so the alert is retried next cycle; a 410
  means the device token is dead and its row is deleted. Any other 4xx, a
  missing token row, or a device token so malformed the push URL can't be
  built is given up on (no retry) and the watch's hash still advances.
- **Errors and run status:** polling and alert errors (recreation.gov
  failures, unrecognized responses, APNs delivery problems) and contained
  per-watch failures are all recorded in the `run_summaries.errors` column
  (a count plus a bounded sample of messages) and surfaced as a
  `::warning::` annotation. The **exit status is decided by error rate**, so
  a broken watch does not cry wolf but broad breakage cannot hide:
  - *isolated* — the run exits 0 and the schedule stays **green**, because
    the healthy watches were served;
  - *systemic* — the run prints an `::error::` annotation, exits non-zero
    and turns the schedule **red**. Systemic means more than
    `SYSTEMIC_ERROR_RATE` (25%) of the watches the cycle actually **served**
    failed **and** at least `SYSTEMIC_ERROR_FLOOR` (2) of them did — so 1 of
    2 stays green while 2 of 2 goes red — or that a failure belonging to no
    watch (the `run_summaries` INSERT, retention pruning, or being unable to
    write `status='error'`) occurred. Both constants live at the top of
    `scripts/monitor.py` and are the tuning knobs.

    The served set is the rate's denominator *and* the scope of its
    numerator: it excludes watches that expired this cycle, that were errored
    for an invalid or persistently-404ing campground, and that the poll time
    budget never reached. A cycle that failed every watch it served goes red
    no matter how much of the pool left for unrelated reasons.

  Systemic runs deliberately leave the watch pool untouched: broad breakage
  is the operator's to fix, not something users should have to recreate
  their watches over. Failures before any of that — an unreachable Supabase,
  a malformed `APNS_P8_KEY`, a missing secret — still exit non-zero.
- **Retention:** `sent_alerts` and `run_summaries` rows older than 30 days are
  pruned every run.
- **Keep-alive:** GitHub disables cron workflows after 60 days without repo
  activity; `keepalive.yml` commits a timestamp monthly to prevent that.

## Upgrade path: self-hosted runner

When the private-repo free tier (2,000 min/month) gets tight, or if
recreation.gov starts blocking GitHub's datacenter IPs, register any always-on
home machine as a self-hosted runner — **unlimited free minutes on private
repos** and a residential IP:

1. Repo → **Settings → Actions → Runners → New self-hosted runner**, follow the
   3-command install on the machine (macOS/Linux/Windows, ~10 minutes).
2. Run it as a service so it survives reboots (`./svc.sh install && ./svc.sh start`).
3. In `.github/workflows/monitor.yml`, change `runs-on: ubuntu-latest` to
   `runs-on: self-hosted`.
4. Optionally tighten the cron to `*/15 * * * *` — minutes are free on
   self-hosted runners.

Alternative: make the repo public (unlimited hosted minutes), at the cost of
the monitoring code being visible.
