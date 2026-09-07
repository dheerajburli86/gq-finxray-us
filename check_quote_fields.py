import sys; sys.path.insert(0, '.')
import fmp_client

quote = fmp_client.get_quote('SPY')
print('FMP Quote fields for SPY:')
for key, value in quote.items():
    print(f'  {key}: {value}')
