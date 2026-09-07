import sys; sys.path.insert(0, '.')
import asyncio
from main import deliver_pending_alerts

# Generate some test alerts first
print('Generating alerts...')
import result_snapshot
result_snapshot.process_pending_snapshots()

from fmp_poller import poll_earnings_calendar, poll_insider_transactions
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

tickers = sb.table('watchlists').select('ticker').execute().data
tickers = [t['ticker'] for t in tickers][:5]

print(f'Testing with {tickers}...')
poll_earnings_calendar(tickers)
poll_insider_transactions(tickers)

print('Sending alerts to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
