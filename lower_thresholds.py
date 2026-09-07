import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Lower the thresholds to make alerts trigger
content = content.replace('VOLUME_SPIKE_THRESHOLD = 1.5', 'VOLUME_SPIKE_THRESHOLD = 1.1')
content = content.replace('PRICE_MOVE_THRESHOLD = 1.0', 'PRICE_MOVE_THRESHOLD = 0.5')

open('etf_flow_poller.py', 'w').write(content)
print('Lowered thresholds: volume 1.5→1.1, price 1.0→0.5')
