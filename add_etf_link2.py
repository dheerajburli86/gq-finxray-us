import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

old_call = """    save_alert(ticker, flow, summary, impact, {
        "name": name, "category": category, "price": price,
        "change_p": round(change_p, 2), "volume": int(volume),
        "prev_volume": int(prev_volume), "volume_ratio": round(volume_ratio, 2),
        "flow": flow, "signal": signal
    })"""

new_call = """    save_alert(ticker, flow, summary, impact, {
        "name": name, "category": category, "price": price,
        "change_p": round(change_p, 2), "volume": int(volume),
        "prev_volume": int(prev_volume), "volume_ratio": round(volume_ratio, 2),
        "flow": flow, "signal": signal
    }, link=etf_quote_url(ticker))"""

content = content.replace(old_call, new_call)
open('etf_flow_poller.py', 'w').write(content)
print('Added Yahoo Finance link to ETF Flow alerts')
