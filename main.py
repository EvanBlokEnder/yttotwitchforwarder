import os
import json
import uuid
import time
import asyncio
import threading
import requests
from aiohttp import ClientSession
from flask import Flask, request, redirect, render_template, make_response
from twitchio.ext import commands

# === CONFIG ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
TWITCH_BOT_ID = os.getenv("TWITCH_BOT_ID")  # numeric user ID string
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:10000/callback")

USERS_FILE = "users.json"

app = Flask(__name__)

# === USER DATA PERSISTENCE ===

def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)

users = load_users()

def get_current_user_id():
    user_id = request.cookies.get("user_id")
    if not user_id:
        user_id = str(uuid.uuid4())
    return user_id

def get_user(user_id):
    return users.get(user_id, {})

def update_user(user_id, data):
    user = users.get(user_id, {})
    user.update(data)
    users[user_id] = user
    save_users(users)

# === FLASK ROUTES ===

@app.route("/")
def index():
    user_id = get_current_user_id()
    user = get_user(user_id) or {}

    resp = make_response(render_template(
        "index.html",
        twitch_user=user.get("twitch_username"),
        yt_channel=user.get("yt_channel"),
        forward_command=user.get("forward_command", ""),
        forward_direction=user.get("forward_direction", ""),
        linked=True if ("twitch_token" in user or "yt_token" in user) else False
    ))

    if "user_id" not in request.cookies:
        resp.set_cookie("user_id", user_id, max_age=60*60*24*365)

    return resp

@app.route("/auth/youtube")
def auth_youtube():
    user_id = get_current_user_id()
    state = f"yt:{user_id}"
    scope = "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/youtube"
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={YT_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&"
        f"scope={scope}&access_type=offline&state={state}&prompt=consent"
    )
    return redirect(url)

@app.route("/auth/twitch")
def auth_twitch():
    user_id = get_current_user_id()
    state = f"twitch:{user_id}"
    scopes = "chat:read chat:edit"
    url = (
        "https://id.twitch.tv/oauth2/authorize?"
        f"client_id={TWITCH_CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&"
        f"scope={scopes}&state={state}&force_verify=true"
    )
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        print(f"Auth error: {error}")
        return f"Error: {error}"

    if not code or not state:
        print("Missing code or state in callback")
        return "Missing code or state", 400

    if state.startswith("twitch:"):
        user_id = state[len("twitch:"):]
        token_url = "https://id.twitch.tv/oauth2/token"
        payload = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
        resp = requests.post(token_url, data=payload)
        if resp.status_code != 200:
            print(f"Failed to get Twitch token: {resp.text}")
            return f"Failed to get Twitch token: {resp.text}", 500
        data = resp.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        headers = {"Authorization": f"Bearer {access_token}", "Client-Id": TWITCH_CLIENT_ID}
        user_resp = requests.get("https://api.twitch.tv/helix/users", headers=headers)
        if user_resp.status_code != 200:
            print(f"Failed to get Twitch user info: {user_resp.text}")
            return f"Failed to get Twitch user info: {user_resp.text}", 500
        user_data = user_resp.json()
        username = user_data["data"][0]["login"]
        update_user(user_id, {
            "twitch_token": access_token,
            "twitch_refresh": refresh_token,
            "twitch_username": username,
            "twitch_token_expiry": time.time() + data.get("expires_in", 0),
        })

    elif state.startswith("yt:"):
        user_id = state[len("yt:"):]
        token_url = "https://oauth2.googleapis.com/token"
        payload = {
            "code": code,
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }
        resp = requests.post(token_url, data=payload)
        if resp.status_code != 200:
            print(f"Failed to get YouTube token: {resp.text}")
            return f"Failed to get YouTube token: {resp.text}", 500
        data = resp.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in", 0)
        headers = {"Authorization": f"Bearer {access_token}"}
        yt_resp = requests.get("https://www.googleapis.com/youtube/v3/channels?part=id&mine=true", headers=headers)
        if yt_resp.status_code != 200:
            print(f"Failed to get YouTube channel info: {yt_resp.text}")
            return f"Failed to get YouTube channel info: {yt_resp.text}", 500
        yt_data = yt_resp.json()
        channel_id = yt_data["items"][0]["id"]
        update_user(user_id, {
            "yt_token": access_token,
            "yt_refresh": refresh_token,
            "yt_channel": channel_id,
            "yt_token_expiry": time.time() + expires_in
        })

    else:
        print("Invalid state format in callback")
        return "Invalid state format", 400

    resp = make_response(redirect("/"))
    resp.set_cookie("user_id", user_id, max_age=60*60*24*365)
    return resp

