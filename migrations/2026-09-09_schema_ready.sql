-- ============================================================================
-- GQ FinXray US — get the schema ready, 2026-09-09  (revised)
--
-- Safe on an existing database, safe to run twice. Nothing is dropped and no
-- data is rewritten.
--
-- REVISION NOTE: the first version of this file paired CREATE TABLE IF NOT
-- EXISTS with indexes that assumed the columns in MY definition. When a table
-- already existed with a different shape, CREATE TABLE silently did nothing and
-- the index then failed with:
--     ERROR: 42703: column "created_at" does not exist
-- Every column this code depends on is now added explicitly with ADD COLUMN IF
-- NOT EXISTS before anything indexes it, so an existing table is brought up to
-- spec instead of being assumed correct.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0 — LOOK FIRST (read-only)
--
-- Run this on its own. It shows which of these tables already exist and
-- whether each has a created_at column, which is what the previous version
-- got wrong.
-- ----------------------------------------------------------------------------
SELECT t.table_name,
       EXISTS (SELECT 1 FROM information_schema.columns c
               WHERE c.table_schema = 'public'
                 AND c.table_name = t.table_name
                 AND c.column_name = 'created_at') AS has_created_at,
       (SELECT string_agg(c.column_name, ', ' ORDER BY c.ordinal_position)
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.table_name = t.table_name
          AND c.data_type LIKE 'timestamp%') AS timestamp_columns
FROM information_schema.tables t
WHERE t.table_schema = 'public'
  AND t.table_name IN ('alerts','raw_filings','watchlists','users','stocks',
                       'payload_log','alert_deliveries','flagged_summaries',
                       'alert_run_log','poller_error_log','ai_summaries',
                       'user_preferences')
ORDER BY t.table_name;

-- If `alerts` or `raw_filings` shows has_created_at = false, STOP and say so.
-- Those two are core tables the running code already orders by created_at, so a
-- missing column there means something different from what this file assumes,
-- and adding one with DEFAULT now() would stamp every existing row as new —
-- which would make the staleness sweep treat old rows as fresh.


-- ----------------------------------------------------------------------------
-- SECTION 1 — create the tables (no-ops where they exist)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.payload_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.alert_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.flagged_summaries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.alert_run_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.poller_error_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.ai_summaries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

CREATE TABLE IF NOT EXISTS public.user_preferences (
    user_id uuid PRIMARY KEY
);


-- ----------------------------------------------------------------------------
-- SECTION 2 — bring every column up to spec
--
-- ADD COLUMN IF NOT EXISTS is a no-op when the column is already there, so this
-- is correct whether the table was just created above or has existed for
-- months with a different shape.
-- ----------------------------------------------------------------------------

-- payload_log: the XBRL/JSON logger, written at DELIVERY time for any alert
-- carrying machine-readable data (a built payload, or the SEC JSON endpoints).
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS alert_id      uuid;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS ticker        text;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS payload_type  text;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS filing_type   text;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS source        text;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS payload       jsonb;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS frontend_link text;
ALTER TABLE public.payload_log ADD COLUMN IF NOT EXISTS created_at    timestamptz NOT NULL DEFAULT now();

-- alert_deliveries: one row per (alert, user). The UNIQUE index in Section 3 is
-- the idempotency key that stops a crash mid-fan-out double-sending on restart.
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS alert_id   uuid;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS user_id    uuid;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS chat_id    text;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS status     text;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS reason     text;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS error      text;
ALTER TABLE public.alert_deliveries ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

-- flagged_summaries: summaries that failed the quality gates and were NOT sent.
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS filing_id          uuid;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS ticker             text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS company_name       text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS final_summary      text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS final_word_count   integer;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS max_target_reached integer;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS failure_reason     text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS attempts           jsonb;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS feature_id         integer;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS feature_name       text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS source             text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS filing_type        text;
ALTER TABLE public.flagged_summaries ADD COLUMN IF NOT EXISTS created_at         timestamptz NOT NULL DEFAULT now();

-- alert_run_log: one row per alert actually pushed to Telegram (cost/audit).
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS alert_id               uuid;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS ticker                 text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS source                 text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS filing_type            text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS feature_id             integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS feature_name           text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS impact                 text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS summarization_attempts integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS input_tokens           integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS output_tokens          integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS total_tokens           integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS llm_calls              integer;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS telegram_success       boolean;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS telegram_error         text;
ALTER TABLE public.alert_run_log ADD COLUMN IF NOT EXISTS created_at             timestamptz NOT NULL DEFAULT now();

ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS poller_name     text;
ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS job_name        text;
ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS error_message   text;
ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS error_traceback text;
ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS context         jsonb;
ALTER TABLE public.poller_error_log ADD COLUMN IF NOT EXISTS created_at      timestamptz NOT NULL DEFAULT now();

ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS filing_id  uuid;
ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS ticker     text;
ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS summary    text;
ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS impact     text;
ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS event_type text;
ALTER TABLE public.ai_summaries ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

-- user_preferences: a missing ROW means defaults, so an empty table is fine —
-- it only has to exist, with these columns, for the delivery join.
ALTER TABLE public.user_preferences ADD COLUMN IF NOT EXISTS min_impact          text    NOT NULL DEFAULT 'MEDIUM';
ALTER TABLE public.user_preferences ADD COLUMN IF NOT EXISTS muted_features      integer[]        DEFAULT '{}';
ALTER TABLE public.user_preferences ADD COLUMN IF NOT EXISTS max_alerts_per_day  integer NOT NULL DEFAULT 200;
ALTER TABLE public.user_preferences ADD COLUMN IF NOT EXISTS receive_market_wide boolean NOT NULL DEFAULT true;
ALTER TABLE public.user_preferences ADD COLUMN IF NOT EXISTS created_at          timestamptz NOT NULL DEFAULT now();


