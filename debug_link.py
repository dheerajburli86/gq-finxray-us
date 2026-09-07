import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get latest ETF_FLOW alert and show the actual link value
result = sb.table('alerts').select('*').eq('source', 'ETF_FLOW').order('created_at', desc=True).limit(1).execute()
if result.data:
    alert = result.data[0]
    print(f'Ticker: {alert.get("ticker")}')
    print(f'Link value: {alert.get("link")}')
    print(f'Link type: {type(alert.get("link"))}')
    print(f'Link is None: {alert.get("link") is None}')
else:
    print('No ETF_FLOW alerts found')
