-- 0008: cap active watches per user at 20 (pre-release abuse guardrail)
--
-- Why: the poll plan is deduped across the whole fleet, but three Supabase-side
-- terms scale linearly with the RAW number of watches — the per-cycle watches
-- read, the batched last_checked_at write, and alert fan-out. A single runaway
-- or scripted client creating thousands of watches could blow all three on its
-- own. This bounds one user's blast radius. The app also caps this in its own UX
-- (a separate camp-assist task), but that is bypassable; this backend trigger is
-- not, because RLS lets a client INSERT its own watch rows directly.
--
-- What counts as "active": status IN ('monitoring','paused') — the non-terminal
-- states a user still holds. 'expired' and 'error' are terminal (nothing ever
-- moves a watch back out of them), so they are NOT counted: a user must never be
-- locked out of creating new watches by their own accumulated history.
--
-- When it fires: only when a row ENTERS the active set — an INSERT, or an UPDATE
-- from a terminal status into an active one. An UPDATE that stays within the
-- active set (the monitor stamping last_checked_at / state_hash on an already
-- 'monitoring' row, or a paused<->monitoring toggle) is NOT re-checked, so the
-- monitor's own writes are never blocked and a pool that predates this cap (or
-- one already above it for any reason) keeps being served — the trigger only
-- stops NEW active rows past the limit, it never wedges existing ones.
--
-- Legible failure: it RAISEs with SQLSTATE check_violation (23514), which
-- PostgREST returns to the app as HTTP 400, and sets HINT = 'WATCH_CAP_EXCEEDED'
-- so the client can recognise this specific rejection (in the response body's
-- `hint` field) and show a friendly message rather than an opaque DB error.
--
-- No new column, so NO preflight.REQUIRED change (like 0007, this adds a
-- trigger, not a column). The monitor needs no code change to tolerate the
-- pre-migration state: it never inserts watches and never moves one back into
-- the active set, so whether or not this trigger exists, every monitor write
-- behaves identically. The only behaviour that changes once applied is the
-- app's watch creation, which is rejected past 20 active watches.
--
-- Idempotent: CREATE OR REPLACE FUNCTION and DROP TRIGGER IF EXISTS + CREATE
-- TRIGGER re-create the same objects, so re-running lands the same guard. Apply
-- it BY HAND in the Supabase SQL editor, like every file here (see README
-- "Database migrations"); CI does not run migrations. The matching definition is
-- in supabase/schema.sql so a fresh install and a migrated database agree.

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