@app.route("/set_forward", methods=["POST"])
def set_forward():
    user_id = get_current_user_id()
    command = request.form.get("command", "").strip()
    direction = request.form.get("direction", "").strip()
    if not command or direction not in ("yt_to_twitch", "twitch_to_yt"):
        print(f"Invalid input for set_forward: command={command}, direction={direction}")
        return "Invalid input", 400

    update_user(user_id, {
        "forward_command": command,
        "forward_direction": direction
    })
    print(f"Set forward for user {user_id}: command={command}, direction={direction}")
    return redirect("/")

# === TOKEN REFRESH LOGIC ===

def refresh_youtube_token(user_id):
    user = get_user(user_id)
    if not user or "yt_refresh" not in user:
        print(f"No YouTube refresh token for user {user_id}")
        return False

    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "client_id": YT_CLIENT_ID,
        "client_secret": YT_CLIENT_SECRET,
        "refresh_token": user["yt_refresh"],
        "grant_type": "refresh_token"
    }
    resp = requests.post(token_url, data=payload)
    if resp.status_code != 200:
        print(f"Failed to refresh YouTube token for user {user_id}: {resp.text}")
        return False
    data = resp.json()
    access_token = data.get("access_token")
    expires_in = data.get("expires_in", 0)
    update_user(user_id, {
        "yt_token": access_token,
        "yt_token_expiry": time.time() + expires_in
    })
    print(f"Refreshed YouTube token for user {user_id}")
    return True

def refresh_twitch_token(user_id):
    user = get_user(user_id)
    if not user or "twitch_refresh" not in user:
        print(f"No Twitch refresh token for user {user_id}")
        return False

    token_url = "https://id.twitch.tv/oauth2/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": user["twitch_refresh"],
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
    }
    resp = requests.post(token_url, data=payload)
    if resp.status_code != 200:
        print(f"Failed to refresh Twitch token for user {user_id}: {resp.text}")
        return False
    data = resp.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 0)
    update_user(user_id, {
        "twitch_token": access_token,
        "twitch_refresh": refresh_token,
        "twitch_token_expiry": time.time() + expires_in,
    })
    print(f"Refreshed Twitch token for user {user_id}")
    return True

# === YOUTUBE LIVE CHAT POLLING + FORWARDING ===

