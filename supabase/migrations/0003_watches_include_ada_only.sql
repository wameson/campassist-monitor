-- 0003: add watches.include_ada_only
--
-- Why: GoingToCamp marks some sites "ADA Only" — only campers with
-- disabilities may reserve them, and the platform's own search excludes them
-- by default (`defaultSearchFilterEnumValue: 1` on attribute definition
-- -32759). Until this column landed the monitor counted them as openings and
-- alerted on them. The captain's decision is a per-watch opt-in, default OFF:
-- ADA-only sites are excluded unless this watch says otherwise, so the campers
-- who need those sites can still watch for them. Per-watch rather than global
-- because the need is per-trip, and because a global setting would silently
-- rewrite the meaning of every existing watch the moment it was toggled.
--
-- Backfill-free: `DEFAULT false` fills every existing row as part of the
-- ALTER, so there is no UPDATE to run. Be clear about what that means: the
-- pre-column monitor alerted on ADA-only sites, so the effective behaviour of
-- every existing watch was include_ada_only = true. This default is therefore
-- a deliberate change of behaviour, applied uniformly to existing and new
-- watches: with the filter now live in going_to_camp's extract_relevant,
-- pre-existing watches have stopped being alerted about ADA-only openings
-- unless their owner opts back in. The captain's decision is to accept that
-- with no backfill.
--
-- Idempotent: `IF NOT EXISTS` makes this safe to re-run. Apply it BY HAND in
-- the Supabase SQL editor, like every file here (see README "Database
-- migrations"); CI does not run migrations.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS include_ada_only BOOLEAN NOT NULL DEFAULT false;
