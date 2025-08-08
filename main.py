import asyncio
import json
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    CallbackQuery,
    InputMediaDocument,
)

# --- CONFIGURATION ---

API_ID = 20660797          # your Telegram API ID
API_HASH = "755e5cdf9bade62a75211e7a57f25601"
BOT_TOKEN = "8448084086:AAGT6-0n3g3OkQDYxxlyXdJESsYmjX9_ISA"
DATA_FILE = "data.json"

ADMINS = {7941175119}     # Telegram user ids of admins

ITEMS_PER_PAGE = 8       # pagination size for repo listings etc

# --- GLOBALS ---

app = Client("spilux_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# In-memory user data cache (load/save from/to DATA_FILE)
data = {
    "users": {},       # user_id: {first_name, tokens: {token_key: {token, username}}, active_token, banned, repos_cache}
    "banned": [],      # list of banned user ids (int)
    "broadcasts": [],  # previous broadcast messages IDs (optional)
}

# User interaction state machine storage (uid -> state dict)
user_states = {}

# --- UTILITIES ---

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_data():
    global data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        save_data()

def user_data(uid: int):
    uid_str = str(uid)
    if "users" not in data:
        data["users"] = {}  # initialize if missing
    if uid_str not in data["users"]:
        data["users"][uid_str] = {
            "first_name": "",
            "tokens": {},
            "active_token": None,
            "banned": False,
            "repos_cache": {},
        }
    return data["users"][uid_str]

def is_banned(uid: int):
    # if uid is None, return False (can't ban unknown)
    if uid is None:
        return False
    return str(uid) in data.get("banned", []) or user_data(uid).get("banned", False)

def make_back_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Back", callback_data="back")]]
    )

def build_keyboard(buttons_list):
    return InlineKeyboardMarkup(buttons_list)

def escape_md(text: str) -> str:
    # Basic Markdown escape for simple usage (we use MARKDOWN, not MarkdownV2)
    if not isinstance(text, str):
        return ""
    # escape backticks and brackets minimally
    return text.replace("`", "\\`").replace("[", "\\[").replace("]", "\\]")

def format_repo(repo):
    # Format repo for messages
    desc = repo.get("description") or ""
    desc = desc[:60] + "…" if len(desc) > 60 else desc
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    full_name = repo.get("full_name", f"{repo.get('owner',{}).get('login','')}/{repo.get('name','')}")
    return f"**{escape_md(full_name)}**\n⭐ {stars} | 🍴 {forks}\n_{escape_md(desc)}_"

def format_user(user):
    name = user.get("name") or user.get("login")
    bio = user.get("bio") or ""
    bio = bio[:100] + "…" if len(bio) > 100 else bio
    repos = user.get("public_repos", 0)
    followers = user.get("followers", 0)
    following = user.get("following", 0)
    return f"👤 **{escape_md(name)}**\n{escape_md(bio)}\nRepos: {repos} | Followers: {followers} | Following: {following}"

async def fetch_json(session: aiohttp.ClientSession, url: str, token: Optional[str] = None, method="GET", data=None):
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        if method == "GET":
            async with session.get(url, headers=headers) as resp:
                return await resp.json(), resp.status
        elif method == "POST":
            async with session.post(url, headers=headers, json=data) as resp:
                return await resp.json(), resp.status
        elif method == "DELETE":
            async with session.delete(url, headers=headers) as resp:
                return await resp.json(), resp.status
        elif method == "PUT":
            async with session.put(url, headers=headers, json=data) as resp:
                # some endpoints (like starring) return empty body -> handle gracefully
                try:
                    body = await resp.json()
                except Exception:
                    body = {}
                return body, resp.status
    except Exception as e:
        return {"error": str(e)}, 500

async def download_repo_zip(session: aiohttp.ClientSession, owner: str, repo: str, token: Optional[str] = None):
    url = f"https://api.github.com/repos/{owner}/{repo}/zipball"
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.read()
            else:
                return None
    except:
        return None

def sanitize_filename(name):
    return "".join(c for c in name if c.isalnum() or c in "._- ").rstrip()

# --- MIDDLEWARE ---

@app.on_message(filters.private)
async def some_handler(client, message: Message):
    # This middleware ensures banned users are blocked early for any private message
    user_id = message.from_user.id if message.from_user else None
    if is_banned(user_id):
        try:
            await message.reply("🚫 You are banned from using this bot.")
        except:
            pass
        return
    # continue handling message (other handlers will run too)

