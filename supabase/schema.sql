-- CampAssist Supabase schema (Phase 1).
-- Paste this whole file into the Supabase SQL editor and run it once.
-- This file is the authoritative DDL; the design behind it is PLAN.md
-- "Supabase schema" in this repo.
--
-- This is the fresh-install bootstrap: the full current schema for a brand-new
-- database. For an EXISTING database, do not re-run this — instead apply any
-- unapplied files in supabase/migrations/ (idempotent, manual; see README
-- "Database migrations"). When you change the schema here, add a matching
-- idempotent migration so already-live databases receive the change too, and
-- the column's entry in the REQUIRED manifest in scripts/preflight.py — a test
-- holds the manifest and this file to each other in both directions. Apply the
-- migration by hand before merging the manifest entry (README, same section).

CREATE TABLE watches (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL DEFAULT auth.uid(),
    provider     TEXT NOT NULL DEFAULT 'recreation_gov' -- which campground provider this watch polls
                 CHECK (provider IN ('recreation_gov','going_to_camp','use_direct')),
    provider_ref JSONB NOT NULL DEFAULT '{}'::jsonb,     -- provider-specific identifiers, e.g. going_to_camp
                                                         -- {"resource_location_id":…,"map_id":…}
                                                         -- Client-writable: identifiers only. The provider's
                                                         -- host is a fixed constant in code — never derive a
                                                         -- fetched host/URL from provider_ref.
    campground_id    TEXT NOT NULL,
    campground_name  TEXT NOT NULL,
    campground_state TEXT,
    site_ids     TEXT[] DEFAULT '{}',                -- empty = any site
    include_ada_only BOOLEAN NOT NULL DEFAULT false, -- opt in to sites reserved for campers
                                                     -- with disabilities (GoingToCamp "ADA Only");
                                                     -- excluded by default, as the platform's own
                                                     -- search excludes them. Read only by
                                                     -- going_to_camp's extract_relevant — see
                                                     -- migrations/0003 and README "Providers"
    start_date   DATE NOT NULL,                       -- fixed mode: check-in. flexible mode:
                                                     -- earliest check-in of the search range.
    end_date     DATE NOT NULL,                       -- fixed mode: check-out (the stay is the
                                                     -- nights [start_date, end_date)). flexible
                                                     -- mode: latest check-out of the search range.
    date_mode    TEXT NOT NULL DEFAULT 'fixed'        -- 'fixed' = one stay [start_date, end_date).
                 CHECK (date_mode IN ('fixed','flexible')),  -- 'flexible' = any consecutive
                                                     -- flex_min_nights..flex_max_nights-night window
                                                     -- inside [start_date, end_date). See Phase 16
                                                     -- in PLAN.md and migrations/0006. Read only via
                                                     -- watch.get(...) with a 'fixed' default, so an
                                                     -- unmigrated DB treats every watch as fixed.
    flex_min_nights INT                              -- flexible mode: shortest qualifying window
                 CHECK (flex_min_nights IS NULL OR flex_min_nights >= 1),  -- (nights). NULL in fixed
                                                     -- mode. This is the alert gate: the monitor
                                                     -- fires when a site has a fully-open run of at
                                                     -- least this many consecutive nights.
    flex_max_nights INT                              -- flexible mode: longest window the user will
                 CHECK (flex_max_nights IS NULL OR flex_max_nights >= 1),  -- take (nights). NULL in
                                                     -- fixed mode. Advisory for the app's display /
                                                     -- booking; does not further restrict alerts
                                                     -- (more consecutive availability only helps).
    status       TEXT NOT NULL DEFAULT 'monitoring'
                 CHECK (status IN ('monitoring','paused','expired','error')),
    error_reason TEXT,                               -- why status='error', from the monitor's own
                                                     -- small vocabulary (monitor.ERROR_REASONS);
                                                     -- NULL for every other status. status='error'
                                                     -- is terminal, so this is the only record of
                                                     -- whether a data fix would revive the watch —
                                                     -- see migrations/0004 and README "Errors and
                                                     -- run status"
    state_hash   TEXT,                               -- delta detection
    consecutive_not_found INT NOT NULL DEFAULT 0,    -- cycles the campground 404ed; status='error' at 3
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    last_found_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- bumped ONLY on a user edit
                                                     -- (status/dates/site_ids/…), never by the
                                                     -- monitor's own bookkeeping writes — see the
                                                     -- watches_touch_updated_at trigger below and
                                                     -- migrations/0009. The egress read-reduction's
                                                     -- watermark reads this to find newly created,
                                                     -- edited, or unpaused watches on a unit whose
                                                     -- availability did not change.
);

-- One row per distinct poll unit, holding the hash of its last-seen RAW
-- availability, so the cycle can read back watch rows only for units that
-- actually changed (egress read-reduction; migrations/0010). Service-role only.
CREATE TABLE poll_units (
    unit_key      TEXT PRIMARY KEY,      -- monitor.unit_key(provider, PollKey)
    raw_hash      TEXT NOT NULL,         -- SHA-256 of the last-seen raw availability
    campground_id TEXT,                  -- operator legibility / campground-scoped prune
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE device_tokens (
    -- Composite PK (user_id, apns_token): one row per device, so a user can hold
    -- more than one push token and an alert fans out to every device (migration
    -- 0012; scripts/apns.py send_alert). user_id alone was the PK before 0012.
    user_id      UUID NOT NULL DEFAULT auth.uid(),
    apns_token   TEXT NOT NULL,
    environment  TEXT NOT NULL DEFAULT 'production'
                 CHECK (environment IN ('production','sandbox')),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, apns_token)
);

