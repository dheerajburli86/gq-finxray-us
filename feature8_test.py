import sys; sys.path.insert(0, '.')
import asyncio
from ipo_poller import run_ipo_poller
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Clear old IPO alerts
result = sb.table('alerts').select('id').eq('source', 'FMP_IPO').execute()
for alert in result.data:
    sb.table('alerts').delete().eq('id', alert['id']).execute()
print(f'Cleared {len(result.data)} old IPO alerts')

print('Generating Feature 8 (IPO Deep Dive) alerts...')
run_ipo_poller()

result = sb.table('alerts').select('*').eq('source', 'FMP_IPO').eq('delivered', False).execute()
print(f'Generated {len(result.data)} IPO alert(s)')
for alert in result.data:
    ticker = alert.get('ticker')
    link = 'YES' if alert.get('link') else 'NO'
    print(f'  {ticker}: link={link}')

print('Sending to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
