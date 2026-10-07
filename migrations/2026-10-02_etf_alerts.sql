-- GQ FinXray US — ETF alerts (Features 14, 15, 16)
-- Run ONCE in the Supabase SQL editor, top to bottom. Safe to re-run.
--
-- Feature 14  ETF Portfolio & Expense Changes   (FMP)   etf_data_poller.py
-- Feature 15  Fund Manager Changes               (SEC)   fund_manager_poller.py
-- Feature 16  ETF Fund Actions                   (SEC)   etf_fund_events_poller.py
--
-- Requires etf_master to exist and be loaded (etf_csv_importer.py, already done).

-- ════════════════════════════════════════════════════════════════════════════
-- 1. Holdings: CURRENT SNAPSHOT model
-- ════════════════════════════════════════════════════════════════════════════
-- The 2026-10-02 first run wrote one dated copy of every holding per day, keyed
-- (ticker, security_symbol, date), and its diff compared against a single row,
-- so every one of its ~190K etf_holdings_changes rows is spurious. Both tables
-- are dropped and rebuilt. Nothing of value is lost: the next poller run
-- re-baselines silently (no alerts) and diffs from then on.
--
-- The drop only fires while the OLD shape is present (etf_holdings still has
-- its per-day "date" column). Re-running this file after go-live is a no-op
-- here, so it can never wipe a live baseline.
--
-- One row per (ETF, security), replaced when FMP publishes a new snapshot.
-- History of what changed lives in etf_holdings_changes, so storage stays flat
-- instead of growing by a full copy of every portfolio every day.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'etf_holdings'
               AND column_name = 'date') THEN
    DROP TABLE IF EXISTS public.etf_holdings_changes;
    DROP TABLE IF EXISTS public.etf_holdings;
    RAISE NOTICE 'Dropped first-run etf_holdings / etf_holdings_changes (old dated shape)';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.etf_holdings (
    ticker          VARCHAR(20) NOT NULL REFERENCES public.etf_master(ticker) ON DELETE CASCADE,
    security_key    TEXT NOT NULL,          -- symbol, else ISIN, else CUSIP, else name
    security_symbol TEXT,
    security_name   TEXT,
    isin            TEXT,
    cusip           TEXT,
    portfolio_pct   NUMERIC,                -- percent, 8.36 = 8.36%
    market_value    NUMERIC,
    shares          NUMERIC,
    as_of           TEXT,                   -- FMP updatedAt of this snapshot
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, security_key)
);
CREATE INDEX IF NOT EXISTS etf_holdings_symbol_idx ON public.etf_holdings (security_symbol);

-- WhatsIn / WhatsOut. Every addition and exit between two FMP snapshots.
CREATE TABLE IF NOT EXISTS public.etf_holdings_changes (
    id              BIGSERIAL PRIMARY KEY,
    ticker          VARCHAR(20) NOT NULL REFERENCES public.etf_master(ticker) ON DELETE CASCADE,
    security_key    TEXT NOT NULL,
    security_symbol TEXT,
    security_name   TEXT,
    action          TEXT NOT NULL CHECK (action IN ('ADDED', 'REMOVED')),
    weight_prev     NUMERIC,
    weight_new      NUMERIC,
    version_from    TEXT,
    version_to      TEXT NOT NULL,
    alerted         BOOLEAN DEFAULT FALSE,
    detected_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (ticker, security_key, action, version_to)
);
CREATE INDEX IF NOT EXISTS etf_holdings_changes_ticker_idx
    ON public.etf_holdings_changes (ticker, detected_at DESC);

-- Per-ETF poller state: which snapshot we hold, last confirmed expense ratio,
-- and an unconfirmed candidate (an expense change must be seen on two
-- consecutive runs before it alerts, so a vendor rounding flap cannot alert).
CREATE TABLE IF NOT EXISTS public.etf_snapshot_state (
    ticker                VARCHAR(20) PRIMARY KEY REFERENCES public.etf_master(ticker) ON DELETE CASCADE,
    holdings_version      TEXT,
    holdings_count        INT,
    holdings_checked_at   TIMESTAMPTZ,
    expense_ratio         NUMERIC,
    expense_pending       NUMERIC,
    expense_pending_count INT DEFAULT 0,
    expense_pending_date  DATE,
    expense_checked_at    TIMESTAMPTZ,
    company_refreshed_at  TIMESTAMPTZ,
    last_note             TEXT,
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);

-- Added after first review: confirmations must come from distinct ET dates.
ALTER TABLE public.etf_snapshot_state ADD COLUMN IF NOT EXISTS expense_pending_date DATE;

-- ════════════════════════════════════════════════════════════════════════════
-- 2. Extra columns on the existing ETF tables
-- ════════════════════════════════════════════════════════════════════════════
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS nav                 NUMERIC;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS nav_currency        TEXT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS holdings_count      INT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS isin                TEXT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS cusip               TEXT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS domicile            TEXT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS website             TEXT;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS avg_volume          NUMERIC;
ALTER TABLE public.etf_master ADD COLUMN IF NOT EXISTS is_actively_trading BOOLEAN;

ALTER TABLE public.etf_performance ADD COLUMN IF NOT EXISTS return_1m  NUMERIC;
ALTER TABLE public.etf_performance ADD COLUMN IF NOT EXISTS return_3m  NUMERIC;
ALTER TABLE public.etf_performance ADD COLUMN IF NOT EXISTS return_6m  NUMERIC;
ALTER TABLE public.etf_performance ADD COLUMN IF NOT EXISTS return_10y NUMERIC;
ALTER TABLE public.etf_performance ADD COLUMN IF NOT EXISTS return_max NUMERIC;

