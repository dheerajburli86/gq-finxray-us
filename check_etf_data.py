import sys; sys.path.insert(0, '.')
import fmp_client

etfs = ['SPY', 'QQQ', 'IWM', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'GLD', 'TLT']

print(f'{"ETF":<8} {"Price":<10} {"Change%":<10} {"Volume":<15} {"Avg Vol":<15}')
print('-' * 65)

for etf in etfs:
    try:
        quote = fmp_client.get_quote(etf)
        if quote:
            price = quote.get('price', 0)
            change_pct = quote.get('changePercentage', 0)
            volume = quote.get('volume', 0)
            avg_vol = quote.get('avgVolume', 0)
            
            ratio = volume / avg_vol if avg_vol else 0
            print(f'{etf:<8} {price:<10.2f} {change_pct:<10.2f}% {volume:<15} {ratio:<15.2f}x')
    except Exception as e:
        print(f'{etf:<8} ERROR: {e}')
