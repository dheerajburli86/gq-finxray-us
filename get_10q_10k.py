import sys; sys.path.insert(0, '.')
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get 10-Q/10-K filings from 3/8/2026 onwards
result = sb.table('raw_filings').select('*').gte('filed_at', '2026-08-03').in_('filing_type', ['10-Q', '10-K']).order('filed_at', desc=True).limit(20).execute()
print(f'Found {len(result.data)} 10-Q/10-K filings from 03/8/2026:\n')
for filing in result.data:
    ticker = filing.get('ticker')
    filing_type = filing.get('filing_type')
    filed_at = filing.get('filed_at')
    status = filing.get('status')
    filing_url = filing.get('filing_url')
    url_short = filing_url[:40] if filing_url else "NONE"
    print(f'{filed_at} | {ticker:8} | {filing_type:6} | {status:8} | {url_short}')
