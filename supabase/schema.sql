-- CampAssist Supabase schema (Phase 1).
-- Paste this whole file into the Supabase SQL editor and run it once.
-- Source of truth: CampAssist PLAN.md "Supabase Schema".

CREATE TABLE watches (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL DEFAULT auth.uid(),
    campground_id    TEXT NOT NULL,
    campground_name  TEXT NOT NULL,
    campground_state TEXT,
    site_ids     TEXT[] DEFAULT '{}',                -- empty = any site
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'monitoring'
                 CHECK (status IN ('monitoring','paused','expired','error')),
    state_hash   TEXT,                               -- delta detection
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ,
    last_found_at   TIMESTAMPTZ
);

CREATE TABLE device_tokens (
    user_id      UUID PRIMARY KEY DEFAULT auth.uid(),
    apns_token   TEXT NOT NULL,
    environment  TEXT NOT NULL DEFAULT 'production'
                 CHECK (environment IN ('production','sandbox')),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE sent_alerts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watch_id     UUID REFERENCES watches(id) ON DELETE CASCADE,
    site_id      TEXT NOT NULL,
    date         DATE NOT NULL,
    sent_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(watch_id, site_id, date)
);

CREATE TABLE run_summaries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ran_at        TIMESTAMPTZ DEFAULT NOW(),
    watches_checked INT,
    campgrounds_polled INT,
    alerts_sent   INT,
    duration_ms   INT,
    errors        TEXT
);

ALTER TABLE watches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE sent_alerts   ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_summaries ENABLE ROW LEVEL SECURITY;

CREATE POLICY "own watches"  ON watches       FOR ALL    USING (user_id = auth.uid());
CREATE POLICY "own token"    ON device_tokens FOR ALL    USING (user_id = auth.uid());
CREATE POLICY "read own alerts" ON sent_alerts FOR SELECT
    USING (watch_id IN (SELECT id FROM watches WHERE user_id = auth.uid()));
CREATE POLICY "read summaries" ON run_summaries FOR SELECT USING (true);
-- Backend writes via service-role key (bypasses RLS)
