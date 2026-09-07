import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Check latest alerts
result = sb.table('alerts').select('*').order('created_at', desc=True).limit(10).execute()
print(f'Total alerts in DB: {len(result.data)}\n')
print(f'{"TICKER":<8} {"FEATURE":<25} {"DELIVERED":<10} {"LINK"}')
print('-' * 80)
for alert in result.data:
    ticker = alert.get('ticker', '')
    filing_type = alert.get('filing_type', '')[:24]
    delivered = alert.get('delivered', False)
    link = 'YES' if alert.get('link') else 'NO'
    print(f'{ticker:<8} {filing_type:<25} {str(delivered):<10} {link}')
