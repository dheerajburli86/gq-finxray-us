# SEC JSON Feature 4 Fix

## The Problem
SEC JSON endpoints (companyfacts, submissions, filing_index) were being built correctly but were not appearing in alerts or being logged to the payload_log table.

## Root Cause
The `extra` column was missing from the `alerts` and `raw_filings` tables in the database schema. The code was attempting to store SEC JSON data in this column, but without the column definition:
- The JSON data could not be persisted
- The links could not be rendered in Telegram messages
- The payload logger could not access the data

## The Solution
Added the `extra` column to both tables in the migration:

```sql
ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS extra jsonb DEFAULT '{}'::jsonb;
ALTER TABLE public.raw_filings ADD COLUMN IF NOT EXISTS extra jsonb DEFAULT '{}'::jsonb;
```

## How It Works

### Data Flow
1. **SEC Poller** (edgar_poller_async.py)
   - Calls `build_sec_json_links(cik, filing_url)` to generate SEC endpoints
   - Stores them in `raw_filings.extra["sec_json"]`

2. **AI Pipeline** (ai_pipeline.py)
   - Reads `raw_filings.extra` including `sec_json`
   - Stores the entire `extra` object in `alerts.extra`

3. **Alert Formatter** (alert_formatter.py)
   - Reads `alerts.extra["sec_json"]`
   - Renders links in the Telegram message:
     ```
     🗂 <a href="...">SEC filing data (JSON)</a>
     ```

4. **Delivery** (delivery.py)
   - Calls `_log_payload()` which reads `alerts.extra["sec_json"]`
   - Upserts to `payload_log` table with the SEC endpoints

## Verification

Run these scripts to verify the feature is working:

```bash
# Quick diagnostic
python diagnose_sec_json.py

# Comprehensive verification
python verify_sec_json_fix.py

# Existing test suite
python tests/test_sec_json_and_feeds.py
python tests/test_async_delivery.py
```

## Database Migration

Before going live, run the migration in Supabase:

```sql
-- From migrations/2026-09-09_schema_ready.sql
-- Run the entire file, or just these lines if the rest was already run:

ALTER TABLE public.alerts ADD COLUMN IF NOT EXISTS extra jsonb DEFAULT '{}'::jsonb;
ALTER TABLE public.raw_filings ADD COLUMN IF NOT EXISTS extra jsonb DEFAULT '{}'::jsonb;
```

## What Users See

When an SEC 8-K Item 2.02 (earnings) alert is delivered:

```
🔴 HIGH · $AAPL — Apple Inc.
📈 $192.50  🟢 +2.15%

Apple Reports Strong Q4 Results

Apple Inc. reported quarterly earnings with strong service growth...

📋 SEC EDGAR · 8-K Current Report · Sep 9, 2:45 PM ET
🔗 <a href="...">View source</a>
🗂 <a href="https://www.sec.gov/Archives/edgar/data/320193/.../index.json">SEC filing data (JSON)</a>
```

The SEC JSON link points directly to the filing index, which contains every document including the EX-99.1 earnings press release.

## Cost Impact
✅ Zero additional cost — SEC data is free and requires no vendor calls
✅ Faster alerts — direct from SEC EDGAR (seconds), not through third-party vendors
