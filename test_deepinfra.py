import os
import requests
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("DEEPINFRA_API_KEY")
if not key:
    print("ERROR: DEEPINFRA_API_KEY not found in .env")
    exit()

print(f"Key found: {key[:8]}...")

url = "https://api.deepinfra.com/v1/openai/chat/completions"
headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json"
}
payload = {
    "model": "google/gemini-2.0-flash-001",
    "messages": [
        {"role": "user", "content": "Say 'DeepInfra connection successful' and nothing else."}
    ],
    "max_tokens": 20
}

try:
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"Status: {r.status_code}")
    data = r.json()
    if r.status_code == 200:
        print(f"Response: {data['choices'][0]['message']['content']}")
    else:
        print(f"Error: {data}")
except Exception as e:
    print(f"Request failed: {e}")
