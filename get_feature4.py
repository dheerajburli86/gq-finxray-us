import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get latest EARNINGS_CALENDAR alerts
result = sb.table('alerts').select('*').eq('filing_type', 'EARNINGS_CALENDAR').order('created_at', desc=True).limit(10).execute()
print(f'Found {len(result.data)} Earnings Calendar alerts:\n')
print(f'{"TICKER":<8} {"CREATED":<20} {"LINK":<6} {"DELIVERED"}')
print('-' * 60)
for alert in result.data:
    ticker = alert.get('ticker')
    created = alert.get('created_at')
    link = 'YES' if alert.get('link') else 'NO'
    delivered = alert.get('delivered')
    print(f'{ticker:<8} {created:<20} {link:<6} {delivered}')

# If none found, generate new ones
if len(result.data) == 0:
    print('No earnings alerts found. Generating new ones...')
    from fmp_poller import poll_earnings_calendar
    tickers = sb.table('watchlists').select('ticker').limit(10).execute()
    tickers = [t['ticker'] for t in tickers.data]
    poll_earnings_calendar(tickers)
    print(f'Generated alerts. Sending to Telegram...')
    import asyncio
    from main import deliver_pending_alerts
    asyncio.run(deliver_pending_alerts())
