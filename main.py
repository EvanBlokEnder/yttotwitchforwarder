import os
import sqlite3
import uuid
import asyncio
import threading
import requests
from flask import Flask, redirect, request, render_template, jsonify
from twitchio.ext import commands

# === CONFIG from ENV ===
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BOT_TOKEN = os.getenv("TWITCH_BOT_TOKEN")
YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:10000/callback")

if not all([TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_BOT_TOKEN, YT_CLIENT_ID, YT_CLIENT_SECRET]):
    raise Exception("Missing environment variables. Please set TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_BOT_TOKEN, YT_CLIENT_ID, YT_CLIENT_SECRET.")

# === DATABASE ===
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
  twitch_username TEXT UNIQUE,
  command TEXT,
  direction TEXT
)
""")
conn.commit()

app = Flask(__name__)

# === Flask ROUTES ===

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/auth/youtube")
def auth_youtube():
    state = str(uuid.uuid4())
    # Save state to identify user session or link later
    # For simplicity, using state as user ID here
    # Real production should use real user session management
    # Store state with no tokens yet
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (state,))
    conn.commit()
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        "?response_type=code"
        f"&client_id={YT_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        "&scope=https://www.googleapis.com/auth/youtube.readonly"
        "&access_type=offline"
        f"&state={state}"
        "&prompt=consent"
    )
    return redirect(url)

@app.route("/auth/twitch")
def auth_twitch():
    state = str(uuid.uuid4())
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (state,))
    conn.commit()
    scopes = "chat:read chat:edit"
    url = (
        "https://id.twitch.tv/oauth2/authorize"
        "?response_type=code"
        f"&client_id={TWITCH_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={scopes.replace(' ', '+')}"
        f"&state={state}"
        "&force_verify=true"
    )
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        return f"OAuth Error: {error}"

    if not code or not state:
        return "Missing code or state", 400

    # Check if state exists in DB
    cur.execute("SELECT id FROM users WHERE id = ?", (state,))
    if not cur.fetchone():
        return "Invalid state", 400

    # Determine if callback is from Twitch or YouTube by referrer or parameters
    # We will guess by trying token exchange for both, catch errors.

    # Try Twitch token exchange
    token_response = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
    )
    if token_response.ok:
        data = token_response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")
        # Get user info
        user_resp = requests.get(
            "https://api.twitch.tv/helix/users",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Client-Id": TWITCH_CLIENT_ID,
            },
        )
        if user_resp.ok:
            user_data = user_resp.json()["data"][0]
            twitch_username = user_data["login"]
            twitch_token_expiry = int(asyncio.get_event_loop().time()) + expires_in

            # Save tokens and username
            cur.execute(
                """
                UPDATE users SET
                twitch_access_token = ?,
                twitch_refresh_token = ?,
                twitch_token_expiry = ?,
                twitch_username = ?
                WHERE id = ?
                """,
                (access_token, refresh_token, twitch_token_expiry, twitch_username.lower(), state),
            )
            conn.commit()
            # Tell bot to join this twitch channel immediately
            if bot:
                asyncio.run_coroutine_threadsafe(bot.join_user_channel(twitch_username.lower()), bot.loop)
            return "Twitch account linked! You can close this tab."
        else:
            return "Failed to get Twitch user info", 500

    # If Twitch failed, try YouTube token exchange
    yt_token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": YT_CLIENT_ID,
            "client_secret": YT_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if yt_token_response.ok:
        data = yt_token_response.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")
        expiry = int(asyncio.get_event_loop().time()) + expires_in

        # Get YouTube channel ID
        headers = {"Authorization": f"Bearer {access_token}"}
        yt_resp = requests.get(
            "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true", headers=headers
        )
        if yt_resp.ok:
            yt_data = yt_resp.json()
            if yt_data["items"]:
                yt_channel_id = yt_data["items"][0]["id"]
                cur.execute(
                    """
                    UPDATE users SET
                    yt_access_token = ?,
                    yt_refresh_token = ?,
                    yt_token_expiry = ?,
                    yt_channel_id = ?
                    WHERE id = ?
                    """,
                    (access_token, refresh_token, expiry, yt_channel_id, state),
                )
                conn.commit()
                return "YouTube account linked! You can close this tab."
            else:
                return "No YouTube channel found.", 400
        else:
            return "Failed to fetch YouTube channel info.", 500
    else:
        return "OAuth token exchange failed for both Twitch and YouTube.", 400

@app.route("/set_forward", methods=["POST"])
def set_forward():
    user_id = request.form.get("user_id")
    command = request.form.get("command")
    direction = request.form.get("direction")
    if not user_id or not command or not direction:
        return "Missing parameters", 400

    # Save forwarding rules
    cur.execute(
        """
        UPDATE users SET command = ?, direction = ?
        WHERE id = ?
        """,
        (command, direction, user_id),
    )
    conn.commit()
    return "Forwarding rule saved."

@app.route("/users")
def get_users():
    # Simple API to see all linked users (for debugging)
    cur.execute("SELECT id, twitch_username, yt_channel_id, command, direction FROM users")
    users = cur.fetchall()
    return jsonify(users)

# === Twitch Bot ===

class TwitchBot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_BOT_TOKEN, prefix="!", initial_channels=[])
        self.joined_channels = set()

    async def event_ready(self):
        print(f"Twitch bot ready as {self.nick}")
        cur.execute("SELECT twitch_username FROM users WHERE twitch_username IS NOT NULL")
        channels = [f"#{row[0]}" for row in cur.fetchall()]
        for chan in channels:
            if chan not in self.joined_channels:
                await self.join_channels([chan])
                self.joined_channels.add(chan)
        print(f"Joined channels: {self.joined_channels}")

    async def join_user_channel(self, username: str):
        chan = f"#{username.lower()}"
        if chan not in self.joined_channels:
            await self.join_channels([chan])
            self.joined_channels.add(chan)
            print(f"Joined new channel: {chan}")

    async def event_message(self, message):
        if message.echo:
            return
        await self.handle_commands(message)

        channel_name = message.channel.name.lower()
        cur.execute("SELECT command, direction, yt_access_token, yt_refresh_token, yt_token_expiry FROM users WHERE twitch_username = ?", (channel_name,))
        row = cur.fetchone()
        if not row:
            return
        command, direction, yt_token, yt_refresh, yt_expiry = row
        if not command:
            return

        if message.content.startswith(command):
            payload = message.content[len(command):].strip()
            if direction == "twitch_to_yt":
                # Here you would implement sending the message to YouTube Live Chat using yt_token
                print(f"Forwarding from Twitch channel {channel_name} to YouTube: {payload}")
                # TODO: Implement YouTube LiveChat API send message with token refresh logic
            elif direction == "yt_to_twitch":
                # Forwarding from YouTube to Twitch is handled elsewhere, or you can extend API for it
                pass

# === Run Flask + Twitch bot concurrently ===

bot = TwitchBot()

def run_flask():
    app.run(host="0.0.0.0", port=10000)

def run_bot():
    asyncio.run(bot.run())

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    run_bot()
