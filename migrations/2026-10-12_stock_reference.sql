-- GQ FinXray US — stock reference data (Features 18-21)
-- Run ONCE in the Supabase SQL editor. Safe to re-run.
--
-- The India system gets a company master from CMOTS. The US side had only
-- `stocks` (ticker, exchange, sector) from a one-time CSV import that nothing
-- refreshes. These tables are filled daily from SEC + FMP + Massive by
-- stock_master_poller.py and stock_ownership_poller.py. `stocks` is left as is.

-- ── Company master ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stock_master (
    ticker            TEXT PRIMARY KEY,
    cik               TEXT,
    name              TEXT,
    exchange          TEXT,          -- SEC: NYSE / Nasdaq / CBOE / OTC
    primary_exchange  TEXT,          -- Massive MIC: XNYS, XNAS, XASE, ARCX, BATS
    security_type     TEXT,          -- Massive: CS, ADRC, PFD, ETF ...
    is_etf            BOOLEAN DEFAULT FALSE,
    sector            TEXT,
    industry          TEXT,
    country           TEXT,
    market_cap        NUMERIC,
    mcap_class        TEXT,          -- MEGA / LARGE / MID / SMALL / MICRO / NANO
    indexes           TEXT[] DEFAULT '{}',   -- SP500 / NASDAQ100 / DOW30
    isin              TEXT,
    cusip             TEXT,
    composite_figi    TEXT,
    ipo_date          DATE,
    is_active         BOOLEAN DEFAULT TRUE,
    delisted_at       TIMESTAMPTZ,
    renamed_to        TEXT,          -- set on the OLD row after a symbol change
    pending           JSONB DEFAULT '{}'::jsonb,  -- unconfirmed changes
    profile_at        TIMESTAMPTZ,
    first_seen_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS stock_master_cik_idx ON public.stock_master (cik);
CREATE INDEX IF NOT EXISTS stock_master_mcap_idx ON public.stock_master (mcap_class);

-- Every confirmed change, watched or not. The alert is a separate row in
-- `alerts`; this is the history.
CREATE TABLE IF NOT EXISTS public.stock_master_events (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    cik          TEXT,
    event_type   TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    detected_on  DATE NOT NULL,
    detected_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (ticker, event_type, new_value, detected_on)
);
CREATE INDEX IF NOT EXISTS stock_master_events_ticker_idx
    ON public.stock_master_events (ticker, detected_at DESC);

CREATE TABLE IF NOT EXISTS public.stock_index_members (
    index_code        TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    name              TEXT,
    sector            TEXT,
    date_first_added  TEXT,
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (index_code, ticker)
);

-- ── 13F institutional ownership, one row per stock per quarter ──────────────
CREATE TABLE IF NOT EXISTS public.stock_13f_summary (
    ticker                    TEXT NOT NULL,
    year                      INT NOT NULL,
    quarter                   INT NOT NULL,
    investors_holding         INT,
    investors_holding_change  INT,
    shares_13f                NUMERIC,
    shares_13f_change         NUMERIC,
    ownership_pct             NUMERIC,
    ownership_pct_change      NUMERIC,
    new_positions             INT,
    closed_positions          INT,
    increased_positions       INT,
    reduced_positions         INT,
    put_call_ratio            NUMERIC,
    raw                       JSONB,
    fetched_at                TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, year, quarter)
);

-- Small key/value store for poller watermarks.
CREATE TABLE IF NOT EXISTS public.stock_ref_state (
    key         TEXT PRIMARY KEY,
    value       JSONB,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Which ETFs hold a stock (reverse of etf_holdings) ───────────────────────
CREATE INDEX IF NOT EXISTS etf_holdings_symbol_idx
    ON public.etf_holdings (security_symbol);
CREATE INDEX IF NOT EXISTS etf_holdings_changes_id_symbol_idx
    ON public.etf_holdings_changes (id, security_symbol);

CREATE OR REPLACE VIEW public.stock_etf_holders AS
SELECT h.security_symbol AS ticker,
       h.ticker          AS etf_ticker,
       m.name            AS etf_name,
       m.fund_house,
       h.portfolio_pct,
       h.shares,
       h.market_value,
       m.aum             AS etf_aum,
       h.as_of
FROM public.etf_holdings h
JOIN public.etf_master m ON m.ticker = h.ticker
WHERE h.security_symbol IS NOT NULL AND h.security_symbol <> '';

-- Same convention as 2026-10-02_etf_alerts.sql: RLS on by default broke the
-- backend's writes, so these are opened to the API roles explicitly.
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['stock_master', 'stock_master_events', 'stock_index_members',
                           'stock_13f_summary', 'stock_ref_state'] LOOP
    EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO anon, authenticated, service_role', t);
  END LOOP;
END $$;
GRANT SELECT ON public.stock_etf_holders TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- ── Check (after the first poller run) ──────────────────────────────────────
-- SELECT mcap_class, COUNT(*) FROM public.stock_master WHERE is_active GROUP BY 1 ORDER BY 2 DESC;
-- SELECT * FROM public.stock_etf_holders WHERE ticker = 'AAPL' ORDER BY market_value DESC NULLS LAST LIMIT 10;
