-- ============================================================================
-- GQ FinXray US — get the schema ready, 2026-09-09
--
-- Safe to run on an existing database, and safe to run twice. Everything is
-- IF NOT EXISTS / ADD COLUMN IF NOT EXISTS. It creates nothing that already
-- exists, drops nothing, and rewrites no data.
--
-- Paste the whole file into the Supabase SQL editor and run it. Sections 1-4
-- are the ones the code needs; Section 5 is conditional and explains itself.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0 — LOOK FIRST (read-only)
--
-- What already exists, so you can see what Section 1-4 will actually add.
-- ----------------------------------------------------------------------------
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;


-- ----------------------------------------------------------------------------
-- SECTION 1 — payload_log
--
-- The XBRL/JSON payload logger. Written at DELIVERY time by delivery.py for
-- every alert carrying machine-readable data: a built structured payload
-- (Result Snapshot, Form 4, IPO, transcript) or the SEC JSON endpoints now
-- attached to every SEC filing.
--
-- Until this exists, delivery.py logs a warning per payload and keeps
-- delivering normally — so a missing table looks exactly like a logger with
-- nothing to log. Run verify_payload_log.py afterwards to confirm.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.payload_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id       uuid,
    ticker         text,
    payload_type   text,        -- 'fr' | 'it' | 'ipo' | 'earning_calls' | 'sec_json'
    filing_type    text,
    source         text,
    payload        jsonb NOT NULL,
    frontend_link  text,        -- GQuants link, or the SEC endpoint when unset
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per alert. delivery.py upserts on this, so a retried delivery cycle
-- never duplicates a payload it already logged.
CREATE UNIQUE INDEX IF NOT EXISTS payload_log_alert_id_uniq
    ON public.payload_log (alert_id);
CREATE INDEX IF NOT EXISTS payload_log_ticker_idx     ON public.payload_log (ticker);
CREATE INDEX IF NOT EXISTS payload_log_created_at_idx ON public.payload_log (created_at DESC);
CREATE INDEX IF NOT EXISTS payload_log_type_idx       ON public.payload_log (payload_type);


-- ----------------------------------------------------------------------------
-- SECTION 2 — tables the pipeline and delivery write to
--
-- Each of these is wrapped in try/except in the code, so a missing one degrades
-- to a logged warning rather than an outage. That also means it can be missing
-- for a long time without anyone noticing.
-- ----------------------------------------------------------------------------

