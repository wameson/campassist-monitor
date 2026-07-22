-- 0001: add watches.consecutive_not_found
--
-- Why: this column was added to supabase/schema.sql after the watches table was
-- first created. Databases bootstrapped from the original schema never received
-- it, so the monitor's PATCH writes to watches started returning HTTP 400 against
-- those live DBs (a 3-day outage). See data/monitor-fail-investigation/report.md.
--
-- Idempotent: safe to re-run. The captain has already applied this by hand to the
-- live DB; it is recorded here so fresh installs and existing DBs converge.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS consecutive_not_found INT NOT NULL DEFAULT 0;
