import os
import sqlite3
import uuid
import asyncio
import threading

from flask import Flask, redirect, request, render_template
from twitchio.ext import commands

# === CONFIG ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:10000/callback")

app = Flask(__name__)

# === DATABASE ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  yt_token TEXT,
  twitch_token TEXT,
  yt_channel TEXT,
  twitch_username TEXT,
  command TEXT,
  direction TEXT
)""")
conn.commit()

# === WEB ROUTES ===

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/auth/youtube")
def auth_youtube():
    state = str(uuid.uuid4())
    return redirect(
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={YT_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&"
        f"scope=https://www.googleapis.com/auth/youtube.readonly&access_type=offline&state={state}"
    )

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    return redirect(
        f"https://id.twitch.tv/oauth2/authorize?"
        f"client_id={TWITCH_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&"
        f"scope=chat:read+chat:edit&state={state}"
    )

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if "google" in request.referrer:
        # handle YouTube (mock only)
        yt_token = f"youtube_access_token_{state}"
        cur.execute("INSERT OR REPLACE INTO users (id, yt_token) VALUES (?, ?)", (state, yt_token))
    else:
        twitch_token = f"twitch_access_token_{state}"
        twitch_username = f"user_{state[:5]}"
        cur.execute("UPDATE users SET twitch_token = ?, twitch_username = ? WHERE id = ?", (twitch_token, twitch_username, state))
    conn.commit()
    return "Account linked. Go back to the index."

@app.route("/set_forward", methods=["POST"])
def set_forward():
    command = request.form["command"]
    direction = request.form["direction"]
    user_id = "webuser_" + str(uuid.uuid4())  # Real systems would use session user id

    cur.execute("INSERT OR REPLACE INTO users (id, command, direction) VALUES (?, ?, ?)", (user_id, command, direction))
    conn.commit()
    return "Forwarding rule saved."

# === TWITCH BOT ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=["#yourchannel"])

    async def event_ready(self):
        print(f"Bot ready: {self.nick}")

    async def event_message(self, message):
        await self.handle_commands(message)

        user = message.author.name
        cur.execute("SELECT command, direction FROM users WHERE twitch_username = ?", (user,))
        row = cur.fetchone()
        if row:
            cmd, direction = row
            if message.content.startswith(cmd):
                payload = message.content[len(cmd):].strip()
                await self.connected_channels[0].send(f"! [BOT] ! {payload}")

bot = TwitchBot()

# === COMBINE ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def run_all():
    thread = threading.Thread(target=run_flask)
    thread.start()
    asyncio.run(bot.run())

if __name__ == "__main__":
    run_all()
