import sys; sys.path.insert(0, '.')
content = open('etf_flow_poller.py').read()

# Comment out the dedup check
content = content.replace(
    '''    if already_sent_today(ticker, flow):
        logger.info(f"[ETF FLOW] Already sent {flow} for {ticker} today, skipping.")
        return None''',
    '''    # TEMP: Disabled dedup for testing
    # if already_sent_today(ticker, flow):
    #     logger.info(f"[ETF FLOW] Already sent {flow} for {ticker} today, skipping.")
    #     return None'''
)

open('etf_flow_poller.py', 'w').write(content)
print('Disabled dedup check')
