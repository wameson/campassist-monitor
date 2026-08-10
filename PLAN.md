# CampAssist Monitor — Backend Plan

**This is the backend plan.** It covers the Supabase schema, the GitHub Actions monitor
workflow, the poll/alert pipeline, the provider seam, and the backend implementation phases.
It is the authoritative sub-plan for everything in this repo.

**The iOS/product plan lives in the `camp-assist` repo's `PLAN.md`** — app architecture,
screens, the Add-Watch wizard, design system, and the iOS halves of the shared phases.
Anything that crosses both repos (a schema column, a `campground_id` format, a provider
contract) is specified here and referenced there.

`README.md` in this repo is the operator-facing summary of what has shipped; `AGENTS.md`
(aliased `CLAUDE.md`) holds the sharp-edge notes. This file is the design and the sequencing.

---

## Context

recreation.gov and GoingToCamp both expose unauthenticated JSON availability APIs. A single
centralized cron polls them for **all** users — deduplicated to one request per unique poll
unit per cycle — detects new openings via state hashes, and sends an APNs push with a direct
booking link. There is no server: GitHub Actions + Supabase + APNs, all free tier.

**Why centralized:** 1,000 phones polling independently look like 1,000 clients and get
throttled. Centralized, a provider sees one polite client making ~20–200 jittered requests
per cycle.

---

## Architecture

```
campassist-monitor (GitHub, PUBLIC since 2026-08-01)
  .github/workflows/monitor.yml   workflow_dispatch (EventBridge-driven); schedule cron dormant, timeout-minutes: 15
    ├── preflight: read-only schema-drift probe (before the jitter)
    ├── start jitter: sleep rand(0–20s)
    ├── read status=eq.monitoring watches + errored-watch census
    ├── expire past-end_date watches
    ├── plan PollUnits (provider, PollKey) — deduped cross-user, per provider
    ├── poll via each watch's Provider conformer — paced, backed off, time-budgeted
    ├── per watch: extract_relevant → state_hash → delta → alert dedup/cooldown → APNs
    ├── lifecycle writes (error/strike), batched last_checked_at PATCH
    └── run_summaries row + 30-day retention prune
  .github/workflows/keepalive.yml  monthly commit (GitHub disables crons after 60 idle days)
  .github/workflows/ci.yml         pytest on PR + push to main (ubuntu)
  .github/workflows/secret-scan.yml  gitleaks on every PR — fails on any finding (Phase D gate)
        ▲ ▼
  Supabase (free): watches · device_tokens · sent_alerts · run_summaries
        ▲ ▼
  CampAssist iOS app (camp-assist repo) — anonymous sign-in, RLS-scoped
```

> **Phase 17 changes the *trigger*, not this topology.** The poll keeps running on GitHub
> Actions — the one egress path proven to reach GoingToCamp — with its **primary trigger now an
> AWS EventBridge Scheduler that fires on the exact wall clock and calls `monitor.yml`'s
> `workflow_dispatch`** (captain, 2026-07-31), fixing the 1–3 h drift of GitHub's best-effort
> `schedule` cron. AWS never talks to GoingToCamp; the diagram above — the poll, Supabase, APNs,
> the six secrets in GitHub Actions Secrets — is unchanged. **Status: live.** The AWS trigger fires
> `workflow_dispatch` on the exact wall clock (Phase B done — the run history shows dispatches at
> exactly `:00`/`:30`). The GitHub `schedule` cron that ran alongside it as an offset backstop has
> since been made **dormant** (commented out in `monitor.yml`, 2026-08-10): running both duplicated
> ~a quarter of polls and GitHub's cron dropped runs under load, so EventBridge is now the sole
> trigger. Re-enabling is a one-line uncomment; Phase C (thinning the backstop) is thus moot.
> Two earlier plans that tried to move the *poll itself* off Actions (AWS Lambda, then Azure
> Container Apps Jobs) were both refused by GoingToCamp's Azure Front Door WAF; see Phase 17
> "Approaches tried and rejected." A later step (Phase D) flipped the repo **public on 2026-08-01**,
> taking Actions minutes to $0.

> **Standing rule since the public flip (2026-08-01).** The repo's **entire git history** is
> public, not just its current state — a secret that was *ever* committed stays readable in old
> commits. Any secret that ever lands in a commit must be **rotated, not merely removed**: removal
> from history does nothing once a commit has been fetched, cloned, or indexed. This governs
> **every future commit**. `.github/workflows/secret-scan.yml` (gitleaks, pinned/checksum-verified)
> gates every PR on its `base..head` diff and fails on any finding (README "Secret scanning").

### GitHub Actions minutes — now $0 (public repo)

> **The repo went public on 2026-08-01 (Phase 17, Phase D), so standard-runner minutes are free
> at any cadence — this is no longer a cost constraint.** The poll still runs on GitHub Actions
> (Phase 17 moves only the *trigger*), but the job's minutes cost nothing. What survives the flip
> is the in-code cycle **time** budget (`CYCLE_TIME_BUDGET_SECONDS`, 480 s), a coverage/correctness
> limit racing only GitHub's own `timeout-minutes: 15` (900 s) — never a billing one.

**Historical, kept as an accepted-cost record.** During the private-repo window the monitor ran
against the 2,000 free min/month tier and overran it, an **accepted ≈$7/mo interim cost** at
30-min cadence — a real cost the captain accepted, erased by the public flip. Actions bills **per
job, rounded up to the whole minute**; a no-change cycle measured ~94–125 s (n=15, ~109 s mean),
so most runs billed 2 min and ~1 in 4 billed 3 → **2.2 billed min/run**, ~3,214 min/mo over 1,461
runs, ~1,214 past the free tier. (15 min would have been ≈$27/mo — see Phase 17 cost table.)

**The jitter fix that averted a far worse bill stays on record as durable evidence.** Until
2026-07-28 the start jitter was 240 s: a 226-run measurement found the median job at 141 s with
~130 s of that billed `time.sleep()` (92% of the `Run monitor` step) against ~10 s of real work,
which would have put an honest `*/30` at **~4,154 min/mo**. At a GitHub Free account's default $0
spending limit that is not a bill — it is every private-repo Action stopping until the next cycle.
Cutting the jitter to 20 s (`START_JITTER_MAX_SECONDS`) removed that 2.9× blowup; the residual is
real pacing plus setup, not sleep. **`START_JITTER_MAX_SECONDS` stays at 20 s regardless** — the
226-run finding is durable evidence about the per-request pacing floor, and Phase 17's exact-
wall-clock trigger *restores* the jitter's desync rationale (a `schedule` cron already spread
delivery; an exact scheduler does not).

**Why 30 min and not 15** (captain): now that minutes are $0, cadence is a latency/politeness
call, not a money one, and **30 min stands** — cycle time, not money, is the ceiling (§ Cadence).
15 min would double request volume against a WAF already refusing one of our egress paths without
raising per-cycle capacity. The scheduler-drift half of the old cadence question (GitHub's
`schedule` drifting **1–3 h**, measured) is Phase 17's to fix, not cadence's. A self-hosted
**residential** runner survives only as a fallback if GitHub's own egress is ever refused — not
the plan.

### Stack

| Layer | Choice | Why |
|---|---|---|
| Scheduling | **Trigger (live, Phase 17):** an **AWS EventBridge Scheduler** → `workflow_dispatch` fires on the exact wall clock — now the **sole** trigger. The Actions `schedule` cron `*/30` that ran as an offset backstop is **dormant** (commented out, 2026-08-10; one-line uncomment to restore). **Execution:** GitHub Actions, unchanged | GitHub's `schedule` is best-effort; an exact scheduler fires on the minute while the poll stays on the only accepted egress |
| Language | Python 3.12 | available on Actions and in a `python:3.12-slim` container, no build step |
| HTTP | `httpx[http2]` | HTTP/2 is required for APNs |
| APNs auth | `PyJWT` + `cryptography` | ES256 JWT signed with the `.p8` key |
| DB | Supabase REST, service-role key | free tier; bypasses RLS server-side |
| Secrets | GitHub Actions Secrets (unchanged) | `.p8` key + Supabase service key; the six poll secrets stay on Actions. Phase 17's trigger adds one AWS-side secret — a GitHub PAT in an SSM SecureString — which never touches the poll |

**Runtime dependencies are capped at those three.** Anything needing a fourth needs a
decision, not a `pip install` — the cap is what keeps the monitor's failure surface small and
is why the preflight manifest is a dict rather than a SQL parser.

---

## Supabase schema

`supabase/schema.sql` is the authoritative DDL and the **fresh-install bootstrap only**. The
shape, abridged:

| Table | Columns | Notes |
|---|---|---|
| `watches` | `id`, `user_id`, `provider`, `provider_ref`, `campground_id`, `campground_name`, `campground_state`, `site_ids`, `include_ada_only`, `start_date`, `end_date`, `date_mode`, `flex_min_nights`, `flex_max_nights`, `status`, `error_reason`, `state_hash`, `consecutive_not_found`, `created_at`, `last_checked_at`, `last_found_at` | `status ∈ monitoring/paused/expired/error`; `site_ids` empty = any site; `date_mode ∈ fixed/flexible` (Phase 16) |
| `device_tokens` | `user_id` (PK), `apns_token`, `environment`, `updated_at` | `environment ∈ production/sandbox` — per-token APNs host routing |
| `sent_alerts` | `id`, `watch_id`, `site_id`, `date`, `sent_at` | `UNIQUE(watch_id, site_id, date)` — the dedup key; **never read by the app** |
| `alert_history` | `id`, `watch_id`, `campground_name`, `start_date`, `end_date`, `site_count`, `delivered_at` | **the app's server-truth Alert History** — one row per DELIVERED push; RLS scoped to the owning user like `sent_alerts` |
| `run_summaries` | `id`, `ran_at`, `watches_checked`, `campgrounds_polled`, `alerts_sent`, `duration_ms`, `errors` | **world-readable** (`USING (true)`) — see Rendering channels |

RLS is on for all five; the app reads its own rows via `auth.uid()`, the backend writes with
the service-role key.

#### `alert_history` — the app-facing contract (Issue 2, server-truth Alert History)

This table is the **frozen contract** the follow-on app task (`alert-history-app-render`)
builds against. The "Site Found" badge is `watches.last_found_at`, set only on an APNs
`DELIVERED`; the app's old Alert History was local `NotificationRecord`s written only when the
device foregrounded/tapped a push, so a delivered-but-not-tapped alert lit the badge yet left
history empty. `alert_history` closes that: the monitor writes one row **in the same step and
on the same condition** it sets `last_found_at` (`monitor.run`, the `DELIVERED` block), so
badge and history can never disagree.

| Column | Type | Meaning |
|---|---|---|
| `id` | `UUID` | PK |
| `watch_id` | `UUID` | FK → `watches(id)` `ON DELETE CASCADE` |
| `campground_name` | `TEXT` | the push title's campground, verbatim from the watch |
| `start_date` | `DATE` | the watch's requested window start, **verbatim** — timezone-independent calendar day, no shift |
| `end_date` | `DATE` | the watch's requested window end, same |
| `site_count` | `INT` | openings this push announced (`len(fresh)`) — the body's "N site(s) open" |
| `delivered_at` | `TIMESTAMPTZ` | when the push was delivered (`iso_now`) |

