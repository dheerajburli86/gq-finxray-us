import sys; sys.path.insert(0, '.')

# 1. Lower thresholds in etf_flow_poller.py
content = open('etf_flow_poller.py').read()
content = content.replace('VOLUME_SPIKE_THRESHOLD = 0.7', 'VOLUME_SPIKE_THRESHOLD = 0.5')
content = content.replace('PRICE_MOVE_THRESHOLD = 0.2', 'PRICE_MOVE_THRESHOLD = 0.1')
open('etf_flow_poller.py', 'w').write(content)
print('Step 1: Lowered thresholds to 0.5x volume, 0.1% price')

# 2. Verify link is in save_alert call
content = open('etf_flow_poller.py').read()
if 'link=etf_quote_url(ticker)' not in content:
    print('ERROR: Link not passed to save_alert!')
else:
    print('Step 2: Link is passed to save_alert')

# 3. Verify link is rendered in main.py
content = open('main.py').read()
if 'if source == "ETF_FLOW":' in content and 'link_line' in content:
    print('Step 3: Link rendering is in place')
else:
    print('ERROR: Link rendering missing from main.py')
