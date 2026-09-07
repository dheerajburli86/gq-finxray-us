import sys; sys.path.insert(0, '.')
import asyncio
from etf_flow_poller import run_etf_flow_poller
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

print('Generating Feature 7 with thresholds: 0.5x volume, 0.1% price')
run_etf_flow_poller()

result = sb.table('alerts').select('*').in_('source', ['ETF_FLOW']).eq('delivered', False).execute()
print(f'Generated {len(result.data)} alerts with links')

print('Sending to Telegram...')
asyncio.run(deliver_pending_alerts())
