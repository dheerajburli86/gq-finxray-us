import sys; sys.path.insert(0, '.')
content = open('claude_massive_client.py').read()

# Add the fund flows function before the crypto section
fund_flows_func = '''
# ── ETF Global (fund flows) ───────────────────────────────────────────────────
def get_fund_flows(ticker, limit=1):
    """Get real ETF fund flow data from Massive ETF Global dataset."""
    data = _get(f"/etf-global/v1/fund-flows", {
        "ticker": ticker,
        "order": "desc",
        "limit": limit
    })
    if data and isinstance(data.get("results"), list) and data["results"]:
        return data["results"][0]
    return None

'''

# Insert before crypto section
insert_pos = content.find('# ── Crypto')
if insert_pos > 0:
    content = content[:insert_pos] + fund_flows_func + content[insert_pos:]
    open('claude_massive_client.py', 'w').write(content)
    print('Added get_fund_flows() to massive_client.py')
else:
    print('ERROR: Could not find insertion point')
