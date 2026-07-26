-- 0003: add watches.include_ada_only
--
-- Why: GoingToCamp marks some sites "ADA Only" — only campers with
-- disabilities may reserve them, and the platform's own search excludes them
-- by default (`defaultSearchFilterEnumValue: 1` on attribute definition
-- -32759). The monitor currently counts them as openings and alerts on them.
-- The captain's decision is a per-watch opt-in, default OFF: ADA-only sites
-- are excluded unless this watch says otherwise, so the campers who need those
-- sites can still watch for them. Per-watch rather than global because the
-- need is per-trip, and because a global setting would silently rewrite the
-- meaning of every existing watch the moment it was toggled.
--
-- Backfill-free: `DEFAULT false` fills every existing row as part of the
-- ALTER, which is the behaviour they already have, so there is no UPDATE to
-- run and nothing about an existing watch changes.
--
-- Idempotent: `IF NOT EXISTS` makes this safe to re-run. Apply it BY HAND in
-- the Supabase SQL editor, like every file here (see README "Database
-- migrations"); CI does not run migrations.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS include_ada_only BOOLEAN NOT NULL DEFAULT false;
