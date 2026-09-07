import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Add debug at start of run_etf_flow_poller
old_start = '''def run_etf_flow_poller():
    """Feature 7 — Institutional inflow/outflow signals."""'''

new_start = '''def run_etf_flow_poller():
    """Feature 7 — Institutional inflow/outflow signals."""
    print('[ETF FLOW] Starting poller...')'''

content = content.replace(old_start, new_start)

# Add debug in the loop
old_loop = '''    for ticker in ETF_TICKERS:
        quote = fmp_client.get_quote(ticker)'''

new_loop = '''    for ticker in ETF_TICKERS:
        print(f'[ETF FLOW] Checking {ticker}...')
        quote = fmp_client.get_quote(ticker)'''

content = content.replace(old_loop, new_loop)

open('etf_flow_poller.py', 'w').write(content)
print('Added debug prints')
