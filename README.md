# campassist-monitor

CampAssist backend: centralized campsite availability monitor for recreation.gov
and GoingToCamp (Washington State Parks).
A GitHub Actions workflow (`monitor.yml`, in this **public** repo), triggered
every ~30 minutes by an AWS EventBridge Scheduler on the exact wall clock (see
[Scheduling](#scheduling-fixing-the-schedule-drift-phase-17)), polls all
users' watches through the conformer each one's `provider` names — deduplicated
to one request per unique poll unit per cycle (see [Providers](#providers)) —
detects new openings via state hashes, and sends APNs push notifications with a
direct booking link. Each cycle runs **one poll job per provider** (a `plan` job
emits the matrix and owns the once-per-cycle retention prune; see
[Per-provider poll jobs](#per-provider-poll-jobs)), so a slow provider can never
starve a fast one of poll time in the same run. Supabase (free tier) is the
shared database; there is no server.

See [`PLAN.md`](PLAN.md) in this repo for the full backend design: schema and
migration rules, the monitor workflow, the poll/alert pipeline, the provider
seam, and the backend phase checklists. The iOS/product plan lives in the
`camp-assist` repo's `PLAN.md`.

## Layout

| Path | Purpose |
|---|---|
| `scripts/monitor.py` | One monitoring cycle, provider-neutral: jittered polling, dedupe, delta detection, alert cooldown, watch lifecycle (expiry + erroring), per-watch failure containment + threshold-gated exit status, retention pruning, run summary |
| `scripts/providers/` | One conformer per `watches.provider` value — the poll/parse/normalize/booking-link strategy (see [Providers](#providers)) |
| `scripts/apns.py` | APNs HTTP/2 client (ES256 JWT auth, sandbox/production routing, dead-token cleanup) |
| `scripts/db.py` | Thin Supabase PostgREST client (service-role key) |
| `scripts/preflight.py` | Read-only schema-drift guard run before each cycle (see [Database migrations](#database-migrations)) |
| `scripts/common.py` | Primitives shared by the cycle and its providers (date coercion, the error-line cap) |
| `supabase/schema.sql` | Fresh-install schema + RLS policies — paste into the Supabase SQL editor for a **new** DB |
| `supabase/migrations/` | Ordered, idempotent SQL applied **by hand** to keep **existing** DBs in sync (see [Database migrations](#database-migrations)) |
| `.github/workflows/monitor.yml` | The poll; triggered by `workflow_dispatch` (fired by AWS EventBridge). One `plan` job (matrix + retention prune) fans out to **one poll job per provider** that has watches (see [Per-provider poll jobs](#per-provider-poll-jobs)). Its GitHub `schedule` cron is **dormant** (commented out) — EventBridge drives the poll; uncomment to restore the 30-min cron |
| `.github/workflows/keepalive.yml` | Monthly bot commit so GitHub never auto-disables the scheduled workflow (60-day rule) |
| `.github/workflows/ci.yml` | pytest on every PR and push to `main` (ubuntu) |
| `.github/workflows/secret-scan.yml` | gitleaks on every PR — fails the check on any finding (see [Secret scanning](#secret-scanning)) |
| `.gitleaks.toml` | gitleaks ruleset config + the narrow test-fixture allowlist |
| `tests/` | Offline pytest suite — fakes and fixtures only, no network or secrets |

## Providers

Each watch names the campground site it polls in its `provider` column, and the
cycle routes it to the matching conformer of the `Provider` protocol
(`scripts/providers/base.py`). A provider owns the four site-specific things:
which poll units a watch needs, how to fetch and parse one, how to normalize
the result to the shared `{site_id: {"campsite_id", "site", "dates": […]}}`
shape, and the booking deep link the push opens. Everything else — the poll
plan dedupe across users, pacing and backoff budgets, `state_hash` delta
detection, alert dedup and cooldown, failure containment, exit status,
`run_summaries`, retention — is provider-neutral and lives in `monitor.py`.

Dispatch follows the `provider` column and nothing else: each planned poll unit
is recorded against the conformer that asked for it, and the 404-strike
bookkeeping is kept per provider, so two providers that happen to name the same
`campground_id` are polled — and struck — entirely separately.

Three conformers ship today:

| `provider` | Module | Poll unit | `provider_ref` |
|---|---|---|---|
| `recreation_gov` | `scripts/providers/recreation_gov.py` | one (campground, month) request | unused — `campground_id` is the whole identity |
| `going_to_camp` | `scripts/providers/going_to_camp.py` | one (park, stay) — the root map plus each child map it names, 2–5 paced GETs inside a single `poll`, plus one park-catalog GET (see below) | `{"resource_location_id": …, "map_id": …}`, both required (a watch without them is errored once, see Watch lifecycle) |
| `use_direct` | `scripts/providers/use_direct.py` | one (facility, stay) — a single read-only POST to the tenant's availability grid | unused — the `campground_id` carries both the tenant and the facility |

GoingToCamp (Washington State Parks, on the Aspira platform) is map-scoped and
recursive: a park's root map answers with pointers to its child maps and no
sites of its own, so `poll` follows those maps (to the bottom, should one ever
nest deeper than the one level the live API needs) and merges the result before
the cycle ever sees it. A site is a `resourceId` with a per-night `availability`
enum in which only `0` is confirmed to mean bookable, so every other value —
including one this build has never seen — parses as taken. The booking deep
link is the park's booking search pre-filled with the watch's dates (the SPA
takes no site preselect), not a per-site page.

The availability body names no site, so a successful poll reads the park's
resource catalog once per cycle (`/api/resourcelocation/resources`, keyless,
~64 KB per park, cached in-process — as are the two tiny vocabulary tables its
enum indices decode through, two further paced GETs the first park of a cycle
pays for and every later one reuses) to turn each `resourceId` into the label
the park actually uses — an opening carries "Site 42", not `-2147482979` (the
push body itself is a count, so the label travels with the opening rather than
appearing in the notification text). The label is display only: a watch that
names sites is matched on the `resourceId`, which is what the client persists
in `site_ids`. That catalog also carries the per-site detail the choose-sites
work needs (capacity, allowed equipment, the electric/water hookup enum) and the
platform's own "ADA Only" flag, which the exclusion below reads. It is cosmetic
to the poll: a park whose catalog a cycle cannot read is still polled, hashed
and alerted on — the selection still matches, only the display label falls back
to the `resourceId` — with one reported line. Being cosmetic, it is
also unretried — one paced attempt each, no backoff, so a dead catalog endpoint
cannot spend the time budget the availability polls need.

**ADA-only sites are not openings unless the watch asked for them.** On this
platform "ADA Only" means only campers with disabilities may reserve the site,
and the platform's own search excludes those sites by default. So does the
monitor: `going_to_camp`'s `extract_relevant` drops them unless the watch sets
`include_ada_only` (default `false` for every watch, old and new — there is no
backfill, so this is a deliberate behaviour change) or names the site in
`site_ids`, in which case the user's own choice wins. The exclusion runs before
the state hash, so an ADA-only site opening and closing is not a delta and
costs no write. It fails open in every direction: only a site the catalog
positively marked is ever dropped, so a catalog fetch that failed suppresses
nothing — a suppressed opening would be invisible to the user, a surplus one is
only noise. `recreation_gov` does not filter at all: that API publishes only a
wider "accessible" flag meaning the site *has* accessibility features, never
that it is reserved (see the comment at its `extract_relevant`).

**Request posture at this host: keyless GET, plus one read-only pricing POST;
still never drive a browser.** The SPA is Azure-WAF captcha-gated and `/api/*`
is not, so every request stays on `/api/*` with a browser UA and the pacing
above. The single exception is the per-night price
(`/api/resource/feeDetails`), which answers `405` to a GET: it is a POST with
an empty body, creating nothing and carrying no cart, cookie or token. It is
not in the polling loop.

Because the recursion costs more requests than a single month call, one park's
whole fan-out is capped (however deeply its maps nest, and a map is never
fetched twice) and every child request is paced and charged against the cycle's
own time budget; a park that only half-polls is a failed unit, so the watch
keeps its old `state_hash` rather than reading the gap as sites vanishing.
`MAX_CHILD_MAPS` (40) is a safety cap set an order of magnitude above any
observed park, not a tuning knob, so a park past it is a fault an operator has
to clear rather than one more failed unit: `poll` raises, the cycle contains it
as a cycle failure, and the run goes non-zero — a park that can never be served
must not sit behind a green exit code while its watches look healthy.

UseDirect (the Tyler platform behind **ReserveCalifornia**, and other western
state parks on the same seam later) is a single read-only POST per (facility,
stay) to the tenant's availability grid — no recursion, no second catalog
request: the grid body carries each site's stable `UnitId`, its display `Name`,
and a per-night `IsFree`, and a night is an opening only when `IsFree` is
positively true (anything else — a held or blocked slice, a missing field — is
taken). It is **multi-tenant by design** because UseDirect runs one deployment
per state, each on its own host: a `Tenant` registry (`use_direct.TENANTS`) pins
each state's request host, path prefix and booking site as vetted constants, and
the `campground_id`'s tenant key only *selects* one of them (an allowlist
lookup). ReserveCalifornia is the one tenant wired today; California has migrated
off `*.usedirect.com` onto a Tyler cloud host, so no host is inferred from a
pattern — each state's is pinned individually. The booking deep link lands on the
tenant's public booking site (the grid carries no id to build a verified
per-facility link from). It applies **no ADA exclusion** and does not read
`include_ada_only`: the grid's `IsAda` is not established to mean "reserved" (it
reads like recreation.gov's "has accessibility features", not GoingToCamp's
explicit "ADA Only"), so filtering on it would risk hiding bookable sites. Its
per-system unit codes (`UnitCategoryId`, `UnitTypeGroupId`) are read by nothing
here, so none can be shared across tenants; a future build that decodes them must
key its vocabulary per tenant and fail open, like GoingToCamp's. Like
GoingToCamp, one facility's grid is capped (`MAX_UNITS_PER_FACILITY`, a safety
cap orders of magnitude above any real facility): a response past it is a
permanent fault an operator must clear, so `poll` raises and the run goes
non-zero rather than serving a runaway response behind a green exit.

**Shared wire contract (owned by this backend conformer; the iOS app builds to
it):** a UseDirect watch has `provider = 'use_direct'` and
`campground_id = '<tenant>_<facilityId>'` — a registered tenant key (`ca` for
ReserveCalifornia), an underscore, and the UseDirect FacilityId as a decimal
integer, e.g. `ca_377`. Underscore, never colon, so it matches
`[A-Za-z0-9_-]+`; `provider_ref` is unused.

A watch whose `provider` this build has no conformer for is left untouched —
unpolled, still `monitoring`, outside the systemic error rate — and reported as
a count in the run summary, so an older monitor cannot mis-serve a row a newer
client wrote.

Whatever a provider's own identifiers are, `watches.campground_id` stays the
watch's campground identity for the cycle's 404-strike lifecycle and
`campgrounds_polled` telemetry, and must match `[A-Za-z0-9_-]+` — a watch whose
id has any other character is errored before it is ever polled. A
`going_to_camp` row therefore needs a stable id in that alphabet: the client
packs `gtc_<resourceLocationId>_<mapId>` — **underscores, never colons**, the
contract `PLAN.md` states in full; a `use_direct` row packs
`<tenant>_<facilityId>` the same way (e.g. `ca_377`). For `recreation_gov` the
backend never parses the id; for `going_to_camp` and `use_direct` it does, but
only into identifiers — never a host (SSRF).

To add a provider: write the conformer in its own module under
`scripts/providers/`, then register it in `scripts/providers/__init__.py`.
**Its request host must be a constant in that module.** `watches.provider_ref`
is client-writable and carries identifiers only — never derive a host, URL, or
path from it (SSRF).

## Setup

### 1. Supabase

1. Create a free project at [supabase.com](https://supabase.com) (any region).
2. Open **SQL Editor**, paste the entire contents of [`supabase/schema.sql`](supabase/schema.sql), and run it once. This creates `watches`, `device_tokens`, `sent_alerts`, `alert_history`, and `run_summaries` with row-level security enabled.
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
completes cleanly: the `plan` job prunes and emits an empty matrix, so no poll
job runs and no `run_summaries` row is written for a provider with no watches.
Thereafter the AWS
EventBridge Scheduler fires `workflow_dispatch` every ~30 minutes on the exact
wall clock (the GitHub `schedule` cron is dormant — see
[Scheduling](#scheduling-fixing-the-schedule-drift-phase-17)); the script adds a
random 0–20 s start delay on top by design — see `START_JITTER_MAX_SECONDS`.

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
  fresh installs get the change), add a new `supabase/migrations/NNNN_<desc>.sql`
  with the next number, using idempotent DDL (so existing DBs get the same change),
  **and** add the column to the `REQUIRED` manifest in `scripts/preflight.py` (below).
  Keep all three in sync — a CI test fails when the manifest and `schema.sql` disagree.
  **Order matters:** apply the migration by hand in the SQL editor **first**, and only
  then merge the manifest entry. A new monitor-written or read-required column is
  classified `halt` unless its read or write is demonstrably tolerant of the column
  being absent (the two bullets below), so merging it ahead of the apply would
  deliberately stop every cycle until an operator got to the SQL editor.
- **A column read with a default is still `warn`.** `watches.include_ada_only`
  (`0003_watches_include_ada_only.sql`) is the per-watch opt-in for sites
  reserved for campers with disabilities, read only as
  `bool(watch.get("include_ada_only"))` in `going_to_camp`'s `extract_relevant`
  (see Providers). An absent column reads `false` — exactly what a migrated row
  carries by default — so a live DB nobody has migrated yet keeps monitoring and
  keeps excluding, which is why it stays `warn` rather than `halt`. Note the
  default is a deliberate behaviour change, not a status quo: the monitor used to
  alert on ADA-only sites, so every pre-existing watch stops being alerted about
  them unless its owner opts back in. There is no backfill.
- **A column the monitor writes can still be `warn` — if the write proves it.**
  `watches.error_reason` (`0004_watches_error_reason.sql`) records *why* a watch was
  errored. The monitor writes it, which normally means `halt`, but the write is
  demonstrably tolerant: the reason rides along with the `status='error'` write and,
  when PostgREST rejects that write for want of this column — `42703`, or the
  `PGRST204` schema-cache miss it answers a write body with — it is retried once
  without it and dropped for the rest of the cycle. That first attempt also skips
  the per-id fallback for this one signature, so the cost is literally one extra
  write however many watches were in the batch. An unmigrated database
  therefore still errors watches, still keeps its lifecycle and still monitors — the
  reason is simply not recorded, and the census reports those rows as `unrecorded`.
  Halting instead would stop the whole cycle over a column that only annotates an
  error, which is exactly the blast radius inversion the `warn` class exists to avoid.
- **The egress read-reduction ships as three `warn` migrations.** `0009_watches_updated_at`
  (a column plus a trigger that bumps it only on a user edit), `0010_poll_units` (a
  per-unit availability-hash table), and `0011_monitoring_plan_view` (a deduped
  planning view). All are backend-only and all fall back to the full monitoring read
  when absent (`monitor.is_missing_schema_object`), so a database on which they are not
  yet applied keeps monitoring correctly, just at the pre-reduction egress. Apply all
  three by hand in the SQL editor together to turn the reduction on; there is no
  ordering constraint against the app, and no monitor code change is needed to run
  before they are applied. Only `0009`/`0010` add manifest entries (`updated_at`,
  `poll_units.*`, all `warn`); the **view is not preflighted** (the drift manifest
  mirrors `CREATE TABLE`s, and its absence is handled at runtime by the same fallback).
- **The monitor checks before it runs.** Each run starts with a read-only schema
  preflight (`scripts/preflight.py`): six `GET`s with `limit=0`, zero writes, no row
  data, before the start jitter. It never applies anything — the by-hand posture above
  is unchanged — it only reports, naming every missing `table.column` and the exact
  migration file to apply.
  - A missing column the monitor **writes or reads as required** prints an `::error::`
    and exits non-zero *before* the cycle starts, turning what used to be a silent
    write-time `400` into a red run with the remedy in the annotation.
  - A missing column the monitor **provably tolerates** (app-only columns such as
    `provider_ref`, or a read with a demonstrated fallback such as `provider`) prints a
    `::warning::` and the cycle runs normally — drift the backend survives must never
    pause cancellation monitoring, even when it breaks the iOS app.
  - A probe that fails for any other reason (5xx, 429, timeout, an unrecognized error
    code) is treated as a Supabase blip: it is recorded against its own table, the
    remaining tables are still probed, and if nothing halting was confirmed the run
    prints a `::warning::` and the cycle runs. Only a positively-identified missing
    object can stop a run — but a blip neither *un*-confirms one nor hides one on
    another table. Whatever the pass did prove missing still counts, so a halting
    column stops the run even when other probes blipped, with the annotation naming
    the tables it could not classify so the listed set is not read as complete.

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The test suite is fully offline: Supabase, APNs, and every campground provider
are all faked. CI runs the same suite on every PR and push to `main`.

## Operating notes

- **Write budget:** a whole-fleet cycle with no availability changes performs ≤5
  Supabase writes (1 blanket `last_checked_at` PATCH, 1 `run_summaries` INSERT, 3
  retention DELETEs — `sent_alerts`, `run_summaries`, `alert_history`)
  regardless of watch count — enforced by `test_write_budget`. Under the
  per-provider poll split (see [Per-provider poll jobs](#per-provider-poll-jobs))
  those 5 are divided by owner, not multiplied: each provider's poll job does
  only the ~2 writes for its own slice, and the plan job does the 3 retention
  DELETEs once for the whole cycle. `last_checked_at` is written two ways: a
  single **filter-scoped blanket** PATCH (`status=eq.monitoring&end_date=gte.today`,
  plus `provider=eq.<job>` on a poll job so it stamps only that provider's slice,
  no id list) that freshens every monitored watch — including the quiet-unit rows
  the cycle never read back (see below) — plus, for the rows it did read, the
  per-id chunked PATCH (≤150 ids per `id=in.(…)` URL, `PATCH_ID_CHUNK_MAX`) that
  keeps the fan-out isolation which errors a genuinely bad row.
- **Reading the watch pool (egress read-reduction):** the per-cycle `watches`
  read no longer grows linearly with the fleet. Every unit is still polled, but a
  cycle reads back only the watch rows that can change a camper's outcome: the
  deduped poll plan comes from the `monitoring_plan` view (scoped to the job's
  provider under the poll split); each polled unit's raw
  availability is diffed against stored hashes (`poll_units`); and a cycle reads
  the full watch rows only for a campground that **changed** (or 404ed) this
  cycle, plus a few small bounded sets — watches **edited since the last cycle**
  (`watches.updated_at`), watches carrying a **404 strike**, and **expiring**
  watches. An unchanged unit cannot change any watcher's state, so on a quiet
  cycle almost no watch rows are read. Openings are never missed: a change reads
  the unit's rows back and alerts, and an unchanged unit with a newly-created or
  edited watch is still caught by the `updated_at` watermark. Every read is
  column-scoped (`WATCH_READ_COLUMNS`, with a `select=*` fallback on a missing
  WARN column) and paginated so a PostgREST `max-rows` cap can never truncate it.
  Before the migrations that add these objects are applied, the cycle falls back
  to reading the full monitoring set — the pre-reduction behaviour — so it is
  correct at the old egress until an operator applies them.
- **Politeness / anti-blocking:** one rotating browser User-Agent per run,
  randomized campground order, 1.2–2.8 s inter-request delays, exponential
  backoff (2 s → 4 s → 8 s, then skip the campground for this cycle).
- <a id="per-provider-poll-jobs"></a>**Per-provider poll jobs:** the poll is split into one GitHub Actions job per
  provider (`monitor.yml`). A `plan` job — the one job that runs every cycle —
  reads which providers currently have watches (`providers_with_watches`), emits
  a dynamic matrix so an idle provider runs no job, and **owns the once-per-cycle
  retention prune** (`plan_main` → `prune_retention`); it fails the cycle red on a
  HALT-table prune failure while the poll jobs still run (`if: !cancelled()`).
  Each poll job then runs `python scripts/monitor.py --provider <name>` — the same
  `run()` scoped to that provider's watches (`run(provider=…, prune=False)`) on
  that provider's own budget (`POLL_PROFILES`), writing its own labelled
  `run_summaries` row (`run_summaries.provider`, migration `0013`) and never
  pruning. The jobs are independent (`fail-fast: false`), so one provider
  failing, being blocked, or exhausting its budget cannot stop the others — which
  is the reliability point: a GoingToCamp-heavy fleet (~11 s/park) can no longer
  consume a shared budget and starve recreation.gov (~2.5–3 s/campground-month) of
  poll time in the same run. This changes **how many jobs run per cycle** (visible
  in the Actions tab): a `plan` job plus one poll job per active provider. The
  whole-fleet `python scripts/monitor.py` (no flags) still works — one run, all
  providers, prune included — so a workflow revert keeps monitoring.
- **Time budget:** polling (including backoff retries) stops once a per-cycle
  budget is spent — 480 s (`CYCLE_TIME_BUDGET_SECONDS`) for the whole-fleet run
  and for the cheap providers' 15-minute jobs, 720 s for GoingToCamp's 20-minute
  job (`POLL_PROFILES`; it is the costliest per unit, so it is sized up rather
  than left to share) — keeping every run, even under sustained 403/429 blocking,
  inside its job's timeout. The job-timeout arithmetic closes for each provider
  (`test_provider_budget_arithmetic_closes_for_each_job`). Skipped campgrounds are
  simply retried next cycle; skipped watches keep their old `last_checked_at`, and
  the run summary counts only what was actually polled. But a cycle that runs out
  of budget **mid-plan goes red, not green**: the parks it never reached were not
  served this cycle, which is a completed miss belonging to no single watch, so
  it is recorded as a cycle failure (systemic → non-zero exit, `::error::`) with
  the skipped count surfaced on the result as `polls_skipped`. There is no
  tolerant threshold — serial per-request politeness caps GoingToCamp's job at ~65
  parks, so any skip already means that provider's fleet is over one cycle's
  capacity. See PLAN.md "Time budget" for the arithmetic.
- **Poll horizon:** every provider clamps what it requests for a watch to
  today through today + 12 months; "today" uses a fixed UTC-8 offset so
  same-night openings at US campgrounds stay alertable during US evening
  hours after UTC midnight. A watch entirely beyond the horizon is polled
  once the horizon reaches it, and one straddling it is served — hashed and
  alerted on — for its in-horizon nights alone.
- **Flexible-date watches:** a watch with `date_mode='flexible'` (Phase 16)
  reuses `start_date`/`end_date` as a search *range* and alerts when **any**
  fully-open consecutive-night window of at least `flex_min_nights` nights fits
  inside it — "any N-night window in `<range>`" rather than one fixed stay.
  `flex_max_nights` is advisory (app display/booking) and does not narrow
  alerts. A fixed watch (the default, `date_mode='fixed'`) is unchanged. The
  match is provider-agnostic (`monitor.apply_flex_window`, after
  `extract_relevant`); see PLAN.md "Phase 16" for the column/DTO contract.
- **Watch lifecycle:** the backend expires watches whose end date has
  passed (`status='expired'`) and errors watches with malformed campground
  ids (`status='error'`, once), whose `provider_ref` their provider cannot
  poll with (also once — a watch that can never poll must not look healthy
  to its user forever, though a *pool-wide* unpollable condition crosses the
  systemic threshold and is left `monitoring` for an operator instead: nobody
  should have to recreate a watch over a client-wide bad key name), whose
  campground has 404ed for 3 consecutive cycles (typo or delisted
  campground; any successful poll resets the count), or whose own database
  writes are permanently rejected (see Failure containment) — the app never
  has to clean these up.

  `status='error'` is **terminal**: an errored watch is absent from the only
  query the cycle runs, and no code path writes the status back. Two things
  follow, both of them deliberate. Every such write also records a
  machine-readable `watches.error_reason` — `invalid_campground_id`,
  `unreadable_provider_ref`, `campground_not_found`, `watch_write_rejected`
  (`monitor.ERROR_REASONS`) — so terminal and fixable can be told apart at all:
  only `campground_not_found` is genuinely permanent, `watch_write_rejected`
  recovers the moment its migration is applied, and the two remaining ones need a
  data or client fix. And every cycle reports how many watches are sitting in
  `status='error'` (see Errors and run status), because a pool that quietly
  shrank must not look like a healthy one. Re-arming an errored watch is
  deliberately **not** automatic: a blanket retry would re-poll known-dead
  watches forever and undo what the terminal design buys.
- **Failure containment:** a failure that belongs to one watch — a rejected
  write, a malformed row — is caught, recorded, and skipped; it never
  aborts the cycle. The other watches are still polled and alerted, and the
  run still writes its `run_summaries` row and prunes. Batched watch writes
  (`id=in.(…)`) are split into chunks of ≤150 ids and each chunk is attempted as
  one write (a pool within one chunk is still the single write the budget
  assumes). Only a *permanently* rejected batch (a PostgREST 4xx other than 429:
  missing column, constraint violation) falls back to one write per id, so a
  single unwritable row cannot silently drop everyone else's update. A
  transient batch failure (429, 5xx, timeout, transport error) is never
  fanned out: it errors nothing, so isolating it buys nothing, while dozens
  of sequential 30-second PATCHes against a struggling Supabase would blow
  the workflow's 15-minute timeout and kill the run before its summary row
  and pruning. For the same reason batches larger than `PER_ID_FALLBACK_MAX`
  (50) skip the fan-out entirely — as does a rejection a caller knows is
  about the *payload* rather than any one row, which today is the missing
  `error_reason` column alone (see Database migrations). The fan-out itself
  is capped twice: it may spend
  `PER_ID_FALLBACK_BUDGET_SECONDS` (100 s) of wall clock in total
  across a cycle — every per-id write is charged when it returns, so however
  many batches fall back, the cycle's whole fan-out spend is that allowance
  plus the one PATCH still in flight when it runs out (30 s) — and it may
  never run past `FANOUT_DEADLINE_SECONDS` (600 s)
  measured from **process start** — so the preflight and the up-to-20 s start
  jitter count against it instead of stacking on top of it. The arithmetic
  closes against the workflow's `timeout-minutes: 15` (900 s): 120 s for
  checkout / setup-python / pip, 600 s to the fan-out deadline, 180 s of
  shutdown reserve for one in-flight PATCH (30 s) plus the summary insert and
  both prunes. Since the jitter came down from 240 s, even a worst case — full
  jitter plus a fully spent 480 s poll budget — reaches the fan-out with ~100 s
  of the 600 left. A run slow enough to arrive past the deadline anyway still
  gets no fan-out at all, which is the right trade: reaching the summary row
  and the pruning matters more than isolating one row.

  A watch moves to `status='error'` only when the failure was **pinned to
  that row** — a single-watch write to `watches`, a per-id fallback write, or
  the delta/`state_hash` write for that one watch — and looks permanent. A
  batch failure nobody attributed to a specific row is recorded and counted
  but never errors a watch: users must not have to recreate a watch over a
  failure that was never shown to be theirs. The same holds for the
  table-scoped work done while serving a watch — the `sent_alerts` cooldown
  lookup and dedup upsert, and the APNs send: a `sent_alerts` schema drift or
  an APNs client error is recorded and counted against that watch but never
  errors it, because it is not evidence that this user's watch row is broken.
  The watch was still polled, so it is also still stamped `last_checked_at`
  rather than left looking unchecked to its user.
  A transient failure likewise leaves the watch `monitoring` to retry next
  cycle. Strike-count (`consecutive_not_found`) writes are pure bookkeeping: a
  rejected one is recorded, but the watch is still delta-checked, alerted, and
  stamped `last_checked_at`.
- **Alert delivery:** a user can register more than one device (the
  `device_tokens` PK is the composite `(user_id, apns_token)` since migration
  0012), so an alert **fans out to every one of that user's tokens** in a single
  send. It is still one alert *event* — deduped once by `(watch_id, site_id,
  date)` — simply delivered to N devices. The per-user outcome is aggregated:
  **delivered** if at least one token returned 200 (the caller marks
  `sent_alerts` and advances the `state_hash`; a device that missed it is
  covered by APNs store-and-forward); otherwise **retryable** if any token was
  5xx/429/transport (keep the old hash, retry the whole set next cycle);
  otherwise **permanent**. An APNs 5xx/429 or transient transport error keeps
  the watch's old `state_hash` so the alert is retried next cycle; a 410
  (Unregistered), or a 400 naming the token itself dead (`BadDeviceToken` /
  `Unregistered`), means that device token is dead and **only its own row** is
  deleted — keyed on the full `(user_id, apns_token)`, so one dead device never
  wipes out the same user's other, live devices and no other user's token is
  touched; the app re-registers a fresh token on next launch.
  The rest of the 4xx space is split by *who can fix it*:
  - A **per-device** rejection (400 `BadDeviceToken` [now pruned] /
    `DeviceTokenNotForTopic`, a reason code we don't enumerate, a missing token
    row, or a device token so malformed the push URL can't be built) is one
    device's problem, not the cycle's. It is given up on (no retry, the hash
    still advances), recorded and surfaced like any other failure, but left
    **out of the rate** that decides the run's exit status — a single dead
    token can't turn the schedule red.
  - A **pool-wide provider/config** fault (403 `ExpiredProviderToken`/
    `InvalidProviderToken`/`MissingProviderToken`, 400 `BadTopic`/
    `TopicDisallowed` — an expired or wrong signing key, or a wrong bundle id)
    would silence *every* push until an operator rotates a credential. It keeps
    the old `state_hash` so the alert retries once fixed, and it **counts**
    toward the rate, so a signing-key or bundle-id outage exits non-zero.

  On top of the per-reason split, a **pool-wide backstop** turns the run
  systemic whenever outright rejections wipe out nearly every served push
  (`APNS_WIPEOUT_RATE`/`APNS_WIPEOUT_FLOOR` in `scripts/monitor.py`), so a
  reason code we did not enumerate still can't yield a silent green outage while
  a handful of genuinely dead tokens in a healthy pool stays green. An APNs
  outage (5xx/429/transport) still counts and still turns a broad enough
  failure red.

  If a push *is* delivered but its `sent_alerts` dedup row is rejected, the
  watch's `state_hash` is written anyway. Nothing would otherwise stop the
  identical push going out again every cycle until the drift is fixed: the
  dedup rows that would suppress it are exactly the ones that failed to write.
  The trade is an occasional missed re-alert instead of a repeating push.
- **Errored-watch census:** every cycle reads how many watches are in
  `status='error'` with a trip that has not passed yet (one extra `select`, no
  writes, so the write budget is untouched) and reports the count on the
  world-readable `run_summaries.errors` row, with a per-reason breakdown in the
  operator-only annotation. A watch that
  has been errored is *absent*, not failing, so without this a run serving one
  watch of four looked exactly like a clean run serving all four — three of five
  watches once went unmonitored for two days across a wall of green runs. The
  count is a standing fact, not this cycle's verdict: it never changes the exit
  code. The breakdown stays operator-only and bucketed (an unknown value counts
  as `other`, a row errored before `0004` as `unrecorded`) because `error_reason`
  sits on a row its owner can write. The count reports the population *entering*
  the cycle; watches errored during it are reported by the lifecycle passes and
  join the census next cycle. Past-date errored watches are left out on purpose:
  nothing can be done about a trip that has already happened, and a warning that
  fires every cycle forever is worth what no warning is worth. (Having the expiry
  pass move those rows to `expired` would be tidier, but it costs writes and mixes
  two concerns — a possible follow-up, not an oversight.)
- **Errors and run status:** polling and alert errors (provider request
  failures, unrecognized responses, APNs delivery problems) and contained
  per-watch failures are all recorded in the `run_summaries.errors` column
  (a count plus a bounded sample of messages) and surfaced as a
  `::warning::` annotation. Each recorded message is one capped line carrying
  the server's own reason — for a rejected write, the PostgREST body naming
  the column or constraint at fault, rather than the generic HTTP status line
  and request URL, so a schema drift says which column is missing.

  Two renderings of the same failures go to two audiences. `run_summaries` is
  **world-readable** (its RLS policy is `USING (true)`, so every app client can
  read it), so the persisted `errors` string is **sanitized**: watch UUIDs
  become per-run ordinals (`watch #1`) and the PostgREST `details`/`hint` — a
  constraint violation's `details` echoes the offending key values — are
  dropped, keeping the status code and the column/constraint name. A failure
  with no response to read (a transport error, a malformed row's `ValueError`)
  is persisted as its exception type alone, since its message is whatever the
  raiser put there and can quote the value that upset it. A user-supplied value
  is never republished either: a malformed `campground_id` is persisted as a
  count of the ids and watches it errored, and the values themselves go only to
  the operator log. This holds for every
  failure path, undelivered APNs pushes included: the client hands back the
  exception and never names the watch, so the row it belongs to is decided —
  and ordinalized — here. The operator-only GitHub Actions
  `::warning::`/`::error::` annotation keeps the full detail (watch UUIDs and
  the complete reason), each failure appearing once. Neither ever carries the
  service-role key. The persisted `(isolated)`/`(systemic)` verdict label is
  chosen after folding in every failure known before the row is written
  (including retention-prune failures, which now run before the summary
  INSERT), so it always matches the run's exit code — and a red run whose only
  failure belonged to no watch persists that label and the failure's sanitized
  text rather than a NULL that would read like a clean cycle. (A summary INSERT
  that itself fails is the one unrepresentable case: there is then no row.)

  The **exit status is decided by error rate**, so
  a broken watch does not cry wolf but broad breakage cannot hide:
  - *isolated* — the run exits 0 and the schedule stays **green**, because
    the healthy watches were served;
  - *systemic* — the run prints an `::error::` annotation, exits non-zero
    and turns the schedule **red**. Systemic means more than
    `SYSTEMIC_ERROR_RATE` (25%) of the watches the cycle actually **served**
    failed **and** at least `SYSTEMIC_ERROR_FLOOR` (2) of them did — so 1 of
    2 stays green while 2 of 2 goes red — or that a failure belonging to no
    watch (the `run_summaries` INSERT, retention pruning, being unable to
    write `status='error'`, a provider raising out of a poll unit its watches
    share, a pool-wide unpollable condition — see [Providers](#providers) — or
    the poll budget running out mid-plan, see Time budget) occurred. Both constants live at the top of
    `scripts/monitor.py` and are the tuning knobs.

    The numerator counts only the failures that say something about this
    cycle's health, so a **per-device** APNs rejection — a dead device token no
    operator can fix — is reported in the row and the annotation but not
    counted; the tally says how many such failures it left out. A **pool-wide**
    provider/config APNs fault *is* counted (see "Alert delivery"), and the
    pool-wide backstop makes the run systemic regardless of per-reason rating
    when outright rejections wipe out nearly every served push.

    The served set is the rate's denominator *and* the scope of its
    numerator, so the ratio can never exceed 1. It excludes watches that
    expired this cycle, that were errored for an invalid or
    persistently-404ing campground, that their provider can never poll
    (errored, or left `monitoring` when that condition is pool-wide — see
    Watch lifecycle), that name a provider this build does not
    serve (see [Providers](#providers)), that are wholly beyond the 12-month
    poll horizon (nothing to poll for them yet), and that the poll time budget
    never reached. A cycle that failed every watch it served goes red no
    matter how much of the pool left — or never entered — for unrelated
    reasons.

  Systemic runs deliberately leave the watch pool untouched: broad breakage
  is the operator's to fix, not something users should have to recreate
  their watches over. Failures before any of that — an unreachable Supabase,
  a malformed `APNS_P8_KEY`, a missing secret — still exit non-zero.
- **Retention:** `sent_alerts`, `run_summaries`, and `alert_history` rows older
  than 30 days are pruned every run. (`alert_history` is otherwise unbounded — one
  row per delivered push — so the app's Alert History shows the last 30 days.)
- **Per-user watch cap:** a database trigger (`enforce_watch_cap`, in
  `schema.sql` / migration `0008`) caps each user at **20 active watches**
  (`status IN ('monitoring','paused')`; terminal `expired`/`error` rows never
  count). It fires only when a row *enters* the active set, so the monitor's own
  writes are never blocked. A breach is rejected with HTTP 400 and the hint
  `WATCH_CAP_EXCEEDED` so the app can show a friendly message. It is a backend
  abuse guardrail against a runaway client, not the primary cost control (the
  cross-user dedup, chunked write, and scoped read are).
- **Keep-alive:** GitHub disables cron workflows after 60 days without repo
  activity; `keepalive.yml` commits a timestamp monthly to prevent that.

## Scheduling: fixing the `schedule` drift (Phase 17)

The poll runs on GitHub Actions, but GitHub's `schedule` cron is best-effort and
**drifts 1–3 hours** in practice (measured 2026-07-31), so alerts lag openings.
Phase 17 (Path A trigger code in-repo as `trigger_lambda.py`; the AWS
resources that run it are now live) fixes this by moving the **trigger**
to AWS while the **poll stays on GitHub Actions** — the one egress path
GoingToCamp's WAF accepts:

- An **AWS EventBridge Scheduler** fires on the exact wall clock and calls
  `monitor.yml`'s `workflow_dispatch` via a ~15-line Lambda (reads a
  single-repo, `Actions: Read and write` fine-grained PAT from an SSM
  SecureString; POSTs the dispatch; logs the HTTP status). AWS never talks to
  GoingToCamp.
- The GitHub `schedule` cron is now **dormant** (commented out in `monitor.yml`,
  2026-08-10) because running both duplicated ~a quarter of polls and GitHub's
  cron dropped runs under load — EventBridge is the sole trigger. Uncomment the
  `schedule:` block to restore it as an offset backstop. Silent-miss coverage now
  rests on the AWS `Invocations` heartbeat alarm (fires on **absence** of
  invocations) plus a `run_summaries.ran_at` freshness check.
- The repo has been **flipped public** (2026-08-01, after the backend was complete
  and a full-history secret scan came back clean), which took Actions minutes to $0.

See PLAN.md "Phase 17 (campassist-monitor backend) — AWS-triggered GitHub
Actions" for the full plan, cost table, token scope, and cutover/rollback.

### Phase A — stand up the trigger (operator steps)

The trigger's code lives in this repo at `trigger_lambda.py` (repo root —
deliberately *not* inside `scripts/`, so it shares nothing with the poll
pipeline; handler `trigger_lambda.handler`).
It is stdlib-only — the HTTP POST goes through `urllib` and `boto3` comes from
the Lambda runtime — so it has **no bundled dependency** and deploys as a single
file. The AWS resources below are the operator's to create; the repo half is
just this code and its tests.

1. **Mint the fine-grained PAT.** GitHub → Settings → Developer settings →
   Fine-grained tokens: **only** repository `wameson/campassist-monitor`,
   repository permission **Actions: Read and write**, **90-day** expiry. Not a
   classic PAT. *(If the dispatch later returns 403, add **Contents: Read** —
   the endpoint may need it to resolve the ref; start without it.)*
2. **Store it in SSM Parameter Store** as a **`SecureString`** (free AWS-managed
   KMS key) named **`/campassist-monitor/github-dispatch-pat`** — the name the
   Lambda reads by default (override with the `GITHUB_PAT_SSM_PARAM` env var if
   you use a different one). Rotation = update this SecureString in place; the
   Lambda picks up the new value on its next invoke, no redeploy.
3. **Create the Lambda** (`python3.12`, handler `trigger_lambda.handler`): paste
   `trigger_lambda.py` into the console inline editor, or `zip trigger.zip
   trigger_lambda.py` and upload — no wheel build, no packaging step. Grant
   its execution role only **`ssm:GetParameter`** on that one parameter (plus
   `kms:Decrypt` on the AWS-managed key) and CloudWatch Logs. Point the
   `Invocations`-heartbeat CloudWatch alarm and the SNS email at it (confirm the
   subscription). *(These reuse the re-scoped role/alarm/SNS the captain already
   built — see PLAN.md.)*
4. **Invoke it once manually.** It should return `{"ok": true, "status": 204}`,
   log the `204`, and a `monitor.yml` run should appear. This exercises the PAT,
   the SSM read, and the dispatch end-to-end with no schedule attached. On
   anything but 204 the Lambda **raises** — the invocation is marked failed and
   the alarm fires, by design (the trigger's failure mode is *silent missing*, so
   every attempt is logged and every non-204 is loud).

Phase B (attach the EventBridge schedule) and beyond are operator steps in
PLAN.md — no repo change. (The GitHub `schedule` cron that Phase B ran alongside
has since been made dormant — see above.)

**Two earlier plans that tried to move the poll *itself* off Actions were both
refused by the WAF** — AWS Lambda (8×403) and Azure Container Apps Jobs (probe
403). That is why the Phase 17 design triggers from AWS but keeps polling on
GitHub Actions: it is the only egress the WAF accepts. The dead artifacts from
the AWS-Lambda poll attempt — `lambda_function.py` and its tests, the
`make lambda-zip` `Makefile`, and `deploy.yml` — have now been removed. The
trigger Lambda (`trigger_lambda.py`, above) is a separate, unrelated stdlib-only
function that fires the workflow and never polls anything.

## Secret scanning

This repo is public (Phase 17, Phase D, since 2026-08-01), so **every commit ever
made** is readable — not just the current tree, and a secret committed once and
later deleted stays readable in history forever. That makes rotation, not removal,
the only real remedy, and it governs **every future commit**. Two layers guard
against a secret ever landing, and they are **complementary**:

- **In-CI scanning (this repo).** `.github/workflows/secret-scan.yml` runs
  [gitleaks](https://github.com/gitleaks/gitleaks) (pinned, checksum-verified) on
  **every pull request** and **fails the check** on any finding — it does not
  merely warn. It scans the commits the PR introduces (`base..head`) with the
  full default ruleset (hundreds of rules + entropy detection), so it catches a
  far broader set than a handful of hand-written patterns and it composes with
  the no-mistakes gate. The scan finishes in well under a minute (free now that the
  repo is public; it cost a few cents of Actions time during the private window). Exemptions
  live in [`.gitleaks.toml`](.gitleaks.toml) as a **narrow, explicit allowlist**
  (one exact fake-token string, not a disabled rule or an exempted file), so a
  reviewer can see exactly what is ignored and why, and a real secret added next
  to the fake one is still caught.
- **GitHub push protection (a Phase D setting to enable — free on public repos).**
  GitHub offers **secret scanning and push protection at no cost on public
  repositories**. Push protection **blocks a known credential format at
  `git push` time — before it ever reaches history** — which is strictly better
  than catching it afterwards, when the only real remedy is to *rotate* the
  credential (deleting the line or rewriting history does not truly remove it:
  GitHub retains PR diffs after force-pushes). This is a repository setting, not
  code: after the public flip, enable **Settings → Code security → Secret
  scanning** and **Push protection**.

The division of labor: GitHub's push protection catches **known provider
formats at the door**; the CI scanner catches a **broader ruleset and covers
history / arbitrary changes** in every PR. The public flip was gated on a **one-time
full-history scan** (`gitleaks git --log-opts=--all`) coming back clean — it did
(108 commits, 2026-08-01) before the repo was published.

## Fallback: self-hosted runner

If GitHub's own runner egress is ever refused by GoingToCamp (it is accepted
today, which is why the poll stays there), register any always-on home machine as a
self-hosted runner — free minutes (unlimited on a private repo) and a
residential IP:

1. Repo → **Settings → Actions → Runners → New self-hosted runner**, follow the
   3-command install on the machine (macOS/Linux/Windows, ~10 minutes).
2. Run it as a service so it survives reboots (`./svc.sh install && ./svc.sh start`).
3. In `.github/workflows/monitor.yml`, change `runs-on: ubuntu-latest` to
   `runs-on: self-hosted`.

This is a fallback only. The plan of record is the AWS trigger plus the public-repo
flip (Phase 17); the flip is done (2026-08-01) and is what took running cost to $0.

## Licence

Licensed under the [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)
(full text in [`LICENSE`](LICENSE); licensor: **wameson**). Non-commercial use —
personal, hobby, research, and other non-commercial purposes — is permitted; any
commercial use requires permission first.

This is **not** an OSI-approved open-source licence: restricting commercial use puts
it outside that definition, so there is deliberately no open-source badge.

For commercial licensing enquiries, open an issue at
[github.com/wameson/campassist-monitor/issues](https://github.com/wameson/campassist-monitor/issues)
(no personal email is published here, since this is a public repository).
