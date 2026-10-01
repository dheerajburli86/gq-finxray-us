-- ============================================================================
-- GQ FinXray US — the two logs, 2026-09-09
--
-- LOG 1  public.alert_run_log   every alert the system produces
-- LOG 2  public.payload_log     the XBRL/JSON payloads and links those alerts carry
--
-- Both tables already exist (2026-09-08_payload_log.sql and
-- 2026-09-09_schema_ready.sql create them and every column used below). What
-- changed in the code, and why this file exists:
--
--   alert_run_log was NEVER WRITTEN. main.py defined a log_alert_run() helper
--   and no caller ever invoked it, so the table stayed empty from the day it was
--   created. The writer now lives in delivery.py, which is the only place that
--   knows whether Telegram actually accepted a message, and it fires once per
--   alert at the moment the alert is settled — including alerts that found no
--   audience, so "built but delivered to nobody" is visible as a row with
--   telegram_success = false rather than being invisible.
--
--   payload_log was already wired, but market-wide alerts were being discarded
--   before delivery ever reached the logging call, so whole feature families
--   never produced a row.
--
-- This migration is additive and idempotent: indexes only, no column or table
-- changes, so it is safe to run against a live database at any time.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- LOG 1 — alert_run_log
-- ----------------------------------------------------------------------------
-- The whole point of feature tagging is per-feature monitoring ("is Feature 5
-- producing anything?"), and that question is a filter on feature_id over a time
-- range. Without this index it is a sequential scan of every alert ever sent.
CREATE INDEX IF NOT EXISTS alert_run_log_feature_created_idx
    ON public.alert_run_log (feature_id, created_at DESC);

-- "Which alerts failed to reach anyone, and when" — the query you actually run
-- when a subscriber says they stopped receiving something.
CREATE INDEX IF NOT EXISTS alert_run_log_failed_idx
    ON public.alert_run_log (created_at DESC)
    WHERE telegram_success IS NOT TRUE;

CREATE INDEX IF NOT EXISTS alert_run_log_ticker_idx
    ON public.alert_run_log (ticker, created_at DESC);

-- One row per alert per settlement. Not UNIQUE on alert_id: an alert deferred
-- for a transient Telegram failure is retried on a later cycle and legitimately
-- logs again, and losing that second row would hide the retry.
CREATE INDEX IF NOT EXISTS alert_run_log_alert_idx
    ON public.alert_run_log (alert_id);


-- ----------------------------------------------------------------------------
-- LOG 2 — payload_log
-- ----------------------------------------------------------------------------
-- Already carries a UNIQUE index on alert_id plus ticker/created_at/type
-- indexes from 2026-09-08_payload_log.sql. One addition: filing_type is how you
-- pull "every Result Snapshot payload this quarter" or "every Form 4 payload",
-- which is the shape the frontend integration will read.
CREATE INDEX IF NOT EXISTS payload_log_filing_type_idx
    ON public.payload_log (filing_type, created_at DESC);


-- ----------------------------------------------------------------------------
-- Verification — run after deploying. Both should be climbing within an hour of
-- market open; alert_run_log fills for every feature, payload_log only for the
-- features that carry structured data (Result Snapshot, Form 4, transcripts,
-- and any SEC filing, which carries companyfacts/filing-index JSON links).
-- ----------------------------------------------------------------------------
-- SELECT feature_id, feature_name,
--        count(*)                                        AS alerts,
--        count(*) FILTER (WHERE telegram_success)         AS delivered,
--        count(*) FILTER (WHERE NOT telegram_success)     AS no_recipient_or_failed,
--        sum(total_tokens)                                AS tokens
--   FROM public.alert_run_log
--  WHERE created_at > now() - interval '24 hours'
--  GROUP BY 1, 2
--  ORDER BY 1;
--
-- SELECT payload_type, filing_type, count(*), max(created_at)
--   FROM public.payload_log
--  WHERE created_at > now() - interval '24 hours'
--  GROUP BY 1, 2
--  ORDER BY 3 DESC;
