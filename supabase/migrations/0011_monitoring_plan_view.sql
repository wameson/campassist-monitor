-- 0011: monitoring_plan view — the deduped poll-planning inputs, computed
--       server-side so the cycle never drags every watch row over the wire just
--       to build its poll plan (egress read-reduction)
--
-- Why: the poll plan must cover EVERY monitoring watch's units (a quiet unit is
-- still polled so an opening on it is noticed), but the columns poll_plan needs
-- are few and enormously duplicated — thousands of campers watching the same
-- campground and dates share one plan. This view returns the DISTINCT set of
-- those columns, so the cycle reads ~one row per real unit instead of one per
-- watch. The Python conformer still runs poll_plan on each distinct row
-- (poll_plan is provider logic and does not belong in SQL); the view only does
-- the deduplication PostgREST cannot express on its own.
--
-- Columns are exactly what the providers' poll_plan reads and nothing else:
-- provider (routes to the conformer), campground_id, provider_ref, start_date,
-- end_date. No user_id, no site_ids, no names — deduping already collapses away
-- anything watch-specific, and the row carries no field a poll plan does not use.
--
-- Scope: status = 'monitoring' only — the same filter the cycle's watch read
-- used. Expired/paused/errored watches are not planned or polled.
--
-- Security: created WITH (security_invoker = true) so the view enforces the RLS
-- on `watches` ('own watches', user_id = auth.uid()) for whatever role queries
-- it, and the default public grants are REVOKEd — no anon/authenticated caller
-- can read any camper's planning inputs over /rest/v1. The monitor queries this
-- as the SERVICE ROLE, which bypasses RLS, so it still sees EVERY monitoring
-- watch's units: the plan stays complete and sublinear and the egress win is
-- unaffected (the REVOKE targets anon/authenticated only; the explicit
-- service_role GRANT self-documents the poller's access and survives a future
-- default-privilege change). It also exposes no host-derivation surface —
-- provider_ref carries identifiers only and every request host stays a pinned
-- constant in code (the standing SSRF invariant).
--
-- NOT preflighted: the schema-drift manifest (scripts/preflight.py) mirrors
-- CREATE TABLEs, and this is a view. Its absence is handled at runtime instead —
-- monitor.run's plan read rejects with a missing-relation code and the cycle
-- falls back to reading the full monitoring set and planning from it (exactly
-- today's behaviour), so an unmigrated live DB keeps monitoring at the old
-- egress. That fallback is why this file may merge before it is applied.
--
-- Idempotent: CREATE OR REPLACE VIEW, REVOKE and GRANT are all safe to re-run,
-- and security_invoker is set at create time so re-running lands the same
-- locked-down view. Apply BY HAND in the Supabase SQL editor (README "Database
-- migrations"); CI does not run migrations. The matching definition is in
-- supabase/schema.sql.

CREATE OR REPLACE VIEW monitoring_plan WITH (security_invoker = true) AS
    SELECT DISTINCT provider, campground_id, provider_ref, start_date, end_date
    FROM watches
    WHERE status = 'monitoring';
REVOKE ALL ON monitoring_plan FROM anon, authenticated;
GRANT SELECT ON monitoring_plan TO service_role;
