import sys; sys.path.insert(0, '.')
import asyncio
from etf_flow_poller import run_etf_flow_poller
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

print('Generating Feature 7 (ETF Flow Alerts)...')
run_etf_flow_poller()

# Get Feature 7 undelivered alerts
result = sb.table('alerts').select('*').in_('source', ['ETF_FLOW']).eq('delivered', False).execute()
print(f'Found {len(result.data)} ETF Flow alerts\n')

for alert in result.data:
    ticker = alert.get('ticker')
    extra = alert.get('extra', {})
    flow = extra.get('flow', '')
    print(f'{ticker} - {flow}')

# Send to Telegram
print('\nSending to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
