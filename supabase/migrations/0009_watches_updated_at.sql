-- 0009: add watches.updated_at + a trigger that bumps it only on a USER edit
--       (egress read-reduction — the watch-side of the "unchanged unit" hole)
--
-- Why: the per-cycle `watches` read is the dominant Supabase egress term and it
-- grows linearly with the fleet (every monitoring row, every cycle). The read
-- reduction in monitor.run polls every unit as before but reads back only the
-- watch rows it actually needs: the rows on a campground whose availability
-- CHANGED this cycle, plus a few small bounded sets. The soundness rests on
-- "an unchanged poll unit means an unchanged watch state_hash" — which holds for
-- the PROVIDER side, but NOT the watch side: a newly created, edited, or
-- paused->monitoring watch can now match an opening that was already there, on a
-- unit whose availability did not change. Those rows must still be read.
--
-- `updated_at` is how the cycle finds them: a watermark read
-- (status=monitoring AND updated_at >= last-cycle-time - margin) returns exactly
-- the watches a user touched since the previous cycle. The margin covers the
-- intra-cycle read/write gap and any skipped cycle; over-reading a few recently
-- edited rows is safe, under-reading one is the missed-opening bug this guards.
--
-- The trigger bumps updated_at ONLY when a column that affects matching or
-- eligibility changes (status, dates, site_ids, campground_id, provider,
-- provider_ref, include_ada_only, the flexible-date columns). It deliberately
-- does NOT bump on the monitor's own bookkeeping writes — state_hash,
-- last_found_at, last_checked_at, consecutive_not_found, error_reason — because
-- if it did, every row the cycle wrote would re-enter the watermark window and
-- be re-read next cycle, defeating the whole reduction. So "updated_at" here
-- means "the user changed something the monitor must re-evaluate", not "any
-- write touched this row".
--
-- Backfill: existing rows are set to created_at (NOT now()), so a migrated
-- database does not put its entire fleet inside the watermark window at apply
-- time — old rows are immediately outside it and only genuinely recent edits are
-- re-read. The backfill UPDATE is guarded `WHERE updated_at IS NULL`, so it runs
-- once (first apply) and is a no-op on every re-run — it never clobbers a later
-- edit's timestamp.
--
-- Preflight class WARN: the monitor reads updated_at only in the watermark
-- filter, and when the column is absent that read rejects with 42703 and the
-- cycle falls back to reading the full monitoring set (exactly today's
-- behaviour) — see scripts/preflight.py REQUIRED and monitor.read_process_set.
-- So an unmigrated live DB keeps monitoring, correctly, at the old egress. WARN
-- is also what lets this file merge before it is applied.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, a guarded backfill, idempotent
-- SET DEFAULT/NOT NULL, and CREATE OR REPLACE FUNCTION + DROP/CREATE TRIGGER.
-- Apply BY HAND in the Supabase SQL editor (README "Database migrations"); CI
-- does not run migrations. The matching definition is in supabase/schema.sql so
-- a fresh install and a migrated database agree.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- First apply only (guarded): seed existing rows from created_at so they land
-- OUTSIDE the watermark window, not inside it. No-op on re-run.
UPDATE watches SET updated_at = COALESCE(created_at, now()) WHERE updated_at IS NULL;

ALTER TABLE watches ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE watches ALTER COLUMN updated_at SET NOT NULL;

-- Bump updated_at only when a user-meaningful column changes. The monitor's own
-- bookkeeping columns are deliberately absent from the list (see header).
CREATE OR REPLACE FUNCTION watches_touch_updated_at() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.updated_at := now();
    ELSIF (NEW.status           IS DISTINCT FROM OLD.status
        OR NEW.site_ids         IS DISTINCT FROM OLD.site_ids
        OR NEW.start_date        IS DISTINCT FROM OLD.start_date
        OR NEW.end_date          IS DISTINCT FROM OLD.end_date
        OR NEW.campground_id     IS DISTINCT FROM OLD.campground_id
        OR NEW.provider          IS DISTINCT FROM OLD.provider
        OR NEW.provider_ref      IS DISTINCT FROM OLD.provider_ref
        OR NEW.include_ada_only  IS DISTINCT FROM OLD.include_ada_only
        OR NEW.date_mode         IS DISTINCT FROM OLD.date_mode
        OR NEW.flex_min_nights   IS DISTINCT FROM OLD.flex_min_nights
        OR NEW.flex_max_nights   IS DISTINCT FROM OLD.flex_max_nights) THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS watches_touch_updated_at ON watches;
CREATE TRIGGER watches_touch_updated_at
    BEFORE INSERT OR UPDATE ON watches
    FOR EACH ROW EXECUTE FUNCTION watches_touch_updated_at();
