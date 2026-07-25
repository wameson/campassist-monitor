-- 0002: add watches.provider + watches.provider_ref
--
-- Why: CampAssist is gaining a 2nd campground provider (GoingToCamp / WA State
-- Parks). `provider` is the discriminator that says which backend a watch polls;
-- `provider_ref` is a per-provider blob so each provider can carry its own
-- identifiers without the table growing a column per provider.
--
-- `provider_ref` carries identifiers only, e.g. going_to_camp
-- {"resource_location_id":…,"map_id":…}. It is client-writable through the
-- `own watches` RLS policy, so whichever code polls a provider MUST use a
-- hardcoded/allowlisted host and must NEVER derive the host or URL it fetches
-- from provider_ref — that is what keeps a user-supplied row from directing
-- the monitor's outbound requests.
--
-- Backfill-free: every existing row is recreation.gov, so `DEFAULT
-- 'recreation_gov'` (and `'{}'::jsonb`) fills them in as part of the ALTER —
-- there is no UPDATE to run, and recreation.gov behavior is unchanged.
--
-- Idempotent: `IF NOT EXISTS` makes both statements safe to re-run. Not yet
-- applied to the live DB — apply it BY HAND in the Supabase SQL editor, like
-- every file here (see README "Database migrations"); CI does not run
-- migrations.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'recreation_gov'
        CHECK (provider IN ('recreation_gov','going_to_camp'));

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS provider_ref JSONB NOT NULL DEFAULT '{}'::jsonb;
