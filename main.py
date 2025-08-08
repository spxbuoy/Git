import os
import re
import json
import aiohttp
import asyncio
import tempfile
import zipfile
import base64
import datetime
import logging
from typing import Dict, Any, Optional, List, Tuple
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
)

API_ID = int(os.getenv("API_ID", "20660797"))
API_HASH = os.getenv("API_HASH", "755e5cdf9bade62a75211e7a57f25601")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8448084086:AAGT6-0n3g3OkQDYxxlyXdJESsYmjX9_ISA")
ADMINS = [int(x) for x in os.getenv("ADMINS", "7941175119").split(",") if x.strip()]
DATA_FILE = "data.json"
ITEMS_PER_PAGE = 6

# -------- Data persistence with async lock --------
class DataStore:
    def __init__(self, filename: str):
        self.filename = filename
        self.data: Dict[str, Any] = {"tokens": {}, "banned": [], "usage": {}, "users": {}}
        self.lock = asyncio.Lock()

    async def load(self):
        async with self.lock:
            if os.path.exists(self.filename):
                with open(self.filename, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            else:
                self.data = {"tokens": {}, "banned": [], "usage": {}, "users": {}}

    async def save(self):
        async with self.lock:
            with open(self.filename, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)

    # User token management
    async def add_token(self, user_id: int, token: str, username: str):
        async with self.lock:
            user_data = self.data["tokens"].setdefault(str(user_id), {"tokens": {}, "active": None, "first_name": ""})
            user_data["tokens"][token] = {"username": username}
            user_data["active"] = token
            await self.save()

    async def remove_token(self, user_id: int, token: str):
        async with self.lock:
            user_data = self.data["tokens"].get(str(user_id), None)
            if not user_data or token not in user_data["tokens"]:
                return False
            del user_data["tokens"][token]
            if user_data["active"] == token:
                user_data["active"] = next(iter(user_data["tokens"]), None)
            await self.save()
            return True

    async def switch_token(self, user_id: int, token: str):
        async with self.lock:
            user_data = self.data["tokens"].get(str(user_id), None)
            if not user_data or token not in user_data["tokens"]:
                return False
            user_data["active"] = token
            await self.save()
            return True

    async def get_active_token(self, user_id: int) -> Optional[str]:
        async with self.lock:
            user_data = self.data["tokens"].get(str(user_id), {})
            return user_data.get("active", None)

    async def get_tokens(self, user_id: int) -> Dict[str, Dict[str, str]]:
        async with self.lock:
            user_data = self.data["tokens"].get(str(user_id), {})
            return user_data.get("tokens", {})

    # Ban management
    async def ban_user(self, user_id: int):
        async with self.lock:
            if user_id not in self.data["banned"]:
                self.data["banned"].append(user_id)
                await self.save()

    async def unban_user(self, user_id: int):
        async with self.lock:
            if user_id in self.data["banned"]:
                self.data["banned"].remove(user_id)
                await self.save()

    async def is_banned(self, user_id: int) -> bool:
        async with self.lock:
            return user_id in self.data["banned"]

    # Users registry for admin
    async def register_user(self, user_id: int, first_name: str, username: Optional[str]):
        async with self.lock:
            self.data["users"][str(user_id)] = {
                "first_name": first_name,
                "username": username or "",
                "last_seen": datetime.datetime.utcnow().isoformat(),
            }
            await self.save()

    async def get_users(self) -> Dict[str, Any]:
        async with self.lock:
            return self.data.get("users", {})

data_store = DataStore(DATA_FILE)

# -------- Utility functions --------

async def gh_request(session: aiohttp.ClientSession, method: str, url: str, token: Optional[str] = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"token {token}"
    headers["Accept"] = "application/vnd.github.v3+json"
    headers["User-Agent"] = "Spilux-GitHub-Bot"
    async with session.request(method, url, headers=headers, **kwargs) as resp:
        try:
            body = await resp.json()
        except:
            body = await resp.text()
        return resp.status, body

async def validate_github_token(token: str) -> Optional[str]:
    """Return GitHub username if valid token, else None"""
    async with aiohttp.ClientSession() as session:
        status, data = await gh_request(session, "GET", "https://api.github.com/user", token=token)
        if status == 200 and "login" in data:
            return data["login"]
    return None

async def download_repo_zip(token: Optional[str], full_repo_name: str) -> Optional[bytes]:
    """Download the zipball archive for a repo"""
    url = f"https://api.github.com/repos/{full_repo_name}/zipball"
    async with aiohttp.ClientSession() as session:
        status, resp = await gh_request(session, "GET", url, token=token)
        if status == 200:
            # The API responds with a redirect to actual zip URL
            if isinstance(resp, dict) and 'message' in resp:
                return None
            # If resp is bytes content or a redirect, try to get content manually
            # For API redirects, we have to follow manually:
            # We'll do a second request to the Location header if needed
            # But aiohttp auto follows redirects by default.
            # So, here, resp is json. So better to request with allow_redirects=True:
            pass
    # We'll do manual direct request below:
    async with aiohttp.ClientSession() as session:
        headers = {}
        if token:
            headers["Authorization"] = f"token {token}"
        async with session.get(url, headers=headers) as r:
            if r.status == 302 or r.status == 301:
                zip_url = r.headers.get("Location")
                if zip_url:
                    async with session.get(zip_url) as rr:
                        if rr.status == 200:
                            return await rr.read()
            elif r.status == 200:
                return await r.read()
    return None

def paginate_items(items: List[Any], page: int, per_page: int) -> Tuple[List[Any], int]:
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    return items[start:end], total_pages

def build_back_button(callback_data="start_menu"):
    return [InlineKeyboardButton("🔙 Back", callback_data=callback_data)]

def is_valid_github_username(name: str) -> bool:
    return re.fullmatch(r"[a-zA-Z0-9\-]{1,39}", name) is not None

# -------- Bot Initialization --------
app = Client("github_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -------- Filters --------
async def not_banned_filter_func(_, __, message: Message):
    if message.from_user is None:
        return False
    return not await data_store.is_banned(message.from_user.id)

not_banned_filter = filters.create(not_banned_filter_func)

# -------- States --------
user_states: Dict[int, Dict[str, Any]] = {}

# -------- Start command --------
@app.on_message(filters.private & filters.command("start") & not_banned_filter)
async def start_menu(_, message: Message):
    uid = message.from_user.id
    await data_store.register_user(uid, message.from_user.first_name or "", message.from_user.username)
    data = await data_store.get_tokens(uid)
    active_token = await data_store.get_active_token(uid)
    kb = [
        [InlineKeyboardButton("🔑 Add Token", callback_data="token_add")],
        [InlineKeyboardButton("🔁 Switch Token", callback_data="token_switch_list")],
        [InlineKeyboardButton("🗑 Remove Token", callback_data="token_remove_list")],
        [InlineKeyboardButton("📂 My Repos", callback_data="myrepos_page:0")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data="upload_zip_start")],
        [InlineKeyboardButton("🔍 Search GitHub User", callback_data="search_user_start")],
        [InlineKeyboardButton("🔎 Search Repos (keyword)", callback_data="search_repo_start")],
        [InlineKeyboardButton("📈 Trending Repos", callback_data="trending_start")],
        [InlineKeyboardButton("🎲 Random Repo", callback_data="random_start")],
        [InlineKeyboardButton("🧾 Create Gist", callback_data="gist_create_start")],
        [InlineKeyboardButton("⭐ Star a Repo", callback_data="star_repo_prompt")],
        [InlineKeyboardButton("📊 GitHub API Stats", callback_data="gh_stats")],
    ]
    if uid in ADMINS:
        kb.append([InlineKeyboardButton("👥 Admin Panel", callback_data="admin_panel")])

    await message.reply(
        "👋 Welcome to Spilux GitHub Bot! Select an option below:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# -------- Callback Query Handler --------
@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
    uid = cq.from_user.id
    data = await data_store.get_tokens(uid)
    active_token = await data_store.get_active_token(uid)

    # Back buttons handler shortcut
    if cq.data == "start_menu":
        await start_menu(_, cq.message)
        await cq.answer()
        return

    if cq.data == "token_add":
        user_states[uid] = {"action": "awaiting_token"}
        await cq.message.edit("🔑 Please send your GitHub Personal Access Token (PAT).", reply_markup=InlineKeyboardMarkup([build_back_button()]))
        await cq.answer()
        return

    if cq.data == "token_switch_list":
        tokens = await data_store.get_tokens(uid)
        if not tokens:
            await cq.answer("You have no saved tokens.", show_alert=True)
            return
        kb = []
        for token, meta in tokens.items():
            uname = meta.get("username", "unknown")
            active_mark = "✅" if active_token == token else ""
            kb.append([InlineKeyboardButton(f"{uname} {active_mark}", callback_data=f"token_switch:{token}")])
        kb.append(build_back_button())
        await cq.message.edit("🔁 Select a token to activate:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    if cq.data and cq.data.startswith("token_switch:"):
        token_to_switch = cq.data.split(":", 1)[1]
        ok = await data_store.switch_token(uid, token_to_switch)
        if ok:
            await cq.message.edit("✅ Token switched successfully.", reply_markup=InlineKeyboardMarkup([build_back_button()]))
        else:
            await cq.answer("Failed to switch token.", show_alert=True)
        return

    if cq.data == "token_remove_list":
        tokens = await data_store.get_tokens(uid)
        if not tokens:
            await cq.answer("You have no tokens to remove.", show_alert=True)
            return
        kb = []
        for token, meta in tokens.items():
            uname = meta.get("username", "unknown")
            kb.append([InlineKeyboardButton(f"{uname}", callback_data=f"token_remove:{token}")])
        kb.append(build_back_button())
        await cq.message.edit("🗑 Select a token to remove:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    if cq.data and cq.data.startswith("token_remove:"):
        token_to_remove = cq.data.split(":", 1)[1]
        ok = await data_store.remove_token(uid, token_to_remove)
        if ok:
            await cq.message.edit("✅ Token removed.", reply_markup=InlineKeyboardMarkup([build_back_button()]))
        else:
            await cq.answer("Failed to remove token.", show_alert=True)
        return

    # List user repos with pagination
    if cq.data and cq.data.startswith("myrepos_page:"):
        page = int(cq.data.split(":")[1])
        await show_user_repos(_, cq.message, uid, active_token, page)
        return

    # Upload zip start
    if cq.data == "upload_zip_start":
        user_states[uid] = {"action": "awaiting_zip", "repo": None}
        kb = InlineKeyboardMarkup([build_back_button()])
        await cq.message.edit("📤 Please send a ZIP file to upload to one of your repos. Use /cancel or 🔙 Back if you want to abort.", reply_markup=kb)
        await cq.answer()
        return

    # Search GitHub user start
    if cq.data == "search_user_start":
        user_states[uid] = {"action": "search_user"}
        kb = InlineKeyboardMarkup([build_back_button()])
        await cq.message.edit("🔍 Send a GitHub username or profile URL.", reply_markup=kb)
        await cq.answer()
        return

    # Search repo keyword start
    if cq.data == "search_repo_start":
        user_states[uid] = {"action": "search_repo"}
        kb = InlineKeyboardMarkup([build_back_button()])
        await cq.message.edit("🔎 Send a keyword to search public repos.", reply_markup=kb)
        await cq.answer()
        return

    # Trending repos start
    if cq.data == "trending_start":
        await show_trending_repos(cq)
        return

    # Random repo start
    if cq.data == "random_start":
        await show_random_repo(cq)
        return

    # Create gist start
    if cq.data == "gist_create_start":
        user_states[uid] = {"action": "awaiting_gist_content", "public": True}
        kb = InlineKeyboardMarkup([build_back_button()])
        await cq.message.edit("🧾 Send gist content (plain text or JSON for multiple files).", reply_markup=kb)
        await cq.answer()
        return

    # Star repo prompt
    if cq.data == "star_repo_prompt":
        user_states[uid] = {"action": "awaiting_star_repo"}
        kb = InlineKeyboardMarkup([build_back_button()])
        await cq.message.edit("⭐ Send full repo name (owner/repo) to star/unstar.", reply_markup=kb)
        await cq.answer()
        return

    # GitHub API stats
    if cq.data == "gh_stats":
        if not active_token:
            await cq.answer("No active token found. Add one first.", show_alert=True)
            return
        await show_gh_stats(cq, active_token)
        return

    # Admin panel access
    if cq.data == "admin_panel":
        if uid not in ADMINS:
            await cq.answer("Unauthorized.", show_alert=True)
            return
        await show_admin_panel(cq)
        return

    # Admin panel commands
    if cq.data and cq.data.startswith("admin_list_users"):
        await admin_list_users(cq)
        return

    if cq.data and cq.data.startswith("admin_ban_user:"):
        target_id = int(cq.data.split(":")[1])
        await data_store.ban_user(target_id)
        await cq.answer("User banned.")
        await show_admin_panel(cq)
        return

    if cq.data and cq.data.startswith("admin_unban_user:"):
        target_id = int(cq.data.split(":")[1])
        await data_store.unban_user(target_id)
        await cq.answer("User unbanned.")
        await show_admin_panel(cq)
        return

    if cq.data and cq.data.startswith("admin_edit_tokens:"):
        target_id = int(cq.data.split(":")[1])
        await admin_edit_user_tokens(cq, target_id)
        return

    # Token-related advanced commands inside admin_edit_tokens (remove/switch token, etc)
    if cq.data and cq.data.startswith("admin_remove_token:"):
        parts = cq.data.split(":")
        target_id = int(parts[1])
        token = parts[2]
        removed = await data_store.remove_token(target_id, token)
        await cq.answer("Token removed." if removed else "Failed to remove token.")
        await admin_edit_user_tokens(cq, target_id)
        return

    if cq.data and cq.data.startswith("admin_switch_token:"):
        parts = cq.data.split(":")
        target_id = int(parts[1])
        token = parts[2]
        switched = await data_store.switch_token(target_id, token)
        await cq.answer("Token switched." if switched else "Failed to switch token.")
        await admin_edit_user_tokens(cq, target_id)
        return

    # TODO: Handle more callbacks (repo buttons, search results, gist buttons, etc) below...
    # e.g. when user clicks repo button to download ZIP, star repo, fork repo, etc.

    # Examples:
    # myrepo:full_name
    # searchrepo:full_name
    # starrepo:full_name
    # forkrepo:full_name
    # gist_view:id

    if cq.data and cq.data.startswith("myrepo_download:"):
        full_name = cq.data.split(":", 1)[1]
        await send_repo_zip(cq.message, uid, active_token, full_name)
        await cq.answer()
        return

    if cq.data and cq.data.startswith("searchuser_repos:"):
        username = cq.data.split(":", 1)[1]
        await show_user_repos_search(cq, username, active_token)
        await cq.answer()
        return

    if cq.data and cq.data.startswith("searchrepo_download:"):
        full_name = cq.data.split(":", 1)[1]
        await send_repo_zip(cq.message, uid, active_token, full_name)
        await cq.answer()
        return

    if cq.data and cq.data.startswith("starrepo_toggle:"):
        full_name = cq.data.split(":", 1)[1]
        await toggle_star_repo(cq.message, uid, active_token, full_name)
        await cq.answer()
        return

    if cq.data and cq.data.startswith("forkrepo:"):
        full_name = cq.data.split(":", 1)[1]
        await fork_repo(cq.message, uid, active_token, full_name)
        await cq.answer()
        return

    # Unknown callback fallback
    await cq.answer("Unknown action.", show_alert=True)

# -------- Message Handler for states --------
@app.on_message(filters.private & not_banned_filter)
async def message_handler(_, message: Message):
    uid = message.from_user.id
    state = user_states.get(uid, {})
    action = state.get("action")

    if action == "awaiting_token":
        token = message.text.strip()
        username = await validate_github_token(token)
        if username:
            await data_store.add_token(uid, token, username)
            await message.reply(f"✅ Token validated and added for GitHub user `{username}`.", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
            user_states.pop(uid, None)
        else:
            await message.reply("❌ Invalid token. Please send a valid GitHub Personal Access Token.", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
        return

    if action == "awaiting_zip":
        if not message.document or not message.document.file_name.endswith(".zip"):
            await message.reply("❌ Please send a valid ZIP file.", quote=True, reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        # Save zip to temp and ask repo selection
        file = await message.download()
        user_states[uid]["zip_path"] = file
        user_states[uid]["action"] = "awaiting_zip_repo"
        await ask_user_repos_for_upload(message, uid)
        return

    if action == "awaiting_zip_repo":
        await message.reply("Please use the buttons to select the repo.", quote=True)
        return

    if action == "search_user":
        text = message.text.strip()
        username = extract_github_username(text)
        if not username:
            await message.reply("❌ Invalid GitHub username or URL. Try again or use 🔙 Back.", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        await show_user_repos_search(message, username, await data_store.get_active_token(uid))
        user_states.pop(uid, None)
        return

    if action == "search_repo":
        keyword = message.text.strip()
        if len(keyword) < 3:
            await message.reply("❌ Keyword too short. Try again or use 🔙 Back.", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        await search_public_repos(message, keyword)
        user_states.pop(uid, None)
        return

    if action == "awaiting_gist_content":
        content = message.text or ""
        if not content.strip():
            await message.reply("❌ Empty gist content. Try again or 🔙 Back.", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        await create_gist(message, content, public=True)
        user_states.pop(uid, None)
        return

    if action == "awaiting_star_repo":
        repo_full = message.text.strip()
        if "/" not in repo_full:
            await message.reply("❌ Invalid repo name. Format: owner/repo", quote=True,
                                reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        await toggle_star_repo(message, uid, await data_store.get_active_token(uid), repo_full)
        user_states.pop(uid, None)
        return

    await message.reply("❌ Unknown input or command. Use /start to see options.", quote=True)

# -------- Helper functions --------

def extract_github_username(text: str) -> Optional[str]:
    # Accept full URLs or plain usernames
    if text.startswith("https://github.com/"):
        parts = text.split("/")
        if len(parts) > 3 and is_valid_github_username(parts[3]):
            return parts[3]
        return None
    elif is_valid_github_username(text):
        return text
    return None

async def show_user_repos(client: Client, message: Message, user_id: int, token: Optional[str], page: int):
    if not token:
        await message.edit("❌ You must add and activate a token first.", reply_markup=InlineKeyboardMarkup([build_back_button()]))
        return
    async with aiohttp.ClientSession() as session:
        url = "https://api.github.com/user/repos?per_page=100&page=1"
        status, repos = await gh_request(session, "GET", url, token=token)
        if status != 200:
            await message.edit(f"❌ Failed to fetch your repos: {repos.get('message', repos)}", reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        repo_list = repos if isinstance(repos, list) else []
        repos_page, total_pages = paginate_items(repo_list, page, ITEMS_PER_PAGE)
        kb = []
        for repo in repos_page:
            kb.append([
                InlineKeyboardButton(repo["full_name"], callback_data=f"myrepo_download:{repo['full_name']}")
            ])
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"myrepos_page:{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"myrepos_page:{page+1}"))
        if nav_buttons:
            kb.append(nav_buttons)
        kb.append(build_back_button())
        await message.edit(f"📂 Your GitHub Repositories (Page {page+1}/{total_pages}):", reply_markup=InlineKeyboardMarkup(kb))

async def send_repo_zip(message: Message, user_id: int, token: Optional[str], full_name: str):
    if not token:
        await message.reply("❌ No active token found. Add one first.", quote=True)
        return
    await message.reply(f"⏳ Preparing ZIP for repo `{full_name}`...", quote=True)
    content = await download_repo_zip(token, full_name)
    if content:
        try:
            await message.reply_document(document=content, file_name=f"{full_name.replace('/', '_')}.zip", quote=True)
        except Exception as e:
            logging.error(f"Failed sending ZIP: {e}")
            await message.reply("❌ Failed to send ZIP file.", quote=True)
    else:
        await message.reply("❌ Failed to download ZIP archive.", quote=True)

async def show_user_repos_search(cq_or_msg: Any, username: str, token: Optional[str]):
    # Accept Message or CallbackQuery.message as input
    if isinstance(cq_or_msg, CallbackQuery):
        message = cq_or_msg.message
    else:
        message = cq_or_msg

    async with aiohttp.ClientSession() as session:
        url = f"https://api.github.com/users/{username}/repos?per_page=100&page=1"
        status, repos = await gh_request(session, "GET", url, token=token)
        if status != 200:
            await message.edit(f"❌ Failed to fetch repos for user `{username}`: {repos.get('message', repos)}",
                               reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        repo_list = repos if isinstance(repos, list) else []
        if not repo_list:
            await message.edit(f"ℹ️ User `{username}` has no public repositories.", reply_markup=InlineKeyboardMarkup([build_back_button()]))
            return
        kb = []
        for repo in repo_list:
            kb.append([InlineKeyboardButton(repo["full_name"], callback_data=f"searchrepo_download:{repo['full_name']}")])
        kb.append(build_back_button())
        await message.edit(f"📂 Repos for user `{username}`:", reply_markup=InlineKeyboardMarkup(kb))

async def search_public_repos(message: Message, keyword: str):
    async with aiohttp.ClientSession() as session:
        url = f"https://api.github.com/search/repositories?q={keyword}&per_page=10"
        status, resp = await gh_request(session, "GET", url)
        if status != 200:
            await message.reply(f"❌ Search failed: {resp.get('message', resp)}", quote=True)
            return
        items = resp.get("items", [])
        if not items:
            await message.reply("ℹ️ No repos found for that keyword.", quote=True)
            return
        kb = []
        for repo in items:
            kb.append([InlineKeyboardButton(repo["full_name"], callback_data=f"searchrepo_download:{repo['full_name']}")])
        kb.append(build_back_button())
        await message.reply(f"🔎 Search results for '{keyword}':", reply_markup=InlineKeyboardMarkup(kb))

async def toggle_star_repo(message: Message, user_id: int, token: Optional[str], full_name: str):
    if not token:
        await message.reply("❌ No active token found.", quote=True)
        return
    async with aiohttp.ClientSession() as session:
        url = f"https://api.github.com/user/starred/{full_name}"
        # Check if starred
        status, _ = await gh_request(session, "GET", url, token=token)
        if status == 204:
            # Unstar
            status2, _ = await gh_request(session, "DELETE", url, token=token)
            if status2 == 204:
                await message.reply(f"⭐ Unstarred {full_name}.", quote=True)
            else:
                await message.reply("❌ Failed to unstar.", quote=True)
        elif status == 404:
            # Star
            status2, _ = await gh_request(session, "PUT", url, token=token, headers={"Content-Length": "0"})
            if status2 == 204:
                await message.reply(f"⭐ Starred {full_name}.", quote=True)
            else:
                await message.reply("❌ Failed to star.", quote=True)
        else:
            await message.reply("❌ Error fetching star status.", quote=True)

async def fork_repo(message: Message, user_id: int, token: Optional[str], full_name: str):
    if not token:
        await message.reply("❌ No active token found.", quote=True)
        return
    async with aiohttp.ClientSession() as session:
        url = f"https://api.github.com/repos/{full_name}/forks"
        status, resp = await gh_request(session, "POST", url, token=token)
        if status == 202:
            await message.reply(f"🍴 Forked {full_name} successfully.", quote=True)
        else:
            await message.reply(f"❌ Failed to fork: {resp.get('message', resp)}", quote=True)

async def create_gist(message: Message, content: str, public=True):
    uid = message.from_user.id
    token = await data_store.get_active_token(uid)
    if not token:
        await message.reply("❌ No active token found.", quote=True)
        return
    try:
        # If JSON, parse multiple files
        files = {}
        try:
            js = json.loads(content)
            if isinstance(js, dict):
                for filename, filecontent in js.items():
                    files[filename] = {"content": filecontent}
            else:
                files["gist.txt"] = {"content": content}
        except Exception:
            files["gist.txt"] = {"content": content}

        payload = {
            "description": f"Gist created via Spilux GitHub Bot @ {datetime.datetime.utcnow().isoformat()}",
            "public": public,
            "files": files,
        }
        async with aiohttp.ClientSession() as session:
            status, resp = await gh_request(session, "POST", "https://api.github.com/gists", token=token, json=payload)
            if status == 201:
                gist_url = resp.get("html_url", "")
                await message.reply(f"🧾 Gist created successfully: {gist_url}", quote=True)
            else:
                await message.reply(f"❌ Failed to create gist: {resp.get('message', resp)}", quote=True)
    except Exception as e:
        await message.reply(f"❌ Error creating gist: {e}", quote=True)

async def show_gh_stats(cq: CallbackQuery, token: str):
    async with aiohttp.ClientSession() as session:
        status, data = await gh_request(session, "GET", "https://api.github.com/rate_limit", token=token)
        if status != 200:
            await cq.answer("Failed to get stats.", show_alert=True)
            return
        core = data.get("rate", {})
        search = data.get("search", {})
        text = (
            f"📊 GitHub API Rate Limits:\n\n"
            f"Core: {core.get('remaining', '?')}/{core.get('limit', '?')} (Resets: {datetime.datetime.fromtimestamp(core.get('reset', 0)).strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"Search: {search.get('remaining', '?')}/{search.get('limit', '?')} (Resets: {datetime.datetime.fromtimestamp(search.get('reset', 0)).strftime('%Y-%m-%d %H:%M:%S')})"
        )
        await cq.message.edit(text, reply_markup=InlineKeyboardMarkup([build_back_button()]))

async def show_trending_repos(cq: CallbackQuery):
    # Using GitHub trending unofficial API or scraping (simplified with a static list here)
    trending = [
        "microsoft/vscode",
        "torvalds/linux",
        "tensorflow/tensorflow",
        "facebook/react",
        "twbs/bootstrap",
        "apple/swift",
    ]
    kb = []
    for repo in trending:
        kb.append([InlineKeyboardButton(repo, callback_data=f"searchrepo_download:{repo}")])
    kb.append(build_back_button())
    await cq.message.edit("🔥 Trending GitHub Repos:", reply_markup=InlineKeyboardMarkup(kb))

async def show_random_repo(cq: CallbackQuery):
    # Random popular repo (hardcoded example)
    import random
    popular = [
        "python/cpython",
        "django/django",
        "pallets/flask",
        "psf/requests",
        "numpy/numpy",
        "keras-team/keras",
    ]
    repo = random.choice(popular)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(repo, callback_data=f"searchrepo_download:{repo}")],
        build_back_button(),
    ])
    await cq.message.edit(f"🎲 Random GitHub Repo:\n{repo}", reply_markup=kb)

async def ask_user_repos_for_upload(message: Message, user_id: int):
    token = await data_store.get_active_token(user_id)
    if not token:
        await message.reply("❌ You must add and activate a token first.", quote=True)
        user_states.pop(user_id, None)
        return
    async with aiohttp.ClientSession() as session:
        url = "https://api.github.com/user/repos?per_page=100&page=1"
        status, repos = await gh_request(session, "GET", url, token=token)
        if status != 200:
            await message.reply(f"❌ Failed to fetch your repos: {repos.get('message', repos)}", quote=True)
            user_states.pop(user_id, None)
            return
        repo_list = repos if isinstance(repos, list) else []
        if not repo_list:
            await message.reply("ℹ️ You have no repositories.", quote=True)
            user_states.pop(user_id, None)
            return
        kb = []
        for repo in repo_list:
            kb.append([InlineKeyboardButton(repo["full_name"], callback_data=f"upload_zip_select:{repo['full_name']}")])
        kb.append(build_back_button())
        await message.reply("📂 Select a repo to upload the ZIP file:", reply_markup=InlineKeyboardMarkup(kb))

@app.on_callback_query(filters.regex(r"upload_zip_select:(.+)"))
async def upload_zip_select_handler(_, cq: CallbackQuery):
    uid = cq.from_user.id
    repo_name = cq.data.split(":", 1)[1]
    state = user_states.get(uid)
    if not state or state.get("action") != "awaiting_zip" or "zip_path" not in state:
        await cq.answer("No ZIP file to upload. Please send a ZIP first.", show_alert=True)
        return
    zip_path = state["zip_path"]
    token = await data_store.get_active_token(uid)
    if not token:
        await cq.answer("No active token found.", show_alert=True)
        return
    await cq.message.edit(f"📤 Uploading ZIP to repo `{repo_name}`...")
    success, msg = await upload_zip_to_repo(token, repo_name, zip_path)
    os.remove(zip_path)
    user_states.pop(uid, None)
    await cq.message.edit(msg, reply_markup=InlineKeyboardMarkup([build_back_button()]))

async def upload_zip_to_repo(token: str, full_repo_name: str, zip_path: str) -> Tuple[bool, str]:
    """Upload ZIP contents as files to repo root via GitHub API"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            files = {}
            for f in zipf.namelist():
                if f.endswith("/"):  # skip folders
                    continue
                with zipf.open(f) as file_content:
                    files[f] = file_content.read().decode("utf-8", errors="ignore")

        async with aiohttp.ClientSession() as session:
            # We upload files one by one using the Create or Update Contents API
            # API: PUT /repos/{owner}/{repo}/contents/{path}
            # Need to commit with message
            owner, repo = full_repo_name.split("/")
            for filepath, content in files.items():
                url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filepath}"
                get_status, get_resp = await gh_request(session, "GET", url, token=token)
                sha = get_resp.get("sha") if isinstance(get_resp, dict) else None
                payload = {
                    "message": f"Upload {filepath} via Spilux Bot",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": "main"
                }
                if sha:
                    payload["sha"] = sha
                put_status, put_resp = await gh_request(session, "PUT", url, token=token, json=payload)
                if put_status not in (200, 201):
                    return False, f"Failed to upload {filepath}: {put_resp.get('message', put_resp)}"
            return True, "✅ ZIP uploaded successfully."
    except Exception as e:
        return False, f"❌ Upload failed: {e}"

# -------- Admin panel --------

async def show_admin_panel(cq: CallbackQuery):
    kb = [
        [InlineKeyboardButton("📋 List Users", callback_data="admin_list_users")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="start_menu")]
    ]
    await cq.message.edit("👥 Admin Panel - Choose an action:", reply_markup=InlineKeyboardMarkup(kb))

async def admin_list_users(cq: CallbackQuery):
    users = await data_store.get_users()
    kb = []
    for uid, info in users.items():
        btn_text = f"{info.get('first_name', '')} (@{info.get('username', '')})"
        kb.append([
            InlineKeyboardButton(btn_text, callback_data=f"admin_edit_tokens:{uid}"),
            InlineKeyboardButton("❌ Ban", callback_data=f"admin_ban_user:{uid}"),
        ])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    text = "👥 Registered Users:"
    await cq.message.edit(text, reply_markup=InlineKeyboardMarkup(kb))

async def admin_edit_user_tokens(cq: CallbackQuery, target_id: int):
    tokens = await data_store.get_tokens(target_id)
    active_token = await data_store.get_active_token(target_id)
    kb = []
    for token, meta in tokens.items():
        uname = meta.get("username", "unknown")
        active_mark = "✅" if active_token == token else ""
        kb.append([
            InlineKeyboardButton(f"{uname} {active_mark}", callback_data=f"admin_switch_token:{target_id}:{token}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"admin_remove_token:{target_id}:{token}")
        ])
    kb.append([InlineKeyboardButton("🔙 Back to Users", callback_data="admin_list_users")])
    await cq.message.edit(f"🛠️ Tokens for user {target_id}:", reply_markup=InlineKeyboardMarkup(kb))

# -------- Run --------

async def startup():
    await data_store.load()
    logging.info("Data loaded.")

app.startup = startup

if __name__ == "__main__":
    print("Starting Spilux GitHub Bot...")
    app.run()