-- ----------------------------------------------------------------------------
-- SECTION 3 — indexes
--
-- Only now, once every column above is guaranteed to exist.
-- ----------------------------------------------------------------------------

-- payload_log: one row per alert. delivery.py upserts on this, so a retried
-- cycle never duplicates a payload it already logged.
CREATE UNIQUE INDEX IF NOT EXISTS payload_log_alert_id_uniq  ON public.payload_log (alert_id);
CREATE INDEX IF NOT EXISTS payload_log_ticker_idx            ON public.payload_log (ticker);
CREATE INDEX IF NOT EXISTS payload_log_created_at_idx        ON public.payload_log (created_at DESC);
CREATE INDEX IF NOT EXISTS payload_log_type_idx              ON public.payload_log (payload_type);

CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_alert_user_uniq
    ON public.alert_deliveries (alert_id, user_id);
CREATE INDEX IF NOT EXISTS alert_deliveries_alert_idx ON public.alert_deliveries (alert_id);
CREATE INDEX IF NOT EXISTS alert_deliveries_user_status_time_idx
    ON public.alert_deliveries (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS flagged_summaries_created_idx ON public.flagged_summaries (created_at DESC);
CREATE INDEX IF NOT EXISTS flagged_summaries_reason_idx  ON public.flagged_summaries (failure_reason);
CREATE INDEX IF NOT EXISTS alert_run_log_created_idx     ON public.alert_run_log (created_at DESC);
CREATE INDEX IF NOT EXISTS poller_error_log_created_idx  ON public.poller_error_log (created_at DESC);

-- Backs the dedup lookup: WHERE ticker = ? ORDER BY created_at DESC LIMIT 10
CREATE INDEX IF NOT EXISTS ai_summaries_ticker_created_idx
    ON public.ai_summaries (ticker, created_at DESC);


-- ----------------------------------------------------------------------------
-- SECTION 4 — indexes on the EXISTING core tables
--
-- These matter more than they look. Both loops read their queues newest-first
-- and sweep the stale tail on a timer, every few seconds. Without an index
-- matching filter+order each read is a sequential scan that degrades as the
-- table grows — the same condition that produced the backlog.
--
-- If Section 0 showed alerts/raw_filings WITHOUT created_at, skip this section
-- and tell me what their timestamp column is called instead.
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS alerts_undelivered_recent_idx
    ON public.alerts (created_at DESC) WHERE delivered = false;
CREATE INDEX IF NOT EXISTS alerts_delivered_created_idx
    ON public.alerts (delivered, created_at);
CREATE INDEX IF NOT EXISTS alerts_ticker_filing_type_idx
    ON public.alerts (ticker, filing_type);

CREATE INDEX IF NOT EXISTS raw_filings_status_created_idx
    ON public.raw_filings (status, created_at DESC);
CREATE INDEX IF NOT EXISTS raw_filings_status_type_created_idx
    ON public.raw_filings (status, filing_type, created_at);
CREATE INDEX IF NOT EXISTS raw_filings_url_idx
    ON public.raw_filings (url);

CREATE INDEX IF NOT EXISTS watchlists_ticker_idx
    ON public.watchlists (ticker);


-- ----------------------------------------------------------------------------
-- SECTION 5 — CONDITIONAL: allow the 'EXPIRED' filing status
--
-- The pipeline retires stale PENDING rows with status = 'EXPIRED'. If
-- raw_filings.status carries a CHECK constraint written before that status
-- existed, every expiry sweep fails SILENTLY — the code catches it and logs a
-- warning — and the backlog never drains.
--
-- Run this SELECT. No rows = no constraint = nothing to do here.
-- ----------------------------------------------------------------------------
SELECT con.conname, pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
WHERE nsp.nspname = 'public'
  AND rel.relname = 'raw_filings'
  AND con.contype = 'c'
  AND pg_get_constraintdef(con.oid) ILIKE '%status%';

-- Only if that returned a constraint whose definition lacks 'EXPIRED': swap in
-- the conname it printed, and keep every status the printed definition already
-- allows — do not paste this list blind.
--
-- ALTER TABLE public.raw_filings DROP CONSTRAINT <constraint_name>;
-- ALTER TABLE public.raw_filings ADD CONSTRAINT <constraint_name>
--     CHECK (status IN ('PENDING','PROCESSED','DISCARDED','FAILED',
--                       'FLAGGED_FOR_REVIEW','IPO_PENDING','EXPIRED'));


-- ----------------------------------------------------------------------------
-- SECTION 6 — CHECK IT WORKED (read-only)
-- ----------------------------------------------------------------------------
SELECT 'payload_log' AS tbl, count(*) AS rows FROM public.payload_log
UNION ALL SELECT 'alert_deliveries',  count(*) FROM public.alert_deliveries
UNION ALL SELECT 'flagged_summaries', count(*) FROM public.flagged_summaries
UNION ALL SELECT 'alert_run_log',     count(*) FROM public.alert_run_log
UNION ALL SELECT 'ai_summaries',      count(*) FROM public.ai_summaries
UNION ALL SELECT 'user_preferences',  count(*) FROM public.user_preferences
UNION ALL SELECT 'poller_error_log',  count(*) FROM public.poller_error_log;

-- Why alerts are failing, if any are. Should be near-empty after the summary
-- gate fixes; before them this was full of too_long / too_short /
-- rhetorical_or_exclamatory_ending.
SELECT failure_reason, count(*) AS n
FROM public.flagged_summaries
WHERE created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
