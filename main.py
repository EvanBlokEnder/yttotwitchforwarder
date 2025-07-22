import os
import sqlite3
import uuid
import threading
import asyncio
import requests
from flask import Flask, redirect, request, render_template, url_for
from twitchio.ext import commands

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# === CONFIG ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

app = Flask(__name__)

# === DATABASE SETUP ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  yt_access_token TEXT,
  yt_refresh_token TEXT,
  yt_token_expiry INTEGER,
  twitch_access_token TEXT,
  twitch_refresh_token TEXT,
  twitch_token_expiry INTEGER,
  yt_channel_id TEXT,
  twitch_username TEXT,
  forward_command TEXT,
  forward_direction TEXT
)
""")
conn.commit()

# === UTILITIES ===

def save_youtube_tokens(user_id, access_token, refresh_token, expires_in, channel_id):
    expiry = int(expires_in) + int(os.time.time())
    cur.execute("""
    INSERT OR REPLACE INTO users (id, yt_access_token, yt_refresh_token, yt_token_expiry, yt_channel_id)
    VALUES (?, ?, ?, ?, ?)
    """, (user_id, access_token, refresh_token, expiry, channel_id))
    conn.commit()

def save_twitch_tokens(user_id, access_token, refresh_token, expires_in, username):
    expiry = int(expires_in) + int(os.time.time())
    cur.execute("""
    INSERT OR REPLACE INTO users (id, twitch_access_token, twitch_refresh_token, twitch_token_expiry, twitch_username)
    VALUES (?, ?, ?, ?, ?)
    """, (user_id, access_token, refresh_token, expiry, username))
    conn.commit()

def update_forwarding(user_id, command, direction):
    cur.execute("""
    UPDATE users SET forward_command = ?, forward_direction = ? WHERE id = ?
    """, (command, direction, user_id))
    conn.commit()

def get_user_by_twitch_username(username):
    cur.execute("SELECT * FROM users WHERE twitch_username = ?", (username,))
    return cur.fetchone()

def get_user_by_youtube_channel(channel_id):
    cur.execute("SELECT * FROM users WHERE yt_channel_id = ?", (channel_id,))
    return cur.fetchone()

# === OAUTH ROUTES ===

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/auth/youtube")
def auth_youtube():
    state = str(uuid.uuid4())
    # Save state in session or DB for CSRF protection, skipped here for brevity
    scope = "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl"
    return redirect(
        "https://accounts.google.com/o/oauth2/v2/auth?" +
        f"client_id={YT_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope={scope}&access_type=offline&state={state}&prompt=consent"
    )

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    scope = "chat:read chat:edit"
    return redirect(
        "https://id.twitch.tv/oauth2/authorize?" +
        f"client_id={TWITCH_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope={scope}&state={state}"
    )

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        return f"Error: {error}"

    # Detect source by checking if Google or Twitch url - hacky but effective
    ref = request.referrer or ""
    if "accounts.google.com" in ref:
        # Exchange code for tokens for YouTube
        token_res = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI
        })
        token_json = token_res.json()
        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")
        expires_in = token_json.get("expires_in")

        # Get channel ID
        creds = Credentials(token=access_token)
        youtube = build('youtube', 'v3', credentials=creds)
        channels = youtube.channels().list(mine=True, part="id").execute()
        channel_id = channels['items'][0]['id']

        # Save tokens and channel ID keyed by state (you can use sessions in prod)
        cur.execute("""
        INSERT OR REPLACE INTO users (id, yt_access_token, yt_refresh_token, yt_token_expiry, yt_channel_id)
        VALUES (?, ?, ?, ?, ?)
        """, (state, access_token, refresh_token, expires_in, channel_id))
        conn.commit()

        return "YouTube account linked successfully. You can close this tab."
    else:
        # Twitch token exchange
        token_res = requests.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI
        })
        token_json = token_res.json()
        access_token = token_json.get("access_token")
        refresh_token = token_json.get("refresh_token")
        expires_in = token_json.get("expires_in")

        # Get username with token
        user_res = requests.get("https://api.twitch.tv/helix/users", headers={
            "Authorization": f"Bearer {access_token}",
            "Client-Id": TWITCH_CLIENT_ID
        })
        user_json = user_res.json()
        username = user_json['data'][0]['login']

        # Save tokens and username keyed by state
        cur.execute("""
        INSERT OR REPLACE INTO users (id, twitch_access_token, twitch_refresh_token, twitch_token_expiry, twitch_username)
        VALUES (?, ?, ?, ?, ?)
        """, (state, access_token, refresh_token, expires_in, username))
        conn.commit()

        return "Twitch account linked successfully. You can close this tab."

@app.route("/set_forward", methods=["POST"])
def set_forward():
    user_id = request.form.get("user_id")
    command = request.form.get("command")
    direction = request.form.get("direction")
    if not all([user_id, command, direction]):
        return "Missing parameters", 400
    update_forwarding(user_id, command, direction)
    return "Forwarding command saved."

# === TWITCH BOT ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=["#yourchannel"])

    async def event_ready(self):
        print(f"Twitch bot ready as {self.nick}")

    async def event_message(self, message):
        await self.handle_commands(message)
        if message.echo:
            return  # Ignore bot's own messages

        user = message.author.name
        user_data = get_user_by_twitch_username(user)
        if not user_data:
            return

        command, direction = user_data[9], user_data[10]
        if not command or not message.content.startswith(command):
            return

        # Extract the message after the command
        payload = message.content[len(command):].strip()
        if direction == "twitch_to_yt":
            # Forward message to YouTube live chat
            yt_channel_id = user_data[7]
            yt_access_token = user_data[1]
            if yt_access_token:
                # Refresh token and send chat message (left as an exercise)
                # Minimal example:
                print(f"Forwarding from Twitch to YouTube: {payload}")
        elif direction == "yt_to_twitch":
            # This bot only listens on Twitch chat, so ignore here
            pass

        # Send a confirmation message in Twitch chat
        await message.channel.send(f"! [BOT] ! {payload}")

# === RUN SERVER & BOT ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def run_bot():
    bot = TwitchBot()
    asyncio.run(bot.run())

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    run_bot()