# --- START COMMAND ---

@app.on_message(filters.private & filters.command("start"))
async def start_cmd(_, msg: Message):
    # guard: ensure from_user exists
    if not msg.from_user:
        return
    uid = msg.from_user.id
    udata = user_data(uid)
    udata["first_name"] = msg.from_user.first_name or udata.get("first_name", "")
    save_data()

    buttons = [
        [InlineKeyboardButton("🔑 Add Token", callback_data="token_add")],
        [InlineKeyboardButton("🔁 Switch Token", callback_data="token_switch_list")],
        [InlineKeyboardButton("🗑 Remove Token", callback_data="token_remove_list")],
        [InlineKeyboardButton("📂 My Repos", callback_data="myrepos_page:0")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data="upload_zip")],
        [InlineKeyboardButton("🔍 Search GitHub User", callback_data="search_user_prompt")],
        [InlineKeyboardButton("🔎 Search Repos (keyword)", callback_data="search_repo_prompt")],
        [InlineKeyboardButton("📈 Trending Repos", callback_data="trending")],
        [InlineKeyboardButton("🎲 Random Repo", callback_data="random_repo")],
        [InlineKeyboardButton("🧾 Create Gist", callback_data="gist_create_prompt")],
        [InlineKeyboardButton("⭐ Star a Repo", callback_data="star_repo_prompt")],
        [InlineKeyboardButton("📊 GitHub API Stats", callback_data="gh_stats")],
    ]
    if uid in ADMINS:
        buttons.append([InlineKeyboardButton("👥 Admin Panel", callback_data="admin_panel")])

    kb = InlineKeyboardMarkup(buttons)
    await msg.reply("👋 Welcome to Spilux GitHub Bot! Select an option:", reply_markup=kb)

# --- CALLBACK QUERY HANDLER ---

