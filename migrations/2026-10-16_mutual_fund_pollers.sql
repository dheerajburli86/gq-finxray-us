-- GQ FinXray US — tables for the mutual fund pollers (Features 19-22)
--
-- Run this whole file once in the Supabase SQL editor. Safe to re-run: every
-- statement is IF NOT EXISTS or an idempotent ALTER, and nothing is dropped.
--
--   mf_managers           current portfolio manager roster per fund   (Feature 19)
--   mf_manager_events     every manager change seen                   (Feature 19)
--   mf_expense_breakdown  prospectus fee table per share class        (Feature 20)
--   mf_fund_events        one row per fund-action document examined   (Feature 21)
--   mf_scheme_snapshot    SEC's fund register as last seen            (Feature 22)
--   mf_scheme_events      what changed in that register               (Feature 22)
--   mf_security_master    company data for what the funds hold
--   mf_filings_processed  one SEC document, read once
--
-- Nothing here touches the ETF tables or the mutual fund tables that already
-- exist (mf_master, mf_holdings, mf_holdings_state, mf_nport_filings).

-- ── Feature 19 — fund managers ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_managers (
    id              BIGSERIAL PRIMARY KEY,
    series_id       TEXT NOT NULL,
    manager_key     TEXT NOT NULL,          -- normalized name, the identity across filings
    manager_name    TEXT,
    title           TEXT,
    role            TEXT,                   -- lead / co / member, when the filing says
    since_year      INT,
    since_month     INT,
    since_raw       TEXT,
    is_inception    BOOLEAN DEFAULT FALSE,
    is_current      BOOLEAN DEFAULT TRUE,
    removed_at      TIMESTAMPTZ,
    source_form     TEXT,
    source_accession TEXT,
    source_url      TEXT,
    filing_date     DATE,
    cik             TEXT,
    last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS mf_managers_uniq ON public.mf_managers (series_id, manager_key);
CREATE INDEX IF NOT EXISTS mf_managers_current_idx ON public.mf_managers (series_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS public.mf_manager_events (
    id              BIGSERIAL PRIMARY KEY,
    series_id       TEXT NOT NULL,
    cik             TEXT,
    manager_key     TEXT NOT NULL,
    manager_name    TEXT,
    event_type      TEXT NOT NULL,          -- ADDED | REMOVED
    effective_date  DATE,
    effective_date_is_filing_date BOOLEAN DEFAULT TRUE,
    detection_source TEXT,                  -- STICKER_497 | REPLACEMENT_TABLE | PROSPECTUS_ROSTER
    accession       TEXT,
    form            TEXT,
    filing_date     DATE,
    filing_url      TEXT,
    evidence        TEXT,
    tickers         JSONB,
    series_name     TEXT,
    fund_family     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
-- A trust files the same supplement under several accessions, one per
-- prospectus. The accession is in the key so each document is recorded, while
-- the poller's own 180-day memory check stops the second copy alerting.
CREATE UNIQUE INDEX IF NOT EXISTS mf_manager_events_uniq
    ON public.mf_manager_events (series_id, manager_key, event_type, accession);
CREATE INDEX IF NOT EXISTS mf_manager_events_recent_idx
    ON public.mf_manager_events (series_id, created_at DESC);

-- ── Feature 20 — expense ratios ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_expense_breakdown (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    series_id           TEXT,
    class_id            TEXT,
    management_fee      NUMERIC,            -- all figures are percent: 0.55 = 0.55%
    distribution_12b1   NUMERIC,
    other_expenses      NUMERIC,
    acquired_fund_fees  NUMERIC,
    gross_expense_ratio NUMERIC,
    fee_waiver          NUMERIC,            -- stored negative
    net_expense_ratio   NUMERIC,
    accession           TEXT NOT NULL,
    form                TEXT,
    filing_date         DATE,
    source_url          TEXT,
    cik                 TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS mf_expense_breakdown_uniq
    ON public.mf_expense_breakdown (ticker, accession);
CREATE INDEX IF NOT EXISTS mf_expense_breakdown_ticker_idx
    ON public.mf_expense_breakdown (ticker, filing_date DESC);
CREATE INDEX IF NOT EXISTS mf_expense_breakdown_series_idx
    ON public.mf_expense_breakdown (series_id);

-- ── Feature 21 — fund actions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_fund_events (
    id              BIGSERIAL PRIMARY KEY,
    doc_key         TEXT NOT NULL,          -- accession:filename — one document, once
    accession       TEXT,
    cik             TEXT,
    form            TEXT,
    filing_date     DATE,
    filing_url      TEXT,
    filer           TEXT,
    status          TEXT,                   -- QUEUED | NO_MATCH | NOT_WATCHED | NO_SERIES | NO_DOC | ERROR
    event_type      TEXT,
    series_ids      JSONB,
    tickers         JSONB,
    matched_phrases JSONB,
    excerpt         TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS mf_fund_events_doc_uniq ON public.mf_fund_events (doc_key);
CREATE INDEX IF NOT EXISTS mf_fund_events_status_idx ON public.mf_fund_events (status, created_at DESC);

-- ── Feature 22 — scheme & family register ────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_scheme_snapshot (
    ticker      TEXT PRIMARY KEY,
    series_id   TEXT,
    class_id    TEXT,
    cik         TEXT,
    series_name TEXT,
    class_name  TEXT,
    entity_name TEXT,
    fund_type   TEXT,
    is_active   BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mf_scheme_snapshot_series_idx ON public.mf_scheme_snapshot (series_id);

CREATE TABLE IF NOT EXISTS public.mf_scheme_events (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,              -- NEW_SHARE_CLASS | TICKER_RETIRED | FUND_RENAMED | FAMILY_CHANGE
    ticker      TEXT,
    series_id   TEXT,
    class_id    TEXT,
    cik         TEXT,
    series_name TEXT,
    class_name  TEXT,
    entity_name TEXT,
    detail      JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
-- One register change is recorded once. A fund renamed twice in a year is two
-- rows because the new name is in `detail`, not in this key — which is why the
-- poller also carries its own dedup_key on the alert.
CREATE UNIQUE INDEX IF NOT EXISTS mf_scheme_events_uniq
    ON public.mf_scheme_events (event_type, COALESCE(ticker, ''), COALESCE(series_id, ''));

-- ── Company data for what the funds hold ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_security_master (
    security_key    TEXT PRIMARY KEY,       -- ISIN, else CUSIP, else ticker: matches mf_holdings
    ticker          TEXT,
    name            TEXT,
    short_name      TEXT,
    cusip           TEXT,
    isin            TEXT,
    cik             TEXT,                   -- the US equivalent of a company code
    asset_category  TEXT,                   -- N-PORT: EC equity, DBT debt, …
    sector          TEXT,
    industry        TEXT,
    exchange        TEXT,                   -- NYSE / NASDAQ: the US equivalent of BSE/NSE codes
    country         TEXT,
    currency        TEXT,
    market_cap      NUMERIC,
    cap_class       TEXT,                   -- MEGA_CAP | LARGE_CAP | MID_CAP | SMALL_CAP | MICRO_CAP | NANO_CAP
    is_fund         BOOLEAN DEFAULT FALSE,
    source          TEXT,                   -- FMP | STOCKS | NPORT
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mf_security_master_ticker_idx ON public.mf_security_master (ticker);
CREATE INDEX IF NOT EXISTS mf_security_master_sector_idx ON public.mf_security_master (sector);

-- ── One SEC document, read once ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_filings_processed (
    id           BIGSERIAL PRIMARY KEY,
    doc_key      TEXT NOT NULL,             -- accession:filename
    purpose      TEXT NOT NULL,             -- MGR_EVENT | MGR_ROSTER | FEE
    accession    TEXT,
    cik          TEXT,
    form         TEXT,
    filing_date  DATE,
    status       TEXT,
    detail       JSONB,
    processed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS mf_filings_processed_uniq
    ON public.mf_filings_processed (doc_key, purpose);
CREATE INDEX IF NOT EXISTS mf_filings_processed_status_idx
    ON public.mf_filings_processed (purpose, status);

-- ── Access ───────────────────────────────────────────────────────────────────
-- Same posture as the other poller tables: the service writes through the
-- Supabase key, so RLS is off and the roles can read and write.
ALTER TABLE public.mf_managers           DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_manager_events     DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_expense_breakdown  DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_fund_events        DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_scheme_snapshot    DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_scheme_events      DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_security_master    DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.mf_filings_processed  DISABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_managers          TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_manager_events    TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_expense_breakdown TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_fund_events       TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_scheme_snapshot   TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_scheme_events     TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_security_master   TO anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.mf_filings_processed TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- ── Check after running ──────────────────────────────────────────────────────
-- SELECT table_name FROM information_schema.tables
--  WHERE table_schema = 'public' AND table_name LIKE 'mf_%' ORDER BY table_name;
-- Expect: mf_expense_breakdown, mf_filings_processed, mf_fund_events, mf_holdings,
--         mf_holdings_changes, mf_holdings_state, mf_manager_events, mf_managers,
--         mf_master, mf_nport_filings, mf_scheme_events, mf_scheme_snapshot,
--         mf_security_master
