import sys; sys.path.insert(0, '.')
import asyncio
from etf_flow_poller import run_etf_flow_poller
from main import deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

print('Generating Feature 7 ETF Flow alerts...')
run_etf_flow_poller()

result = sb.table('alerts').select('*').in_('source', ['ETF_FLOW']).eq('delivered', False).execute()
print(f'Generated {len(result.data)} alerts\n')
for alert in result.data:
    link = 'YES' if alert.get('link') else 'NO'
    print(f'{alert.get("ticker"):8} | LINK={link}')

print('\nSending to Telegram...')
asyncio.run(deliver_pending_alerts())
print('Done!')
