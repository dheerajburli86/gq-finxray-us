-- GQ FinXray US — ETF reference upkeep
-- Run ONCE in the Supabase SQL editor. Safe to re-run.
--
--   1. etf_expense_breakdown   prospectus fee tables (management fee, 12b-1,
--                              other, acquired fund fees, gross, waiver, net)
--   2. etf_listing_state/events  ETF launches and closures vs Massive
--   3. amc_aliases + amc_master  one row per sponsor, whatever FMP calls it
--   4. fund_filings_processed    allow purpose 'EXPENSE'

-- ── 1. Fee tables ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.etf_expense_breakdown (
    ticker               TEXT NOT NULL,
    accession            TEXT NOT NULL,
    series_id            TEXT,
    class_id             TEXT,
    form                 TEXT,
    filing_date          DATE,
    management_fee       NUMERIC,     -- percent of assets, 0.20 = 0.20%
    distribution_12b1    NUMERIC,
    other_expenses       NUMERIC,
    acquired_fund_fees   NUMERIC,
    gross_expense_ratio  NUMERIC,
    fee_waiver           NUMERIC,     -- negative, as printed in the table
    net_expense_ratio    NUMERIC,
    source_url           TEXT,
    extracted_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, accession)
);
CREATE INDEX IF NOT EXISTS etf_expense_breakdown_latest_idx
    ON public.etf_expense_breakdown (ticker, filing_date DESC);
CREATE INDEX IF NOT EXISTS etf_expense_breakdown_series_idx
    ON public.etf_expense_breakdown (series_id);

-- Latest fee table per ETF.
CREATE OR REPLACE VIEW public.etf_expense_latest AS
SELECT DISTINCT ON (ticker) *
FROM public.etf_expense_breakdown
ORDER BY ticker, filing_date DESC NULLS LAST, extracted_at DESC;

