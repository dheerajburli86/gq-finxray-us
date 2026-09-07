import sys; sys.path.insert(0, '.')
import asyncio
from etf_flow_poller import run_etf_flow_poller
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

print('Generating Feature 7 ETF Flow alert...')
run_etf_flow_poller()

result = sb.table('alerts').select('*').eq('source', 'ETF_FLOW').eq('delivered', False).execute()
print(f'Generated {len(result.data)} alert(s)')

if result.data:
    print('Sending to Telegram...')
    asyncio.run(deliver_pending_alerts())
