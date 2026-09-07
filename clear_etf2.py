import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Delete all ETF_FLOW alerts
result = sb.table('alerts').select('id').eq('source', 'ETF_FLOW').execute()
for alert in result.data:
    sb.table('alerts').delete().eq('id', alert['id']).execute()

print(f'Cleared {len(result.data)} old alerts')