class YouTubeLiveChatPoller:
    def __init__(self, bot):
        self.bot = bot
        self.running = True
        self.last_message_ids = {}  # user_id -> set(message_ids)

    async def start(self):
        async with ClientSession() as session:
            while self.running:
                await self.poll_all_users(session)
                await asyncio.sleep(5)  # poll every 5 seconds

    async def poll_all_users(self, session):
        for user_id, user in list(users.items()):
            if "yt_token" not in user or "yt_channel" not in user or "forward_direction" not in user:
                print(f"Skipping user {user_id}: Missing YouTube token, channel, or forward direction")
                continue
            if user["forward_direction"] != "yt_to_twitch":
                print(f"Skipping user {user_id}: Forward direction is {user['forward_direction']}")
                continue

            expiry = user.get("yt_token_expiry", 0)
            if time.time() > expiry - 60:
                print(f"Refreshing YouTube token for user {user_id}")
                if not refresh_youtube_token(user_id):
                    print(f"Failed to refresh YouTube token for user {user_id}, skipping")
                    continue
                user = get_user(user_id)

            await self.poll_live_chat(user_id, user, session)

    async def poll_live_chat(self, user_id, user, session):
        try:
            headers = {"Authorization": f"Bearer {user['yt_token']}"}

            url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={user['yt_channel']}&eventType=live&type=video"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"YT Live search failed for user {user_id}: {await resp.text()}")
                    return
                data = await resp.json()

            items = data.get("items", [])
            if not items:
                print(f"No live stream found for user {user_id}")
                return

            live_video_id = items[0]["id"]["videoId"]
            print(f"Found live stream {live_video_id} for user {user_id}")

            details_url = f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={live_video_id}"
            async with session.get(details_url, headers=headers) as details_resp:
                if details_resp.status != 200:
                    print(f"YT Live details failed for user {user_id}: {await details_resp.text()}")
                    return
                details_data = await details_resp.json()

            live_chat_id = details_data["items"][0]["liveStreamingDetails"].get("activeLiveChatId")
            if not live_chat_id:
                print(f"No active live chat for video {live_video_id} for user {user_id}")
                return

            chat_url = f"https://www.googleapis.com/youtube/v3/liveChat/messages?liveChatId={live_chat_id}&part=snippet,authorDetails"
            async with session.get(chat_url, headers=headers) as chat_resp:
                if chat_resp.status != 200:
                    print(f"YT Live chat messages failed for user {user_id}: {await chat_resp.text()}")
                    return
                chat_data = await resp.json()

            messages = chat_data.get("items", [])
            print(f"Found {len(messages)} messages for user {user_id}")

            if user_id not in self.last_message_ids:
                self.last_message_ids[user_id] = set()

            for message in messages:
                msg_id = message["id"]
                if msg_id in self.last_message_ids[user_id]:
                    continue
                self.last_message_ids[user_id].add(msg_id)

                text = message["snippet"]["displayMessage"]
                author = message["authorDetails"]["displayName"]

                twitch_username = user.get("twitch_username")
                if twitch_username and twitch_username.lower() in self.bot.connected_channels:
                    channel = self.bot.connected_channels[twitch_username.lower()]
                    send_text = f"[YT] {author}: {text}"
                    print(f"Forwarding YT->Twitch for user {user_id}: {send_text}")
                    await channel.send(send_text)
                    await channel.send(f"!@{twitch_username} response from YouTube {text}")
                else:
                    print(f"Cannot forward YT->Twitch for user {user_id}: Twitch channel {twitch_username} not connected")

        except Exception as e:
            print(f"Error polling YouTube live chat for user {user_id}: {e}")

# === TWITCH BOT ===

