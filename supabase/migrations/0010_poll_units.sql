-- 0010: poll_units — one row per distinct poll unit, carrying the hash of its
--       last-seen RAW availability (egress read-reduction, change detection)
--
-- Why: the cycle polls every unit every run (that is how a quiet unit becoming
-- available is noticed), but it should read back the WATCH rows only for units
-- whose availability actually changed. To know which changed, it needs the
-- previous cycle's raw availability to compare against — and each cycle is a
-- fresh process, so that state must persist here. After polling a unit the cycle
-- hashes its raw parsed availability (monitor.state_hash of the provider's
-- parsed body, BEFORE any per-watch extract) and compares to the stored hash:
--   * hash unchanged  -> no watch on that unit can have changed (the per-watch
--                        state_hash is a pure function of this raw availability
--                        plus the watch's own static config), so its rows are
--                        NOT read.
--   * hash changed / unit unseen -> the unit's campground is read back and its
--                        watches are processed and alerted as before.
-- The stored hash is upserted only for CHANGED units, so a quiet cycle writes to
-- this table zero times and the per-cycle write budget is unchanged.
--
-- unit_key is the monitor's stable string for a (provider, PollKey) pair
-- (monitor.unit_key). raw_hash is a SHA-256 hex digest. campground_id is stored
-- for operator legibility; it is not otherwise read by the cycle (the
-- changed-campground set comes from the live poll results, not from this table).
--
-- This table is intentionally NOT pruned per cycle. Growth is naturally bounded
-- — one row per distinct unit the active fleet polls to the rolling horizon —
-- and read egress stays bounded because the cycle reads it scoped/chunked to
-- only the units it actually polls, never the whole table. A prune is
-- deliberately omitted rather than merely deferred: updated_at is bumped ONLY
-- when a unit's availability changes, so a stable-but-active unit (a popular,
-- fully-booked campground polled every cycle) goes stale, and an updated_at-based
-- prune would evict exactly those rows — then force a re-read of their watches,
-- defeating the reduction for the units it helps most.
--
-- Service-role only: RLS is enabled with NO policy, so no anon/user role can
-- read or write it. Only the monitor (service role, which bypasses RLS) touches
-- it. It holds no user data — a unit_key is a provider + campground id + a
-- date/map subkey, all non-secret — and a raw availability hash.
--
-- Preflight class WARN (every column): when this table is absent the cycle's
-- read of it rejects (missing relation) and monitor.run falls back to treating
-- every unit as changed, i.e. reading the full monitoring set exactly as today —
-- correct, just at the old egress. WARN is also what lets this file merge before
-- it is applied. See scripts/preflight.py REQUIRED.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + guarded RLS enable. Apply BY HAND in
-- the Supabase SQL editor (README "Database migrations"); CI does not run
-- migrations. The matching definition is in supabase/schema.sql.

CREATE TABLE IF NOT EXISTS poll_units (
    unit_key      TEXT PRIMARY KEY,      -- monitor.unit_key(provider, PollKey)
    raw_hash      TEXT NOT NULL,         -- SHA-256 of the last-seen raw availability
    campground_id TEXT,                  -- operator legibility (table is not pruned)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE poll_units ENABLE ROW LEVEL SECURITY;
-- deliberately no CREATE POLICY: service-role (monitor) only.