ALTER TABLE public.etf_company_data ADD COLUMN IF NOT EXISTS cusip           TEXT;
ALTER TABLE public.etf_company_data ADD COLUMN IF NOT EXISTS beta            NUMERIC;
ALTER TABLE public.etf_company_data ADD COLUMN IF NOT EXISTS top_country     TEXT;
ALTER TABLE public.etf_company_data ADD COLUMN IF NOT EXISTS sector_weights  JSONB;
ALTER TABLE public.etf_company_data ADD COLUMN IF NOT EXISTS country_weights JSONB;

-- ════════════════════════════════════════════════════════════════════════════
-- 3. Fund manager tables (Feature 15)
-- ════════════════════════════════════════════════════════════════════════════
-- Full ETF + mutual fund ticker universe from SEC company_tickers_mf.json
CREATE TABLE IF NOT EXISTS public.fund_universe (
    ticker      TEXT PRIMARY KEY,
    cik         TEXT NOT NULL,
    series_id   TEXT NOT NULL,
    class_id    TEXT,
    fund_type   TEXT CHECK (fund_type IN ('ETF', 'MF')),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS fund_universe_series_idx ON public.fund_universe (series_id);

CREATE TABLE IF NOT EXISTS public.fund_managers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id        TEXT NOT NULL,
    cik              TEXT,
    manager_key      TEXT NOT NULL,
    manager_name     TEXT NOT NULL,
    title            TEXT,
    role             TEXT,
    since_year       INT,
    since_month      INT,
    since_raw        TEXT,
    is_inception     BOOLEAN DEFAULT FALSE,
    is_current       BOOLEAN DEFAULT TRUE,
    removed_at       TIMESTAMPTZ,
    source_form      TEXT,
    source_accession TEXT,
    source_url       TEXT,
    filing_date      DATE,
    first_seen_at    TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (series_id, manager_key)
);
CREATE INDEX IF NOT EXISTS fund_managers_current_idx ON public.fund_managers (series_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS fund_managers_name_idx ON public.fund_managers (manager_key);

CREATE TABLE IF NOT EXISTS public.fund_manager_events (
    id                            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id                     TEXT NOT NULL,
    series_name                   TEXT,
    cik                           TEXT,
    tickers                       TEXT[],
    manager_key                   TEXT NOT NULL,
    manager_name                  TEXT NOT NULL,
    event_type                    TEXT CHECK (event_type IN ('ADDED', 'REMOVED')),
    effective_date                DATE,
    effective_date_is_filing_date BOOLEAN DEFAULT TRUE,
    detection_source              TEXT,
    accession                     TEXT NOT NULL,
    form                          TEXT,
    filing_date                   DATE,
    filing_url                    TEXT,
    evidence                      TEXT,
    created_at                    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (accession, series_id, manager_key, event_type)
);
CREATE INDEX IF NOT EXISTS fund_manager_events_series_idx
    ON public.fund_manager_events (series_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.fund_filings_processed (
    accession    TEXT NOT NULL,
    purpose      TEXT NOT NULL CHECK (purpose IN ('EVENT', 'ROSTER')),
    cik          TEXT,
    form         TEXT,
    filing_date  DATE,
    status       TEXT,
    n_series     INT,
    n_items      INT,
    error        TEXT,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (accession, purpose)
);

-- ════════════════════════════════════════════════════════════════════════════
-- 4. Fund action events (Feature 16) — liquidation / merger / fee / strategy
-- ════════════════════════════════════════════════════════════════════════════
-- One row per SEC document examined. doc_key is the idempotency key, so an
-- hourly poll never queues the same supplement twice.
CREATE TABLE IF NOT EXISTS public.etf_fund_events (
    id              BIGSERIAL PRIMARY KEY,
    doc_key         TEXT NOT NULL UNIQUE,   -- accession:filename
    accession       TEXT NOT NULL,
    cik             TEXT,
    form            TEXT,
    filing_date     DATE,
    filing_url      TEXT,
    filer           TEXT,
    event_type      TEXT,
    matched_phrases TEXT[],
    series_ids      TEXT[],
    tickers         TEXT[],
    watched_tickers TEXT[],
    excerpt         TEXT,
    status          TEXT,                   -- QUEUED | NOT_WATCHED | NO_SERIES | NO_MATCH | NO_DOC | ERROR
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS etf_fund_events_created_idx ON public.etf_fund_events (created_at DESC);

-- ════════════════════════════════════════════════════════════════════════════
-- 5. Access — the poller writes with the anon key, so RLS must be off
-- ════════════════════════════════════════════════════════════════════════════
-- Grants alone were not enough on 2026-10-02: RLS was enabled by default on new
-- tables and rejected every insert with 42501. Both are set here.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'etf_master', 'etf_holdings', 'etf_holdings_changes', 'etf_snapshot_state',
    'etf_expenses', 'etf_performance', 'etf_company_data',
    'fund_universe', 'fund_managers', 'fund_manager_events', 'fund_filings_processed',
    'etf_fund_events'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO anon, authenticated, service_role', t);
  END LOOP;
END $$;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- ════════════════════════════════════════════════════════════════════════════
-- 6. Verify — every row should say rls_enabled = false
-- ════════════════════════════════════════════════════════════════════════════
SELECT relname AS table_name, relrowsecurity AS rls_enabled
FROM pg_class
WHERE relnamespace = 'public'::regnamespace
  AND relname IN ('etf_master', 'etf_holdings', 'etf_holdings_changes', 'etf_snapshot_state',
                  'etf_expenses', 'etf_performance', 'etf_company_data',
                  'fund_universe', 'fund_managers', 'fund_manager_events',
                  'fund_filings_processed', 'etf_fund_events')
ORDER BY relname;
