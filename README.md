# campassist-monitor

CampAssist backend: centralized campsite availability monitor for recreation.gov
and GoingToCamp (Washington State Parks).
A GitHub Actions cron job (every 30 minutes, in this **private** repo) polls all
users' watches through the conformer each one's `provider` names — deduplicated
to one request per unique poll unit per cycle (see [Providers](#providers)) —
detects new openings via state hashes, and sends APNs push notifications with a
direct booking link. Supabase (free tier) is the shared database; there is no
server.

See the CampAssist `PLAN.md` (Phase 1) for the full design: write budget,
anti-blocking rules, and the recreation.gov API contract.

## Layout

| Path | Purpose |
|---|---|
| `scripts/monitor.py` | One monitoring cycle, provider-neutral: jittered polling, dedupe, delta detection, alert cooldown, watch lifecycle (expiry + erroring), per-watch failure containment + threshold-gated exit status, retention pruning, run summary |
| `scripts/providers/` | One conformer per `watches.provider` value — the poll/parse/normalize/booking-link strategy (see [Providers](#providers)) |
| `scripts/apns.py` | APNs HTTP/2 client (ES256 JWT auth, sandbox/production routing, 410 token cleanup) |
| `scripts/db.py` | Thin Supabase PostgREST client (service-role key) |
| `scripts/preflight.py` | Read-only schema-drift guard run before each cycle (see [Database migrations](#database-migrations)) |
| `scripts/common.py` | Primitives shared by the cycle and its providers (date coercion, the error-line cap) |
| `supabase/schema.sql` | Fresh-install schema + RLS policies — paste into the Supabase SQL editor for a **new** DB |
| `supabase/migrations/` | Ordered, idempotent SQL applied **by hand** to keep **existing** DBs in sync (see [Database migrations](#database-migrations)) |
| `.github/workflows/monitor.yml` | 30-minute cron + manual `workflow_dispatch` |
| `.github/workflows/keepalive.yml` | Monthly bot commit so GitHub never auto-disables the scheduled workflow (60-day rule) |
| `.github/workflows/ci.yml` | pytest on every PR and push to `main` (ubuntu) |
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

Two conformers ship today:

| `provider` | Module | Poll unit | `provider_ref` |
|---|---|---|---|
| `recreation_gov` | `scripts/providers/recreation_gov.py` | one (campground, month) request | unused — `campground_id` is the whole identity |
| `going_to_camp` | `scripts/providers/going_to_camp.py` | one (park, stay) — the root map plus each child map it names, 2–5 paced GETs inside a single `poll`, plus one park-catalog GET (see below) | `{"resource_location_id": …, "map_id": …}`, both required (a watch without them is errored once, see Watch lifecycle) |

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

A watch whose `provider` this build has no conformer for is left untouched —
unpolled, still `monitoring`, outside the systemic error rate — and reported as
a count in the run summary, so an older monitor cannot mis-serve a row a newer
client wrote.

Whatever a provider's own identifiers are, `watches.campground_id` stays the
watch's campground identity for the cycle's 404-strike lifecycle and
`campgrounds_polled` telemetry, and must match `[A-Za-z0-9_-]+` — a watch whose
id has any other character is errored before it is ever polled. A
`going_to_camp` row therefore needs a stable id in that alphabet (the tests use
`gtc_<resourceLocationId>`); the backend never parses it.

To add a provider: write the conformer in its own module under
`scripts/providers/`, then register it in `scripts/providers/__init__.py`.
**Its request host must be a constant in that module.** `watches.provider_ref`
is client-writable and carries identifiers only — never derive a host, URL, or
path from it (SSRF).

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
  fresh installs get the change), add a new `supabase/migrations/NNNN_<desc>.sql`
  with the next number, using idempotent DDL (so existing DBs get the same change),
  **and** add the column to the `REQUIRED` manifest in `scripts/preflight.py` (below).
  Keep all three in sync — a CI test fails when the manifest and `schema.sql` disagree.
  **Order matters:** apply the migration by hand in the SQL editor **first**, and only
  then merge the manifest entry. A new monitor-written or read-required column is
  classified `halt`, so merging it ahead of the apply would deliberately stop every
  cycle until an operator got to the SQL editor.
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
- **The monitor checks before it runs.** Each run starts with a read-only schema
  preflight (`scripts/preflight.py`): four `GET`s with `limit=0`, zero writes, no row
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
- **Poll horizon:** every provider clamps what it requests for a watch to
  today through today + 12 months; "today" uses a fixed UTC-8 offset so
  same-night openings at US campgrounds stay alertable during US evening
  hours after UTC midnight. A watch entirely beyond the horizon is polled
  once the horizon reaches it, and one straddling it is served — hashed and
  alerted on — for its in-horizon nights alone.
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
  (`id=in.(…)`) are attempted as one write, as the write budget assumes.
  Only a *permanently* rejected batch (a PostgREST 4xx other than 429:
  missing column, constraint violation) falls back to one write per id, so a
  single unwritable row cannot silently drop everyone else's update. A
  transient batch failure (429, 5xx, timeout, transport error) is never
  fanned out: it errors nothing, so isolating it buys nothing, while dozens
  of sequential 30-second PATCHes against a struggling Supabase would blow
  the workflow's 15-minute timeout and kill the run before its summary row
  and pruning. For the same reason batches larger than `PER_ID_FALLBACK_MAX`
  (50) skip the fan-out entirely, and the fan-out itself is capped twice: it
  may spend `PER_ID_FALLBACK_BUDGET_SECONDS` (100 s) of wall clock in total
  across a cycle — every per-id write is charged when it returns, so however
  many batches fall back, the cycle's whole fan-out spend is that allowance
  plus the one PATCH still in flight when it runs out (30 s) — and it may
  never run past `FANOUT_DEADLINE_SECONDS` (600 s)
  measured from **process start** — so the up-to-240 s start jitter counts
  against it instead of stacking on top of it. The arithmetic closes against
  the workflow's `timeout-minutes: 15` (900 s): 120 s for checkout /
  setup-python / pip, 600 s to the fan-out deadline, 180 s of shutdown
  reserve for one in-flight PATCH (30 s) plus the summary insert and both
  prunes. A worst-case run — full jitter and a full poll budget — therefore
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
- **Alert delivery:** an APNs 5xx/429 or transient transport error keeps
  the watch's old `state_hash` so the alert is retried next cycle; a 410
  means the device token is dead and its row is deleted. The rest of the 4xx
  space is split by *who can fix it*:
  - A **per-device** rejection (400 `BadDeviceToken`/`DeviceTokenNotForTopic`,
    a reason code we don't enumerate, a missing token row, or a device token so
    malformed the push URL can't be built) is one device's problem, not the
    cycle's. It is given up on (no retry, the hash still advances), recorded and
    surfaced like any other failure, but left **out of the rate** that decides
    the run's exit status — a single dead token can't turn the schedule red.
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
  writes, so the write budget is
  untouched) and reports the count on the world-readable `run_summaries.errors`
  row, with a per-reason breakdown in the operator-only annotation. A watch that
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
    share, or a pool-wide unpollable condition — see [Providers](#providers))
    occurred. Both constants live at the top of
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
