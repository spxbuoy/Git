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
    return str(uid) in data.get("banned", []) or user_data(uid).get("banned", False)

def make_back_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Back", callback_data="back")]]
    )

def build_keyboard(buttons_list):
    return InlineKeyboardMarkup(buttons_list)

def escape_md(text: str) -> str:
    # Basic MarkdownV2 escape
    escapes = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in escapes else c for c in text)

def format_repo(repo):
    # Format repo for messages
    desc = repo.get("description") or ""
    desc = desc[:60] + "…" if len(desc) > 60 else desc
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    return f"**{escape_md(repo['full_name'])}**\n⭐ {stars} | 🍴 {forks}\n_{escape_md(desc)}_"

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
                return await resp.json(), resp.status
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

@app.middleware()
async def ban_check(_, __, msg: Message, proceed):
    if msg.from_user and is_banned(msg.from_user.id):
        try:
            await msg.reply("🚫 You are banned from using this bot.")
        except:
            pass
        return
    await proceed()

# --- START COMMAND ---

@app.on_message(filters.private & filters.command("start"))
async def start_cmd(_, msg: Message):
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

@app.on_message(filters.private & filters.create(lambda _, __, msg: msg.from_user.id in user_states))
async def user_states_handler(_, msg: Message):
    uid = msg.from_user.id
    text = msg.text or ""
    if text.lower() == "back":
        user_states.pop(uid)
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
            user_states.pop(uid)
            await msg.reply(f"✅ Token added and activated as '{token_key}' (user: {username}).", reply_markup=make_back_button())

        elif action == "search_user":
            username = text.strip()
            async with aiohttp.ClientSession() as session:
                user_info, status = await fetch_json(session, f"https://api.github.com/users/{username}")
                if status != 200:
                    await msg.reply(f"❌ User not found or error: {user_info.get('message', 'Unknown error')}", reply_markup=make_back_button())
                    user_states.pop(uid)
                    return
                repos, rstatus = await fetch_json(session, f"https://api.github.com/users/{username}/repos?per_page={ITEMS_PER_PAGE}")
                if rstatus != 200 or not repos:
                    await msg.reply(f"ℹ️ No public repos found for {username}.", reply_markup=make_back_button())
                    user_states.pop(uid)
                    return
            kb = []
            for repo in repos:
                kb.append([InlineKeyboardButton(repo["name"], callback_data=f"download_repo_zip:{username}:{repo['name']}")])
            kb.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
            await msg.reply(format_user(user_info) + "\n\nSelect a repo to download ZIP:", reply_markup=InlineKeyboardMarkup(kb))
            user_states.pop(uid)

        elif action == "search_repo":
            keyword = text.strip()
            async with aiohttp.ClientSession() as session:
                search_result, status = await fetch_json(session, f"https://api.github.com/search/repositories?q={keyword}&per_page={ITEMS_PER_PAGE}")
            if status != 200 or not search_result.get("items"):
                await msg.reply("❌ No repositories found for that keyword.", reply_markup=make_back_button())
                user_states.pop(uid)
                return
            kb = []
            for repo in search_result["items"]:
                owner = repo["owner"]["login"]
                name = repo["name"]
                kb.append([InlineKeyboardButton(f"{owner}/{name}", callback_data=f"download_repo_zip:{owner}:{name}")])
            kb.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
            await msg.reply(f"Repositories matching '{keyword}':", reply_markup=InlineKeyboardMarkup(kb))
            user_states.pop(uid)

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
                user_states.pop(uid)
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
                await msg.reply(f"✅ Gist created: [Open gist]({url})", parse_mode=ParseMode.MARKDOWN)
            else:
                await msg.reply(f"❌ Failed to create gist: {resp.get('message', 'Unknown error')}")
            user_states.pop(uid)

        elif action == "star_repo":
            repo_full = text.strip()
            if "/" not in repo_full:
                await msg.reply("❌ Repo must be in owner/repo format.")
                return
            owner, repo = repo_full.split("/", 1)
            udata = user_data(uid)
            active_token_key = udata.get("active_token")
            if not active_token_key:
                await msg.reply("Activate a token first.", reply_markup=make_back_button())
                user_states.pop(uid)
                return
            token = udata["tokens"][active_token_key]["token"]
            url = f"https://api.github.com/user/starred/{owner}/{repo}"
            async with aiohttp.ClientSession() as session:
                resp, status = await fetch_json(session, url, token=token, method="PUT")
            if status in (204, 304):
                await msg.reply(f"⭐ Starred {owner}/{repo} successfully!")
            else:
                await msg.reply(f"❌ Failed to star repo: {resp.get('message', 'Unknown error')}")
            user_states.pop(uid)

        else:
            await msg.reply("❌ Unknown action or state. Send /start to restart.", reply_markup=make_back_button())
            user_states.pop(uid)

    except Exception as e:
        await msg.reply(f"❌ Error: {e}")
        user_states.pop(uid)