**RLS predicate the app queries** (mirrors `sent_alerts`' `read own alerts`, `SELECT` only):

```sql
CREATE POLICY "read own alert history" ON alert_history FOR SELECT
    USING (watch_id IN (SELECT id FROM watches WHERE user_id = auth.uid()));
```

An anon user reads **only their own** delivered alerts, joined through the owning watch. The
table is never opened to all. Dates are `DATE` and stored verbatim from the watch, so the app
must treat them as timezone-independent calendar days (do **not** re-introduce the Issue 1
UTC-vs-local shift on read).

**Retention:** not pruned in this build (unlike `sent_alerts`/`run_summaries`) — history is
meant to persist for the user, and adding a per-cycle prune would spend the last slot of the
≤5-write no-change budget. Delivered-alert volume is low (one row per delivered push, deduped
by the alert cooldown), so growth is bounded in practice; a retention prune can be added later
if needed.

### Migrations

| File | Adds | Preflight class |
|---|---|---|
| `0001_watches_consecutive_not_found.sql` | `consecutive_not_found` | `HALT` (monitor writes it) |
| `0002_watches_provider.sql` | `provider`, `provider_ref` | `WARN` (read-tolerant / app-only) |
| `0003_watches_include_ada_only.sql` | `include_ada_only` | `WARN` (read via `.get`) |
| `0004_watches_error_reason.sql` | `error_reason` | `WARN` (write proves tolerance) |
| `0005_alert_history.sql` | `alert_history` table + RLS policy | `WARN` (write-only, fully contained — an unapplied migration keeps monitoring) |
| `0006_watches_flexible_dates.sql` | `date_mode`, `flex_min_nights`, `flex_max_nights` | `WARN` (read via `.get` with a `'fixed'`/None default — unmigrated DB reads every watch as fixed) |
| `0007_watches_provider_use_direct.sql` | widens the `provider` CHECK to admit `'use_direct'` (no new column) | n/a — no column, so `preflight.REQUIRED` is unchanged |

**Migrations are applied by hand in the Supabase SQL editor. CI does not run them — this is
deliberate.** There is no auto-apply anywhere: not in CI, not in the monitor, not behind a
flag. The preflight guard *detects* drift and never applies it.

**A schema change ships three things together:** the column in `supabase/schema.sql` (so
fresh installs get it), an idempotent `supabase/migrations/NNNN_<desc>.sql` (so live DBs get
it), and the column's entry in `preflight.REQUIRED` (so a missed apply is caught). A CI test
holds the manifest and `schema.sql` to each other in both directions.

**Ordering is load-bearing: apply the SQL by hand FIRST, then merge the manifest entry.**
A `HALT` entry merged ahead of the apply halts every cycle until an operator reaches the SQL
editor — the zero-monitoring window the guard exists to avoid.

**Every migration is idempotent** (`ADD COLUMN IF NOT EXISTS`, …) so re-running the whole
directory in order is always safe when you don't know which are outstanding.

---

## Schema-drift guard (`scripts/preflight.py`)

**The failure mode it closes:** reads are `select=*`, so a missing column is simply absent
from the dict and Python tolerates it. Drift is silent until a *write* names the column and
PostgREST answers `400 / 42703`. Two production incidents took exactly that shape:

| # | Date | Drift | Blast radius |
|---|---|---|---|
| 1 | 2026-07-19 | `consecutive_not_found` in `schema.sql`, not in the live DB | every `watches` PATCH 400ed — multi-day outage |
| 2 | 2026-07-25 | `provider` / `provider_ref` unapplied | **monitor stayed green**; the iOS app could not save any watch |

Incident 2 is the load-bearing lesson: the guard checks the schema **the product** depends on,
not only what the monitor would crash without.

**How it probes:** one `GET <table>?select=<all manifest columns>&limit=0` per table, four
total, before the start jitter. Zero writes, no row data crosses the wire, no new secret
(reuses `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`). PostgREST validates the `select` list while
planning, so `limit=0` still proves the columns exist.

**Narrowing pass:** PostgREST's `42703` body names only **one** missing column, so on drift
the guard re-probes that table's columns individually and classifies off the *complete* set.
Without it a `WARN` column reported first would mask a missing `HALT` column — incident 1
let through by the guard built to stop it.

### HALT vs WARN

The question is **"would the monitor actually break if this column were missing?"** — not the
narrower "does the monitor write it?".

| Class | Contents | Behaviour |
|---|---|---|
| **`HALT`** | every column the monitor **writes**, plus every column it **reads as required** (direct subscript, no fallback) | `::error::` naming every missing `table.column` + the migration file, then `SystemExit(1)` **before** `run()` — nothing polled, nothing written |
| **`WARN`** | app/iOS-only columns, demonstrated read-tolerant columns, and monitor-untouched bootstrap columns | `::warning::` with the same message shape, then the cycle runs normally |

**Conservative default: when a read column's tolerance is not demonstrated, it is `HALT`.**
A false halt is loud, safe and recoverable; warning past a genuinely-required missing column
lets a real outage through opaquely. Every `WARN` classification must carry a cited fallback
in the actual access pattern — the default is a gate, not advice.

Two `WARN` classifications and the citations that earn them:

- `include_ada_only` — read only as `bool(watch.get("include_ada_only"))`; absent reads
  `false`, which is the documented default. **Standing condition:** the tolerance depends on
  the `watches` read passing no `select` param. If any future monitor read names columns
  explicitly, this must be revisited — a named select 400s the *whole* fetch.
- `error_reason` — the monitor writes it, which normally means `HALT`, but the write is
  self-proving: `write_errored` retries once without the column when the rejection is
  recognized as missing-column (`42703`, or PostgREST's write-body `PGRST204`, matched on
  `message` alone because `details`/`hint` echo row values), then drops it for the cycle. That
  attempt also vetoes the per-id fan-out for exactly that signature, so an unapplied `0004`
  costs one extra write total. An unmigrated DB still errors watches and still monitors.
- `date_mode` / `flex_min_nights` / `flex_max_nights` — the Phase 16 flexible-date columns,
  read only through `flex_min_nights(watch)` in `monitor.run`: `str(watch.get("date_mode") or
  "fixed")` and `watch.get("flex_min_nights")`, every read `.get` with a default. Absent
  `date_mode` reads `'fixed'`, so an unmigrated DB treats every watch as a fixed stay over
  `[start_date, end_date)` — byte-identical to the pre-Phase-16 monitor, never halting and
  never suppressing an opening differently. The nights columns are read only *after*
  `date_mode` already reports `'flexible'`, which an unmigrated DB never does. The monitor
  never writes any of the three. **Standing condition** (same as `include_ada_only`): the
  tolerance depends on the `watches` fetch naming no `select` list — a named select would
  400 the whole read. A read that ever subscripts one of these belongs in the `HALT` set.

**A missing table** takes the highest severity of its columns — all four halt today.

**Drift vs transient:** drift is a 4xx whose PostgREST code is a schema code (`42703`,
`42P01`, `PGRST205`). Everything else — 5xx, 429, timeout, transport error, an unrecognized
4xx code — is a **transient probe failure**: `::warning::` and continue. A Supabase blip must
never take the monitor down; only a positively-identified missing object may. A blip on one
table neither un-confirms nor hides a halting column on another; the annotation names the
tables it could not classify so the listed set is not read as complete.

**Accepted tradeoff:** product-only drift produces only a warning line in a green run's log,
which can be missed. Accepted because (a) cancellation monitoring never pauses, and (b) an
app-only drift surfaces on its own as the app failing to save a watch — which is how incident
2 was actually noticed.

**Rejected alternatives:** parsing `migrations/*.sql` at runtime (additive-only, never sees
base columns); parsing `schema.sql` at runtime (a formatting change would redden production,
and the dependency cap rules out a real SQL parser); `information_schema` (PostgREST exposes
only `public`, so reaching it needs a SQL function — a migration that must itself be applied
by hand, disarming the guard with exactly the failure it catches).

**Known coverage gap, stated not papered over:** a `select`-list probe sees columns and
tables, not constraints. The `UNIQUE(watch_id, site_id, date)` the alert dedup depends on
cannot be verified without a write. Both incidents to date were missing columns.

---

## Poll / alert pipeline

### Write budget
A no-change cycle performs **≤5 Supabase writes** regardless of watch count: 1 batched
`last_checked_at` PATCH, 1 `run_summaries` INSERT, 2 retention DELETEs. Enforced by
`test_write_budget`. **Do not add per-watch writes** — the free tier is the constraint, and
delta-only writing is what keeps ~200–500 writes/day at any scale instead of ~9,600.

### Anti-blocking (jittered, polite)
Random 0–20 s start delay per run; 1.2–2.8 s inter-request delays; randomized poll order;
one realistic browser User-Agent per run, rotated across runs; exponential backoff on
403/429/5xx (2 → 4 → 8 s, then skip that unit this cycle). Escalation path is the self-hosted
runner (residential IP).

### Time budget
Polling stops once an 8-minute per-cycle budget (`CYCLE_TIME_BUDGET_SECONDS = 480`) is spent,
keeping every run — even under sustained blocking — inside the workflow's 15-minute timeout.
Skipped units retry next cycle; skipped watches keep their old `last_checked_at`; the summary
counts only what was polled.

**A budget-exhausted cycle goes red, not green.** Serial per-request politeness caps one cycle
at **~40 GoingToCamp parks** (≈11 s/park against the 480 s budget); past that the poll loop runs
out of budget and leaves the remaining parks unpolled — their watches simply do not fire that
cycle. That is a *completed miss*, not an in-cycle transient the next run heals, and it belongs
to no single watch (the poll plan is shared across every user), so it is recorded through
`record_cycle_failure` (systemic by definition → non-zero exit, `::error::`), and the skipped
count is surfaced on the result as `polls_skipped`, mirroring the errored-watch census (no added
read or write — the ≤5-write budget holds; no watch is moved to `status='error'`). **There is
deliberately no tolerant threshold:** the budget only trips once serial work already exceeds one
cycle's capacity, so any skip already means the fleet (or a stalled upstream) is over capacity;
a `K>0` threshold would silently under-serve up to `K` parks every cycle — the exact silent-miss
class the census exists to prevent. Before this, a budget-exhausted cycle appended a bare warning
to the world-readable `run_summaries.errors` and **exited 0 (green)**, so a fleet growing past
~40 parks would silently stop polling its cold parks with nothing an operator watches saying so.
The ~40-park ceiling is now visible: it makes the run red instead of hiding behind a warning line.

### Poll horizon
Every provider clamps a watch's request to today → today + 12 months. "Today" uses a fixed
UTC-8 offset so same-night US openings stay alertable during US evening hours after UTC
midnight. A watch straddling the horizon is served for its in-horizon nights alone.

### Deduplication impact

| Users | Unique campgrounds | Requests/cycle | Runtime incl. jitter |
|---|---|---|---|
| 100 | 20 | ~20 | ~1 min, at the floor |
| 1,000 | 80 | ~80 | ~3 min |
| 10,000 | 200 | ~200 | ~7 min |

Dominated by the 1.2–2.8 s inter-request pacing (~2 s mean); the ≤20 s start jitter is noise
at every row. Today's poll set is far smaller than the first row — the poll cycle itself
measures 11–25 s end to end — but the *billed* job also carries GitHub Actions' fixed overhead
(checkout + `setup-python` + `pip install`), which is why the whole job lands at ~109 s and
bills ~2 min (§ GitHub Actions minutes). The scaling point is where the *growth* comes from:
20 poll units is ~19 gaps × ~2 s ≈ 38 s of pacing plus request time, and **past roughly 20 poll
units the binding growth term of a no-change cycle is the per-request pacing, not the start
jitter**. Now that the repo is public that wall clock is no longer a billing lever, but it is
still the scaling term to watch — it races the in-code 480 s cycle-time budget as the pool grows.
This is an observation about the
budget: `INTER_REQUEST_DELAY_RANGE` is politeness toward the providers, is not a cost lever, and
stays as it is.

### Alert delivery
APNs HTTP/2 with an ES256 JWT (`iss`=team, `kid`=key, cached ~50 min), `apns-push-type: alert`,
`apns-priority: 10`, `apns-topic`=bundle id, host routed by `device_tokens.environment`.
A `410` (Unregistered) deletes the dead token row; so does a `400` naming the token itself
dead (`BadDeviceToken` / `Unregistered`, `apns.DEAD_TOKEN_REASONS`) — the app re-registers a
fresh token on next launch (Phase 15). Both are keyed on `device_tokens.user_id` (the PK), so
the prune removes exactly that one user's token and no other's, and only when a push was being
sent (no per-watch write on a no-change cycle). A `410` is a clean prune reported to neither
audience; a `400` still surfaces to the operator like every other 4xx (unrated all the same).

The rest of the 4xx space splits by **who can fix it**:

| Class | Examples | Retry | Rated? |
|---|---|---|---|
| Retryable | 5xx, 429, transport | keeps old `state_hash` | **yes** |
| Per-device permanent | `BadDeviceToken`, `DeviceTokenNotForTopic`, unenumerated reason, missing token row, unbuildable push URL | no (hash advances) | **no** — one dead token can't redden the schedule |
| Pool-wide config | `ExpiredProviderToken`, `InvalidProviderToken`, `MissingProviderToken`, `BadTopic`, `TopicDisallowed` | keeps old hash | **yes** — a signing-key or bundle-id outage must exit non-zero |

A **wipeout backstop** (`APNS_WIPEOUT_RATE` / `_FLOOR`) additionally turns the run systemic
when outright rejections wipe out nearly every served push, catching reason codes the split
does not enumerate.

If a push is delivered but its `sent_alerts` dedup row is rejected, the `state_hash` is
written anyway — otherwise the identical push repeats every cycle, because the rows that
would suppress it are exactly the ones that failed to write. An occasional missed re-alert
beats a repeating push.

On a `DELIVERED` push the monitor also inserts one `alert_history` row (the app's server-truth
Alert History; see the schema section), in the **same step and on the same condition** it sets
`watches.last_found_at`, so the badge and the app's history are derived from one fact and can
never disagree. That insert is purely additive: it is contained in its own `try`, and a failure
(e.g. an unapplied `0005`) records a **non-blocking, unrated** cycle failure — it never blocks
the row's own `state_hash`/`last_found_at` write, never errors the watch, and never reddens the
run. So a live DB missing `alert_history` keeps monitoring and keeps delivering; only the
history rows go unwritten until the migration is applied.

### Retention
`sent_alerts` and `run_summaries` rows older than 30 days are pruned every run, before the
summary INSERT so prune failures land in the row's verdict.

---

## Providers

`monitor.run` is **provider-neutral**. Everything site-specific lives behind the `Provider`
protocol (`scripts/providers/base.py`): `poll_plan` / `poll` / `extract_relevant` /
`booking_url` (+ the optional `unpollable_reason` hook), one conformer per `watches.provider`
value, registered in `scripts/providers/__init__.py`.

| `provider` | Poll unit | `provider_ref` |
|---|---|---|
| `recreation_gov` | one (campground, month) `GET /api/camps/availability/campground/{id}/month?start_date=…` (UTC month start) | unused — `campground_id` is the whole identity |
| `going_to_camp` | one (park, stay): root map + each child map, 2–5 paced GETs inside one `poll`, plus one park-catalog GET | `{"resource_location_id": …, "map_id": …}`, both required |
| `use_direct` | one (facility, stay): a single read-only POST to the tenant's availability grid (no recursion, no catalog fetch — the grid body carries `UnitId`, `Name`, per-night `IsFree`) | unused — `campground_id = '<tenant>_<facilityId>'` carries both the tenant and the facility |

A watch naming a provider this build does not register is **skipped untouched** — never
errored — so an older monitor cannot mis-serve a row a newer client wrote. An *unknown*
provider is refused rather than defaulted; a **missing/null** `provider` defaults to
`recreation_gov`.

Booking links: recreation.gov → `https://www.recreation.gov/camping/campsites/{campsite_id}`;
GoingToCamp → `/create-booking/results?mapId=…&resourceLocationId=…&startDate=…&endDate=…`
(the SPA takes no site preselect); UseDirect → the tenant's public booking site
(`https://www.reservecalifornia.com/` for `ca`; the grid carries no id to build a verified
per-facility link from).

### SSRF invariant — the one that must never be relaxed

- **Every request host is a hardcoded constant in its provider module** (or an explicit
  allowlist). A new conformer's host is a module constant, no exceptions.
- **`watches.provider_ref` is client-writable and carries identifiers only.** A host, base
  URL, or path is **never** derived from it — nor from `campground_id`, nor from any pasted
  string.
- Only **validated identifiers** reach a query string: `campground_id` matched against
  `CAMPGROUND_ID_RE`, `resource_location_id` bounded by `_identifier`, and for pricing the
  `resourceId` and `startDate`. A park's `rootMapId` comes from the catalog, never a query
  string.
- The GoingToCamp poll key deliberately **omits `host`** — it is the module constant, and no
  key material, like no request URL, may derive from `provider_ref`.

### `campground_id` format contract

**`watches.campground_id` must match `CAMPGROUND_ID_RE = [A-Za-z0-9_-]+`.** A watch whose id
has any other character is errored before it is ever polled, and `status='error'` is terminal.

**GoingToCamp ids are packed `gtc_<resourceLocationId>_<mapId>` — UNDERSCORES, NOT COLONS.**
A colon is not in that character class. Negative ids are safe unencoded, because `-` is.

> This exact spec being stale caused a production incident: the client packed
> `gtc:<resourceLocationId>:<mapId>` against a doc comment citing a "backend convention" that
> never existed in backend code, and **all three** GoingToCamp watches were errored on first
> sight and never polled — silently, behind green runs with `errors: null`, for two days.
> **The regex was deliberately not loosened:** it constrains a client-writable value that
> reaches outbound request construction, and weakening it erodes the SSRF invariant above.
> The fix was client-side (one `goingToCampMarker` constant, plus a legacy `gtc:` read path)
> and is pinned by `CampgroundProviderRoutingTests` in `camp-assist`.

`PollKey[0]` is the watch's own `campground_id` by contract — the 404-strike lifecycle and
`campgrounds_polled` telemetry are campground-scoped. Poll dispatch is anchored to the
**provider**, never to the client-writable `campground_id`, so a `campground_id` two providers
share can never cross between them. **The backend never parses the id.**

### GoingToCamp network posture

**Keyless GET, plus one read-only pricing POST; never drive a browser.**

- The SPA HTML is Azure-WAF captcha-gated; `/api/*` is not. Every request stays on `/api/*`
  with a browser UA and the standard pacing. Driving a browser is what trips the WAF.
- The one exception (captain decision, 2026-07-26): `POST /api/resource/feeDetails`, which
  answers `405` to a GET. Empty body, no cart, no cookie, no token, creates nothing, and is
  **not in the polling loop** — it is discovery-time only, on-device. The SSRF invariant
  applies to it in full.
- Availability parses "unknown means **taken**": only `availability == 0` is confirmed
  bookable, so every other enum value — including one never seen — parses as taken. A missed
  alert the user can see coming beats a phantom one.
- The park catalog (`/api/resourcelocation/resources`, keyless, ~64 KB/park, cached for the
  process — one process = one cycle) supplies real site labels and the per-site detail,
  decoded through `/api/attribute/filterable` and `/api/equipment`. It is **cosmetic**: a park
  whose catalog a cycle cannot read still polls, hashes and alerts with the `resourceId`
  fallback. Being cosmetic, it is also **unretried** — one paced attempt, no backoff, so a
  dead catalog endpoint cannot spend the time budget availability needs.
- Every metadata field is **unknown, never no**, when the platform did not publish it: a
  filter built on one must fail open, because a suppressed opening is invisible to the user.
  Enum indices mean nothing on their own (`0` is "Yes" on ADA Only and "Not Available" on Pad
  Location), so Yes/No attributes resolve through the vocabulary, never a cast.
- `MAX_CHILD_MAPS` (40) is a **safety cap** an order of magnitude above any observed park, not
  a tuning knob. A park past it is a fault an operator must clear: `poll` **raises**, the cycle
  contains it as a cycle failure, and the run goes non-zero. `poll` returning `None` means
  *transient* (keep the old hash, retry next cycle) — a fault no retry can clear must be
  raised, or a park that can never be served sits behind a green exit code.

### The ADA-Only exclusion

The one place a metadata field changes what the cycle alerts on, and it is `going_to_camp`'s
`extract_relevant` alone. Three placement rules, each with a test:

1. **After the `site_ids` match** — a user who named the site outranks the filter. The filter
   removes noise from an *undirected* watch; it never overrules a deliberate choice.
2. **Before the caller hashes the shape** — an ADA-only site opening and closing is not a
   delta and costs no `watches` PATCH.
3. **Only a site the catalog positively marked is dropped** — an unreadable catalog or
   vocabulary suppresses nothing.

Opt-in is read `bool(watch.get("include_ada_only"))` — the `.get` with a default is exactly
what keeps that column `WARN`. Default `false` for every watch, old and new, with **no
backfill**: a deliberate behaviour change, since the monitor used to alert on these sites.

**`recreation_gov` never filters and never reads the column** (captain decision): its
`is_accessible` means the site *has* accessibility features, not that it is reserved for
campers with disabilities. Filtering on it would hide roughly 2.75 bookable sites per
restricted one. The decision is cited at its `extract_relevant` so the asymmetry does not look
like an oversight and does not get "fixed" into over-filtering.

### Adding a provider

Write the conformer in its own module under `scripts/providers/`, register it in
`__init__.py`, put its host in a module constant. Two invariants a new conformer must respect:

- `campground_id` must match `CAMPGROUND_ID_RE` whatever the site's own id scheme is — a
  namespaced id needs `gtc_…`, not `gtc:…`.
- **`poll_plan` runs outside the cycle's per-watch containment, so it must never raise.** A
  watch it cannot plan for plans nothing; `extract_relevant`, which *does* run inside
  containment, is where that failure is raised. `unpollable_reason` also runs outside
  containment and must not raise; its reason string reaches the operator channel, so it names
  the field, never the client's value.

Primitives both layers need (`as_date`, `capped_line`, `MAX_ERROR_MESSAGE_CHARS`) live in
`scripts/common.py` so a provider never imports `monitor` (which imports it).

---

## Watch lifecycle, errors, and run status

### `status='error'` is terminal

Nothing writes the status back to `monitoring`, and the cycle's only `watches` read filters
`eq.monitoring` — an errored watch is **absent**, not failing. That once left 3 of 5 watches
dead for two days behind green runs with `errors: null`.

### The four error causes

| `error_reason` | Cause | Fixable? |
|---|---|---|
| `invalid_campground_id` | `campground_id` fails `[A-Za-z0-9_-]+` | **fixable** — data or client fix |
| `unreadable_provider_ref` | the watch's provider cannot poll with its `provider_ref` | **fixable** — data or client fix |
| `campground_not_found` | campground 404ed 3 consecutive cycles (typo or delisted; any success resets) | **permanent** |
| `watch_write_rejected` | the watch's own writes are permanently rejected | **fixable** — recovers the moment the migration is applied |

`monitor.ERROR_REASONS` is a fixed vocabulary. **Every `status='error'` write goes through
`write_errored`, never a bare `write_watches`**, so it carries one of these values. A new error
site needs a *reason*, not a new string — the values are what a future retry policy classifies
on.

**A pool-wide unpollable condition is not a watch's fault:** `unpollable_reason` crosses
`is_systemic` against the active pool, and a pool-wide hit is left `monitoring` and reported as
a cycle failure. Broad breakage is the operator's to fix; nobody should have to recreate a
watch over a client-wide bad key name.

**Automatic re-arming of errored watches is deliberately out of scope** (captain, 2026-07-27):
reason data first. A blanket retry would re-poll known-dead watches forever and undo what the
terminal design buys.

### Errored-watch census

Every cycle counts the `status=eq.error` population still worth acting on
(`end_date=gte.<today>`) — **one extra select, no per-watch writes**, so the ≤5 budget holds —
and puts the count on the world-readable `run_summaries` row, with the per-reason breakdown
operator-only.

- Past-date errored rows are excluded on purpose: nothing can be done about a trip that has
  already happened, and a warning that fires forever is worth what no warning is worth.
- The count is a **standing fact, not this cycle's verdict** — it never changes the exit code.
- It reports the population *entering* the cycle; watches errored during it are reported by
  the lifecycle passes and join the census next cycle.
- Both census and breakdown are **bucketed, never echoed** (unknown → `other`, pre-`0004` →
  `unrecorded`), because `error_reason` and `campground_id` are client-writable.

### Failure containment

Per-watch work in `monitor.run` is contained: one watch's failure never aborts the cycle, and
end-of-cycle bookkeeping (`run_summaries`, pruning) always runs. New watch writes go through
`patch_watches` (batched, per-id fallback) and new failure paths through `record_failures`, so
they count toward the systemic threshold.

**A watch moves to `status='error'` only for a failure a write pinned to its own row**
(`PatchOutcome.isolated` / `isolated_failure`). Table-scoped `sent_alerts` / APNs failures use
`unattributed_failure` and must not error the watch — that is not evidence this user's row is
broken. Such a watch was still polled, so it is still stamped `last_checked_at`.

Fan-out rules, each with a reason:

- Only a **permanently** rejected batch (PostgREST 4xx other than 429) falls back to one write
  per id, so one unwritable row cannot silently drop everyone else's update.
- A **transient** batch failure is never fanned out: it errors nothing, so isolation buys
  nothing, while dozens of sequential 30 s PATCHes against a struggling Supabase would blow the
  15-minute timeout and kill the run before its summary row and pruning.
- Batches larger than `PER_ID_FALLBACK_MAX` (50) skip the fan-out, as does a rejection the
  caller knows is about the *payload* rather than any one row — today the missing
  `error_reason` column alone.
- The fan-out is capped twice: `PER_ID_FALLBACK_BUDGET_SECONDS` (100 s) of total wall clock,
  and never past `FANOUT_DEADLINE_SECONDS` (600 s) **from process start**, so the preflight and
  the up-to-20 s jitter count against it rather than stacking on top. The arithmetic closes
  against `timeout-minutes: 15` (900 s): 120 s setup + 600 s deadline + 180 s shutdown reserve.
  Since the jitter came down from 240 s, even a worst case — full jitter plus a fully spent
  480 s poll budget, 500 s of the 600 — reaches the fan-out with ~100 s left; a run slow enough
  to arrive past the deadline still gets none at all, because reaching the summary row and the
  pruning matters more than isolating one row.

### Exit status

| Verdict | Exit | Rule |
|---|---|---|
| *isolated* | 0, schedule green | the healthy watches were served |
| *systemic* | non-zero + `::error::`, schedule red | more than `SYSTEMIC_ERROR_RATE` (25%) of **served** watches failed **and** at least `SYSTEMIC_ERROR_FLOOR` (2) did — so 1-of-2 stays green, 2-of-2 goes red — **or** a failure belonging to no watch occurred |

Failures belonging to no watch: the `run_summaries` INSERT, retention pruning, being unable to
write `status='error'`, a provider raising out of a shared poll unit, a pool-wide unpollable
condition, and the **poll budget running out mid-plan** (skipped parks were not served this
cycle — see the Time budget section for why any skip goes red).

The **served set** is both the denominator and the scope of the numerator, so the ratio can
never exceed 1. It excludes watches that expired this cycle, were errored, name an unregistered
provider, are wholly beyond the poll horizon, or that the time budget never reached. A cycle
that failed every watch it served goes red however much of the pool left for unrelated reasons.

The numerator counts only failures that say something about this cycle's health — a per-device
APNs rejection is reported but not counted, and the tally says how many it left out.

**Systemic runs leave the watch pool untouched:** broad breakage is the operator's to fix, not
something users should have to recreate watches over. Failures before any of that (unreachable
Supabase, malformed `APNS_P8_KEY`, missing secret) still exit non-zero. **Keep loud failures
loud.**

### Two rendering channels

`run_summaries.errors` is **world-readable**; the Actions annotation is operator-only. The
same failures get two renderings:

| | World-readable row (`safe=True`) | Operator annotation |
|---|---|---|
| Watch identity | per-run ordinals (`watch #1`) | full watch UUIDs |
| PostgREST body | status + column/constraint name; `details`/`hint` **dropped** (they echo offending row values) | full reason |
| Exception with no response | type name only (its message can quote the value that upset it) | full message |
| User-supplied value (a rejected `campground_id`) | an aggregate **count** only | the values, via `detail_only` |

Rules that keep this true as code is added:

- A failure reaches the row **only through a rendering path** (`watch_failures` per-watch,
  `record_cycle_failure` otherwise) — never by appending a pre-formatted message to the shared
  `errors` list, which is copied into the row verbatim. Anything that knows a watch id (e.g.
  `apns.send_alert`) hands back the **exception** and lets `run` attribute and render it.
- Everything on the shared channel goes through `capped_line`, which bounds the whole composed
  line, prefix included.
- Each cycle failure travels **one channel per audience** (`public_cycle_failures` → the row,
  `cycle_failures` → `cycle_errors`), because `failure_annotation` joins `errors_detail` and
  `cycle_errors` and a copy in both prints twice.
- Neither channel ever carries the service-role key.

The persisted `(isolated)`/`(systemic)` label is chosen after folding in every failure known
before the row is written, so it always matches the exit code. A red run whose only failure
belonged to no watch persists that label rather than a NULL that would read like a clean cycle.
A summary INSERT that itself fails is the one unrepresentable case — there is then no row.

---

## Implementation phases

> **Checkbox status is authoritative and was established by a verification pass
> (camp-assist PR #26). Do not re-tick, un-tick, or tidy these.** Items under **Validate** are
> operator gates that only an operator closes — several are verified offline yet stay unticked
> for that reason.
>
> One exception, recorded so a later reader knows why it differs from camp-assist's copy: the
> **Phase 13a backend Build/Tests blocks and the single Phase 13c backend item** were
> re-verified against *this* repo's code when the backend plan was split out here, and each
> box ticked there carries the `file:line` (or shipping test name) that proves it — the same
> evidence discipline camp-assist PR #26 used. Nothing else was re-ticked, and no `Validate`
> box was touched. No lasting divergence is created: a follow-on task removes these backend
> sections from camp-assist's `PLAN.md`, making this doc their only home.

### Phase 1 — Supabase + backend core

**Build:**
- [x] Supabase project created; schema SQL applied; RLS policies active; Anonymous Sign-In enabled
- [x] `campassist-monitor` **private** repo: `scripts/monitor.py` (jittered polling, dedupe, delta, cooldown, expiry, pruning), `scripts/apns.py`, `scripts/db.py`, `requirements.txt`
- [x] `monitor.yml` (`workflow_dispatch`, EventBridge-driven; 30-min `schedule` cron dormant since 2026-08-10), `keepalive.yml`, `ci.yml` (pytest on PR)
- [x] All 6 GitHub Secrets configured; setup documented in README

**Tests (pytest, all must pass in CI):**
- [x] `test_dedupe_poll_plan` — 3 watches same campground+month → 1 poll entry; different months → separate
- [x] `test_delta_detection` — unchanged availability → no alert; new opening → alert
- [x] `test_alert_cooldown` — (site,date) alerted <6 h ago → suppressed; >6 h → re-alerted
- [x] `test_expiry` — watch past `end_date` → marked expired, excluded from polling
- [x] `test_jitter_bounds` — inter-request delays within [1.2, 2.8]s; start delay within [0, 20]s
- [x] `test_apns_jwt` — ES256, correct `iss`/`kid`; cached within run, refreshed after 50 min
- [x] `test_apns_410_cleanup` — 410 → token row deleted
- [x] `test_apns_400_dead_token_pruned` — 400 `BadDeviceToken`/`Unregistered` → token row deleted (Phase 15), only that user's, config faults (`BadTopic`) spared
- [x] `test_env_routing` — sandbox token → sandbox host; production → production host
- [x] `test_apns_headers` — `apns-push-type: alert`, `apns-priority: 10`, `apns-topic` present
- [x] `test_parser_defensive` — missing/renamed JSON fields → partial parse, no crash (fixture-driven)
- [x] `test_backoff` — 429 → 2s/4s/8s retries → skip; run continues
- [x] `test_write_budget` — 100 watches, no changes → ≤5 DB writes total
- [x] `test_retention` — rows >30 days pruned

**Validate:**
- [ ] `workflow_dispatch` manual run against a real campground completes green *(needs live Supabase + APNs secrets — not exercisable from a clone)*
- [ ] Run with 0 watches completes cleanly (no crash on empty state) *(same)*
- [ ] `run_summaries` row written with correct counts *(same)*
- [ ] Supabase RLS verified: anon user A cannot read user B's watches (manual REST probe) *(same)*

### Phase 7 — Schema + provider abstraction

**Build:**
- [x] Migration: `ALTER TABLE watches` adds `provider` (default `'recreation_gov'`, CHECK-constrained) + `provider_ref` JSONB (default `'{}'`). Backfill-free by construction; mirrored in the schema section above.
- [x] Refactor the poll/parse/normalize trio behind a `Provider` strategy (`poll_plan` / `poll` / `extract_relevant` / `booking_url`). The cycle skeleton — jitter, dedupe, delta hash, alert dedup, write/time budget, `run_summaries`, retention — stays **provider-agnostic**.
- [x] `RecreationGovProvider` becomes the first conformer — pure extraction, no behavior change. `PROVIDERS` selected per watch.
- [x] Dedupe poll plan stays cross-user but is keyed **per provider**, each unit tagged with its provider's name by `monitor.PollUnit`. *(The GTC key omits `host` deliberately — a security invariant, not a shortfall: see the SSRF invariant. `PollKey[0]` is the watch's own `campground_id` by contract.)*

**Tests (the refactor is gated by the EXISTING suite — no new behavior for rec.gov):**
- [x] Entire current pytest suite passes unchanged — proof the refactor preserved recreation.gov behavior exactly.
- [x] Provider dispatch is covered — a watch's `provider` selects the strategy; a **missing/null** `provider` defaults to `recreation_gov`, an **unknown** one is **refused** (`provider_for` raises `KeyError`; the cycle filters those watches out untouched rather than polling them with another provider's poller). *(Shipped as `test_provider_name_defaults_to_recreation_gov` and `test_provider_for_routes_and_refuses_the_unknown`; the planned name `test_provider_dispatch` does not exist.)*

