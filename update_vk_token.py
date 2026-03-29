import sqlite3
import sys

db_path = "/root/tgplaylistbot/data/bot.db"
token_val = "vk1.a.BVK9p1YX1ONlA18iOMorCDWnXoNOzH_B-q6EPi4MawmAylX69by_z9xgJSYqXbQEd3LLznJHGHbBzv4LcTi6-fdY1dkAg4uvzRKmq34yDNiIJ9ehs3SVFIe2p22KD_yGd17DPl1uFjnTozh71N5Hc1mLP-hq9HeekEZTg3VfD6BncfN_bfEPmtMC4Tii-L9rUIdNN-R3BM83eP2MbQjJNw"

try:
    conn = sqlite3.connect(db_path, timeout=10)
    c = conn.cursor()
    c.execute("UPDATE bot_settings SET value = ? WHERE key = ?", (token_val, "vk_token"))
    conn.commit()
    c.execute("SELECT value FROM bot_settings WHERE key = ?", ("vk_token",))
    row = c.fetchone()
    if row and row[0] == token_val:
        print("SUCCESS_DB_UPDATE")
    else:
        print("FAIL_DB_UPDATE")
    conn.close()
except Exception as e:
    print(f"ERROR_DB_UPDATE: {e}")
    sys.exit(1)
