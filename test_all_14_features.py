import sys; sys.path.insert(0, '.')
import asyncio
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get your existing tickers
result = sb.table('watchlists').select('ticker').execute()
tickers = [t['ticker'] for t in result.data]
print(f'Testing with {len(tickers)} tickers\n')

# Feature 3: Result Snapshot
print('[FEATURE 3] Result Snapshot...')
from result_snapshot import process_pending_snapshots
process_pending_snapshots()

# Feature 4: Earnings Calendar
print('[FEATURE 4] Earnings Calendar...')
from fmp_poller import poll_earnings_calendar
poll_earnings_calendar(tickers[:10])

# Feature 5: Insider Transactions
print('[FEATURE 5] Insider Transactions...')
from fmp_poller import poll_insider_transactions
poll_insider_transactions(tickers[:10])

# Feature 6: Technical Alerts
print('[FEATURE 6] Technical Alerts...')
from technical_poller import run_technical_poller
run_technical_poller()

# Feature 7: ETF Flow
print('[FEATURE 7] ETF Flow...')
from etf_flow_poller import run_etf_flow_poller
run_etf_flow_poller()

# Feature 8: IPO
print('[FEATURE 8] IPO Deep Dive...')
from ipo_poller import run_ipo_poller
run_ipo_poller()

# Feature 9: Sector Heatmap
print('[FEATURE 9] Sector Heatmap...')
from heatmap_generator import run_sector_heatmap_weekly
run_sector_heatmap_weekly()

# Feature 10: News Roundup
print('[FEATURE 10] News Roundup...')
from news_roundup import send_market_open_report
asyncio.run(send_market_open_report())

# Feature 11: Earnings Transcripts
print('[FEATURE 11] Earnings Transcripts...')
from claude_earnings_transcript_poller import run_earnings_transcript_poller
run_earnings_transcript_poller()

# Send all to Telegram
print('\n[SENDING] Alerts to Telegram...')
from main import deliver_pending_alerts
asyncio.run(deliver_pending_alerts())
print('[DONE]')
