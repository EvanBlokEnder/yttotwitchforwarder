import os
import json
import uuid
import time
import threading
import asyncio
import requests
from flask import Flask, redirect, request, session, render_template, url_for, flash
from twitchio.ext import commands
from requests_oauthlib import OAuth2Session

# === Config from ENV ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
FLASK_SECRET = os.getenv("FLASK_SECRET", "supersecretkey")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:10000/callback")

USERS_FILE = "users.json"

app = Flask(__name__)
app.secret_key = FLASK_SECRET

# === Load and save users JSON ===
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

# Initialize in-memory users dict
users = load_users()

# === OAuth constants ===
TWITCH_AUTH_BASE = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

YT_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl"]

# === Helper functions ===

def save_user_data():
    global users
    save_users(users)

def get_current_user_id():
    # From cookie/session
    return session.get("user_id")

def get_user_by_id(user_id):
    return users.get(user_id)

def add_or_update_user(user_id, data):
    global users
    users[user_id] = {**users.get(user_id, {}), **data}
    save_user_data()

def find_user_by_twitch_username(twitch_username):
    twitch_username = twitch_username.lower()
    for uid, user in users.items():
        if user.get("twitch_username", "").lower() == twitch_username:
            return uid, user
    return None, None

def find_user_by_yt_channel_id(yt_channel_id):
    for uid, user in users.items():
        if user.get("yt_channel_id") == yt_channel_id:
            return uid, user
    return None, None

# === Flask routes ===

@app.route("/")
def index():
    user_id = get_current_user_id()
    user = get_user_by_id(user_id) if user_id else None

    twitch_user = user.get("twitch_username") if user else None
    yt_channel = user.get("yt_channel_id") if user else None
    forward_command = user.get("forward_command", "")
    forward_direction = user.get("forward_direction", "")

    return render_template("index.html",
                           twitch_user=twitch_user,
                           yt_channel=yt_channel,
                           forward_command=forward_command,
                           forward_direction=forward_direction)

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    session['oauth_state'] = state
    session['oauth_in_progress'] = 'twitch'
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
    session['oauth_in_progress'] = 'youtube'
    oauth = OAuth2Session(YT_CLIENT_ID, redirect_uri=REDIRECT_URI, scope=YT_SCOPES, state=state, access_type='offline', prompt='consent')
    auth_url, _ = oauth.authorization_url(YT_AUTH_BASE)
    return redirect(auth_url)

@app.route("/callback")
def callback():
    try:
        state = request.args.get("state")
        if not state or state != session.get("oauth_state"):
            return "Invalid OAuth state", 400

        code = request.args.get("code")
        if not code:
            return "Missing code", 400

        in_progress = session.get("oauth_in_progress")
        if not in_progress:
            return "No OAuth process in progress", 400

        if in_progress == "twitch":
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

            # Create or update user
            user_id = session.get("user_id")
            if not user_id:
                user_id = str(uuid.uuid4())
                session["user_id"] = user_id

            add_or_update_user(user_id, {
                "twitch_username": twitch_username,
                "twitch_access_token": access_token,
                "twitch_refresh_token": refresh_token,
                "twitch_token_expiry": expiry
            })

        else:  # YouTube OAuth callback
            oauth = OAuth2Session(YT_CLIENT_ID, redirect_uri=REDIRECT_URI, scope=YT_SCOPES)
            token = oauth.fetch_token(YT_TOKEN_URL,
                                      client_secret=YT_CLIENT_SECRET,
                                      code=code)

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

            user_id = session.get("user_id")
            if not user_id:
                user_id = str(uuid.uuid4())
                session["user_id"] = user_id

            add_or_update_user(user_id, {
                "yt_access_token": access_token,
                "yt_refresh_token": refresh_token,
                "yt_token_expiry": expiry,
                "yt_channel_id": yt_channel_id
            })

        # Clean up
        session.pop("oauth_state", None)
        session.pop("oauth_in_progress", None)
        flash("Account linked successfully!")
        return redirect(url_for("index"))
    except Exception as e:
        return f"Error during OAuth callback: {e}", 500

@app.route("/set_forward", methods=["POST"])
def set_forward():
    user_id = get_current_user_id()
    if not user_id:
        return "You must link accounts first", 400

    command = request.form.get("command", "").strip()
    direction = request.form.get("direction", "").strip()

    if not command or not direction:
        return "Missing command or direction", 400

    user = get_user_by_id(user_id)
    if not user:
        return "User not found", 400

    add_or_update_user(user_id, {
        "forward_command": command,
        "forward_direction": direction
    })
    flash("Forwarding rule saved")
    return redirect(url_for("index"))

