import os
import sqlite3
import uuid
import asyncio
import threading
import time
import requests
from flask import Flask, redirect, request, session, render_template
from twitchio.ext import commands
from requests_oauthlib import OAuth2Session
import json

# === CONFIG from ENV ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
FLASK_SECRET = os.getenv("FLASK_SECRET", "supersecretkey")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:10000/callback")

# === Flask App ===
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# === DB Setup ===
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    twitch_username TEXT UNIQUE,
    twitch_access_token TEXT,
    twitch_refresh_token TEXT,
    twitch_token_expiry INTEGER,
    yt_access_token TEXT,
    yt_refresh_token TEXT,
    yt_token_expiry INTEGER,
    yt_channel_id TEXT,
    forward_command TEXT,
    forward_direction TEXT,
    last_yt_message_id TEXT
)
""")
conn.commit()

# === OAuth constants ===
TWITCH_AUTH_BASE = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

YT_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl"]

# === Helper DB funcs ===

def save_user(user):
    cur.execute("""
        INSERT INTO users (id, twitch_username, twitch_access_token, twitch_refresh_token, twitch_token_expiry, yt_access_token, yt_refresh_token, yt_token_expiry, yt_channel_id, forward_command, forward_direction, last_yt_message_id)
        VALUES (:id, :twitch_username, :twitch_access_token, :twitch_refresh_token, :twitch_token_expiry, :yt_access_token, :yt_refresh_token, :yt_token_expiry, :yt_channel_id, :forward_command, :forward_direction, :last_yt_message_id)
        ON CONFLICT(id) DO UPDATE SET
          twitch_username=excluded.twitch_username,
          twitch_access_token=excluded.twitch_access_token,
          twitch_refresh_token=excluded.twitch_refresh_token,
          twitch_token_expiry=excluded.twitch_token_expiry,
          yt_access_token=excluded.yt_access_token,
          yt_refresh_token=excluded.yt_refresh_token,
          yt_token_expiry=excluded.yt_token_expiry,
          yt_channel_id=excluded.yt_channel_id,
          forward_command=excluded.forward_command,
          forward_direction=excluded.forward_direction,
          last_yt_message_id=excluded.last_yt_message_id
    """, user)
    conn.commit()

def get_user_by_twitch_username(username):
    cur.execute("SELECT * FROM users WHERE twitch_username=?", (username,))
    row = cur.fetchone()
    if not row:
        return None
    keys = [desc[0] for desc in cur.description]
    return dict(zip(keys, row))

def get_user_by_id(user_id):
    cur.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return None
    keys = [desc[0] for desc in cur.description]
    return dict(zip(keys, row))

def update_forward(user_id, command, direction):
    cur.execute("UPDATE users SET forward_command=?, forward_direction=? WHERE id=?", (command, direction, user_id))
    conn.commit()

def update_last_yt_message_id(user_id, msg_id):
    cur.execute("UPDATE users SET last_yt_message_id=? WHERE id=?", (msg_id, user_id))
    conn.commit()

# === Token refresh ===

def refresh_twitch_token(user):
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': user['twitch_refresh_token'],
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_CLIENT_SECRET,
    }
    resp = requests.post(TWITCH_TOKEN_URL, data=data)
    if resp.status_code == 200:
        js = resp.json()
        access_token = js['access_token']
        refresh_token = js.get('refresh_token', user['twitch_refresh_token'])
        expires_in = js.get('expires_in', 3600)
        expiry = int(time.time()) + expires_in
        cur.execute("UPDATE users SET twitch_access_token=?, twitch_refresh_token=?, twitch_token_expiry=? WHERE id=?",
                    (access_token, refresh_token, expiry, user['id']))
        conn.commit()
        return access_token
    return None

def refresh_yt_token(user):
    data = {
        'client_id': YT_CLIENT_ID,
        'client_secret': YT_CLIENT_SECRET,
        'refresh_token': user['yt_refresh_token'],
        'grant_type': 'refresh_token'
    }
    resp = requests.post(YT_TOKEN_URL, data=data)
    if resp.status_code == 200:
        js = resp.json()
        access_token = js['access_token']
        expires_in = js.get('expires_in', 3600)
        expiry = int(time.time()) + expires_in
        cur.execute("UPDATE users SET yt_access_token=?, yt_token_expiry=? WHERE id=?",
                    (access_token, expiry, user['id']))
        conn.commit()
        return access_token
    return None

# === Flask routes ===

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    session['oauth_state'] = state
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "chat:read chat:edit",
        "state": state,
        "force_verify": "true",
    }
    url = f"{TWITCH_AUTH_BASE}?{'&'.join(f'{k}={v}' for k,v in params.items())}"
    return redirect(url)

@app.route("/auth/youtube")
def auth_youtube():
    state = str(uuid.uuid4())
    session['oauth_state'] = state
    oauth = OAuth2Session(YT_CLIENT_ID, redirect_uri=REDIRECT_URI, scope=YT_SCOPES, state=state, access_type='offline', prompt='consent')
    auth_url, _ = oauth.authorization_url(YT_AUTH_BASE)
    return redirect(auth_url)

@app.route("/callback")
def callback():
    state = request.args.get("state")
    if state != session.get('oauth_state'):
        return "Invalid OAuth state", 400

    code = request.args.get("code")
    if not code:
        return "Missing code", 400

    if 'scope' in request.args and ('twitch' in request.args.get('scope', '') or 'id.twitch.tv' in (request.referrer or '')):
        # Twitch OAuth callback
        data = {
            'client_id': TWITCH_CLIENT_ID,
            'client_secret': TWITCH_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': REDIRECT_URI,
        }
        r = requests.post(TWITCH_TOKEN_URL, data=data)
        if r.status_code != 200:
            return f"Twitch token error: {r.text}", 400
        js = r.json()
        access_token = js['access_token']
        refresh_token = js.get('refresh_token')
        expires_in = js.get('expires_in', 3600)
        expiry = int(time.time()) + expires_in

        headers = {
            'Client-ID': TWITCH_CLIENT_ID,
            'Authorization': f"Bearer {access_token}"
        }
        user_info = requests.get(f"{TWITCH_API_BASE}/users", headers=headers).json()
        if "data" not in user_info or len(user_info["data"]) == 0:
            return "Failed to get Twitch user info", 400
        twitch_username = user_info["data"][0]["login"]

        user_id = f"twitch_{twitch_username}"
        existing = get_user_by_id(user_id)
        if existing:
            cur.execute("""
                UPDATE users SET twitch_access_token=?, twitch_refresh_token=?, twitch_token_expiry=?, twitch_username=?
                WHERE id=?
            """, (access_token, refresh_token, expiry, twitch_username, user_id))
        else:
            cur.execute("""
                INSERT INTO users (id, twitch_username, twitch_access_token, twitch_refresh_token, twitch_token_expiry)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, twitch_username, access_token, refresh_token, expiry))
        conn.commit()
        return f"Twitch account @{twitch_username} linked successfully. You can close this tab."

    else:
        # YouTube OAuth callback
        oauth = OAuth2Session(YT_CLIENT_ID, redirect_uri=REDIRECT_URI, scope=YT_SCOPES)
        try:
            token = oauth.fetch_token(YT_TOKEN_URL,
                                      client_secret=YT_CLIENT_SECRET,
                                      code=code)
        except Exception as e:
            return f"Failed to get YouTube token: {e}", 400

        access_token = token.get("access_token")
        refresh_token = token.get("refresh_token")
        expires_in = token.get("expires_in", 3600)
        expiry = int(time.time()) + expires_in

        headers = {"Authorization": f"Bearer {access_token}"}
        r = requests.get("https://www.googleapis.com/youtube/v3/channels?part=id&mine=true", headers=headers)
        if r.status_code != 200:
            return "Failed to get YouTube channel info", 400
        data = r.json()
        if "items" not in data or len(data["items"]) == 0:
            return "No YouTube channel found", 400
        yt_channel_id = data["items"][0]["id"]

        user_id = f"yt_{yt_channel_id}"
        existing = get_user_by_id(user_id)
        if existing:
            cur.execute("""
                UPDATE users SET yt_access_token=?, yt_refresh_token=?, yt_token_expiry=?, yt_channel_id=?
                WHERE id=?
            """, (access_token, refresh_token, expiry, yt_channel_id, user_id))
        else:
            cur.execute("""
                INSERT INTO users (id, yt_access_token, yt_refresh_token, yt_token_expiry, yt_channel_id)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, access_token, refresh_token, expiry, yt_channel_id))
        conn.commit()
        return f"YouTube channel linked successfully. You can close this tab."

@app.route("/set_forward", methods=["POST"])
def set_forward():
    command = request.form.get("command", "").strip()
    direction = request.form.get("direction", "").strip()
    twitch_username = request.form.get("twitch_username", "").strip().lower()

    if not command or not direction or not twitch_username:
        return "Missing fields", 400

    user = get_user_by_twitch_username(twitch_username)
    if not user:
        return "Twitch user not linked yet", 400

    update_forward(user['id'], command, direction)
    return "Forwarding rule saved."

# === Twitch Bot ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=[])
        self.loop = asyncio.get_event_loop()

    async def event_ready(self):
        print(f"Twitch Bot logged in as | {self.nick}")
        cur.execute("SELECT twitch_username FROM users WHERE twitch_username IS NOT NULL")
        channels = [row[0] for row in cur.fetchall()]
        for ch in channels:
            if ch not in [c.name for c in self.connected_channels]:
                await self.join_channels([ch])

    async def event_message(self, message):
        if message.echo:
            return
        await self.handle_commands(message)

        user = message.author.name.lower()
        user_data = get_user_by_twitch_username(user)
        if not user_data or not user_data['forward_command']:
            return

        cmd = user_data['forward_command']
        direction = user_data['forward_direction']
        if message.content.startswith(cmd):
            content = message.content[len(cmd):].strip()
            if direction == "twitch_to_yt":
                await send_message_to_youtube(user_data, content)
            elif direction == "yt_to_twitch":
                # handled in poller
                pass

async def send_message_to_youtube(user_data, message_text):
    now = int(time.time())
    access_token = user_data['yt_access_token']
    if user_data['yt_token_expiry'] is None or user_data['yt_token_expiry'] < now:
        access_token = refresh_yt_token(user_data)
        if not access_token:
            print("Failed to refresh YouTube token")
            return

    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet&broadcastStatus=active&broadcastType=all&mine=true", headers=headers)
    if r.status_code != 200:
        print("Failed to get live broadcasts:", r.text)
        return
    data = r.json()
    if not data.get("items"):
        print("No active YouTube live broadcasts found")
        return
    live_chat_id = data["items"][0]["snippet"]["liveChatId"]

    url = "https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet"
    payload = {
        "snippet": {
            "liveChatId": live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": message_text}
        }
    }
    r = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
    if r.status_code != 200:
        print("Failed to send YouTube chat message:", r.text)
        return
    print("Forwarded Twitch msg to YouTube chat")

# === YouTube → Twitch polling ===

async def poll_yt_chats(bot):
    while True:
        cur.execute("SELECT * FROM users WHERE forward_direction='yt_to_twitch'")
        users = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        for user in users:
            await poll_yt_chat_for_user(bot, user)
        await asyncio.sleep(5)

async def poll_yt_chat_for_user(bot, user):
    now = int(time.time())
    access_token = user['yt_access_token']
    if user['yt_token_expiry'] is None or user['yt_token_expiry'] < now:
        access_token = refresh_yt_token(user)
        if not access_token:
            print(f"Failed to refresh YouTube token for {user['id']}")
            return

    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet&broadcastStatus=active&broadcastType=all&mine=true", headers=headers)
    if r.status_code != 200:
        print(f"Failed to get live broadcasts for {user['id']}: {r.text}")
        return
    data = r.json()
    if not data.get("items"):
        # No active stream, skip
        return
    live_chat_id = data["items"][0]["snippet"]["liveChatId"]

    params = {
        "liveChatId": live_chat_id,
        "part": "snippet,authorDetails",
        "maxResults": 50,
    }
    if user['last_yt_message_id']:
        params['pageToken'] = user['last_yt_message_id']

    r = requests.get("https://www.googleapis.com/youtube/v3/liveChat/messages", headers=headers, params=params)
    if r.status_code != 200:
        print(f"Failed to get live chat messages for {user['id']}: {r.text}")
        return
    messages_data = r.json()
    messages = messages_data.get("items", [])
    if not messages:
        return

    for msg in messages:
        msg_id = msg['id']
        author = msg['authorDetails']['displayName']
        text = msg['snippet']['displayMessage']
        # Ignore messages sent by this bot itself (optional)
        if author == user['yt_channel_id']:
            continue

        twitch_username = user['twitch_username']
        channel = next((c for c in bot.connected_channels if c.name == twitch_username), None)
        if channel:
            try:
                await channel.send(f"[YT] {author}: {text}")
            except Exception as e:
                print(f"Failed to send message to Twitch channel {twitch_username}: {e}")

        # Update last seen message ID
        update_last_yt_message_id(user['id'], messages_data.get('nextPageToken', msg_id))

# === Run app + bot ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

async def run_bot():
    bot = TwitchBot()
    # Start YouTube chat polling in background
    asyncio.create_task(poll_yt_chats(bot))
    await bot.run()

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    asyncio.run(run_bot())
