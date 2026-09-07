import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Use 2 days back instead of 1
old = "yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')"
new = "yesterday = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')"

content = content.replace(old, new)
open('etf_flow_poller.py', 'w').write(content)
print('Fixed: using 2 days back for historical volume')
