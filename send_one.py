import sys; sys.path.insert(0, '.')
import asyncio
from main import format_alert, deliver_pending_alerts
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get one undelivered alert
result = sb.table('alerts').select('*').eq('delivered', False).limit(1).execute()
if result.data:
    alert = result.data[0]
    print(f'Alert: {alert.get("ticker")} - {alert.get("filing_type")}')
    
    # Try to send it
    print('Attempting delivery...')
    asyncio.run(deliver_pending_alerts())
else:
    print('No undelivered alerts found')