@app.on_callback_query()
async def cb_handler(_, cq: CallbackQuery):
    # guard
    if not cq.from_user:
        await cq.answer("Unknown user.", show_alert=True)
        return

    uid = cq.from_user.id
    data_cb = cq.data
    udata = user_data(uid)

    if is_banned(uid):
        await cq.answer("🚫 You are banned.", show_alert=True)
        return

    # BACK BUTTON (returns to start menu)
    if data_cb == "back":
        await start_cmd(_, cq.message)
        await cq.answer()
        return

    # TOKEN ADD
    if data_cb == "token_add":
        user_states[uid] = {"action": "token_add"}
        await cq.message.edit(
            "🔑 Send your GitHub Personal Access Token (PAT).\nSend 'Back' to cancel.",
            reply_markup=make_back_button(),
        )
        await cq.answer()
        return

    # TOKEN SWITCH LIST
    if data_cb == "token_switch_list":
        tokens = udata.get("tokens", {})
        if not tokens:
            await cq.answer("You have no saved tokens.", show_alert=True)
            return
        buttons = []
        for key in tokens.keys():
            buttons.append([InlineKeyboardButton(key, callback_data=f"token_switch:{key}")])
        buttons.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
        await cq.message.edit("Select a token to activate:", reply_markup=InlineKeyboardMarkup(buttons))
        await cq.answer()
        return

    if data_cb.startswith("token_switch:"):
        token_key = data_cb.split(":", 1)[1]
        if token_key in udata.get("tokens", {}):
            udata["active_token"] = token_key
            save_data()
            await cq.message.edit(f"✅ Token switched to {token_key}", reply_markup=make_back_button())
        else:
            await cq.answer("Token not found.", show_alert=True)
        return

    # TOKEN REMOVE LIST
    if data_cb == "token_remove_list":
        tokens = udata.get("tokens", {})
        if not tokens:
            await cq.answer("You have no tokens.", show_alert=True)
            return
        buttons = []
        for key in tokens.keys():
            buttons.append([InlineKeyboardButton(key, callback_data=f"token_remove:{key}")])
        buttons.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
        await cq.message.edit("Select a token to remove:", reply_markup=InlineKeyboardMarkup(buttons))
        await cq.answer()
        return

    if data_cb.startswith("token_remove:"):
        token_key = data_cb.split(":", 1)[1]
        if token_key in udata.get("tokens", {}):
            udata["tokens"].pop(token_key)
            if udata.get("active_token") == token_key:
                udata["active_token"] = None
            save_data()
            await cq.message.edit(f"🗑 Token {token_key} removed.", reply_markup=make_back_button())
        else:
            await cq.answer("Token not found.", show_alert=True)
        return

    # MY REPOS - pagination
    if data_cb.startswith("myrepos_page:"):
        page = int(data_cb.split(":")[1])
        active_token_key = udata.get("active_token")
        if not active_token_key:
            await cq.answer("Activate a token first.", show_alert=True)
            return
        token = udata["tokens"].get(active_token_key, {}).get("token")
        if not token:
            await cq.answer("Invalid active token.", show_alert=True)
            return
        await show_repos_page(cq.message, token, page)
        await cq.answer()
        return

    # DOWNLOAD REPO ZIP
    if data_cb.startswith("download_repo_zip:"):
        parts = data_cb.split(":")
        if len(parts) != 3:
            await cq.answer("Invalid callback data.", show_alert=True)
            return
        owner, repo_name = parts[1], parts[2]
        active_token_key = udata.get("active_token")
        if not active_token_key:
            await cq.answer("Activate a token first.", show_alert=True)
            return
        token = udata["tokens"].get(active_token_key, {}).get("token")
        await cq.answer("Downloading ZIP...")
        async with aiohttp.ClientSession() as session:
            zip_data = await download_repo_zip(session, owner, repo_name, token)
        if not zip_data:
            await cq.answer("Failed to download ZIP.", show_alert=True)
            return
        try:
            filename = sanitize_filename(f"{repo_name}.zip")
            await cq.message.reply_document(zip_data, file_name=filename)
        except Exception as e:
            await cq.answer(f"Failed to send ZIP: {e}", show_alert=True)
        return

    # SEARCH USER PROMPT
    if data_cb == "search_user_prompt":
        user_states[uid] = {"action": "search_user"}
        await cq.message.edit(
            "🔍 Send the GitHub username to search for.\nSend 'Back' to cancel.",
            reply_markup=make_back_button(),
        )
        await cq.answer()
        return

    # SEARCH REPO PROMPT
    if data_cb == "search_repo_prompt":
        user_states[uid] = {"action": "search_repo"}
        await cq.message.edit(
            "🔎 Send the keyword to search public repos.\nSend 'Back' to cancel.",
            reply_markup=make_back_button(),
        )
        await cq.answer()
        return

    # GIST CREATE PROMPT
    if data_cb == "gist_create_prompt":
        user_states[uid] = {"action": "gist_create"}
        await cq.message.edit(
            "🧾 Send the gist content (text). Title your gist's file with 'filename: your_filename.ext' on the first line.\nSend 'Back' to cancel.",
            reply_markup=make_back_button(),
        )
        await cq.answer()
        return

    # STAR REPO PROMPT
    if data_cb == "star_repo_prompt":
        user_states[uid] = {"action": "star_repo"}
        await cq.message.edit(
            "⭐ Send the full repo name to star (owner/repo), e.g. 'torvalds/linux'.\nSend 'Back' to cancel.",
            reply_markup=make_back_button(),
        )
        await cq.answer()
        return

    # TRENDING REPOS
    if data_cb == "trending":
        await cq.answer("Fetching trending repos...")
        await send_trending_repos(cq.message)
        return

    # RANDOM REPO
    if data_cb == "random_repo":
        await cq.answer("Fetching a random popular repo...")
        await send_random_repo(cq.message)
        return

    # GH STATS
    if data_cb == "gh_stats":
        active_token_key = udata.get("active_token")
        if not active_token_key:
            await cq.answer("Activate a token first.", show_alert=True)
            return
        token = udata["tokens"].get(active_token_key, {}).get("token")
        await send_gh_stats(cq.message, token)
        await cq.answer()
        return

    # UPLOAD ZIP TO REPO (stub: To implement: accept ZIP file upload and push to repo)
    if data_cb == "upload_zip":
        await cq.message.edit("📤 Send ZIP file to upload to your repo.\nFeature coming soon.")
        await cq.answer()
        return

    # ADMIN PANEL
    if uid in ADMINS and data_cb.startswith("admin_"):
        await admin_callbacks(_, cq)
        return

    await cq.answer("Unknown or unimplemented command.", show_alert=True)