# --- SHOW REPOS PAGE (for myrepos_page:X) ---

async def show_repos_page(message: Message, token: str, page: int):
    async with aiohttp.ClientSession() as session:
        repos, status = await fetch_json(session, f"https://api.github.com/user/repos?per_page={ITEMS_PER_PAGE}&page={page+1}", token=token)
    if status != 200 or not repos:
        await message.edit("❌ Failed to fetch repositories or none available.", reply_markup=make_back_button())
        return
    buttons = []
    for repo in repos:
        buttons.append(
            [InlineKeyboardButton(repo["name"], callback_data=f"download_repo_zip:{repo['owner']['login']}:{repo['name']}")]
        )
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"myrepos_page:{page-1}"))
    nav_buttons.append(InlineKeyboardButton("↩️ Back", callback_data="back"))
    if len(repos) == ITEMS_PER_PAGE:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"myrepos_page:{page+1}"))
    buttons.append(nav_buttons)
    await message.edit("📂 Your repositories (click to download ZIP):", reply_markup=InlineKeyboardMarkup(buttons))

# --- TRENDING REPOS (using GitHub trending API proxy) ---

async def send_trending_repos(message: Message):
    url = "https://ghapi.huchen.dev/repositories?since=daily"
    async with aiohttp.ClientSession() as session:
        try:
            resp = await session.get(url)
            repos = await resp.json()
        except:
            repos = []
    if not repos:
        await message.reply("❌ Failed to fetch trending repos.")
        return
    buttons = []
    for repo in repos[:ITEMS_PER_PAGE]:
        name = repo.get("name")
        owner = repo.get("author")
        if name and owner:
            buttons.append([InlineKeyboardButton(f"{owner}/{name}", callback_data=f"download_repo_zip:{owner}:{name}")])
    buttons.append([InlineKeyboardButton("↩️ Back", callback_data="back")])
    await message.reply("🔥 Trending Repositories:", reply_markup=InlineKeyboardMarkup(buttons))

# --- RANDOM REPO (from trending or popular list) ---

import random

async def send_random_repo(message: Message):
    url = "https://api.github.com/search/repositories?q=stars:>10000&sort=stars&order=desc&per_page=100"
    async with aiohttp.ClientSession() as session:
        resp_json, status = await fetch_json(session, url)
    if status != 200 or not resp_json.get("items"):
        await message.reply("❌ Failed to fetch popular repos.")
        return
    repo = random.choice(resp_json["items"])
    text = format_repo(repo)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Download ZIP", callback_data=f"download_repo_zip:{repo['owner']['login']}:{repo['name']}"),
          InlineKeyboardButton("↩️ Back", callback_data="back")]]
    )
    await message.reply(text, reply_markup=kb)

# --- GITHUB API STATS ---

async def send_gh_stats(message: Message, token: str):
    async with aiohttp.ClientSession() as session:
        user_info, status = await fetch_json(session, "https://api.github.com/user", token=token)
    if status != 200:
        await message.reply(f"❌ Failed to fetch stats: {user_info.get('message', 'Unknown error')}")
        return
    text = (
        f"👤 **{escape_md(user_info.get('login','Unknown'))}**'s GitHub Stats:\n"
        f"Public Repos: {user_info.get('public_repos', 0)}\n"
        f"Followers: {user_info.get('followers', 0)}\n"
        f"Following: {user_info.get('following', 0)}\n"
        f"Plan: {user_info.get('plan', {}).get('name', 'N/A')}"
    )
    await message.reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=make_back_button())

# --- ADMIN PANEL CALLBACKS ---

