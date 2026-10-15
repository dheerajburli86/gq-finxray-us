-- GQ FinXray US — mutual funds (Feature 18 + MF coverage in 15 / 16)
-- Run ONCE in the Supabase SQL editor, AFTER 2026-10-12_etf_reference.sql.
-- Safe to re-run.

-- ── Scheme master: one row per mutual fund share class with a ticker ────────
-- Filled daily by mf_poller.refresh_mf_master from SEC's fund ticker registry
-- (company_tickers_mf.json) + SEC's series/class file (names, registrant).
CREATE TABLE IF NOT EXISTS public.mf_master (
    ticker          TEXT PRIMARY KEY,
    cik             TEXT NOT NULL,
    series_id       TEXT NOT NULL,
    class_id        TEXT,
    entity_name     TEXT,          -- registrant (trust), e.g. "VANGUARD INDEX FUNDS"
    series_name     TEXT,          -- the fund (scheme), e.g. "Vanguard 500 Index Fund"
    class_name      TEXT,          -- share class, e.g. "Admiral Shares"
    city            TEXT,
    state           TEXT,
    fund_type       TEXT,          -- MUTUAL_FUND | MONEY_MARKET | INSURANCE (only MUTUAL_FUND alerts)
    fund_family     TEXT,          -- FMP, when it covers the fund
    category        TEXT,          -- FMP asset class / category
    aum             NUMERIC,
    expense_ratio   NUMERIC,       -- FMP, percent
    inception_date  DATE,
    is_active       BOOLEAN DEFAULT TRUE,
    fmp_checked_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mf_master_series_idx ON public.mf_master (series_id);
CREATE INDEX IF NOT EXISTS mf_master_cik_idx ON public.mf_master (cik);

-- AMC rollup for mutual funds (FMP family if known, else the registrant name),
-- merged through the same amc_aliases patterns as the ETF side.
CREATE OR REPLACE VIEW public.mf_amc_master AS
WITH s AS (
    SELECT m.*, COALESCE(a.canonical, NULLIF(btrim(COALESCE(m.fund_family, m.entity_name)), '')) AS amc
    FROM public.mf_master m
    LEFT JOIN LATERAL (
        SELECT canonical FROM public.amc_aliases
        WHERE COALESCE(m.fund_family, m.entity_name) ILIKE pattern
        ORDER BY priority, pattern LIMIT 1
    ) a ON TRUE
)
SELECT amc                                   AS fund_house,
       COUNT(DISTINCT series_id)             AS scheme_count,
       COUNT(*)                              AS share_class_count,
       SUM(aum)                              AS total_aum,
       ARRAY_AGG(DISTINCT entity_name)       AS registrants
FROM s
WHERE amc IS NOT NULL
GROUP BY amc;

-- ── Quarterly holdings from SEC N-PORT ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mf_holdings (
    series_id       TEXT NOT NULL,
    security_key    TEXT NOT NULL,   -- ISIN, else CUSIP, else ticker, else name
    security_name   TEXT,
    title           TEXT,
    ticker          TEXT,
    cusip           TEXT,
    isin            TEXT,
    asset_category  TEXT,
    portfolio_pct   NUMERIC,         -- percent of net assets
    value_usd       NUMERIC,
    balance         NUMERIC,         -- shares / principal
    rep_date        DATE,
    accession       TEXT,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (series_id, security_key)
);

CREATE TABLE IF NOT EXISTS public.mf_holdings_state (
    series_id       TEXT PRIMARY KEY,
    accession       TEXT,
    rep_date        DATE,
    net_assets      NUMERIC,
    holdings_count  INT,
    checked_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.mf_holdings_changes (
    id            BIGSERIAL PRIMARY KEY,
    series_id     TEXT NOT NULL,
    security_key  TEXT NOT NULL,
    security_name TEXT,
    action        TEXT NOT NULL,      -- ADDED | REMOVED | RESIZED
    weight_prev   NUMERIC,
    weight_new    NUMERIC,
    rep_date_from DATE,
    rep_date_to   DATE NOT NULL,
    detected_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (series_id, security_key, action, rep_date_to)
);

-- Which series each public N-PORT filing belongs to (read once from the
-- filing header, so a trust with dozens of funds is not re-scanned daily).
CREATE TABLE IF NOT EXISTS public.mf_nport_filings (
    accession     TEXT PRIMARY KEY,
    cik           TEXT,
    series_ids    TEXT[],
    filing_date   DATE,
    primary_doc   TEXT,
    seen_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Fee tables now hold mutual fund share classes too.
ALTER TABLE public.etf_expense_breakdown ADD COLUMN IF NOT EXISTS fund_type TEXT DEFAULT 'ETF';

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['mf_master', 'mf_holdings', 'mf_holdings_state', 'mf_holdings_changes', 'mf_nport_filings'] LOOP
    EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO anon, authenticated, service_role', t);
  END LOOP;
END $$;
GRANT SELECT ON public.mf_amc_master TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- ── Check (after tomorrow's 05:20 ET run, or a manual `python mf_poller.py master`)
-- SELECT COUNT(*) FROM public.mf_master;
-- SELECT * FROM public.mf_amc_master ORDER BY share_class_count DESC LIMIT 20;