**Validate:**
- [ ] Migration is backfill-free: every pre-existing row reads back `provider = 'recreation_gov'`, `provider_ref = {}` with no UPDATE pass *(live Supabase run needs secrets)*.
- [ ] `workflow_dispatch` against an existing recreation.gov campground is byte-for-byte unchanged in `run_summaries` counts *(same)*.

### Phase 8 — GoingToCampProvider

**Build:**
- [x] `GoingToCampProvider.poll` — one `GET /api/availability/map` per child map with `getDailyAvailability=true`, root→child recursion **internally**, `availability == 0 → "Available"` keyed by `resourceId`, returning the **same normalized shape both providers share** so `state_hash` and the cycle loop are untouched.
- [ ] Reuse the existing backoff / rotating browser-UA / inter-request pacing **verbatim**; fold the 2–5 GETs/park into `CYCLE_TIME_BUDGET_SECONDS`; cache the park→child-map structure. *(Left unchecked: backoff/UA/pacing reuse and the budget folding are in `fetch_map`/`poll_park`, but there is no park→child-map structure cache — the only process-lifetime caches are the site-metadata/vocabulary ones.)*
- [x] `apns.py`: booking-URL construction becomes **provider-dispatched**.
- [x] Defensive parsing: unknown `availability` enum values → **not available**; tolerate negative-int IDs, empty `resourceAvailabilities`, missing child maps.
- [x] GTC-specific health signal surfaced in `run_summaries.errors`, recorded distinctly from rec.gov failures.

