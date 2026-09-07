import sys; sys.path.insert(0, '.')
# Temporarily lower thresholds
import etf_flow_poller
etf_flow_poller.VOLUME_SPIKE_THRESHOLD = 1.1  # was 1.5
etf_flow_poller.PRICE_MOVE_THRESHOLD = 0.5    # was 1.0

import asyncio
from main import deliver_pending_alerts

print('Generating Feature 7 with lowered thresholds...')
etf_flow_poller.run_etf_flow_poller()

# Get Feature 7 undelivered alerts
from supabase import create_client
import os
from dotenv import load_dotenv
load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

result = sb.table('alerts').select('*').in_('source', ['ETF_FLOW']).eq('delivered', False).execute()
print(f'Found {len(result.data)} ETF Flow alerts\n')

if result.data:
    print('Sending to Telegram...')
    asyncio.run(deliver_pending_alerts())
else:
    print('Still no alerts (market too calm)')
