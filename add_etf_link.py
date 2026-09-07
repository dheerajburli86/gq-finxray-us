import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Add fallback function
fallback_func = '''
def etf_quote_url(ticker):
    """Yahoo Finance quote page for the ETF."""
    return f"https://finance.yahoo.com/quote/{ticker}"
'''

# Insert before run_etf_flow_poller
lines = content.split('\n')
insert_pos = 0
for i, line in enumerate(lines):
    if line.startswith('def run_etf_flow_poller'):
        insert_pos = i
        break

lines.insert(insert_pos, fallback_func)
content = '\n'.join(lines)

# Now add link to save_alert call
old_call = '''    }, link=f"https://finance.yahoo.com/quote/{ticker}")
    # No link by design: ETF flow signals are computed'''

new_call = '''    }, link=etf_quote_url(ticker))
    # Link to ETF quote page for reference'''

content = content.replace(old_call, new_call)
open('etf_flow_poller.py', 'w').write(content)
print('Added ETF quote link to Feature 7')