-- ── 2. Launches / closures ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.etf_listing_state (
    ticker            TEXT PRIMARY KEY,
    name              TEXT,
    status            TEXT NOT NULL,   -- ACTIVE | PENDING_CLOSE | CLOSED | NOT_LISTED | PENDING_LAUNCH
    confirm_count     INT DEFAULT 0,
    confirm_last      TEXT,            -- ET date of the last counted day
    primary_exchange  TEXT,
    first_seen_at     TIMESTAMPTZ DEFAULT NOW(),
    closed_at         TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.etf_listing_events (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    event_type   TEXT NOT NULL,        -- LAUNCHED | CLOSED | RELISTED
    name         TEXT,
    exchange     TEXT,
    detected_on  DATE NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (ticker, event_type, detected_on)
);
CREATE INDEX IF NOT EXISTS etf_listing_events_recent_idx
    ON public.etf_listing_events (detected_on DESC);

-- ── 3. Sponsor (AMC) names ──────────────────────────────────────────────────
-- FMP's etfCompany is free text: "iShares", "BlackRock", "BlackRock Fund
-- Advisors" are one sponsor. Patterns are ILIKE; lowest priority wins.
CREATE TABLE IF NOT EXISTS public.amc_aliases (
    pattern    TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL,
    priority   INT DEFAULT 100
);

INSERT INTO public.amc_aliases (pattern, canonical, priority) VALUES
  ('%ishares%', 'BlackRock (iShares)', 10), ('%blackrock%', 'BlackRock (iShares)', 11),
  ('%spdr%', 'State Street (SPDR)', 10), ('%state street%', 'State Street (SPDR)', 11),
  ('%ssga%', 'State Street (SPDR)', 12), ('%select sector%', 'State Street (SPDR)', 13),
  ('%vanguard%', 'Vanguard', 10),
  ('%invesco%', 'Invesco', 10), ('%powershares%', 'Invesco', 11),
  ('%schwab%', 'Charles Schwab', 10),
  ('%first trust%', 'First Trust', 10),
  ('%proshare%', 'ProShares', 10),
  ('%direxion%', 'Direxion', 10),
  ('%wisdomtree%', 'WisdomTree', 10),
  ('%vaneck%', 'VanEck', 10), ('%van eck%', 'VanEck', 11), ('%market vectors%', 'VanEck', 12),
  ('%global x%', 'Global X', 10),
  ('%j.p. morgan%', 'J.P. Morgan', 10), ('%jpmorgan%', 'J.P. Morgan', 11), ('%jp morgan%', 'J.P. Morgan', 12),
  ('%fidelity%', 'Fidelity', 10),
  ('%franklin%', 'Franklin Templeton', 10),
  ('%dimensional%', 'Dimensional', 10),
  ('%goldman%', 'Goldman Sachs', 10),
  ('%pacer%', 'Pacer', 10),
  ('%ark invest%', 'ARK Invest', 10), ('ark etf%', 'ARK Invest', 11),
  ('%amplify%', 'Amplify', 10),
  ('%innovator%', 'Innovator', 10),
  ('%janus%', 'Janus Henderson', 10),
  ('%pimco%', 'PIMCO', 10),
  ('%xtrackers%', 'DWS (Xtrackers)', 10), ('%dws%', 'DWS (Xtrackers)', 11),
  ('%avantis%', 'Avantis (American Century)', 10), ('%american century%', 'Avantis (American Century)', 11),
  ('%yieldmax%', 'YieldMax', 10), ('%tidal%', 'Tidal', 20),
  ('%roundhill%', 'Roundhill', 10),
  ('%graniteshares%', 'GraniteShares', 10),
  ('%defiance%', 'Defiance', 10),
  ('%nuveen%', 'Nuveen', 10), ('%teucrium%', 'Teucrium', 10),
  ('%neos%', 'NEOS', 10), ('%simplify%', 'Simplify', 10),
  ('%principal%', 'Principal', 10), ('%hartford%', 'Hartford', 10),
  ('%columbia%', 'Columbia Threadneedle', 10), ('%capital group%', 'Capital Group', 10),
  ('%t. rowe%', 'T. Rowe Price', 10), ('%t rowe%', 'T. Rowe Price', 11),
  ('%morgan stanley%', 'Morgan Stanley', 10), ('%eaton vance%', 'Morgan Stanley', 11),
  ('%calamos%', 'Calamos', 10), ('%grayscale%', 'Grayscale', 10),
  ('%bitwise%', 'Bitwise', 10), ('%sprott%', 'Sprott', 10),
  ('%krane%', 'KraneShares', 10), ('%kraneshares%', 'KraneShares', 9),
  ('%alps%', 'ALPS', 10), ('%virtus%', 'Virtus', 10), ('%putnam%', 'Franklin Templeton', 11),
  ('%dimensional fund%', 'Dimensional', 9), ('%rex %', 'REX Shares', 10), ('%rex shares%', 'REX Shares', 9),
  ('%us global%', 'U.S. Global Investors', 10), ('%u.s. global%', 'U.S. Global Investors', 10),
  ('%united states commodity%', 'USCF', 10), ('%uscf%', 'USCF', 11)
ON CONFLICT (pattern) DO UPDATE SET canonical = EXCLUDED.canonical, priority = EXCLUDED.priority;

-- Canonical sponsor per ETF; unmatched names pass through unchanged.
CREATE OR REPLACE VIEW public.etf_sponsor AS
SELECT m.ticker,
       m.fund_house AS fund_house_raw,
       COALESCE(a.canonical, NULLIF(btrim(m.fund_house), '')) AS fund_house
FROM public.etf_master m
LEFT JOIN LATERAL (
    SELECT canonical FROM public.amc_aliases
    WHERE m.fund_house ILIKE pattern
    ORDER BY priority, pattern
    LIMIT 1
) a ON TRUE;

-- amc_master now rolls up by canonical sponsor. Same columns as before plus
-- the raw spellings that were merged into each row.
DROP VIEW IF EXISTS public.amc_master;
CREATE VIEW public.amc_master AS
SELECT
    sp.fund_house                                          AS fund_house,
    COUNT(*)                                               AS etf_count,
    COUNT(*) FILTER (WHERE m.is_actively_trading)          AS active_etf_count,
    SUM(m.aum)                                             AS total_aum,
    ROUND(AVG(s.expense_ratio)::numeric, 4)                AS avg_expense_ratio,
    MIN(s.expense_ratio)                                   AS min_expense_ratio,
    MAX(s.expense_ratio)                                   AS max_expense_ratio,
    COUNT(s.expense_ratio)                                 AS etfs_with_expense,
    MIN(m.inception_date)                                  AS first_launch,
    MAX(m.inception_date)                                  AS latest_launch,
    (ARRAY_AGG(m.ticker ORDER BY m.aum DESC NULLS LAST))[1] AS largest_etf,
    MAX(m.aum)                                             AS largest_etf_aum,
    ARRAY_AGG(DISTINCT sp.fund_house_raw)                  AS raw_names
FROM public.etf_master m
JOIN public.etf_sponsor sp ON sp.ticker = m.ticker
LEFT JOIN public.etf_snapshot_state s ON s.ticker = m.ticker
WHERE sp.fund_house IS NOT NULL
GROUP BY sp.fund_house;

-- ── 4. Filing ledger: allow the fee-table purpose ───────────────────────────
ALTER TABLE public.fund_filings_processed DROP CONSTRAINT IF EXISTS fund_filings_processed_purpose_check;
ALTER TABLE public.fund_filings_processed
    ADD CONSTRAINT fund_filings_processed_purpose_check CHECK (purpose IN ('EVENT', 'ROSTER', 'EXPENSE'));

-- ── Access (same convention as 2026-10-02_etf_alerts.sql) ───────────────────
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['etf_expense_breakdown', 'etf_listing_state', 'etf_listing_events', 'amc_aliases'] LOOP
    EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO anon, authenticated, service_role', t);
  END LOOP;
END $$;
GRANT SELECT ON public.etf_expense_latest, public.etf_sponsor, public.amc_master
    TO anon, authenticated, service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- ── Check ───────────────────────────────────────────────────────────────────
-- Sponsors still unmatched (add a pattern for any big name here):
SELECT fund_house, etf_count, total_aum, raw_names
FROM public.amc_master
WHERE fund_house NOT IN (SELECT DISTINCT canonical FROM public.amc_aliases)
ORDER BY total_aum DESC NULLS LAST
LIMIT 25;
