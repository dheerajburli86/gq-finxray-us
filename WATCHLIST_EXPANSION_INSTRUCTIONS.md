# Watchlist Expansion Instructions

## Overview
The watchlist has been configured to expand from 10 to 20 stocks to increase alerts from features 1, 3, 4-12.

**Current 10 stocks:** AAPL, AMZN, CMCSA, GOOGL, META, MSFT, MU, NVDA, SPCX, TSLA

**Adding 10 new stocks:** NFLX, CRWD, ADBE, CRM, DDOG, AMD, AVGO, INTC, PYPL, UBER

These stocks were selected for:
- High SEC filing activity (8-K, 10-Q, 10-K, Form 4)
- Strong insider trading volume
- Frequent earnings calendar events
- Heavy analyst coverage

## RSS Feed Changes
✓ **Disabled:** Nasdaq Originals, MarketWatch Economy
✓ **Kept:** 17 financial feeds (CNBC sections, MarketWatch, Yahoo Finance, IBD, Fortune)

## Steps to Expand Watchlist

### Option 1: Run Python Script (Recommended)

1. Navigate to the project directory on your local machine:
   ```bash
   cd /path/to/gq-finxray-us
   ```

2. Ensure your `.env` file has `SUPABASE_URL` and `SUPABASE_KEY` set

3. Run the expansion script:
   ```bash
   python expand_watchlist_simple.py
   ```

   Expected output:
   ```
   Found N users with watchlists
   ✓ Added NFLX to user@example.com's watchlist
   ✓ Added CRWD to user@example.com's watchlist
   ...
   Completed! Added NN ticker-user pairs
   
   Updated watchlist sizes:
   user@example.com: 20 stocks
   ```

### Option 2: Manual SQL (Alternative)

If you prefer to add stocks manually via Supabase dashboard:

1. Go to your Supabase project dashboard
2. Open the SQL Editor
3. Run this query (replace `user_id` with actual user IDs):

```sql
INSERT INTO watchlists (user_id, ticker) VALUES
  ('user_id_here', 'NFLX'),
  ('user_id_here', 'CRWD'),
  ('user_id_here', 'ADBE'),
  ('user_id_here', 'CRM'),
  ('user_id_here', 'DDOG'),
  ('user_id_here', 'AMD'),
  ('user_id_here', 'AVGO'),
  ('user_id_here', 'INTC'),
  ('user_id_here', 'PYPL'),
  ('user_id_here', 'UBER')
ON CONFLICT DO NOTHING;
```

## Verification

After expansion, verify the watchlist sizes:

```sql
SELECT user_id, COUNT(DISTINCT ticker) as stock_count
FROM watchlists
GROUP BY user_id;
```

All users should show **20 stocks** (or close to it if some duplicates existed).

## Expected Impact on Alerts

With the expanded watchlist and improved RSS feed quality:

- **Feature 1** (SEC Filings): NFLX, CRWD, ADBE, CRM often file 8-K, 10-Q, Form 4
- **Feature 3** (Large Market Moves): High-volatility tech/SaaS stocks
- **Feature 4** (Earnings Calendar): All 20 stocks report earnings quarterly
- **Feature 5** (Insider Trading): Tech leadership frequently buys/sells
- **Features 7-13** (Technical, Market): Active tickers with high volume

Expect alert frequency to **increase 2-3x** within 24 hours of this change.
