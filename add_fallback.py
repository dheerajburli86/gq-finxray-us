import sys; sys.path.insert(0, '.')
content = open('fmp_poller.py').read()

# Add fallback function after imports
fallback_func = '''
def form4_fallback_url(ticker):
    """SEC EDGAR browse URL for Form 4 filings when exact filing not found."""
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcompany&CIK={ticker}&type=4"
        "&dateb=&owner=include&count=10"
    )
'''

# Insert after the imports section (after line 25)
lines = content.split('\n')
insert_pos = 0
for i, line in enumerate(lines):
    if line.startswith('def get_market_cap'):
        insert_pos = i
        break

lines.insert(insert_pos, fallback_func)
content = '\n'.join(lines)

# Now update the form4_url logic to use fallback
old_logic = '''form4_url = edgar_link.find_filing_url(
                        ticker, form_type="4", target_date=txn_date
                    )
                    store_alert(ticker, bulk_summary, "HIGH", "BULK_DEAL", {'''

new_logic = '''form4_url = edgar_link.find_filing_url(
                        ticker, form_type="4", target_date=txn_date
                    ) or form4_fallback_url(ticker)
                    store_alert(ticker, bulk_summary, "HIGH", "BULK_DEAL", {'''

content = content.replace(old_logic, new_logic)
open('fmp_poller.py', 'w').write(content)
print('Added Form 4 fallback URL')
