-- ============================================================================
-- GQ FinXray US — payload logger, 2026-09-08
--
-- Every alert that carries a structured XBRL/JSON payload (Result Snapshot,
-- Form 4 insider trading, S-1 IPO detail, earnings-call content) gets logged
-- here the moment it actually goes out to Telegram. This is independent of
-- any frontend integration — GQUANTS_ALERT_BASE_URL can stay unset and this
-- table still fills up. It exists purely as the audit trail: "what XBRL/JSON
-- data did we generate and when did it reach a user's channel."
--
-- Run this once against Supabase before deploying delivery.py's payload
-- logging call. If the table does not exist yet, that insert fails silently
-- (logged as a warning) and delivery is unaffected either way.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.payload_log (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id       uuid REFERENCES public.alerts(id),
    ticker         text,
    payload_type   text,        -- 'fr', 'it', 'ipo', 'earning_calls', 'tradingview'
    filing_type    text,
    source         text,
    payload        jsonb NOT NULL,
    frontend_link  text,        -- populated only if GQUANTS_ALERT_BASE_URL is set
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per alert — delivery.py upserts on this so a retried delivery cycle
-- never duplicates the payload it already logged.
CREATE UNIQUE INDEX IF NOT EXISTS payload_log_alert_id_uniq
    ON public.payload_log (alert_id);

CREATE INDEX IF NOT EXISTS payload_log_ticker_idx ON public.payload_log (ticker);
CREATE INDEX IF NOT EXISTS payload_log_created_at_idx ON public.payload_log (created_at DESC);
CREATE INDEX IF NOT EXISTS payload_log_type_idx ON public.payload_log (payload_type);
