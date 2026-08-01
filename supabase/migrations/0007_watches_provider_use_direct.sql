-- 0007: allow watches.provider = 'use_direct' (UseDirect / Tyler, ReserveCalifornia)
--
-- Why: CampAssist is gaining a 3rd campground provider, UseDirect — the Tyler
-- platform behind ReserveCalifornia (and, on the same seam later, other western
-- state parks). The backend conformer (scripts/providers/use_direct.py) polls a
-- watch whose `provider` column says 'use_direct'. The `provider` CHECK
-- constraint added in 0002 lists only 'recreation_gov' and 'going_to_camp', so
-- an INSERT of a use_direct watch is REJECTED until this widens it. The monitor
-- only ever reads watches and never inserts them, so the constraint blocks the
-- iOS app's watch creation, not the poll — which is exactly why this must be
-- applied before the app ships UseDirect watch creation.
--
-- Shared wire contract this enables (owned by the backend conformer; the app
-- builds to it):
--   * provider raw value: 'use_direct'
--   * campground_id format: '<tenant>_<facilityId>', a registered tenant key +
--     '_' + the UseDirect FacilityId as a decimal integer, e.g. 'ca_377'
--     (ReserveCalifornia facility 377). Underscore, never colon, so it matches
--     monitor.CAMPGROUND_ID_RE ([A-Za-z0-9_-]+). provider_ref is unused.
--
-- No new column, so NO preflight.REQUIRED change: this only widens an existing
-- column's CHECK. `provider` is already in the manifest (WARN, from 0002), and
-- the preflight probes for missing columns/tables, never for a stale CHECK. A
-- database on which this is not yet applied simply cannot store a use_direct
-- watch; the monitor keeps polling every other watch normally.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS then ADD re-creates the same
-- auto-named constraint (`watches_provider_check`, Postgres's name for the
-- inline CHECK in 0002/schema.sql), so re-running lands the same widened
-- constraint. Backfill-free — widening a CHECK never rejects an existing row,
-- since every stored value is one of the original two. Apply it BY HAND in the
-- Supabase SQL editor, like every file here (see README "Database migrations");
-- CI does not run migrations.

ALTER TABLE watches
    DROP CONSTRAINT IF EXISTS watches_provider_check;

ALTER TABLE watches
    ADD CONSTRAINT watches_provider_check
        CHECK (provider IN ('recreation_gov','going_to_camp','use_direct'));
