import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Remove link from save_alert call
old = "}, link=etf_quote_url(ticker))"
new = "})"

content = content.replace(old, new)
open('etf_flow_poller.py', 'w').write(content)
print('Removed Yahoo Finance link from Feature 7')