**Tests (against captured 2026-07-18 JSON fixtures, `tests/test_providers_going_to_camp.py`):**
- [x] `test_poll_recurses_from_the_root_map_into_every_child` (+ `test_poll_follows_a_park_that_nests_deeper_than_one_level`)
- [x] `test_availability_zero_is_open_and_every_other_value_is_not`
- [x] `test_extract_relevant_returns_the_shape_recreation_gov_returns`
- [x] `test_booking_url_opens_the_parks_booking_search_for_the_stay`
- [x] `test_parse_map_degrades_instead_of_crashing` (+ `test_parse_map_refuses_a_body_it_does_not_recognize`)
- [x] `test_poll_paces_the_recursion_and_charges_it_to_the_cycle_budget` (+ `test_the_fan_out_cap_bounds_the_whole_park_however_deep_it_nests`)

**Validate:**
- [ ] Fixture-driven end-to-end: a captured park with a known opening produces exactly one normalized opening and the correct booking deep-link. *(Left unchecked deliberately, not unverified — the offline half is covered by `test_the_cycle_serves_a_going_to_camp_watch_end_to_end`, but this item sits under **Validate**, where every box is an operator gate.)*
- [ ] Live `workflow_dispatch` against a real WA park detects a real opening *(needs live secrets + a real opening — a manual gate)*.

### Phase 12 — Schema-drift preflight guard

