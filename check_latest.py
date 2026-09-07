import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

result = sb.table('alerts').select('*').order('created_at', desc=True).limit(5).execute()
print(f'Latest 5 alerts:')
for alert in result.data:
    ticker = alert.get('ticker')
    filing_type = alert.get('filing_type')
    delivered = alert.get('delivered')
    created = alert.get('created_at')[:10]
    print(f'{created} | {ticker:8} | {filing_type:20} | DELIVERED={delivered}')
