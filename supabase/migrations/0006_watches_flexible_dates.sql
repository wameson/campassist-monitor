-- 0006: add watches.date_mode + watches.flex_min_nights + watches.flex_max_nights
--       (Phase 16 — flexible-date / flexible-length watches)
--
-- Why: until now a watch is a single fixed stay, the nights [start_date,
-- end_date). Flexible mode lets one watch catch "any N-night window inside a
-- date range" — the monitor alerts when ANY consecutive run of the requested
-- length is fully available anywhere in [start_date, end_date). This is
-- backend-owned match logic (monitor.run) plus these three columns; the iOS app
-- builds its WatchDTO to the contract in PLAN.md "Phase 16".
--
-- The range bounds REUSE start_date/end_date rather than adding window columns:
--   * fixed mode   — start_date/end_date are the one stay, exactly as before.
--   * flexible mode — start_date is the earliest check-in and end_date the
--     latest check-out of the search range; the nights considered are
--     [start_date, end_date), the same half-open span date_in_watch already
--     uses. A length-L window with check-in `a` occupies nights [a, a+L) and
--     qualifies when a >= start_date and a + L <= end_date.
-- Reusing the existing NOT NULL DATE bounds means the poll horizon, the per-
-- provider poll_plan, and extract_relevant's date filtering all already cover
-- the whole range with NO provider changes, and a fixed watch stays
-- byte-identical. See PLAN.md "Phase 16" for the full rationale.
--
-- Column semantics:
--   date_mode        'fixed' (default) | 'flexible'. The discriminator.
--   flex_min_nights  shortest qualifying window, in nights. THE ALERT GATE: a
--                    flexible watch fires when a site has a fully-open run of at
--                    least this many consecutive nights inside the range. NULL
--                    in fixed mode.
--   flex_max_nights  longest window the user will accept, in nights. Advisory
--                    for the app's display/booking only — it does NOT further
--                    restrict alerts, because more consecutive availability can
--                    only ever help. NULL in fixed mode. (The app validates
--                    flex_min_nights <= flex_max_nights; the monitor tolerates a
--                    bad pair by simply not firing.)
--
-- Backfill-free: `DEFAULT 'fixed'` fills every existing row as part of the
-- ALTER, so there is no UPDATE to run and every existing watch keeps behaving
-- exactly as it did — a fixed stay over [start_date, end_date). The nights
-- columns default to NULL, which fixed mode never reads.
--
-- Preflight class WARN (all three): the monitor reads them only through
-- `watch.get(...)` with a 'fixed'/None default, so an unmigrated live DB reads
-- every watch as fixed and keeps monitoring unchanged (no HALT, no suppression
-- difference) — see scripts/preflight.py's REQUIRED manifest and its citation.
-- WARN is also what lets this file merge before it is applied.
--
-- Idempotent: `IF NOT EXISTS` makes every statement safe to re-run. Apply it BY
-- HAND in the Supabase SQL editor, like every file here (see README "Database
-- migrations"); CI does not run migrations. **Apply this to the live database
-- BEFORE the iOS app half (phase16-flexible-dates-app) ships**, so the app's
-- flexible-watch writes never 400 on a missing column.

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS date_mode TEXT NOT NULL DEFAULT 'fixed'
        CHECK (date_mode IN ('fixed','flexible'));

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS flex_min_nights INT
        CHECK (flex_min_nights IS NULL OR flex_min_nights >= 1);

ALTER TABLE watches
    ADD COLUMN IF NOT EXISTS flex_max_nights INT
        CHECK (flex_max_nights IS NULL OR flex_max_nights >= 1);
