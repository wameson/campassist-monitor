# CampAssist Monitor — Backend Plan

**This is the backend plan.** It covers the Supabase schema, the GitHub Actions monitor
workflow, the poll/alert pipeline, the provider seam, and the backend implementation phases.
It is the authoritative sub-plan for everything in this repo.

**The iOS/product plan lives in the `camp-assist` repo's `PLAN.md`** — app architecture,
screens, the Add-Watch wizard, design system, and the iOS halves of the shared phases.
Anything that crosses both repos (a schema column, a `campground_id` format, a provider
contract) is specified here and referenced there.

`README.md` in this repo is the operator-facing summary of what has shipped; `CLAUDE.md` holds
the sharp-edge notes. This file is the design and the sequencing.

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
campassist-monitor (GitHub, PRIVATE)
  .github/workflows/monitor.yml   cron */30 + workflow_dispatch, timeout-minutes: 15
    ├── preflight: read-only schema-drift probe (before the jitter)
    ├── start jitter: sleep rand(0–240s)
    ├── read status=eq.monitoring watches + errored-watch census
    ├── expire past-end_date watches
    ├── plan PollUnits (provider, PollKey) — deduped cross-user, per provider
    ├── poll via each watch's Provider conformer — paced, backed off, time-budgeted
    ├── per watch: extract_relevant → state_hash → delta → alert dedup/cooldown → APNs
    ├── lifecycle writes (error/strike), batched last_checked_at PATCH
    └── run_summaries row + 30-day retention prune
  .github/workflows/keepalive.yml  monthly commit (GitHub disables crons after 60 idle days)
  .github/workflows/ci.yml         pytest on PR + push to main (ubuntu)
        ▲ ▼
  Supabase (free): watches · device_tokens · sent_alerts · run_summaries
        ▲ ▼
  CampAssist iOS app (camp-assist repo) — anonymous sign-in, RLS-scoped
