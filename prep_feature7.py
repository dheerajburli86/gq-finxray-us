import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# 1. Lower thresholds to generate alerts
content = content.replace('VOLUME_SPIKE_THRESHOLD = 0.8', 'VOLUME_SPIKE_THRESHOLD = 0.7')
content = content.replace('PRICE_MOVE_THRESHOLD = 0.3', 'PRICE_MOVE_THRESHOLD = 0.2')

# 2. Make sure link is passed to save_alert
if 'link=etf_quote_url(ticker)' not in content:
    old = '''    })
    # No link by design'''
    new = '''    }, link=etf_quote_url(ticker))
    # Link to Yahoo Finance quote page'''
    content = content.replace(old, new)

open('etf_flow_poller.py', 'w').write(content)
print('Thresholds: volume 0.7x, price 0.2%')
print('Links enabled')
