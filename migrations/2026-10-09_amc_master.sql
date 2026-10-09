-- GQ FinXray US — AMC master
-- Run ONCE in the Supabase SQL editor. Safe to re-run.
--
-- The India system gets an AMC list from CMOTS as its own feed. There is no US
-- equivalent to subscribe to, but we are already storing the piece that matters:
-- etf_fmp_client.master_row writes FMP's etfCompany into etf_master.fund_house
-- on EVERY run, light ones included. So the sponsor layer is not missing data,
-- it is missing a way to read it.
--
-- A view rather than a table, deliberately. Every column below is derived from
-- etf_master and etf_snapshot_state, so a table would need a refresh job and
-- would be wrong between runs -- a sponsor's AUM would be whatever it was when
-- the job last fired. A view is correct the moment the poller writes.

CREATE OR REPLACE VIEW public.amc_master AS
SELECT
    m.fund_house                                          AS fund_house,
    COUNT(*)                                              AS etf_count,
    COUNT(*) FILTER (WHERE m.is_actively_trading)         AS active_etf_count,
    SUM(m.aum)                                            AS total_aum,
    -- Expense stats come from snapshot_state, not etf_expenses: state holds the
    -- CONFIRMED ratio (seen on two separate days), which is the one the product
    -- alerts on. etf_expenses is the dated history behind it.
    ROUND(AVG(s.expense_ratio)::numeric, 4)               AS avg_expense_ratio,
    MIN(s.expense_ratio)                                  AS min_expense_ratio,
    MAX(s.expense_ratio)                                  AS max_expense_ratio,
    COUNT(s.expense_ratio)                                AS etfs_with_expense,
    MIN(m.inception_date)                                 AS first_launch,
    MAX(m.inception_date)                                 AS latest_launch,
    (ARRAY_AGG(m.ticker ORDER BY m.aum DESC NULLS LAST))[1] AS largest_etf,
    MAX(m.aum)                                            AS largest_etf_aum
FROM public.etf_master m
LEFT JOIN public.etf_snapshot_state s ON s.ticker = m.ticker
WHERE m.fund_house IS NOT NULL
  AND btrim(m.fund_house) <> ''
GROUP BY m.fund_house;

COMMENT ON VIEW public.amc_master IS
    'Sponsor/AMC rollup derived from etf_master.fund_house (FMP etfCompany). '
    'Always current; no refresh job.';

GRANT SELECT ON public.amc_master TO anon, authenticated, service_role;

-- Which ETFs belong to a sponsor, for drilling in from the rollup above.
CREATE INDEX IF NOT EXISTS etf_master_fund_house_idx
    ON public.etf_master (fund_house);

-- ── Check ────────────────────────────────────────────────────────────────────
-- Expect the familiar names at the top: iShares, Vanguard, SPDR, Invesco,
-- Schwab. FMP's etfCompany spelling is NOT normalised -- one sponsor can appear
-- under several strings ("SPDR" vs "State Street Global Advisors"), which
-- splits its rollup. Read the list before trusting a total, and send it over if
-- the big families look split.
SELECT fund_house, etf_count, total_aum, avg_expense_ratio, largest_etf
FROM public.amc_master
ORDER BY total_aum DESC NULLS LAST
LIMIT 25;