# --- USER STATE MESSAGE HANDLER ---
# NOTE: replaced filters.create(...) with filters.private & filters.text and checking membership inside handler
@app.on_message(filters.private & filters.text)
async def user_states_handler(_, msg: Message):
    # guard
    if not msg.from_user:
        return
    uid = msg.from_user.id
    # only handle messages when user has a state
    if uid not in user_states:
        return

    text = msg.text or ""
    if text.lower() == "back":
        user_states.pop(uid, None)
        await start_cmd(_, msg)
        return

    state = user_states.get(uid)
    if not state:
        return

    action = state.get("action")

    try:
        if action == "token_add":
            token = text.strip()
            if len(token) < 10:
                await msg.reply("❌ Token too short or invalid. Try again or send 'Back' to cancel.")
                return
            # Validate token by fetching user info
            async with aiohttp.ClientSession() as session:
                user_info, status = await fetch_json(session, "https://api.github.com/user", token=token)
            if status != 200:
                await msg.reply(f"❌ Invalid token or API error: {user_info.get('message','Unknown error')}")
                return
            username = user_info.get("login") or "unknown"
            udata = user_data(uid)
            token_key = f"token{len(udata['tokens']) + 1}"
            udata["tokens"][token_key] = {"token": token, "username": username}
            udata["active_token"] = token_key
            save_data()
            user_states.pop(uid, None)
            await msg.reply(f"✅ Token added and activated as '{token_key}' (user: {username}).", reply_markup=make_back_button())

        elif action == "search_user":
            username = text.strip()
            async with aiohttp.ClientSession() as session:
                user_info, status = await fetch_json(session, f"https://api.github.com/users/{username}")
                if status != 200:
                    await msg.reply(f"❌ User not found or error: {user_info.get('message', 'Unknown error')}", reply_markup=make_back_button())
                    user_states.pop(uid, None)
                    return
                repos, rstatus = await fetch_json(session, f"https://api.github.com/users/{username}/repos?per_page={ITEMS_PER_PAGE}")
                if rstatus != 200 or not repos:
                    await msg.reply(f"ℹ️ No public repos found for {username}.", reply_markup=make_back_button())
                    user_states.pop(uid, None)
                    return
            kb = []
            for repo in repos:
                kb.append([InlineKeyboardButton(repo["name"], callback_data=f"download_repo_zip:{username}:{repo['name']}")])
            kb.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
            await msg.reply(format_user(user_info) + "\n\nSelect a repo to download ZIP:", reply_markup=InlineKeyboardMarkup(kb))
            user_states.pop(uid, None)

        elif action == "search_repo":
            keyword = text.strip()
            async with aiohttp.ClientSession() as session:
                search_result, status = await fetch_json(session, f"https://api.github.com/search/repositories?q={keyword}&per_page={ITEMS_PER_PAGE}")
            if status != 200 or not search_result.get("items"):
                await msg.reply("❌ No repositories found for that keyword.", reply_markup=make_back_button())
                user_states.pop(uid, None)
                return
            kb = []
            for repo in search_result["items"]:
                owner = repo["owner"]["login"]
                name = repo["name"]
                kb.append([InlineKeyboardButton(f"{owner}/{name}", callback_data=f"download_repo_zip:{owner}:{name}")])
            kb.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
            await msg.reply(f"Repositories matching '{keyword}':", reply_markup=InlineKeyboardMarkup(kb))
            user_states.pop(uid, None)

        elif action == "gist_create":
            # Expecting first line: filename: yourfilename.ext
            lines = text.split("\n")
            if not lines or not lines[0].lower().startswith("filename:"):
                await msg.reply("❌ Please start your gist text with 'filename: your_filename.ext' on the first line.")
                return
            filename = lines[0][9:].strip()
            content = "\n".join(lines[1:]).strip()
            if not filename or not content:
                await msg.reply("❌ Filename or content missing. Try again or send 'Back'.")
                return
            udata = user_data(uid)
            active_token_key = udata.get("active_token")
            if not active_token_key:
                await msg.reply("Activate a token first.", reply_markup=make_back_button())
                user_states.pop(uid, None)
                return
            token = udata["tokens"][active_token_key]["token"]
            gist_data = {
                "description": f"Gist created via Spilux bot by {uid}",
                "public": True,
                "files": {filename: {"content": content}},
            }
            async with aiohttp.ClientSession() as session:
                resp, status = await fetch_json(session, "https://api.github.com/gists", token=token, method="POST", data=gist_data)
            if status == 201:
                url = resp.get("html_url", "unknown")
                await msg.reply(