```

### GitHub Actions minutes budget (private repo, 2,000 free min/month)

| Item | Consumption |
|---|---|
| Monitor cron every 30 min (~1 min/run, billed rounded up) | ~1,440 min/mo |
| Backend pytest CI (ubuntu 1×, ~2 min/PR) | ~40 min/mo |
| iOS unit tests on merge to main (macOS **10×**) | ~320 min/mo |
| **Total** | **~1,800 / 2,000** |

**Why 30 min and not 15:** the budget above. Escape hatches when it is hit, in order:
(1) **self-hosted runner** on an always-on home machine — unlimited free minutes on private
repos *and* a residential IP a provider won't flag; (2) make the repo public.

### Stack

| Layer | Choice | Why |
|---|---|---|
| Scheduling | Actions cron `*/30 * * * *` + keep-alive | fits private-repo free tier |
| Language | Python 3.12 | available in Actions, no build step |
| HTTP | `httpx[http2]` | HTTP/2 is required for APNs |
| APNs auth | `PyJWT` + `cryptography` | ES256 JWT signed with the `.p8` key |
| DB | Supabase REST, service-role key | free tier; bypasses RLS server-side |
| Secrets | GitHub Actions Secrets | `.p8` key, Supabase service key |

**Runtime dependencies are capped at those three.** Anything needing a fourth needs a
decision, not a `pip install` — the cap is what keeps the monitor's failure surface small and
is why the preflight manifest is a dict rather than a SQL parser.

---

## Supabase schema

`supabase/schema.sql` is the authoritative DDL and the **fresh-install bootstrap only**. The
shape, abridged:

| Table | Columns | Notes |
|---|---|---|
| `watches` | `id`, `user_id`, `provider`, `provider_ref`, `campground_id`, `campground_name`, `campground_state`, `site_ids`, `include_ada_only`, `start_date`, `end_date`, `status`, `error_reason`, `state_hash`, `consecutive_not_found`, `created_at`, `last_checked_at`, `last_found_at` | `status ∈ monitoring/paused/expired/error`; `site_ids` empty = any site |
| `device_tokens` | `user_id` (PK), `apns_token`, `environment`, `updated_at` | `environment ∈ production/sandbox` — per-token APNs host routing |
| `sent_alerts` | `id`, `watch_id`, `site_id`, `date`, `sent_at` | `UNIQUE(watch_id, site_id, date)` — the dedup key |
| `run_summaries` | `id`, `ran_at`, `watches_checked`, `campgrounds_polled`, `alerts_sent`, `duration_ms`, `errors` | **world-readable** (`USING (true)`) — see Rendering channels |

RLS is on for all four; the app reads its own rows via `auth.uid()`, the backend writes with
the service-role key.

### Migrations

| File | Adds | Preflight class |
|---|---|---|
| `0001_watches_consecutive_not_found.sql` | `consecutive_not_found` | `HALT` (monitor writes it) |
| `0002_watches_provider.sql` | `provider`, `provider_ref` | `WARN` (read-tolerant / app-only) |
| `0003_watches_include_ada_only.sql` | `include_ada_only` | `WARN` (read via `.get`) |
| `0004_watches_error_reason.sql` | `error_reason` | `WARN` (write proves tolerance) |

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
Random 0–240 s start delay per run; 1.2–2.8 s inter-request delays; randomized poll order;
one realistic browser User-Agent per run, rotated across runs; exponential backoff on
403/429/5xx (2 → 4 → 8 s, then skip that unit this cycle). Escalation path is the self-hosted
runner (residential IP).

### Time budget
Polling stops once an 8-minute per-cycle budget is spent, keeping every run — even under
sustained blocking — inside the workflow's 15-minute timeout. Skipped units retry next cycle;
skipped watches keep their old `last_checked_at`; the summary counts only what was polled.

### Poll horizon
Every provider clamps a watch's request to today → today + 12 months. "Today" uses a fixed
UTC-8 offset so same-night US openings stay alertable during US evening hours after UTC
midnight. A watch straddling the horizon is served for its in-horizon nights alone.

### Deduplication impact

| Users | Unique campgrounds | Requests/cycle | Runtime incl. jitter |
|---|---|---|---|
| 100 | 20 | ~20 | ~1 min |
| 1,000 | 80 | ~80 | ~3–4 min |
| 10,000 | 200 | ~200 | ~7–9 min |

### Alert delivery
APNs HTTP/2 with an ES256 JWT (`iss`=team, `kid`=key, cached ~50 min), `apns-push-type: alert`,
`apns-priority: 10`, `apns-topic`=bundle id, host routed by `device_tokens.environment`.
A `410` deletes the dead token row.

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

A watch naming a provider this build does not register is **skipped untouched** — never
errored — so an older monitor cannot mis-serve a row a newer client wrote. An *unknown*
provider is refused rather than defaulted; a **missing/null** `provider` defaults to
`recreation_gov`.

Booking links: recreation.gov → `https://www.recreation.gov/camping/campsites/{campsite_id}`;
GoingToCamp → `/create-booking/results?mapId=…&resourceLocationId=…&startDate=…&endDate=…`
(the SPA takes no site preselect).

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
  and never past `FANOUT_DEADLINE_SECONDS` (600 s) **from process start**, so the up-to-240 s
  jitter counts against it rather than stacking on top. The arithmetic closes against
  `timeout-minutes: 15` (900 s): 120 s setup + 600 s deadline + 180 s shutdown reserve. A
  worst-case run gets no fan-out at all — reaching the summary row and the pruning matters more
  than isolating one row.

### Exit status

| Verdict | Exit | Rule |
|---|---|---|
| *isolated* | 0, schedule green | the healthy watches were served |
| *systemic* | non-zero + `::error::`, schedule red | more than `SYSTEMIC_ERROR_RATE` (25%) of **served** watches failed **and** at least `SYSTEMIC_ERROR_FLOOR` (2) did — so 1-of-2 stays green, 2-of-2 goes red — **or** a failure belonging to no watch occurred |

