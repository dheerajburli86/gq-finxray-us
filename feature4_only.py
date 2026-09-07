import sys; sys.path.insert(0, '.')
import asyncio
from datetime import datetime, timedelta, timezone
from fmp_poller import poll_earnings_calendar
from main import format_alert
from supabase import create_client
import os
from dotenv import load_dotenv
from aiogram import Bot

load_dotenv()
sb = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_KEY'))

# Get your 20 tickers
tickers = sb.table('watchlists').select('ticker').execute()
tickers = [t['ticker'] for t in tickers.data]

print(f'Generating Feature 4 (Earnings Calendar) for {len(tickers)} tickers...')
count = poll_earnings_calendar(tickers)
print(f'Generated {count} earnings alerts\n')

# Get ONLY Feature 4 undelivered alerts
result = sb.table('alerts').select('*').eq('filing_type', 'EARNINGS_CALENDAR').eq('delivered', False).execute()
print(f'Found {len(result.data)} Feature 4 alerts to send:\n')

# Send only Feature 4 to Telegram
if result.data:
    bot = Bot(token=os.getenv('TELEGRAM_TOKEN'))
    channel_id = os.getenv('TELEGRAM_CHANNEL_ID')
    
    for alert in result.data:
        text = format_alert(alert)
        asyncio.run(bot.send_message(chat_id=channel_id, text=text, parse_mode='Markdown'))
        sb.table('alerts').update({'delivered': True}).eq('id', alert['id']).execute()
        print(f'Sent: {alert.get("ticker")} - {alert.get("created_at")[:10]}')
else:
    print('No Feature 4 alerts to send')
