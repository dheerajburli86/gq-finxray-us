-- GQ FinXray US — opt-in for the ETF signal engine (etf_signals_poller.py)
-- Run ONCE in the Supabase SQL editor. Safe to re-run. Takes a brief lock on
-- user_preferences only (adding a column with a constant default is instant).
--
-- Default FALSE: nobody's feed changes until their row is switched on. Opted-in
-- users get Feature 7 v2 momentum + Feature 17 price signals for the ETFs on
-- their own watchlist, and stop receiving v1 ETF_FLOW rows (v2 replaces them).

ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS etf_signals_beta boolean NOT NULL DEFAULT false;

-- Dheeraj only. Selecting from users means a wrong id inserts nothing instead
-- of opting in a user that does not exist.
INSERT INTO public.user_preferences (user_id, etf_signals_beta)
SELECT id, true FROM public.users WHERE id = 'a1146826-39ea-48c5-9236-45405ff166f2'
ON CONFLICT (user_id) DO UPDATE SET etf_signals_beta = true;

-- ── Check ────────────────────────────────────────────────────────────────────
-- Expect exactly one row: your Telegram username, true, and 17 NOT in
-- muted_features.
SELECT u.telegram_username, p.etf_signals_beta, p.muted_features, p.min_impact
FROM public.user_preferences p
JOIN public.users u ON u.id = p.user_id
WHERE p.etf_signals_beta;