CREATE TABLE sent_alerts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id     UUID REFERENCES watches(id) ON DELETE CASCADE,
    site_id      TEXT NOT NULL,
    date         DATE NOT NULL,
    sent_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(watch_id, site_id, date)
);

CREATE TABLE alert_history (
    -- One row per DELIVERED push (an APNs 200), written in the same step and on
    -- the same condition as watches.last_found_at, so the badge and the app's
    -- Alert History can never disagree. This is the app's read-facing source of
    -- truth for delivered alerts; sent_alerts stays dedup bookkeeping (per
    -- site+date, aggressively pruned, never read by the app) and is a different
    -- grain. Columns mirror the push payload the app already parses (apns.py):
    -- campground_name + start_date/end_date + opening count. Dates are DATE
    -- (timezone-independent calendar days) and are stored verbatim from the
    -- watch — no timezone shift.
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id        UUID REFERENCES watches(id) ON DELETE CASCADE,
    campground_name TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    site_count      INT NOT NULL,        -- openings this push announced (len of fresh)
    delivered_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE run_summaries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ran_at        TIMESTAMPTZ DEFAULT NOW(),
    watches_checked INT,
    campgrounds_polled INT,
    alerts_sent   INT,
    duration_ms   INT,
    errors        TEXT,
    -- Which provider this row summarizes, so the one-job-per-provider poll can
    -- write a labelled per-provider row instead of an unlabelled slice that
    -- reads like the whole cycle (migrations/0013). NULL for the whole-fleet
    -- `python scripts/monitor.py` run and for rows predating the column. The
    -- monitor writes it drift-tolerantly (drops+retries without it on a database
    -- missing this column), so it is WARN in preflight.REQUIRED, not HALT.
    provider      TEXT
);

ALTER TABLE watches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE sent_alerts   ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_summaries ENABLE ROW LEVEL SECURITY;
-- poll_units carries no user data and is service-role only: RLS on, NO policy,
-- so no anon/user role can touch it (the monitor's service role bypasses RLS).
ALTER TABLE poll_units    ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own watches"  ON watches       FOR ALL    USING (user_id = auth.uid());
CREATE POLICY "own token"    ON device_tokens FOR ALL    USING (user_id = auth.uid());
CREATE POLICY "read own alerts" ON sent_alerts FOR SELECT
    USING (watch_id IN (SELECT id FROM watches WHERE user_id = auth.uid()));
-- Same owner scoping as sent_alerts: an anon user reads ONLY their own delivered
-- alerts, joined through the watch's owner. Never opened to all.
CREATE POLICY "read own alert history" ON alert_history FOR SELECT
    USING (watch_id IN (SELECT id FROM watches WHERE user_id = auth.uid()));
CREATE POLICY "read summaries" ON run_summaries FOR SELECT USING (true);
-- Backend writes via service-role key (bypasses RLS)

-- Per-user active-watch cap (pre-release abuse guardrail; migrations/0008).
-- Enforced backend-side so the app's own UX cap cannot be bypassed by a client
-- that INSERTs watch rows directly under RLS. "Active" = status IN
-- ('monitoring','paused') (the non-terminal states a user still holds); the
-- terminal 'expired'/'error' rows are never counted, so accumulated history
-- never locks a user out. Checked only when a row ENTERS the active set (INSERT,
-- or an UPDATE from a terminal status), so the monitor's own writes on already
-- active rows are never blocked. On breach it RAISEs check_violation (HTTP 400
-- via PostgREST) with HINT 'WATCH_CAP_EXCEEDED' so the app can recognise it.
CREATE OR REPLACE FUNCTION enforce_watch_cap() RETURNS trigger AS $$
DECLARE
    active_count INT;
BEGIN
    IF NEW.status IN ('monitoring','paused')
       AND (TG_OP = 'INSERT' OR OLD.status NOT IN ('monitoring','paused')) THEN
        SELECT count(*) INTO active_count
        FROM watches
        WHERE user_id = NEW.user_id
          AND status IN ('monitoring','paused')
          AND id <> NEW.id;
        IF active_count >= 20 THEN
            RAISE EXCEPTION 'watch cap exceeded: 20 active watches per user'
                USING ERRCODE = 'check_violation', HINT = 'WATCH_CAP_EXCEEDED';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS watch_cap ON watches;
CREATE TRIGGER watch_cap
    BEFORE INSERT OR UPDATE ON watches
    FOR EACH ROW EXECUTE FUNCTION enforce_watch_cap();

-- Bump watches.updated_at only on a user-meaningful edit (migrations/0009).
-- The monitor's own bookkeeping columns (state_hash, last_found_at,
-- last_checked_at, consecutive_not_found, error_reason) are deliberately absent
-- from the change test: if a monitor write bumped updated_at, every row it wrote
-- would re-enter the read-reduction's watermark window and be re-read next cycle,
-- defeating the reduction. So "updated_at" means "a user changed something the
-- monitor must re-evaluate", not "any write touched this row".
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

-- Deduped poll-planning inputs, computed server-side so the cycle reads ~one row
-- per real unit instead of one per watch (egress read-reduction; migrations/0011).
-- Columns are exactly what the providers' poll_plan reads; deduplication
-- collapses everything watch-specific. provider_ref carries identifiers only —
-- request hosts stay pinned constants in code (SSRF invariant).
CREATE OR REPLACE VIEW monitoring_plan AS
    SELECT DISTINCT provider, campground_id, provider_ref, start_date, end_date
    FROM watches
    WHERE status = 'monitoring';