async def admin_callbacks(_, cq: CallbackQuery):
    uid = cq.from_user.id
    data_cb = cq.data

    if data_cb == "admin_panel":
        buttons = [
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list_users")],
            [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user_prompt")],
            [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user_prompt")],
            [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast_prompt")],
            [InlineKeyboardButton("📊 Bot Stats", callback_data="admin_bot_stats")],
            [InlineKeyboardButton("↩️ Back to Main Menu", callback_data="back")],
        ]
        await cq.message.edit("👥 Admin Panel:", reply_markup=InlineKeyboardMarkup(buttons))
        await cq.answer()
        return

    # List users
    if data_cb == "admin_list_users":
        users_list = list(data["users"].items())
        if not users_list:
            await cq.message.edit("No users found.", reply_markup=make_back_button())
            await cq.answer()
            return
        lines = []
        for uid_str, udata in users_list[:50]:  # limit output
            lines.append(f"- `{uid_str}`: {udata.get('first_name', 'Unknown')}, Tokens: {len(udata.get('tokens', {}))}, Banned: {udata.get('banned', False)}")
        text = "👥 Registered Users (max 50):\n" + "\n".join(lines)
        await cq.message.edit(text, reply_markup=make_back_button())
        await cq.answer()
        return

    # Ban user prompt
    if data_cb == "admin_ban_user_prompt":
        user_states[uid] = {"action": "admin_ban_user"}
        await cq.message.edit("🚫 Send the Telegram user ID to ban.\nSend 'Back' to cancel.", reply_markup=make_back_button())
        await cq.answer()
        return

    # Unban user prompt
    if data_cb == "admin_unban_user_prompt":
        user_states[uid] = {"action": "admin_unban_user"}
        await cq.message.edit("✅ Send the Telegram user ID to unban.\nSend 'Back' to cancel.", reply_markup=make_back_button())
        await cq.answer()
        return

    # Broadcast prompt
    if data_cb == "admin_broadcast_prompt":
        user_states[uid] = {"action": "admin_broadcast"}
        await cq.message.edit("📢 Send the message to broadcast to all users.\nSend 'Back' to cancel.", reply_markup=make_back_button())
        await cq.answer()
        return

    # Bot stats
    if data_cb == "admin_bot_stats":
        total_users = len(data["users"])
        banned_users = len([u for u in data["users"].values() if u.get("banned")]) + len(data.get("banned", []))
        text = f"🤖 Bot Stats:\nTotal users: {total_users}\nBanned users: {banned_users}"
        await cq.message.edit(text, reply_markup=make_back_button())
        await cq.answer()
        return

# --- ADMIN STATE HANDLER ---

@app.on_message(filters.private & filters.create(lambda _, __, msg: msg.from_user.id in user_states))
async def admin_states_handler(_, msg: Message):
    uid = msg.from_user.id
    if uid not in ADMINS:
        return
    text = msg.text or ""
    if text.lower() == "back":
        user_states.pop(uid)
        await start_cmd(_, msg)
        return

    state = user_states.get(uid)
    if not state:
        return

    action = state.get("action")
    try:
        if action == "admin_ban_user":
            try:
                ban_uid = int(text.strip())
                if ban_uid == uid:
                    await msg.reply("❌ You cannot ban yourself!")
                    return
                user_d = user_data(ban_uid)
                user_d["banned"] = True
                if str(ban_uid) not in data.get("banned", []):
                    data["banned"].append(str(ban_uid))
                save_data()
                await msg.reply(f"🚫 User {ban_uid} banned successfully.", reply_markup=make_back_button())
                user_states.pop(uid)
            except Exception:
                await msg.reply("❌ Invalid user ID. Try again or send 'Back'.")
            return

        if action == "admin_unban_user":
            try:
                unban_uid = int(text.strip())
                user_d = user_data(unban_uid)
                user_d["banned"] = False
                if str(unban_uid) in data.get("banned", []):
                    data["banned"].remove(str(unban_uid))
                save_data()
                await msg.reply(f"✅ User {unban_uid} unbanned successfully.", reply_markup=make_back_button())
                user_states.pop(uid)
            except Exception:
                await msg.reply("❌ Invalid user ID. Try again or send 'Back'.")
            return

        if action == "admin_broadcast":
            msg_text = text.strip()
            if not msg_text:
                await msg.reply("❌ Message cannot be empty. Try again or send 'Back'.")
                return
            count = 0
            failed = 0
            for user_id_str in data["users"].keys():
                try:
                    user_id = int(user_id_str)
                    await app.send_message(user_id, f"📢 Broadcast from admin:\n\n{msg_text}")
                    count += 1
                    await asyncio.sleep(0.1)  # avoid flood limits
                except:
                    failed += 1
            await msg.reply(f"✅ Broadcast sent to {count} users.\nFailed: {failed}", reply_markup=make_back_button())
            user_states.pop(uid)
            return

    except Exception as e:
        await msg.reply(f"❌ Error: {e}")
        user_states.pop(uid)

# --- STARTUP ---

if __name__ == "__main__":
    print("Loading data...")
    load_data()
    print("Starting bot...")
    app.run()