-- The delivery ledger: one row per (alert, user). The UNIQUE constraint is the
-- idempotency key — it is what stops a crash mid-fan-out from double-sending
-- on restart, so it is the single most important line in this file.
CREATE TABLE IF NOT EXISTS public.alert_deliveries (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id    uuid NOT NULL,
    user_id     uuid NOT NULL,
    chat_id     text,
    status      text NOT NULL,      -- SENT | FAILED | SKIPPED | UNDELIVERABLE
    reason      text,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_alert_user_uniq
    ON public.alert_deliveries (alert_id, user_id);
CREATE INDEX IF NOT EXISTS alert_deliveries_alert_idx ON public.alert_deliveries (alert_id);
-- Backs the daily-cap count: WHERE user_id = ? AND status = 'SENT' AND created_at >= ?
CREATE INDEX IF NOT EXISTS alert_deliveries_user_status_time_idx
    ON public.alert_deliveries (user_id, status, created_at DESC);

-- Summaries that failed the quality gates and were NOT sent.
CREATE TABLE IF NOT EXISTS public.flagged_summaries (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id          uuid,
    ticker             text,
    company_name       text,
    final_summary      text,
    final_word_count   integer,
    max_target_reached integer,
    failure_reason     text,
    attempts           jsonb,
    feature_id         integer,
    feature_name       text,
    source             text,
    filing_type        text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS flagged_summaries_created_idx ON public.flagged_summaries (created_at DESC);
CREATE INDEX IF NOT EXISTS flagged_summaries_reason_idx  ON public.flagged_summaries (failure_reason);

-- One row per alert actually pushed to Telegram — the cost/audit trail.
CREATE TABLE IF NOT EXISTS public.alert_run_log (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id               uuid,
    ticker                 text,
    source                 text,
    filing_type            text,
    feature_id             integer,
    feature_name           text,
    impact                 text,
    summarization_attempts integer,
    input_tokens           integer,
    output_tokens          integer,
    total_tokens           integer,
    llm_calls              integer,
    telegram_success       boolean,
    telegram_error         text,
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alert_run_log_created_idx ON public.alert_run_log (created_at DESC);

CREATE TABLE IF NOT EXISTS public.poller_error_log (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    poller_name     text,
    job_name        text,
    error_message   text,
    error_traceback text,
    context         jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS poller_error_log_created_idx ON public.poller_error_log (created_at DESC);

CREATE TABLE IF NOT EXISTS public.ai_summaries (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id   uuid,
    ticker      text,
    summary     text,
    impact      text,
    event_type  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- Backs the dedup lookup: WHERE ticker = ? ORDER BY created_at DESC LIMIT 10
CREATE INDEX IF NOT EXISTS ai_summaries_ticker_created_idx
    ON public.ai_summaries (ticker, created_at DESC);

-- Per-user delivery preferences. A missing row means defaults, so this table
-- being empty is fine — it must simply exist for the join.
CREATE TABLE IF NOT EXISTS public.user_preferences (
    user_id             uuid PRIMARY KEY,
    min_impact          text    NOT NULL DEFAULT 'MEDIUM',
    muted_features      integer[]        DEFAULT '{}',
    max_alerts_per_day  integer NOT NULL DEFAULT 200,
    receive_market_wide boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);


-- ----------------------------------------------------------------------------
-- SECTION 3 — columns added since the tables were first created
--
-- No-ops when the column is already there.
-- ----------------------------------------------------------------------------
ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS receive_market_wide boolean NOT NULL DEFAULT true;
ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS muted_features integer[] DEFAULT '{}';
ALTER TABLE public.alert_deliveries
    ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE public.payload_log
    ADD COLUMN IF NOT EXISTS frontend_link text;


-- ----------------------------------------------------------------------------
-- SECTION 4 — indexes for the queue queries
--
-- These matter more than they look. The pipeline and delivery loops now read
-- their queues NEWEST-FIRST and sweep the stale tail on a timer, and both run
-- every few seconds. Without an index matching the filter+order, each of those
-- reads is a sequential scan that gets slower as the tables grow — which is
-- precisely the condition that produced a backlog in the first place.
-- ----------------------------------------------------------------------------

-- delivery: WHERE delivered = false ORDER BY created_at DESC LIMIT 100
CREATE INDEX IF NOT EXISTS alerts_undelivered_recent_idx
    ON public.alerts (created_at DESC) WHERE delivered = false;

-- delivery expiry sweep: WHERE delivered = false AND created_at < cutoff
CREATE INDEX IF NOT EXISTS alerts_delivered_created_idx
    ON public.alerts (delivered, created_at);

-- result_snapshot dedup: WHERE ticker = ? AND filing_type = 'RESULT_SNAPSHOT'
CREATE INDEX IF NOT EXISTS alerts_ticker_filing_type_idx
    ON public.alerts (ticker, filing_type);

-- pipeline: WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 10
CREATE INDEX IF NOT EXISTS raw_filings_status_created_idx
    ON public.raw_filings (status, created_at DESC);

-- expiry sweep: WHERE status = 'PENDING' AND filing_type [NOT] IN (...) AND created_at < cutoff
CREATE INDEX IF NOT EXISTS raw_filings_status_type_created_idx
    ON public.raw_filings (status, filing_type, created_at);

-- edgar poller dedup: WHERE url IN (...)
CREATE INDEX IF NOT EXISTS raw_filings_url_idx ON public.raw_filings (url);

-- delivery audience: WHERE ticker IN (...)
CREATE INDEX IF NOT EXISTS watchlists_ticker_idx ON public.watchlists (ticker);


-- ----------------------------------------------------------------------------
-- SECTION 5 — CONDITIONAL: allow the 'EXPIRED' filing status
--
-- The pipeline now retires stale PENDING rows by setting status = 'EXPIRED'.
-- If raw_filings.status carries a CHECK constraint written before that status
-- existed, every expiry sweep fails — silently, because the code catches it and
-- logs a warning, and the backlog then never drains.
--
-- Run this SELECT first. If it returns NO rows, there is no constraint and you
-- are done — skip the rest of this section.
-- ----------------------------------------------------------------------------
SELECT con.conname, pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
WHERE nsp.nspname = 'public'
  AND rel.relname = 'raw_filings'
  AND con.contype = 'c'
  AND pg_get_constraintdef(con.oid) ILIKE '%status%';

-- If the SELECT above returned a constraint whose definition does NOT include
-- 'EXPIRED', replace <constraint_name> below with the conname it printed and
-- run these two statements. Read the printed definition first and keep every
-- status it already allows.
--
-- ALTER TABLE public.raw_filings DROP CONSTRAINT <constraint_name>;
-- ALTER TABLE public.raw_filings ADD CONSTRAINT <constraint_name>
--     CHECK (status IN ('PENDING', 'PROCESSED', 'DISCARDED', 'FAILED',
--                       'FLAGGED_FOR_REVIEW', 'IPO_PENDING', 'EXPIRED'));


-- ----------------------------------------------------------------------------
-- SECTION 6 — CHECK IT WORKED (read-only)
-- ----------------------------------------------------------------------------
SELECT 'payload_log'       AS table, count(*) AS rows FROM public.payload_log
UNION ALL SELECT 'alert_deliveries',  count(*) FROM public.alert_deliveries
UNION ALL SELECT 'flagged_summaries', count(*) FROM public.flagged_summaries
UNION ALL SELECT 'alert_run_log',     count(*) FROM public.alert_run_log
UNION ALL SELECT 'ai_summaries',      count(*) FROM public.ai_summaries
UNION ALL SELECT 'user_preferences',  count(*) FROM public.user_preferences
UNION ALL SELECT 'poller_error_log',  count(*) FROM public.poller_error_log;

-- Why alerts are failing, if any are (should be empty on a healthy system):
SELECT failure_reason, count(*) AS n
FROM public.flagged_summaries
WHERE created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
