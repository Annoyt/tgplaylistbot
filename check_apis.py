import sqlite3
import urllib.request
import json
from pathlib import Path
import os
import sys

# Try to get DB path from .env directly since config might also require external libs
def get_env_vals():
    env_path = ".env"
    vals = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    vals[k] = v.strip('"\'')
    return vals

db_path = "data/bot.db"
env_vals = get_env_vals()
if "DATABASE_URL" in env_vals:
    db_path = env_vals["DATABASE_URL"].replace("sqlite+aiosqlite:///", "")

if not os.path.exists(db_path):
    print(f"Database at {db_path} does not exist. Cannot test keys.")
    sys.exit(0)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
try:
    cursor.execute("SELECT key, value FROM bot_settings")
    bot_settings = {row['key']: row['value'] for row in cursor.fetchall()}
except sqlite3.OperationalError:
    bot_settings = {}

# Check Telegram Token
tg_token = bot_settings.get('telegram_bot_token') or env_vals.get('TELEGRAM_BOT_TOKEN')
if tg_token:
    print(f"Testing Telegram Token: {tg_token[:10]}...")
    url = f"https://api.telegram.org/bot{tg_token}/getMe"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            if data.get('ok'):
                print("✅ Telegram API: OK! Bot Username:", data['result']['username'])
            else:
                print("❌ Telegram API: Error", data)
    except Exception as e:
        print("❌ Telegram API Request Failed:", e)
else:
    print("⚠️ No Telegram Token found")

# Check OpenWeatherMap
owm_key = bot_settings.get('openweathermap_api_key') or env_vals.get('OPENWEATHERMAP_API_KEY')
if owm_key:
    print(f"\nTesting OpenWeatherMap API: {owm_key[:10]}...")
    url = f"https://api.openweathermap.org/data/2.5/weather?q=London&appid={owm_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            print(f"✅ OpenWeatherMap API: OK! Weather in London: {data['weather'][0]['description']}")
    except Exception as e:
        print("❌ OpenWeatherMap API Request Failed:", e)
else:
    print("\n⚠️ No OpenWeatherMap key found")