Design, classification and rationale are in [Schema-drift guard](#schema-drift-guard-scriptspreflightpy) above.

**Invariants this phase must not break:**
- [x] **Manual application is preserved.** The guard detects only — never runs DDL, never applies a migration, no write path of any kind.
- [x] **No new production-DB secret.** Reuses `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.
- [x] **No auto-apply, ever** — not in CI, not in the monitor, not behind a flag.
- [x] **Zero writes.** `GET`-only, so the ≤5 no-change write budget is untouched (`test_preflight_performs_no_writes`, `test_healthy_cycle_keeps_the_write_budget`).
- [x] **Offline suite.** No network and no real secrets in tests.

**Build:**
- [x] `scripts/preflight.py`: a `REQUIRED` manifest of `{table: {column: (severity, introducing-migration-or-None)}}` mirroring every table and column in `supabase/schema.sql`, plus `check_schema(db) -> list[Drift]` issuing one `db.select(table, {"select": …, "limit": "0"})` per table. No change to `scripts/db.py`.
- [x] Narrowing fallback: on a table-level `42703`, re-probe that table's columns individually to enumerate **every** missing column; classify off the complete set, never off the first reported column.
- [x] Classify every manifest column `HALT` vs `WARN` (monitor-written + read-as-required vs app-only / demonstrated-tolerant / monitor-untouched bootstrap). A missing table takes the highest severity among its columns. Keep the classification beside the column so it cannot drift from the manifest.
- [x] Apply the **conservative default**: any read column whose tolerance is not demonstrated starts as `HALT`.
- [x] Per-column confirmation obligation: for each `WARN`-classified read column, record the citation proving the fallback. Anything unproven moves to `HALT` before merge.
- [x] Classify each probe outcome: `200` → clean; 4xx carrying `42703` / `42P01` / `PGRST205` → **drift**; everything else → **transient**.
- [x] Wire into `main()`: build the client and run the preflight **before** the start jitter so a halting run fails fast. Halt-set drift → `::error::` + `SystemExit(1)` without entering `run()`; warn-set drift → `::warning::` and continue; transient → `::warning::` and continue. When a run turns up both, the halt wins and the message lists every missing object.
- [x] Account for the probe in the budget arithmetic comment: up to four `GET`s on the healthy path, charged against `FANOUT_DEADLINE_SECONDS`; the narrowing pass costs at worst one extra `GET` per column of a drifted table.
- [x] `README.md` "Database migrations" gains the guard's behaviour; `AGENTS.md` gains the clause that a schema change means `schema.sql` + a migration **+ the `preflight.REQUIRED` entry**, classified `WARN` only with a proven fallback — and states the **apply-first ordering**.

**Tests (offline, reusing `tests/helpers.py`):**
- [x] `test_preflight_clean`
- [x] `test_preflight_detects_missing_column` — incident 2 replayed: both `provider` and `provider_ref` absent yields a `::warning::` naming **both** plus `0002`, no `SystemExit`, cycle still runs. Exercises the narrowing pass.
- [x] `test_preflight_missing_written_column_hard_fails` — incident 1 replayed: `::error::`, non-zero `SystemExit`, message names `0001`, `run()` never entered.
- [x] `test_preflight_mixed_severity_halts` — a `WARN` and a `HALT` column missing from the same table with the `WARN` one reported first: verdict must be halt and the message must name both. Guards the masking hole.
- [x] `test_preflight_transient_does_not_hard_fail` — 503, 429 and `httpx.ConnectError` each warn, no `SystemExit`.
- [x] `test_preflight_performs_no_writes`
- [x] `test_preflight_manifest_matches_schema_sql` — the CI sync guard, both directions, with a sanity assert so a parser that matched nothing fails loudly instead of passing vacuously.
- [x] `test_preflight_message_is_publishable` — both drift messages carry only schema identifiers and migration filenames.

**Validate:**
- [ ] Re-confirm every `WARN` classification against the code before merge, column by column. Anything unproven moves to `HALT` — the conservative default is a gate, not advice.
- [ ] Confirm against a real PostgREST that an unknown column returns `400`/`42703` and an unknown table returns `404` with `42P01` or `PGRST205` *(needs live credentials; a `workflow_dispatch` run is the gate)*.
- [ ] `workflow_dispatch` against the live DB **with `0002` applied**: preflight passes, cycle byte-for-byte unchanged in `run_summaries` counts.
- [ ] Negative check without touching production, both severities: point the manifest at a nonexistent column classified `HALT`, confirm red with the remedy in the annotation and `watches` never written; then classify it `WARN` and confirm green with the warning and a normal cycle.
- [ ] Whole existing suite green — `test_write_budget` and `test_healthy_cycle_keeps_the_write_budget` unchanged.

**Optional follow-on — CI migration nudge** (*optional*, not required by Phase 12): a `ci.yml`
step that diffs against the base ref and, when a `supabase/migrations/*.sql` file is **added**,
posts a `::notice::` reminder to apply it by hand. Reminder only — no `contents: write`, no DB
access, never a required check, no permission change. A nudge at *merge* time; the preflight is
the guarantee at *run* time. If only one ships, it must be the preflight.

### Phase 13a (backend half) — GoingToCamp metadata layer: ADA filter + site labels

Placement rules and the fail-open contract are in
[The ADA-Only exclusion](#the-ada-only-exclusion) above.

**Build — backend:**
- [x] Park-metadata fetch in `scripts/providers/going_to_camp.py`: `GET https://{HOST}/api/resourcelocation/resources?resourceLocationId=<id>`, host from the module constant, id via `_identifier`, and — unlike `fetch_map` — **one paced attempt with no backoff**, because the catalog is cosmetic and a dead endpoint must not spend the time budget the availability polls need (see the network-posture section above). A failure fails open to no labels rather than failing the poll unit. *(`going_to_camp.py:60` `RESOURCES_URL`, `:844-875` `site_metadata`, `:737-796` `_fetch_json`'s "one attempt, no backoff" contract, `:181` `_identifier`.)*
- [x] **One fetch per distinct park per cycle**, deduplicated within the cycle. **No persistent cache and no TTL** — the monitor is a `*/30` cron and every cycle is a fresh process, so there is nothing to carry over and nothing to go stale. On failure the cycle has no marker set: filter nothing, labels fall back to resourceIds. Do not introduce cross-process storage; the zero-new-writes property depends on its absence. *(`going_to_camp.py:799-806` — two process-lifetime caches, a failed fetch cached as `{}`; `:874-875`.)*
- [x] Resolve the `ADA Only` attribute id rather than trusting the magic negative int: read `-32759` back from `/api/attribute/filterable` by `displayName` and pin it in a fixture assertion, so a renumbering surfaces as a **test failure** rather than a filter that silently stops filtering. *(`going_to_camp.py:105` `ADA_ONLY_DEF`, `:652` resolved through `_yes_no(vocabulary, …)` never a cast; pinned by `test_the_pinned_attribute_ids_still_mean_what_this_build_says`.)*
- [x] Each resource is tagged with `ada_only` and its `"site"` set to the real label from `localizedValues[].name`, replacing the resourceId fallback. Absent metadata ⇒ no `ada_only` key and the resourceId fallback — today's behavior exactly. *(Shipped in `apply_site_metadata` (`going_to_camp.py:468-509`) rather than in `parse_map` as predicted, which keeps the availability parse independent of the catalog; `:508-509` sets `ada_only` **only** where the platform published it, so absent reads unknown rather than `False`.)*
- [x] `extract_relevant` skips `ada_only` sites **after** the `wanted` site-id match and **before** `state_hash`, unless `bool(watch.get("include_ada_only"))`. Missing key reads `false`. *(`going_to_camp.py:1038` reads the opt-in and `:1067` is the `exclude` predicate `single_unit_open_sites`/`relevant_open_sites` apply after the `wanted` match.)*
- [x] `scripts/providers/recreation_gov.py`: **no filtering**, with the decision cited in a comment at `extract_relevant` so the asymmetry is deliberate and documented. *(`recreation_gov.py:178-188`.)*
- [x] `supabase/migrations/0003_watches_include_ada_only.sql` (idempotent, backfill-free) + the column in `supabase/schema.sql` + the `("include_ada_only", (WARN, "0003_…"))` entry in `preflight.REQUIRED`, with the tolerance citation. **Apply the migration by hand first, then merge the manifest entry.** *(`supabase/migrations/0003_watches_include_ada_only.sql:28`, `supabase/schema.sql:28`, `preflight.py:102` with the citation at `:148`.)*
- [ ] Charge the metadata GET against the existing pacing/time budget in the arithmetic comment, as Phase 12 did for its probes. *(Still open, and deliberately left unticked: the runtime half ships — `going_to_camp.py:770-773` short-circuits on `budget_exhausted()` and paces with `CHILD_MAP_DELAY_SECONDS` — but the accounting half does not. `monitor.py`'s budget arithmetic (`:77-79` and `:164-192`) still enumerates only the poll requests and the Phase 12 preflight probes, never the per-park catalog GET, and this box is the only record of that gap.)*

**Tests — backend (offline, captured fixtures).** Every ADA/metadata test below shipped under a
different name than this plan predicted, so the shipping name is cited on each one; only the
last two kept their predicted names:
- [x] ADA-only sites are not openings for an **undirected** watch, and nothing else is dropped. *(`test_an_ada_only_site_is_not_an_opening_for_a_watch_that_did_not_ask` — the captured park marks one id ADA Only, not the two predicted, and it asserts the park's other open sites survive; it also covers the absent-column case, a live DB without `0003`, reading the same `false`.)*
- [x] Same fixture with `include_ada_only: true` yields it. *(`test_the_same_site_is_an_opening_for_a_watch_that_opted_in`.)*
- [x] A watch naming an ADA-only resource alerts on it **with the opt-in off**, proving the exclusion runs after the `wanted` match. *(`test_a_watch_that_named_an_ada_only_site_still_matches_it`.)*
- [x] Attribute absent or unrecognized enum → stays bookable. *(`test_an_ada_only_site_is_not_an_opening_for_a_watch_that_did_not_ask` asserts a site carrying no `ADA Only` attribute is still an opening; `test_metadata_this_cycle_cannot_read_excludes_nothing` covers the undecodable enum via an unreadable vocabulary.)*
- [x] A metadata fetch failure filters nothing, labels degrade to resourceIds, cycle completes normally. *(`test_metadata_this_cycle_cannot_read_excludes_nothing` and `test_a_catalog_this_cycle_cannot_read_never_fails_the_unit`.)*
- [x] Two watches on the same park cost one metadata GET, and no state survives the process. *(`test_the_catalog_and_vocabulary_are_read_once_per_process`; `tests/conftest.py` clears the caches between tests.)*
- [x] An ADA-only site opening and closing leaves `state_hash` byte-identical on an undirected watch. *(`test_an_ada_only_site_opening_and_closing_never_churns_the_state_hash`, which also asserts the opted-in watch *does* see the change.)*
- [x] `-32759` resolves to `displayName "ADA Only"`; a renumber fails here first. *(`test_the_pinned_attribute_ids_still_mean_what_this_build_says`.)*
- [x] `site` is the human label; a resource missing from metadata falls back to the resourceId. *(`test_the_label_replaces_the_resource_id_and_falls_back_when_it_cannot`.)*
- [x] An accessible rec.gov site is still alertable. *(`test_recreation_gov_never_applies_the_ada_only_exclusion`, `tests/test_providers.py:131`.)*
- [x] `test_preflight_manifest_matches_schema_sql` passes with the new column in both, and `test_include_ada_only_is_warn_and_carries_its_migration` pins the `WARN` classification to `0003`. *(`tests/test_preflight.py:396`.)*
- [x] Write-budget and cycle-time suites unchanged — this phase adds no per-watch write; the catalog read is a `GET`. *(`test_write_budget`, `test_healthy_cycle_keeps_the_write_budget`.)*

**Validate:**
- [ ] Confirm `0002_watches_provider.sql` is applied to the live DB **before** anything here ships; then apply `0003` by hand and only then merge the manifest entry. The preflight reports the live column set — use it rather than guessing.
- [ ] Re-confirm live (one `curl`, no browser) that `-32759` is still `ADA Only` and the availability→catalog id join is still total on a real park. *(Needs network; a manual gate.)*
- [ ] `workflow_dispatch` against a real WA park: the three known ADA-only ids no longer appear in openings, `run_summaries` counts otherwise unchanged, alert copy names a real site label instead of a negative integer. *(Needs live secrets.)*

### Phase 13c — backend item

The rest of 13c is iOS and lives in camp-assist's `PLAN.md`. Its one backend item, dispatched
as its own `campassist-monitor` task (captain, 2026-07-26):

- [x] `going_to_camp.extract_relevant`'s `wanted` match already tests `{resource_id, campsite_id, site}`, and 13a made `site` the real label — confirm a watch written with `resourceId` values matches, and that alert copy reads "Site 42". The iOS half shipped independently, since the wizard writes the `resourceId` the backend already matches on and neither side's contract moved. *(`test_a_watch_on_one_resource_id_alerts_on_that_site_under_its_label` — the id selects and `monitor.available_sites` carries the catalog label, while `alert_rows` stays keyed on the id; `test_site_ids_match_the_resource_id_and_never_the_display_label` proves the label never selects; `test_a_selection_still_matches_when_the_label_could_not_be_resolved` proves a degraded label never fails the match closed.)*

---

## Phase 16 — Flexible-date / flexible-length watches *(scout B6)*

**What it adds:** today a watch is one fixed stay. A *flexible* watch asks for **"any N-night
window inside a date range"** and the monitor alerts when **any** qualifying consecutive-night
window inside the range is fully available. This is backend-owned: these three `watches`
columns and the match logic in `monitor.run`. The camp-assist app builds its `WatchDTO` to the
contract below.

### The column / DTO contract (frozen — the app builds to this)

The range bounds **reuse the existing `start_date` / `end_date`** rather than adding new window
columns. Both are already `NOT NULL` `DATE` (timezone-independent calendar days — no shift),
and reusing them means the poll horizon, each provider's `poll_plan`, and `extract_relevant`'s
date filter already cover the whole range with **no provider changes**, and a fixed watch stays
byte-identical. Three columns are added (`supabase/migrations/0006_watches_flexible_dates.sql`):

| Column | Type | Default | Nullable | Meaning |
|---|---|---|---|---|
| `date_mode` | `TEXT` `CHECK IN ('fixed','flexible')` | `'fixed'` | no | The discriminator. `'fixed'` = one stay. `'flexible'` = any window in the range. |
| `flex_min_nights` | `INT` `CHECK (… >= 1)` | `NULL` | yes | **Flexible only.** Shortest qualifying window, in nights. **The alert gate.** `NULL` in fixed mode. |
| `flex_max_nights` | `INT` `CHECK (… >= 1)` | `NULL` | yes | **Flexible only.** Longest window the user will take, in nights. **Advisory** (app display/booking); does **not** narrow alerts. `NULL` in fixed mode. |

**How `start_date` / `end_date` are read by mode:**

- **fixed** (`date_mode` absent, `NULL`, or `'fixed'`) — `start_date` = check-in, `end_date` =
  check-out; the stay is the nights `[start_date, end_date)`, exactly as before this phase.
- **flexible** (`date_mode = 'flexible'`) — `start_date` = **earliest** check-in and `end_date`
  = **latest** check-out of the search range. The nights considered are `[start_date, end_date)`.
  A length-`L` window with check-in `a` occupies nights `[a, a+L)` and qualifies when
  `a >= start_date` **and** `a + L <= end_date`.

**On the wire (what `WatchDTO` sends/reads):** a fixed watch is unchanged — it may omit the
three new keys entirely (they default). A flexible watch sends
`date_mode: "flexible"`, `flex_min_nights: <int ≥ 1>`, `flex_max_nights: <int ≥ 1>`
(`flex_max_nights == flex_min_nights` for a fixed-length flexible window), and uses
`start_date` / `end_date` as the range bounds. Dates stay `DATE` strings (`YYYY-MM-DD`),
timezone-independent, verbatim. The app validates `flex_min_nights <= flex_max_nights` and a
window that fits the range; the monitor tolerates a bad pair by simply not firing.

### The match logic (`monitor.run`, provider-agnostic)

Layered on the shared availability shape **after** `extract_relevant` and **before** the
`state_hash` delta check, so both providers get it with no per-provider code
(`monitor.apply_flex_window` → `qualifying_nights`):

- **The gate is `flex_min_nights`.** A site's open nights are grouped into maximal runs of
  consecutive calendar days; a run shorter than `flex_min_nights` holds no window and is
  dropped, a run at least that long holds a `flex_min_nights`-length window covering every one
  of its nights, so all of them are kept. The reduced shape flows into the existing hash /
  `available_sites` / dedup / alert path unchanged.
- **`flex_max_nights` does not narrow the alert set**, by construction: a run long enough for
  the minimum already contains a minimum-length window covering each of its nights, and a
  longer allowed window can only add more. It is advisory for the app (how long a stay to
  offer). "Consider each valid length" reduces to "gate on the minimum."
- **Fixed is the degenerate case, kept byte-identical.** `apply_flex_window` returns the input
  **unchanged (same object)** for a fixed watch (and for a flexible watch with a one-night
  floor), so the hash, openings, and alert are identical to before Phase 16.
- **Write budget & containment unchanged.** The reduction is pure in-memory work inside the
  per-watch containment, adds **no** Supabase writes (`test_flexible_write_budget`), and runs
  before the delta check — so an unchanged qualifying window hashes the same and does not
  re-alert, while a newly-formed or grown one changes the hash and fires exactly once
  (`test_flexible_delta_does_not_re_alert_an_unchanged_window`).

### Build

- [x] `date_mode` / `flex_min_nights` / `flex_max_nights` in `supabase/schema.sql`, the
  idempotent `0006_watches_flexible_dates.sql`, and the three `WARN` entries in
  `preflight.REQUIRED` with the tolerance citation. **Apply `0006` by hand to the live DB
  BEFORE the app half (`phase16-flexible-dates-app`) ships**, then the manifest entry is
  already merged (WARN, so ordering is safe either way). *(`supabase/schema.sql`,
  `supabase/migrations/0006_watches_flexible_dates.sql`, `preflight.py` REQUIRED + citation.)*
- [x] `monitor.apply_flex_window` / `qualifying_nights` / `flex_min_nights`, hooked into
  `run` right after `extract_relevant`. Provider-agnostic; no provider file changed.

### Tests

- [x] Fires when **any** qualifying window opens; does **not** fire when no run reaches the
  floor; min/max variants; a stray out-of-window night is dropped while the run alerts.
  *(`tests/test_monitor_flex.py`.)*
- [x] Fixed watch byte-identical: `apply_flex_window` is identity for a fixed watch, and the
  existing fixed-watch suite is untouched. *(`test_apply_flex_window_is_identity_for_fixed_watch`
  + the whole pre-existing suite.)*
- [x] Write budget respected; delta detection prevents re-alerting an unchanged window.
  *(`test_flexible_write_budget`, `test_flexible_delta_does_not_re_alert_an_unchanged_window`.)*
- [x] Manifest/`schema.sql` agree and the flex columns are pinned `WARN`+`0006`.
  *(`test_preflight_manifest_matches_schema_sql`, `test_flexible_date_columns_are_warn_and_carry_0006`.)*

---

## Phase 17 (campassist-monitor backend) — AWS-triggered GitHub Actions (fix the scheduler drift)

**Path A is live: Phases A and B are done, Phase C (optional) is not.** The trigger code
(`trigger_lambda.py` + its offline tests) is in-repo, the AWS cloud resources are provisioned,
and the **exact-time trigger is firing in production**: the run history shows `workflow_dispatch`
runs landing at exactly `:00` and `:30` every 30 min (e.g. `2026-08-02T00:00:14Z`,
`23:30:15Z`, `23:00:14Z`, … — no gaps), which only the AWS Lambda can produce. The GitHub
`schedule` cron ran alongside it as an offset backstop through Phase B, but was made **dormant
on 2026-08-10** (commented out in `monitor.yml`): running both duplicated ~a quarter of polls and
GitHub's cron dropped runs under load, so EventBridge is now the **sole** trigger. Phase C —
thinning the `schedule` backstop to hourly — is therefore moot; re-enabling the cron is a one-line
uncomment. (Verify the live trigger anytime with `gh run list --workflow monitor.yml`; the AWS resources
themselves live in the captain's account and are not visible from a repo clone — their absence
from the clone is not evidence they are undeployed.)

**This is a reliability fix, not a cost fix, and the poll does not move.** The
`*/30 * * * *` Actions cron did not fire as configured: a 2026-07-31 measurement of the
20 most-recent scheduled runs found gaps of **1–3 hours** — ~12–13 runs/day, not the
intended 48 — because GitHub's `schedule` trigger is best-effort and is delayed or dropped
under load. Alerts therefore lagged openings by hours, and catching a cancellation fast is the
whole product. The fix, **now live**, is to stop firing the poll from GitHub's best-effort
`schedule`: an **AWS EventBridge Scheduler fires on the exact wall clock and calls
`monitor.yml`'s `workflow_dispatch`** (the `schedule` cron ran as an offset backstop through
Phase B, now dormant — see above),
while the poll itself keeps running on GitHub Actions — the one egress path proven to reach
GoingToCamp. **AWS never talks to GoingToCamp; it only tells GitHub when to run.**

**Sequencing and a numbering caveat.** This is **this repo's** backend Phase 17, distinct
from **camp-assist's Phase 17** (the GoingToCamp *jurisdiction expansion*). The two repos'
phase numbers diverge here; the heading carries a repo qualifier, the same disambiguation
"Phase 13a (backend half)" uses. Fixing the scheduler precedes the expansion — a drifting
trigger only gets worse as more parks are added. Bare "Phase 17" below always means **this**
phase; the expansion is only ever named "camp-assist's Phase 17."

### Approaches tried and rejected — why AWS triggers but GitHub polls

The monitor's home has been re-planned three times; each off-Actions target was abandoned on
egress. The record is kept **compressed** — not the three plans in full — because "trigger
from AWS, poll from GitHub" is a genuinely surprising design that only makes sense against
this history:

- **Render (a hosted cron/worker).** The first off-Actions plan; superseded before egress
  became the deciding evidence.
- **AWS Lambda running the poll itself.** Killed by **direct evidence**: two consecutive
  Lambda invocations from `us-west-2` returned **8 × HTTP 403 — every one a GoingToCamp
  park** (confirmed twice), while GitHub Actions polled the same campgrounds successfully in
  the same window (`alerts_sent: 138`, zero 403s).
- **Azure Container Apps Jobs running the poll itself.** The on-hypothesis successor
  ("GitHub's runners are on Azure, so an Azure job's egress should pass too") — but the
  **Phase 0 egress probe returned 403** from the actual Container Apps pool. Azure's
  *general* egress pool is not GitHub's *specific* published Azure subset, and the WAF
  refused it.

**The mechanism.** GoingToCamp sits behind **Azure Front Door** (confirmed by the
`x-azure-ref` response header), whose bot manager refuses source networks on Microsoft's
threat-intelligence / IP-reputation feeds. AWS's published egress ranges are heavily
represented on those feeds; Azure's general Container Apps pool was refused too. **GitHub's
hosted runners egress from a specific Azure range the WAF does *not* refuse** — an Azure
client reaching Azure Front Door — which is exactly why the incumbent poll works and why the
poll must not move off it. The two egress paths *proven* to reach GoingToCamp are **GitHub
Actions and a residential IP** — nothing else.

**So Path A keeps the proven egress and moves only the trigger.** The trigger path talks only
to `api.github.com`; the 403 that killed both migrations cannot apply to it. Full evidence:
`monitor-egress-options/report.md`, `monitor-serverless-15min/report.md`,
`monitor-azure-cost/report.md`, and the arch/cost report `monitor-aws-trigger-arch/report.md`
(all 2026-07-31).

### The architecture — Path A (Scheduler → Lambda → workflow_dispatch)

```
AWS account <AWS_ACCOUNT_ID>  (talks only to api.github.com — never to GoingToCamp)
  EventBridge Scheduler   cron */30, exact wall clock
      └─▶ Lambda (trigger hop, ~15 lines: read PAT, POST dispatch, log HTTP status)
              ├── reads the GitHub PAT from an SSM SecureString at invoke
              └── emits Invocations metric ─▶ CloudWatch alarm (Sum<1) ─▶ SNS email
                     │
                     ▼  POST /repos/wameson/campassist-monitor/actions/workflows/monitor.yml/dispatches
  GitHub Actions   runs monitor.yml   (the poll, unchanged)
      └─▶ polls GoingToCamp FROM GitHub's accepted Azure runner range (200 OK)
```

**Path B — a scheduled EventBridge *Rule → API destination* with no Lambda — was considered
and rejected.** Its token would live in an EventBridge **Connection → AWS Secrets Manager at
≈$0.40/secret/mo** (the single line item that would make the AWS side non-zero), and its
delivery is opaque (a `FailedInvocations` metric + a DLQ) — the wrong trade for a system
whose entire failure mode is *silent* missing. Path A's ~15-line Lambda logs every attempt
and GitHub's exact HTTP status, and keeps the token in **SSM (free)**.
(`monitor-aws-trigger-arch/report.md` §4.)

### Cost — the poll is now $0 (public repo); the AWS trigger is $0 forever

**The money decision was repo visibility, not AWS**, and it is settled: the repo went public on
2026-08-01, so the poll's Actions minutes are **$0**. The AWS trigger is $0 at every cadence, this
year and next — every service on the path sits inside a **permanent** free tier (EventBridge
Scheduler 14 M invocations/mo; Lambda 1 M req + 400,000 GB-s/mo; SSM Standard parameters;
CloudWatch 10 alarms + 5 GB logs/mo; SNS 1,000 emails/mo), and the trigger's usage
(≤ 2,922 invocations/mo, ~36 GB-s) rounds to $0 even at list price. **No ECR-style year-two
trap:** no container registry, no NAT gateway, no data-transfer line.

**Historical — the private-repo window (accepted-cost record).** Until the flip the only cost was
GitHub Actions minutes on the **private** repo, an **accepted ≈$7/mo interim** at 30-min cadence:

| Cost line | 30 min · private | 15 min · private | public (any cadence) |
|---|---:|---:|---:|
| GitHub Actions poll (`monitor.yml`) | **≈ $7/mo** | **≈ $27/mo** | **$0 (now)** |
| └ range across 2.0–3.0 billed-min/run | $6–$14 | $23–$41 | $0 |
| └ if the account is **Pro** (3,000 free min) | ≈ $1/mo | ≈ $21/mo | $0 |
| AWS trigger path | $0 | $0 | $0 |
| **Total (Free plan, measured)** | **≈ $7/mo** | **≈ $27/mo** | **$0** |

Figures used the **measured** average of **2.2 billed min/run** (n = 15 recent `monitor.yml`
runs: 94–125 s wall clock, mean ~109 s; GitHub bills **per job, rounded up to the whole minute**,
at the **verified $0.006/min** Linux rate — a correction from the stale $0.008 prior reports used)
over **1,461 runs/mo** at 30 min (30.44-day month). Two caveats sat behind those private figures:
the account's **Free-vs-Pro plan could not be read** from the worktree token (Pro's 3,000 free min
would have dropped the 30-min case to ≈$1); and the free-minute pool was **shared** across the
owner's private repos (~+$2/mo). The **public** column is a hard $0 — public repos get unlimited
standard-runner minutes — and is where the poll sits now. Source:
`monitor-aws-trigger-arch/report.md` §§1–3.

**The captain's decisions:** **30 minutes** — not 15 (during the private window it was ~$7 vs
~$27/mo, and 15 min still doubles request volume against a WAF already refusing our other egress);
and **flip the repo public after the backend implementation was complete**, which happened on
2026-08-01 and took the ~$7/mo to **$0**. The ~$7/mo was the **accepted interim cost** of the
private window, now closed.

### The GitHub token — scope, storage, blast radius

- **The auto `GITHUB_TOKEN` cannot do this** — by design it cannot trigger
  `workflow_dispatch`, so a real credential must live on the AWS side.
- **Minimal scope:** a **fine-grained PAT**, scoped to the **single repo**
  `wameson/campassist-monitor`, repository permission **Actions: Read and write** (what the
  "Create a workflow dispatch event" REST endpoint requires), with an **expiry** (e.g. 90
  days). **Not** a classic PAT — that needs the broad `repo` scope. *Caveat (report §8):* the
  endpoint may additionally want **Contents: Read** to resolve the ref; start with
  Actions:write only and add Contents:read only if the dispatch 403s.
- **Storage:** an **SSM Parameter Store SecureString** (KMS-encrypted with the free
  AWS-managed key), read by the Lambda at invoke. Rotation = update the SecureString in
  place; the Lambda picks up the new value on its next run, no redeploy.
- **Blast radius if it leaks:** start or cancel workflow runs in **this one repo** — nothing
  else. A fine-grained single-repo Actions:write PAT **cannot read code, read Actions
  secrets, push commits, or edit workflows.** The blast radius is "waste some minutes on one
  repo," not "exfiltrate the Supabase service key." A GitHub App installed on the one repo
  (auto-rotating installation tokens) is strictly better still and a fair future hardening,
  not required for launch.

### Duplicate-dispatch control — EventBridge is at-least-once

EventBridge Scheduler delivers **at least once**, and the monitor has **no run-level lock or
lease** (the cycle just selects `status=eq.monitoring` — no `FOR UPDATE`, no lease column). A
duplicate dispatch is **prevented**, and even an escaped one is harmless:

- **Prevent:** set the Scheduler schedule's **`MaximumRetryAttempts` to 0**, so a single tick
  never re-fires on its own retry path (a missed tick is covered by the next tick and the
  `schedule` backstop; aggressive retry only buys duplicate minutes). The Lambda POSTs exactly
  once per invoke.
- **Contain (belt-and-suspenders):** `monitor.yml` already declares
  `concurrency: { group: monitor, cancel-in-progress: false }`. Because **both** the AWS
  trigger and the `schedule` backstop fire the **same workflow on the same host**, that one
  concurrency group serializes **every** run regardless of which trigger started it — a
  duplicate queues behind the in-flight run and starts only after it commits. **No lock is
  needed:** a cycle is capped at `CYCLE_TIME_BUDGET_SECONDS` (480 s) + ≤20 s jitter ≈ 500 s,
  comfortably inside the 1,800 s (30 min) gap, so runs never overlap. A no-change cycle also
  **writes nothing** (the ≤5-write budget / hash compare), and real re-alerts are deduped by
  `sent_alerts`, so the worst case of a double-fire is a few wasted Actions minutes, never bad
  data.

### Failure visibility — the silent-miss class, designed for explicitly

**If the trigger silently stops firing, the monitor silently stops — and no Actions failure
email is ever sent, because no run happens.** This is the same silent-miss class as the
budget-exhaustion bug: a normal Actions failure email covers a run that *runs and fails*, not
a *trigger that never fires*. Three complementary layers, all within free tiers:

1. ~~**Keep `schedule` as a low-frequency backstop.**~~ **Retired 2026-08-10.** The `schedule`
   cron was made dormant (commented out in `monitor.yml`) because running it alongside the
   EventBridge trigger duplicated ~a quarter of polls and GitHub's cron dropped runs under
   load. If the AWS trigger dies the monitor now degrades to **zero**, not to best-effort
   cadence — so silent-miss coverage rests on layers 2 and 3 below. Re-enabling this backstop
   is a one-line uncomment of the `schedule:` block.
2. **AWS heartbeat alarm.** A CloudWatch alarm on the trigger Lambda's `Invocations` metric —
   `Sum < 1` over a window (e.g. 45 min), **treat-missing-data = breaching** → SNS email.
   Catches "Scheduler stopped invoking." One of the 10 always-free alarms.
3. **Best detector — `run_summaries.ran_at` freshness.** The monitor already writes a
   world-readable `ran_at` every cycle. A staleness check ("no successful run in > X min")
   catches **every** trigger-failure cause at once — Scheduler, Lambda, token expiry, GitHub
   outage — regardless of *why* the trigger stopped. This belongs in the iOS operator view
   and/or a tiny external check, and is the single most robust silent-miss guard.

### What changes in the repo — almost nothing — and dispositions

- **`monitor.yml` needs no edit to function.** It **already** declares `workflow_dispatch`
  and the `concurrency` guard. The `schedule` trigger **stays** as the offset backstop (do
  not remove it — it is failure-visibility layer 1); the AWS trigger is added *alongside* it,
  not in place of it. No code, no schema, no secret-contract change. The SSRF posture is
  intact — the trigger's only URL is the hardcoded `api.github.com` dispatch endpoint;
  nothing is derived from any client-writable value.
- **`keepalive.yml` stays.** It exists to stop GitHub auto-disabling the *scheduled* workflow
  after 60 idle days, and the `schedule` backstop is retained, so it is still needed. (It
  would become removable only if `schedule` were ever dropped entirely; `workflow_dispatch`
  is not subject to the 60-day rule.)
- **The dead poll-in-Lambda artifacts have been removed** (the follow-up cleanup this section
  anticipated). They were the shim for running the *poll itself* inside AWS Lambda — the plan
  the 8×403 killed — and comprised `lambda_function.py` (the SSM→env / `SystemExit`→invocation-
  error shim), `tests/test_lambda_function.py`, the `make lambda-zip` `Makefile`, and
  `deploy.yml` (OIDC build-and-push of the Lambda zip → a scoped `lambda:UpdateFunctionCode`
  role). Path A runs a **new, unrelated ~15-line trigger Lambda** (`trigger_lambda.py`,
  deployed operator-side, not built from this shim), and the poll never moves to AWS, so
  nothing in the repo ever referenced any of them. `deploy.yml` had no successor to become —
  the trigger Lambda is ~15 lines the operator deploys once from the console/CLI, needing no
  per-merge deploy workflow. The refusal history is preserved above (§ "Approaches tried and
  rejected"); only the dead code is gone.

### The AWS resources the captain already built

The captain provisioned AWS resources for the dead Lambda-*polling* plan. Against Path A:

| Resource | Reusable for Path A? | Note |
|---|---|---|
| **Execution role** | **reusable, re-scoped** | the trigger Lambda needs only SSM `GetParameter` (the PAT) + CloudWatch Logs — narrower than the polling role |
| **SSM parameters** (the 6 monitor secrets: Supabase + APNs) | **orphaned** | the trigger polls nothing and needs none of them; Path A needs **one new** SSM SecureString: the GitHub PAT |
| **Lambda function** | **shell reusable, code replaced** | the polling handler is discarded; the ~15-line trigger handler takes its place |
| **CloudWatch alarm** | **reusable, re-pointed** | from the polling Lambda's `Errors` metric to the trigger Lambda's `Invocations` heartbeat (§ Failure visibility) |
| **SNS topic + email subscription** | **reusable as-is** | same "email the operator" purpose |
| **EventBridge schedule** | **reusable, re-targeted** | now targets the trigger Lambda; set `MaximumRetryAttempts = 0` |

Any resource left orphaned costs nothing (all inside permanent free tiers) and can be deleted
at leisure.

### Cutover and rollback — and why the double-alert risk does *not* apply here

The earlier Azure plan carried a sharp **double-alert** warning: two schedulers (GitHub cron
+ Azure job) on **two separate hosts**, no shared lock, so a genuinely concurrent overlap
could make both runs send the same push — which is why that plan needed a staggered
parallel-proof window. **That risk does not apply to Path A**, and the reason is structural:
the AWS trigger and the `schedule` backstop both fire the **same `monitor.yml` on the same
GitHub host**, where `concurrency: group: monitor` serializes **all** runs regardless of
trigger source. Two triggers can never produce two *concurrent* runs — the later one queues
and starts only after the first commits, at which point the committed `state_hash` /
`sent_alerts` rows dedup it correctly. A single execution host with a concurrency group is
exactly what the Azure two-host plan lacked. So Path A needs **no staggered parallel-proof
window**; the trigger can be added while `schedule` keeps running, with the concurrency guard
doing the serialization for free.

**Rollback is instant and zero-risk:** disable or delete the EventBridge schedule, then
uncomment the `schedule:` block in `monitor.yml` (dormant since 2026-08-10) so the GitHub cron
resumes as the sole trigger. There is no data migration; the only gap is between the two steps,
which is why the cron is kept as a one-line uncomment rather than deleted.

### Ordered implementation steps

**Phase A — stand up the trigger (no cutover): DONE.**

- [x] Mint the fine-grained PAT (single repo, Actions: Read and write, 90-day expiry) and
  store it as an SSM SecureString. *(Necessarily done — the live Lambda reads it from SSM and
  dispatches successfully.)*
- [x] Create the trigger Lambda (**code already in-repo: `trigger_lambda.py`, handler
  `trigger_lambda.handler`** — read the PAT from SSM, `POST …/monitor.yml/dispatches`, log the
  HTTP status; deploy as a single file, no wheel build), its re-scoped execution role, the
  CloudWatch `Invocations` heartbeat alarm, and the SNS email (confirm the subscription).
  *(Deployed in the captain's AWS account — the run history proves the Lambda + role + PAT path;
  whether the alarm actually **fires** on a disabled schedule is the separate check under Phase B
  step 2 / Validate, still open.)*
- [x] Invoke the Lambda manually once; confirm it returns GitHub's `204` and a `monitor.yml`
  run appears. This exercises the PAT, the SSM read, and the dispatch path end-to-end. *(Proven by
  the standing `workflow_dispatch` runs in the history — dispatches at exactly `:00`/`:30`.)*

**Phase B — turn on the exact-time trigger alongside the backstop: DONE (per captain; drift-fix
verified from run history).**

- [x] Attach the EventBridge schedule at **`*/30`** (exact wall clock),
  `MaximumRetryAttempts = 0`, and **leave `monitor.yml`'s `schedule` cron running** as the
  offset backstop. The `concurrency` guard serializes the two sources; **no stagger is
  required** (see above). *(Verified: `workflow_dispatch` runs land at exactly `:00`/`:30` while
  the `schedule` cron still fires its own drifty runs alongside — both triggers running by design.)*
- [ ] Run for several days; confirm from `run_summaries.ran_at` that runs now land on the
  exact minute (no 1–3 h drift), and that the heartbeat alarm and email fire when the schedule
  is briefly disabled. *(The exact-minute half is confirmed from the run history; the
  heartbeat-alarm-fires-on-disable half is the one Phase-B check not yet evidenced here — see
  Validate gate 3. Left unticked for that half alone.)*

**Phase C — thin the backstop (optional): NOT done — deliberately, the cron stays at `*/30`.**

- [ ] Once the AWS trigger is proven, optionally thin the `schedule` cron to hourly (keeping
  it as a backstop, **not** removing it — it is the trigger-down safety net). Do **not** drop
  `schedule` entirely: that also makes `keepalive.yml` load-bearing to remove and gives up
  layer 1 of failure visibility.

**Phase D — flip the repository public (the $0 step — gated on a secret scan): DONE 2026-08-01.**

*Done after the backend implementation completed (captain, 2026-07-31).* The full-history
secret scan came back clean and the repository is now public, which took the running cost from
~$7/mo to **$0** and made cadence a free knob again.

- [x] **Full git-history secret scan FIRST.** Making a repository public exposes its **entire
  history, not just its current state** — a secret that was ever committed and later removed
  stays readable in old commits. Done 2026-07-31: `gitleaks git --log-opts=--all` came back
  **clean at 108 commits** (the AWS account ID in `6090b18b`/`970ceee6` is captain-accepted,
  not a credential, and unflagged). gitleaks is now the committed tool and also gates **every
  PR** on its `base..head` — see README "Secret scanning". Re-run over any commits added since,
  right before the flip.
- [x] **Any hit is a blocker requiring credential rotation, not merely removal from history.**
  Rewriting history does not help once a commit has been fetched, cloned, or indexed; the
  exposed credential (the Supabase service key, the APNs `.p8`, any PAT) must be **rotated**.
  This rule now governs **every future commit**, not just the flip: the history is public, so
  any secret that ever lands in a commit must be rotated, never merely removed.
- [x] **The flip is irreversible.** The repository is public and its history is now readable to
  anyone who clones or indexes it. Done after confirming the scan was clean.
- [x] Flip visibility. **Done 2026-08-01** — the repository is public. Actions minutes are now
  free at any cadence; 15-min polling is revisited on its merits (§ Cadence), no longer on price.

### Cadence — 30 min, now a free knob (repo is public)

**30 minutes** (captain, 2026-07-31): during the private window it was ~$7/mo vs ~$27 at 15 min,
and 15 min doubles daily request volume against GoingToCamp (~1,900 → ~3,800/day) — low
absolute volume, but it spends politeness margin on a WAF already refusing one of our egress
paths. 15 min does **not** raise per-cycle capacity (a cycle is still capped at the 480 s
budget, ~40 parks, and simply repeats twice as often — past ~40 parks the fix is sharding,
orthogonal to cadence). Now that the repo is public (Phase D, done 2026-08-01), 15 min costs
the same $0 as 30 min, so cadence is a **latency/politeness** call, not a money one. **30 min
stands** (captain): cycle time, not money, is the ceiling. Source:
`monitor-aws-trigger-arch/report.md` §6.

### The scaling ceiling is unchanged by this phase

The poll host does **not** change, so nothing here moves the ~40-parks-per-cycle ceiling: the
governor is still the in-code `CYCLE_TIME_BUDGET_SECONDS` (480 s) against GitHub Actions'
`timeout-minutes: 15` (900 s), exactly as today. Broad coverage still needs an architectural
change (shard by host, tiered cadence, per-host async), owned by the cycle-budget
re-derivation (`monitor-cycle-budget-rederive`) and camp-assist's Phase 17 — not this trigger
work.

### Build

- [x] Trigger Lambda **code** (`trigger_lambda.py` at repo root; ~15 lines: SSM
  `GetParameter` for the PAT, `POST …/monitor.yml/dispatches`, log the HTTP status, 204→success
  else raise). Stdlib-only (`urllib` + runtime `boto3`), so it deploys as a single file with no
  bundled dependency; `requirements.txt`, `monitor.py`, and the whole poll pipeline are
  **untouched**. (PR: repo half of Phase A.)
- [x] Deploy that code as a Lambda + re-scoped execution role (`ssm:GetParameter` on the one
  PAT parameter + Logs) — **operator stand-up**, no further repo change. *(Live in the captain's
  AWS account — proven by the standing `workflow_dispatch` runs.)*
- [x] EventBridge schedule (`*/30`, exact wall clock, `MaximumRetryAttempts = 0`) targeting
  the Lambda; SSM SecureString holding the fine-grained PAT; CloudWatch `Invocations`
  heartbeat alarm; SNS topic + confirmed email. *(Schedule + SSM + Lambda proven live from the
  run history; the alarm/SNS are provisioned as Phase-A deliverables — the separate check that the
  alarm **fires** on a disabled schedule is Validate gate 3, still open.)*
- [x] `monitor.yml`: **no change required** — `workflow_dispatch` + `concurrency` already
  present (verified); `schedule` retained as the offset backstop.

### Tests

- [x] The offline suite (`pytest`) stays fully offline — no business logic moves, so its
  coverage of `monitor.py` / providers / budgets / containment / exit-status carries over
  as-is. Phase A **adds** `tests/test_trigger_lambda.py` (fully offline: fake SSM + fake HTTP
  poster; covers 204→success, non-204→raise, and token-never-logged); no existing test changed.
- [x] The dormant AWS shim (`lambda_function.py`) and its tests
  (`tests/test_lambda_function.py`) have been **removed** in the post-Path-A cleanup, along
  with `make lambda-zip` and `deploy.yml`; the offline suite stays green without them (the
  9 shim tests are the only drop).
- [ ] Live gates are manual, not offline tests: the Phase A manual invoke (`204` + a real
  run) and the Phase B on-time-run confirmation are both **now confirmed** from the live run
  history; only the heartbeat-alarm firing test remains open. The suite makes no cloud calls.

### Validate (operator gates)

- [x] Manual Lambda invoke returns GitHub `204` and a `monitor.yml` run appears. *(Confirmed by
  the standing `workflow_dispatch` runs the AWS trigger produces — visible via
  `gh run list --workflow monitor.yml`.)*
- [x] After Phase B, `run_summaries.ran_at` shows runs on the exact minute, no 1–3 h drift.
  *(The `workflow_dispatch` runs fire at exactly `:00`/`:30`, e.g. `2026-08-02T00:00:14Z`,
  `23:30:15Z`, `23:00:14Z` — the drift is fixed. The drifty `schedule` runs remain as the
  backstop.)*
- [ ] Disabling the EventBridge schedule fires the heartbeat alarm and the SNS email within
  the alarm window *(the one still-open Phase-B gate — the alarm's fire-on-disable behaviour is
  not evidenced by the run history and needs the live AWS console; not exercisable from a clone)*.
- [x] Phase D: the full-history secret scan is clean (or all hits rotated) before the
  irreversible public flip *(operator judgement — the gate is a blocker, not a test)*. Clean
  at 108 commits on 2026-07-31; re-run over any newer commits at flip time.

---

## Backend bugs and incidents

| Date | Issue | Status |
|---|---|---|
| 2026-07-19 | Drift incident 1: `consecutive_not_found` in `schema.sql` but not the live DB → every `watches` PATCH 400ed, multi-day outage | fixed by hand; motivated Phase 12 |
| 2026-07-25 | Drift incident 2: `provider` / `provider_ref` unapplied — monitor green, iOS app could not save any watch | motivated the "check what the *product* needs" manifest rule |
| 2026-07-27 | **Every GoingToCamp watch permanently errored.** The client packed `gtc:<rlid>:<mapId>`; the monitor has validated `campground_id` against `[A-Za-z0-9_-]+` since 2026-07-08, and a colon is not in that class. `status='error'` is terminal and silent, so all three watches sat dead behind green runs with `errors: null` for two days. The colon "convention" came from a research proposal never reconciled against the validation rule the backend already had. | fixed **client-side** — the regex was deliberately **not** loosened (see the format contract). Motivated the errored-watch census + `error_reason` (PR #12). |

---

## Backend decisions (confirmed)

| Decision | Choice | Why |
|---|---|---|
| Polling cadence | 30 min (captain, 2026-07-31); the poll stays on GitHub Actions | during the private window 15 min was ≈$27/mo vs ≈$7 at 30 and doubled request volume against the WAF; now the repo is public (2026-08-01) any cadence is $0, so 30 min stands on cycle-time/latency, not price — cycle time, not money, is the ceiling |
| Hosting / trigger | **Path A — AWS EventBridge Scheduler → ~15-line Lambda → GitHub `workflow_dispatch`** (captain, 2026-07-31); the poll keeps running on **GitHub Actions**, only the trigger moves — see Phase 17. **Supersedes two abandoned poll-migration plans**: AWS Lambda running the poll (8×403 from GoingToCamp's Azure Front Door WAF) and Azure Container Apps Jobs (Phase 0 probe 403) | GitHub's `schedule` cron drifts 1–3 h, which only a real scheduler fixes; the WAF refuses AWS and general-Azure egress but not GitHub's runner range, so the poll must stay on Actions; the AWS trigger never touches GoingToCamp, so its 403 risk is nil |
| Start jitter | 20 s, cut from 240 s (captain, 2026-07-28) | 240 s was 92% of the billed minutes and bought nothing under `schedule`, which already spreads delivery uniformly; ≤20 s stayed inside the Actions 1-minute billing floor and keeps the desync for the exact-wall-clock AWS trigger now live in Phase 17 — so it is **not** dropped |
| Repo visibility | **Public since 2026-08-01** (captain, 2026-07-31), flipped after the backend implementation completed and a clean full-history secret scan | Actions minutes are now **$0** (the ≈$7/mo at 30 min was the accepted interim during the private-repo window). Irreversible once cloned/indexed, so any historical secret must be rotated, not merely removed — a rule that now governs every future commit; see Phase 17 Phase D. A self-hosted **residential** runner survives only as a fallback if GitHub's own egress is ever refused |
| Alerting (v1) | APNs push with a direct booking link — nothing else | free programmatic SMS no longer exists; carrier email gateways are defunct |
| DB writes | Delta-only via `state_hash`, one summary row/cycle, 30-day pruning | naive per-watch writing blew the free tier ~6× |
| Watch expiry | Backend-owned (`status='expired'`) | client-side expiry cannot be trusted to run |
| Migrations | Manual, by hand, apply-before-merge; CI never applies | see the ordering rule — a `HALT` entry ahead of the apply is a monitoring outage |
| Drift guard | Detect only, red-run-only alerting, conservative-default classification | a false halt is recoverable; a missed halt is a silent outage |
| `campground_id` regex | Not loosened for GoingToCamp | it constrains a client-writable value that reaches outbound request construction |
| GoingToCamp posture | Keyless GET + one read-only pricing POST; never drive a browser | `/api/*` is open, the SPA is WAF captcha-gated, and browsers are what trip it |
| ADA-only filtering | GoingToCamp only, per-watch opt-in, default off | rec.gov publishes only a wider "accessible" flag that would hide ~2.75 bookable sites per restricted one |
| Re-arming errored watches | Deliberately not automatic (captain, 2026-07-27) | reason data first; a blanket retry re-polls known-dead watches forever |
| Flexible-date range bounds | Reuse `start_date`/`end_date`, add only `date_mode` + nights (Phase 16) | avoids provider special-casing (poll horizon / `poll_plan` / `extract_relevant` already cover the range) and keeps fixed watches byte-identical; separate window columns would duplicate the already-`NOT NULL` bounds |
| Flexible-date alert gate | `flex_min_nights` only; `flex_max_nights` advisory | a run long enough for the minimum already contains a min-length window covering every night, so the maximum can only add availability, never suppress it |
| Multi-campground per watch | Not in v1 | — |

---

## Known limitations (backend)

| Limitation | Impact | Mitigation |
|---|---|---|
| ~~GitHub `schedule` drift~~ (resolved) | The `schedule` cron drifted **1–3 h** (measured 2026-07-31), far past the intended 30 min | **resolved by Phase 17** — the AWS EventBridge Scheduler now fires `workflow_dispatch` on the exact minute (verified: dispatches land at exactly `:00`/`:30`); the poll stays on Actions. The drifty `schedule` cron is kept alongside as the offset backstop (Phase C thinning it is optional and not done) |
| ~~Private-repo minute budget~~ (resolved) | Measured ~2.2 billed min/run → the monitor alone ran ~3,214 min/mo, over the 2,000 free tier | **resolved by the public flip (2026-08-01)** — standard-runner minutes are now free at any cadence; the ≈$7/mo at 30 min was the accepted interim cost of the private window |
| Unofficial provider APIs | Could change, break, or block | defensive parsing, captured fixtures, jitter, backoff, residential-IP fallback |
| GoingToCamp's Azure Front Door WAF blocks source networks by IP reputation | **Confirmed twice** — 8×403 from AWS Lambda and a 403 from the Azure Container Apps probe (both 2026-07-31), which killed both poll-migration plans. GitHub's Azure runner range is accepted, which is why the poll stays there; the AWS trigger never sends a packet to GoingToCamp, so it is unaffected | keep to plain `httpx` GET + browser UA + pacing, never a browser; the provider seam contains the blast radius to GTC; the residential-runner fallback stays available if the runner range is ever refused |
| Supabase free tier pauses after 7 idle days | n/a — the cron hits it every 30 min | inherent keep-alive |
| Constraint drift is invisible to the preflight | `UNIQUE(watch_id, site_id, date)` unverified | out of scope; both incidents to date were missing columns |

## Backlog (backend)

- 15-min polling — free now the repo is public (2026-08-01); it was deferred during the private window (≈$27/mo vs ≈$7 at 30 min). Now a pure latency/politeness call, not a cost one, and **30 min still stands** (captain: cycle time, not money, is the ceiling — § Cadence). The AWS exact-cron trigger is already live, so enabling 15 min needs no scheduler work — just widening the EventBridge cadence
- A retry policy for errored watches, classified on `error_reason` (data first — see Decisions)
- Moving past-date errored rows to `expired` in the expiry pass (tidier census, but it costs
  writes and mixes two concerns)
- Caching the GoingToCamp park→child-map structure (Phase 8, still open)