# === Twitch Bot ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=[])
        self.loop = asyncio.get_event_loop()

    async def event_ready(self):
        print(f"Twitch Bot logged in as | {self.nick}")
        # Join all Twitch usernames in users.json
        for user_id, user in users.items():
            twitch_user = user.get("twitch_username")
            if twitch_user:
                if twitch_user.lower() not in [c.name for c in self.connected_channels]:
                    await self.join_channels([twitch_user.lower()])

    async def event_message(self, message):
        if message.echo:
            return
        await self.handle_commands(message)

        user = message.author.name.lower()
        # Find user who owns this twitch username
        _, user_data = find_user_by_twitch_username(user)
        if not user_data or not user_data.get('forward_command'):
            return

        cmd = user_data.get('forward_command')
        direction = user_data.get('forward_direction')
        if message.content.startswith(cmd):
            content = message.content[len(cmd):].strip()
            if direction == "twitch_to_yt":
                await send_message_to_youtube(user_data, content)

async def send_message_to_youtube(user_data, message_text):
    now = int(time.time())
    access_token = user_data.get('yt_access_token')
    expiry = user_data.get('yt_token_expiry', 0)
    if not access_token or expiry < now:
        access_token = await refresh_yt_token_async(user_data)
        if not access_token:
            print("Failed to refresh YouTube token")
            return

    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get("https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet&broadcastStatus=active&mine=true", headers=headers)
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

async def refresh_yt_token_async(user_data):
    data = {
        'client_id': YT_CLIENT_ID,
        'client_secret': YT_CLIENT_SECRET,
        'refresh_token': user_data.get('yt_refresh_token'),
        'grant_type': 'refresh_token'
    }
    r = requests.post(YT_TOKEN_URL, data=data)
    if r.status_code == 200:
        js = r.json()
        access_token = js['access_token']
        expires_in = js.get('expires_in', 3600)
        expiry = int(time.time()) + expires_in

        # Update user data
        user_id = None
        for uid, u in users.items():
            if u == user_data:
                user_id = uid
                break
        if user_id:
            add_or_update_user(user_id, {
                "yt_access_token": access_token,
                "yt_token_expiry": expiry
            })
        return access_token
    return None

# === YouTube → Twitch polling ===

async def poll_yt_chats(bot):
    while True:
        for user_id, user in users.items():
            if user.get("forward_direction") == "yt_to_twitch":
                await poll_yt_chat_for_user(bot, user)
        await asyncio.sleep(5)

async def poll_yt_chat_for_user(bot, user):
    now = int(time.time())
    access_token = user.get('yt_access_token')
    expiry = user.get('yt_token_expiry', 0)
    if not access_token or expiry < now:
        access_token = await refresh_yt_token_async(user)
        if not access_token:
            print(f"Failed to refresh YouTube token for user {user.get('twitch_username') or 'unknown'}")
            return

    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get("https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet&broadcastStatus=active&mine=true", headers=headers)
    if r.status_code != 200:
        print(f"Failed to get live broadcasts for user: {r.text}")
        return
    data = r.json()
    if not data.get("items"):
        return
    live_chat_id = data["items"][0]["snippet"]["liveChatId"]

    params = {
        "liveChatId": live_chat_id,
        "part": "snippet,authorDetails",
        "maxResults": 50,
    }
    last_msg_id = user.get("last_yt_message_id")
    if last_msg_id:
        params["pageToken"] = last_msg_id

    r = requests.get("https://www.googleapis.com/youtube/v3/liveChat/messages", headers=headers, params=params)
    if r.status_code != 200:
        print(f"Failed to get live chat messages: {r.text}")
        return
    messages_data = r.json()
    messages = messages_data.get("items", [])
    if not messages:
        return

    for msg in messages:
        msg_id = msg['id']
        author = msg['authorDetails']['displayName']
        text = msg['snippet']['displayMessage']

        twitch_username = user.get("twitch_username")
        if not twitch_username:
            continue
        channel = next((c for c in bot.connected_channels if c.name == twitch_username.lower()), None)
        if channel:
            try:
                await channel.send(f"[YT] {author}: {text}")
            except Exception as e:
                print(f"Error sending message to Twitch channel: {e}")

        # Save last message ID pageToken for next poll
        users[user_id]["last_yt_message_id"] = messages_data.get('nextPageToken', msg_id)
        save_user_data()

# === Run Flask + Twitch bot ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

async def run_bot():
    bot = TwitchBot()
    asyncio.create_task(poll_yt_chats(bot))
    await bot.run()

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    asyncio.run(run_bot())