class TwitchBot(commands.Bot):
    def __init__(self):
        if not TWITCH_BOT_TOKEN:
            raise ValueError("TWITCH_BOT_TOKEN is not set")
        super().__init__(
            token=TWITCH_BOT_TOKEN,
            prefix="!",
            initial_channels=[],
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            bot_id=TWITCH_BOT_ID,
        )
        self.youtube_poller = YouTubeLiveChatPoller(self)

    async def event_ready(self):
        print(f"Bot ready: {self.nick}")
        print(f"Connected channels: {list(self.connected_channels.keys())}")
        asyncio.create_task(self.join_linked_channels())
        asyncio.create_task(self.youtube_poller.start())

    async def join_linked_channels(self):
        twitch_users = {u["twitch_username"].lower() for u in users.values() if "twitch_username" in u}
        for channel in twitch_users:
            print(f"Attempting to join Twitch channel: {channel}")
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                try:
                    await self.join_channels([channel])
                    print(f"Successfully joined Twitch channel: {channel}")
                    break
                except Exception as e:
                    retry_count += 1
                    print(f"Failed to join channel {channel} (attempt {retry_count}/{max_retries}): {e}")
                    if retry_count == max_retries:
                        print(f"Giving up on joining channel {channel}")
                    await asyncio.sleep(5)

    async def event_message(self, message):
        if message.echo:
            return

        await self.handle_commands(message)

        user = message.author.name.lower()
        matched_user_id = None
        matched_user = None
        for uid, u in users.items():
            if u.get("twitch_username", "").lower() == user:
                matched_user_id = uid
                matched_user = u
                break

        if matched_user:
            cmd = matched_user.get("forward_command")
            direction = matched_user.get("forward_direction")
            print(f"Processing message from {user}: command={cmd}, direction={direction}")
            if cmd and message.content.startswith(cmd):
                payload = message.content[len(cmd):].strip()
                if direction == "twitch_to_yt":
                    print(f"Processing Twitch->YT for user {matched_user_id}: {payload}")
                    await message.channel.send(f"!@{user} the message will be sent to YouTube")
                    expiry = matched_user.get("yt_token_expiry", 0)
                    if time.time() > expiry - 60:
                        print(f"Refreshing YouTube token for user {matched_user_id}")
                        if not refresh_youtube_token(matched_user_id):
                            print(f"Failed to refresh YouTube token for user {matched_user_id}")
                            await message.channel.send(f"!@{user} Failed to forward to YouTube: Token refresh failed")
                            return
                        matched_user = get_user(matched_user_id)

                    try:
                        async with ClientSession() as session:
                            headers = {"Authorization": f"Bearer {matched_user['yt_token']}"}
                            url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={matched_user['yt_channel']}&eventType=live&type=video"
                            async with session.get(url, headers=headers) as resp:
                                if resp.status != 200:
                                    print(f"YT Live search failed for user {matched_user_id}: {await resp.text()}")
                                    await message.channel.send(f"!@{user} Failed to forward to YouTube: No live stream found")
                                    return
                                data = await resp.json()

                            items = data.get("items", [])
                            if not items:
                                print(f"No live stream found for user {matched_user_id}")
                                await message.channel.send(f"!@{user} Failed to forward to YouTube: No live stream found")
                                return

                            live_video_id = items[0]["id"]["videoId"]
                            details_url = f"https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id={live_video_id}"
                            async with session.get(details_url, headers=headers) as details_resp:
                                if details_resp.status != 200:
                                    print(f"YT Live details failed for user {matched_user_id}: {await details_resp.text()}")
                                    await message.channel.send(f"!@{user} Failed to forward to YouTube: Could not get live stream details")
                                    return
                                details_data = await details_resp.json()

                            live_chat_id = details_data["items"][0]["liveStreamingDetails"].get("activeLiveChatId")
                            if not live_chat_id:
                                print(f"No active live chat for video {live_video_id} for user {matched_user_id}")
                                await message.channel.send(f"!@{user} Failed to forward to YouTube: No active live chat")
                                return

                            chat_url = f"https://www.googleapis.com/youtube/v3/liveChat/messages?part=snippet"
                            payload = {
                                "snippet": {
                                    "liveChatId": live_chat_id,
                                    "type": "textMessageEvent",
                                    "textMessageDetails": {
                                        "messageText": f"[Twitch] {user}: {payload}"
                                    }
                                }
                            }
                            async with session.post(chat_url, headers=headers, json=payload) as chat_resp:
                                if chat_resp.status != 200:
                                    print(f"Failed to send message to YouTube for user {matched_user_id}: {await chat_resp.text()}")
                                    await message.channel.send(f"!@{user} Failed to forward to YouTube: Could not send message")
                                    return
                                chat_data = await chat_resp.json()
                                sent_message = chat_data["snippet"]["displayMessage"]
                                print(f"Forwarded Twitch->YT for user {matched_user_id}: {sent_message}")
                                await message.channel.send(f"!@{user} response from YouTube {sent_message}")

                    except Exception as e:
                        print(f"Error forwarding Twitch->YT for user {matched_user_id}: {e}")
                        await message.channel.send(f"!@{user} Failed to forward to YouTube: Internal error")
                else:
                    print(f"Twitch message from {user} not forwarded: Direction is {direction}")

# === RUN SERVER + BOT ===

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def run_bot():
    missing_vars = []
    env_vars = {
        "TWITCH_CLIENT_ID": TWITCH_CLIENT_ID,
        "TWITCH_CLIENT_SECRET": TWITCH_CLIENT_SECRET,
        "TWITCH_BOT_TOKEN": TWITCH_BOT_TOKEN,
        "TWITCH_BOT_ID": TWITCH_BOT_ID,
        "YT_CLIENT_ID": YT_CLIENT_ID,
        "YT_CLIENT_SECRET": YT_CLIENT_SECRET
    }
    for var_name, var_value in env_vars.items():
        if not var_value:
            missing_vars.append(var_name)
    if missing_vars:
        print(f"Missing required environment variables: {', '.join(missing_vars)}")
        return
    bot = TwitchBot()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.start())
    except Exception as e:
        print(f"Bot failed to start: {e}")
    finally:
        loop.close()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