Failures belonging to no watch: the `run_summaries` INSERT, retention pruning, being unable to
write `status='error'`, a provider raising out of a shared poll unit, a pool-wide unpollable
condition.

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
- [x] `monitor.yml` (30-min cron + `workflow_dispatch`), `keepalive.yml`, `ci.yml` (pytest on PR)
- [x] All 6 GitHub Secrets configured; setup documented in README

**Tests (pytest, all must pass in CI):**
- [x] `test_dedupe_poll_plan` — 3 watches same campground+month → 1 poll entry; different months → separate
- [x] `test_delta_detection` — unchanged availability → no alert; new opening → alert
- [x] `test_alert_cooldown` — (site,date) alerted <6 h ago → suppressed; >6 h → re-alerted
- [x] `test_expiry` — watch past `end_date` → marked expired, excluded from polling
- [x] `test_jitter_bounds` — inter-request delays within [1.2, 2.8]s; start delay within [0, 240]s
- [x] `test_apns_jwt` — ES256, correct `iss`/`kid`; cached within run, refreshed after 50 min
- [x] `test_apns_410_cleanup` — 410 → token row deleted
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
- [x] Park-metadata fetch in `scripts/providers/going_to_camp.py`: `GET https://{HOST}/api/resourcelocation/resources?resourceLocationId=<id>`, host from the module constant, id via `_identifier`, and — unlike `fetch_map` — **one paced attempt with no backoff**, because the catalog is cosmetic and a dead endpoint must not spend the time budget the availability polls need (see the network-posture section above). A failure fails open to no labels rather than failing the poll unit. *(`going_to_camp.py:54` `RESOURCES_URL`, `:867-899` `site_metadata`, `:760-819` `_fetch_json`'s "one attempt, no backoff" contract, `:175` `_identifier`.)*
- [x] **One fetch per distinct park per cycle**, deduplicated within the cycle. **No persistent cache and no TTL** — the monitor is a `*/30` cron and every cycle is a fresh process, so there is nothing to carry over and nothing to go stale. On failure the cycle has no marker set: filter nothing, labels fall back to resourceIds. Do not introduce cross-process storage; the zero-new-writes property depends on its absence. *(`going_to_camp.py:820-829` — two process-lifetime caches, a failed fetch cached as `{}`; `:885-899`.)*
- [x] Resolve the `ADA Only` attribute id rather than trusting the magic negative int: read `-32759` back from `/api/attribute/filterable` by `displayName` and pin it in a fixture assertion, so a renumbering surfaces as a **test failure** rather than a filter that silently stops filtering. *(`going_to_camp.py:99` `ADA_ONLY_DEF`, `:675` resolved through `_yes_no(vocabulary, …)` never a cast; pinned by `test_the_pinned_attribute_ids_still_mean_what_this_build_says`.)*
- [x] Each resource is tagged with `ada_only` and its `"site"` set to the real label from `localizedValues[].name`, replacing the resourceId fallback. Absent metadata ⇒ no `ada_only` key and the resourceId fallback — today's behavior exactly. *(Shipped in `apply_site_metadata` (`going_to_camp.py:491-536`) rather than in `parse_map` as predicted, which keeps the availability parse independent of the catalog; `:531-532` sets `ada_only` **only** where the platform published it, so absent reads unknown rather than `False`.)*
- [x] `extract_relevant` skips `ada_only` sites **after** the `wanted` site-id match and **before** `state_hash`, unless `bool(watch.get("include_ada_only"))`. Missing key reads `false`. *(`going_to_camp.py:1071` and `:1099`.)*
- [x] `scripts/providers/recreation_gov.py`: **no filtering**, with the decision cited in a comment at `extract_relevant` so the asymmetry is deliberate and documented. *(`recreation_gov.py:203-214`.)*
- [x] `supabase/migrations/0003_watches_include_ada_only.sql` (idempotent, backfill-free) + the column in `supabase/schema.sql` + the `("include_ada_only", (WARN, "0003_…"))` entry in `preflight.REQUIRED`, with the tolerance citation. **Apply the migration by hand first, then merge the manifest entry.** *(`supabase/migrations/0003_watches_include_ada_only.sql:28`, `supabase/schema.sql:28`, `preflight.py:102` with the citation at `:148`.)*
- [ ] Charge the metadata GET against the existing pacing/time budget in the arithmetic comment, as Phase 12 did for its probes. *(Still open, and deliberately left unticked: the runtime half ships — `going_to_camp.py:793-796` short-circuits on `budget_exhausted()` and paces with `CHILD_MAP_DELAY_SECONDS` — but the accounting half does not. `monitor.py`'s budget arithmetic (`:77-79` and `:164-192`) still enumerates only the poll requests and the Phase 12 preflight probes, never the per-park catalog GET, and this box is the only record of that gap.)*

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
| Polling cadence | Centralized Actions cron, private repo, every 30 min with jitter | 2,000-min free tier; jitter desynchronizes from the cron tick |
| Scale-up path | Self-hosted runner (unlimited free, residential IP) or public repo | avoids paying, and a residential IP is less likely to be flagged |
| Alerting (v1) | APNs push with a direct booking link — nothing else | free programmatic SMS no longer exists; carrier email gateways are defunct |
| DB writes | Delta-only via `state_hash`, one summary row/cycle, 30-day pruning | naive per-watch writing blew the free tier ~6× |
| Watch expiry | Backend-owned (`status='expired'`) | client-side expiry cannot be trusted to run |
| Migrations | Manual, by hand, apply-before-merge; CI never applies | see the ordering rule — a `HALT` entry ahead of the apply is a monitoring outage |
| Drift guard | Detect only, red-run-only alerting, conservative-default classification | a false halt is recoverable; a missed halt is a silent outage |
| `campground_id` regex | Not loosened for GoingToCamp | it constrains a client-writable value that reaches outbound request construction |
| GoingToCamp posture | Keyless GET + one read-only pricing POST; never drive a browser | `/api/*` is open, the SPA is WAF captcha-gated, and browsers are what trip it |
| ADA-only filtering | GoingToCamp only, per-watch opt-in, default off | rec.gov publishes only a wider "accessible" flag that would hide ~2.75 bookable sites per restricted one |
| Re-arming errored watches | Deliberately not automatic (captain, 2026-07-27) | reason data first; a blanket retry re-polls known-dead watches forever |
| Multi-campground per watch | Not in v1 | — |

---

## Known limitations (backend)

| Limitation | Impact | Mitigation |
|---|---|---|
| 30-min cron + GitHub scheduling lag | Alerts lag openings by 0–35 min | honest UI copy; self-hosted runner or public repo → 15-min polling |
| Private-repo minute budget | ~1,800/2,000 min/month used | the budget table; self-hosted runner escape hatch |
| Unofficial provider APIs | Could change, break, or block | defensive parsing, captured fixtures, jitter, backoff, residential-IP fallback |
| Azure WAF could extend to GoingToCamp `/api/*` | GTC path breaks | keep to plain `httpx` GET + browser UA + pacing, never a browser; the provider seam contains the blast radius to GTC |
| Supabase free tier pauses after 7 idle days | n/a — the cron hits it every 30 min | inherent keep-alive |
| Constraint drift is invisible to the preflight | `UNIQUE(watch_id, site_id, date)` unverified | out of scope; both incidents to date were missing columns |

## Backlog (backend)

- 15-min polling via a self-hosted runner
- A retry policy for errored watches, classified on `error_reason` (data first — see Decisions)
- Moving past-date errored rows to `expired` in the expiry pass (tidier census, but it costs
  writes and mixes two concerns)
- Caching the GoingToCamp park→child-map structure (Phase 8, still open)
