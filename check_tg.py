import os
from dotenv import load_dotenv
load_dotenv()
token = os.getenv('TELEGRAM_TOKEN')
channel = os.getenv('TELEGRAM_CHANNEL_ID')
print(f'TELEGRAM_TOKEN: {token[:20] if token else "MISSING"}...')
print(f'TELEGRAM_CHANNEL_ID: {channel if channel else "MISSING"}')
