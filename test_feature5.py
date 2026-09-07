import sys; sys.path.insert(0, '.')
import asyncio
from fmp_poller import poll_insider_transactions
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

tickers = ['META', 'MSFT', 'AAPL']
print(f'Generating Feature 5 alerts for {tickers}...')
poll_insider_transactions(tickers)

print('Sending to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
