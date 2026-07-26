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
-- ALTER, so there is no UPDATE to run. Be clear about what that means: the
-- monitor alerts on ADA-only sites TODAY, so the effective behaviour of every
-- existing watch is include_ada_only = true. This default is therefore a
-- deliberate change of behaviour, applied uniformly to existing and new
-- watches, and once the filter lands in the follow-on phase pre-existing
-- watches WILL stop being alerted about ADA-only openings. The captain's
-- decision is to accept that with no backfill.
--
-- Idempotent: `IF NOT EXISTS` makes this safe to re-run. Apply it BY HAND in
-- the Supabase SQL editor, like every file here (see README "Database
-- migrations"); CI does not run migrations.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS include_ada_only BOOLEAN NOT NULL DEFAULT false;
