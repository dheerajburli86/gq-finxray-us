import sys; sys.path.insert(0, '.')
import asyncio
from fmp_poller import poll_earnings_calendar
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get your 20 tickers
tickers = sb.table('watchlists').select('ticker').execute()
tickers = [t['ticker'] for t in tickers.data]

print(f'Generating Feature 4 (Earnings Calendar) for {len(tickers)} tickers...')
count = poll_earnings_calendar(tickers)
print(f'Generated {count} earnings alerts')

print('Sending to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
