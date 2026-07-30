-- 0005: add the alert_history table (server-truth Alert History, Issue 2)
--
-- Why: the "Site Found" badge and the app's Alert History were two disjoint
-- sources. The badge is driven by watches.last_found_at, which the monitor sets
-- only on an APNs DELIVERED. Alert History was local NotificationRecords, written
-- only when THIS device foregrounded or tapped the push — so a delivered-but-not-
-- tapped alert lit the badge yet left history empty (diagnosis Issue 2, captain
-- chose option A → server-fetched). This table is the server-readable record of
-- what the backend actually delivered, so the app can render history from the
-- same fact the badge is derived from and the two can never disagree.
--
-- Why a new table, not sent_alerts: sent_alerts is per (watch_id, site_id, date)
-- dedup bookkeeping — a different grain, aggressively pruned, and it carries none
-- of the rendered alert fields (campground_name, the date range, the opening
-- count). One delivered push spans many sent_alerts rows and none of them says
-- "this notification". alert_history's grain is exactly one delivered push, and
-- its columns mirror the push payload the app already parses (apns.py).
--
-- Grain: one row per DELIVERED push. The monitor inserts it in the SAME step and
-- on the SAME condition it sets watches.last_found_at (only on outcome ==
-- DELIVERED — monitor.run).
--
-- Dates are DATE (timezone-independent calendar days), stored verbatim from the
-- watch's start_date/end_date. No timezone shift is introduced (Issue 1 was
-- exactly that class of bug; the fix must not add a new one).
--
-- RLS mirrors sent_alerts' "read own alerts": an anon user reads ONLY their own
-- rows, scoped through the owning watch. The table is NOT opened to all. The
-- backend writes with the service-role key, which bypasses RLS.
--
-- Preflight class WARN (scripts/preflight.REQUIRED): the monitor writes this
-- table but never reads it, and the insert is fully contained — a failure records
-- a non-blocking, unrated, unattributed cycle failure and never errors a watch,
-- halts the cycle, or reddens the run (monitor.run, the DELIVERED block). So a
-- live DB on which this migration is not yet applied keeps monitoring and keeps
-- delivering pushes; only the history rows are not written until it is applied.
-- That tolerance is what lets this file merge before it is applied (a HALT class
-- would halt every cycle until an operator reached the SQL editor).
--
-- Idempotent: CREATE TABLE / ENABLE RLS re-run harmlessly, and the policy is
-- dropped-then-created so a re-run replaces it in place (Postgres has no
-- CREATE POLICY IF NOT EXISTS). Apply it BY HAND in the Supabase SQL editor, like
-- every file here (see README "Database migrations"); CI does not run migrations.
--
-- NOT DONE until applied: this file merged is not the same as applied. Apply it
-- to live before the app half (alert-history-app-render) ships, or the app will
-- query a table that does not exist.

CREATE TABLE IF NOT EXISTS alert_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id        UUID REFERENCES watches(id) ON DELETE CASCADE,
    campground_name TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    site_count      INT NOT NULL,
    delivered_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE alert_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "read own alert history" ON alert_history;
CREATE POLICY "read own alert history" ON alert_history FOR SELECT
    USING (watch_id IN (SELECT id FROM watches WHERE user_id = auth.uid()));
