-- ============================================================================
-- GQ FinXray US — production hardening, 2026-08-13
--
-- NOT APPLIED AUTOMATICALLY. Read each section before running it.
-- Run sections IN ORDER. Section 1 is a prerequisite for Section 2.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0 — PREREQUISITE CHECK: which key does the backend use?
--
-- This decides whether Section 2 is safe or catastrophic.
--   * service_role key  -> BYPASSES RLS entirely. Section 2 is safe.
--   * anon key          -> RLS BLOCKS EVERYTHING. Section 2 takes the system
--                          down until policies exist for the anon role.
--
-- Decode the SUPABASE_KEY your deployment uses at https://jwt.io and read the
-- "role" claim, or run this while the pollers are live:
-- ----------------------------------------------------------------------------
SELECT usename AS connected_role, count(*) AS connections
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1 ORDER BY 2 DESC;
-- Expect 'authenticator' for PostgREST traffic. If you see meaningful traffic
-- and cannot confirm the key is service_role, STOP and confirm before Section 2.


-- ----------------------------------------------------------------------------
-- SECTION 1 — Prevent duplicate watchlist rows.  SAFE TO RUN NOW.
--
-- Two real incidents trace to this table:
--   a) rows with user_id IS NULL (orphans) routed to nobody;
--   b) no UNIQUE(user_id, ticker), so `/add AAPL` twice inserted two rows and
--      the user received every AAPL alert twice. telegram_bot.add_ticker now
--      checks before inserting, but the constraint is the real guarantee.
-- ----------------------------------------------------------------------------

-- 1a. Remove orphaned rows that can never be delivered to anyone.
DELETE FROM public.watchlists WHERE user_id IS NULL;

-- 1b. Collapse existing duplicates, keeping the earliest row per (user, ticker).
DELETE FROM public.watchlists a
USING public.watchlists b
WHERE a.user_id = b.user_id
  AND upper(a.ticker) = upper(b.ticker)
  AND a.ctid > b.ctid;

-- 1c. Normalise casing so the constraint below actually catches 'aapl' vs 'AAPL'.
UPDATE public.watchlists SET ticker = upper(ticker) WHERE ticker <> upper(ticker);

-- 1d. Enforce it going forward.
ALTER TABLE public.watchlists
  ALTER COLUMN user_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS watchlists_user_ticker_uniq
  ON public.watchlists (user_id, ticker);


-- ----------------------------------------------------------------------------
-- SECTION 2 — Enable Row Level Security.  READ SECTION 0 FIRST.
--
-- Today all 12 tables are readable AND writable by anyone holding the anon key,
-- including public.users (telegram_chat_id, email, whatsapp_number).
--
-- service_role bypasses RLS, so with a service_role backend these statements
-- close the hole with zero application impact. Enabling RLS with NO policies is
-- deliberate here: it denies anon entirely. Add policies only if you later need
-- browser/client access.
--
-- Apply to ONE table first, confirm the pollers still write, then do the rest.
-- ----------------------------------------------------------------------------

-- Start with the least critical table as a canary:
ALTER TABLE public.market_data_cache ENABLE ROW LEVEL SECURITY;

-- Confirm a poller still writes, then continue:
-- ALTER TABLE public.users             ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.watchlists        ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.raw_filings       ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.ai_summaries      ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.alerts            ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.subscriptions     ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.delivery_logs     ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.user_preferences  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.event_categories  ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.feedback          ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE public.stocks            ENABLE ROW LEVEL SECURITY;

-- ROLLBACK for any table, if something breaks:
-- ALTER TABLE public.<table> DISABLE ROW LEVEL SECURITY;


-- ----------------------------------------------------------------------------
-- SECTION 3 — Requeue summaries lost to the truncation bug.
--
-- RUN THIS ONLY AFTER THE FIXED ai_pipeline.py IS DEPLOYED. On the old code
-- these rows will simply be truncated and re-flagged, at full LLM cost.
--
-- Context: gemini-2.5-flash is a reasoning model whose thinking tokens counted
-- against max_tokens=600, so summaries were cut off mid-sentence and rejected
-- as too_short/incomplete. ~560 rows were flagged for a fault in the caller.
-- ----------------------------------------------------------------------------

-- 3a. Look before you leap.
SELECT r.source, r.filing_type, count(*) AS rows_to_requeue
FROM public.raw_filings r
WHERE r.status = 'FLAGGED_FOR_REVIEW'
GROUP BY 1, 2 ORDER BY 3 DESC;

-- 3b. Requeue in one controlled batch. Start with a LIMIT to sanity-check cost
--     and output quality before doing all of them.
UPDATE public.raw_filings
SET status = 'PENDING'
WHERE id IN (
  SELECT id FROM public.raw_filings
  WHERE status = 'FLAGGED_FOR_REVIEW'
  ORDER BY created_at DESC
  LIMIT 25
);

-- 3c. Once satisfied with the requeued batch, release the rest:
-- UPDATE public.raw_filings
-- SET status = 'PENDING'
-- WHERE status = 'FLAGGED_FOR_REVIEW';

-- 3d. Clear the review queue for rows that have been requeued, so the backlog
--     count reflects reality rather than the old failures.
-- DELETE FROM public.flagged_summaries
-- WHERE filing_id IN (SELECT id FROM public.raw_filings WHERE status = 'PENDING');


-- ----------------------------------------------------------------------------
-- SECTION 4 — Post-deploy verification. Run ~1 hour after the fix is live.
-- ----------------------------------------------------------------------------

-- 4a. Summarisation should now mostly succeed. PROCESSED should climb;
--     FLAGGED_FOR_REVIEW should stop growing.
SELECT status, count(*), max(created_at) AS latest
FROM public.raw_filings
WHERE created_at > now() - interval '1 hour'
GROUP BY 1 ORDER BY 2 DESC;

-- 4b. Alerts should be reaching real users, not dying with no audience.
SELECT d.status, count(*) AS n
FROM public.alert_deliveries d
WHERE d.created_at > now() - interval '24 hours'
GROUP BY 1;

-- 4c. Alerts marked delivered that reached nobody (should trend to ~0 for
--     watchlisted tickers; ticker='MARKET' rows are expected while
--     GQ_ENABLE_MARKET_WIDE is false).
SELECT a.ticker, a.source, count(*) AS undelivered_fanouts
FROM public.alerts a
LEFT JOIN public.alert_deliveries d ON d.alert_id = a.id
WHERE a.delivered = true
  AND d.id IS NULL
  AND a.created_at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 3 DESC;

-- 4d. Poller health: anything spiking here is a live regression.
SELECT poller_name, count(*) AS errors, max(occurred_at) AS latest
FROM public.poller_error_log
WHERE occurred_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
