import os
import time
import uuid
import threading
import asyncio
import sqlite3
import requests
from flask import Flask, redirect, request, render_template, session, url_for
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
app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey")

# === DATABASE ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    yt_access_token TEXT,
    yt_refresh_token TEXT,
    yt_token_expiry INTEGER,
    yt_channel_id TEXT,
    twitch_access_token TEXT,
    twitch_refresh_token TEXT,
    twitch_token_expiry INTEGER,
    twitch_username TEXT,
    forward_command TEXT,
    forward_direction TEXT
)
""")
conn.commit()

# === TOKEN UTILITIES ===

def get_current_time():
    return int(time.time())

def save_youtube_tokens(user_id, access_token, refresh_token, expires_in, channel_id):
    expiry = get_current_time() + expires_in
    cur.execute("""
    INSERT OR REPLACE INTO users
    (id, yt_access_token, yt_refresh_token, yt_token_expiry, yt_channel_id)
    VALUES (?, ?, ?, ?, ?)
    """, (user_id, access_token, refresh_token, expiry, channel_id))
    conn.commit()

def save_twitch_tokens(user_id, access_token, refresh_token, expires_in, username):
    expiry = get_current_time() + expires_in
    cur.execute("""
    INSERT OR REPLACE INTO users
    (id, twitch_access_token, twitch_refresh_token, twitch_token_expiry, twitch_username)
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

def get_user_by_id(user_id):
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cur.fetchone()

# === TOKEN REFRESH LOGIC ===

def refresh_youtube_token(user):
    refresh_token = user[2]
    if not refresh_token:
        return None
    data = {
        "client_id": YT_CLIENT_ID,
        "client_secret": YT_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    res = requests.post("https://oauth2.googleapis.com/token", data=data)
    if res.status_code != 200:
        print("Failed to refresh YouTube token:", res.text)
        return None
    tokens = res.json()
    access_token = tokens["access_token"]
    expires_in = tokens.get("expires_in", 3600)
    # Update DB
    expiry = get_current_time() + expires_in
    cur.execute("""
    UPDATE users SET yt_access_token = ?, yt_token_expiry = ? WHERE id = ?
    """, (access_token, expiry, user[0]))
    conn.commit()
    return access_token

def refresh_twitch_token(user):
    refresh_token = user[6]
    if not refresh_token:
        return None
    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
    }
    res = requests.post("https://id.twitch.tv/oauth2/token", params=params)
    if res.status_code != 200:
        print("Failed to refresh Twitch token:", res.text)
        return None
    tokens = res.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", refresh_token)
    expires_in = tokens.get("expires_in", 3600)
    expiry = get_current_time() + expires_in
    # Update DB
    cur.execute("""
    UPDATE users SET twitch_access_token = ?, twitch_refresh_token = ?, twitch_token_expiry = ? WHERE id = ?
    """, (access_token, refresh_token, expiry, user[0]))
    conn.commit()
    return access_token

# === API HELPERS ===

def get_valid_youtube_token(user):
    if user[3] is None or user[3] < get_current_time():
        # Token expired, refresh
        new_token = refresh_youtube_token(user)
        return new_token
    return user[1]

def get_valid_twitch_token(user):
    if user[7] is None or user[7] < get_current_time():
        # Token expired, refresh
        new_token = refresh_twitch_token(user)
        return new_token
    return user[5]

def send_message_to_youtube_livechat(channel_id, access_token, message):
    try:
        # Get live broadcast ID for the channel
        creds = Credentials(token=access_token)
        youtube = build("youtube", "v3", credentials=creds)
        live_broadcasts = youtube.liveBroadcasts().list(
            part="snippet",
            broadcastStatus="active",
            broadcastType="all",
            mine=True
        ).execute()
        if not live_broadcasts["items"]:
            print("No active live broadcast for YouTube channel")
            return False
        live_chat_id = live_broadcasts["items"][0]["snippet"]["liveChatId"]

        # Send chat message
        youtube.liveChatMessages().insert(
            part="snippet",
            body={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": message
                    }
                }
            }
        ).execute()
        print("Sent message to YouTube live chat:", message)
        return True
    except Exception as e:
        print("Error sending message to YouTube live chat:", e)
        return False

# === FLASK WEB APP ===

@app.route("/")
def index():
    user_id = session.get("user_id")
    user = get_user_by_id(user_id) if user_id else None
    return render_template("index.html", user=user)

@app.route("/auth/youtube")
def auth_youtube():
    state = str(uuid.uuid4())
    session["oauth_state"] = state
    scope = "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl"
    return redirect(
        "https://accounts.google.com/o/oauth2/v2/auth?" +
        f"client_id={YT_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope={scope}&access_type=offline&state={state}&prompt=consent"
    )

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    session["oauth_state"] = state
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

    if state != session.get("oauth_state"):
        return "Invalid state parameter", 400

    # Determine provider by checking query params (Google sends 'scope')
    if "scope" in request.args:
        # YouTube token exchange
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

        creds = Credentials(token=access_token)
        youtube = build('youtube', 'v3', credentials=creds)
        channels = youtube.channels().list(mine=True, part="id").execute()
        channel_id = channels['items'][0]['id']

        user_id = session.get("user_id") or str(uuid.uuid4())
        session["user_id"] = user_id
        save_youtube_tokens(user_id, access_token, refresh_token, expires_in, channel_id)
        return "YouTube account linked! You can close this tab."
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

        user_res = requests.get("https://api.twitch.tv/helix/users", headers={
            "Authorization": f"Bearer {access_token}",
            "Client-Id": TWITCH_CLIENT_ID
        })
        user_json = user_res.json()
        username = user_json['data'][0]['login']

        user_id = session.get("user_id") or str(uuid.uuid4())
        session["user_id"] = user_id
        save_twitch_tokens(user_id, access_token, refresh_token, expires_in, username)
        return "Twitch account linked! You can close this tab."

@app.route("/set_forward", methods=["POST"])
def set_forward():
    user_id = session.get("user_id")
    if not user_id:
        return "No user session found. Link your accounts first.", 400
    command = request.form.get("command")
    direction = request.form.get("direction")
    if not command or not direction:
        return "Missing command or direction", 400
    update_forwarding(user_id, command, direction)
    return "Forwarding command saved!"

# === TWITCH BOT ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=["#yourchannel"])

    async def event_ready(self):
        print(f"Twitch bot ready as {self.nick}")

    async def event_message(self, message):
        await self.handle_commands(message)
        if message.echo:
            return  # Ignore own messages

        user = message.author.name.lower()
        user_data = get_user_by_twitch_username(user)
        if not user_data:
            return

        command, direction = user_data[9], user_data[10]
        if not command or not message.content.startswith(command):
            return

        payload = message.content[len(command):].strip()
        if direction == "twitch_to_yt":
            yt_access_token = get_valid_youtube_token(user_data)
            yt_channel_id = user_data[4]
            if yt_access_token and yt_channel_id:
                success = send_message_to_youtube_livechat(yt_channel_id, yt_access_token, payload)
                if success:
                    await message.channel.send(f"Forwarded to YouTube: {payload}")
                else:
                    await message.channel.send("Failed to forward message to YouTube.")
            else:
                await message.channel.send("YouTube account not linked or token expired.")
        elif direction == "yt_to_twitch":
            # YouTube -> Twitch forwarding requires YouTube chat listener (not implemented here)
            pass

# === RUN APP & BOT ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def run_bot():
    bot = TwitchBot()
    asyncio.run(bot.run())

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    run_bot()
