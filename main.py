import os
import re
import json
import aiohttp
import asyncio
import tempfile
from typing import Any, Dict, Optional
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ------------- CONFIG ----------------
API_ID = int(os.getenv("API_ID", "20660797"))
API_HASH = os.getenv("API_HASH", "755e5cdf9bade62a75211e7a57f25601")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8448084086:AAGT6-0n3g3OkQDYxxlyXdJESsYmjX9_ISA")
ADMINS = [int(x) for x in os.getenv("7941175119", "").split(",") if x.strip()]
DATA_FILE = "data.json"
ITEMS_PER_PAGE = 6

# ------------- DATA STORAGE --------------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"tokens": {}, "banned": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

data = load_data()

# ------------- STATES -------------------
user_states: Dict[int, Dict[str, Any]] = {}
admin_states: Dict[int, Dict[str, Any]] = {}

# ------------- HELPERS ------------------
def not_banned_filter(_, __, message: Message):
    return (message.from_user and (message.from_user.id not in set(data.get("banned", []))))

def is_admin(uid: int) -> bool:
    return uid in ADMINS

def make_back_keyboard(text="Back to menu"):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data="start_back")]]
    )

def extract_github_username_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"github\.com/([A-Za-z0-9\-_.]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_.]{1,39}", text.strip()):
        return text.strip()
    return None

async def gh_request(session: aiohttp.ClientSession, method: str, url: str, token: Optional[str] = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers.update({"Authorization": f"token {token}"})
    headers.update({"Accept": "application/vnd.github.v3+json", "User-Agent": "Spilux-GitHub-Bot"})
    async with session.request(method, url, headers=headers, **kwargs) as resp:
        try:
            body = await resp.json()
        except:
            body = await resp.text()
        return resp.status, body

async def download_repo_zip(token: Optional[str], full_name: str) -> Optional[bytes]:
    """
    Download repo as ZIP archive via GitHub API.
    If token is None, only public repos will work.
    """
    url = f"https://api.github.com/repos/{full_name}/zipball"
    async with aiohttp.ClientSession() as session:
        status, data = await gh_request(session, "GET", url, token)
        if status == 200:
            # data is binary ZIP content
            return await session.get(url, headers={"Authorization": f"token {token}"} if token else None).then(lambda r: r.read())
        else:
            return None

# ------------- APP --------------------
app = Client("github_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --------- START / MAIN MENU -----------
@app.on_message(filters.private & filters.command("start") & filters.create(not_banned_filter))
async def cmd_start(_, msg: Message):
    uid = msg.from_user.id
    # Ensure user record
    data.setdefault("tokens", {})
    rec = data["tokens"].setdefault(str(uid), {"first_name": msg.from_user.first_name or "", "tokens": {}, "active": None})
    rec.setdefault("first_name", msg.from_user.first_name or "")
    save_data(data)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Add Token", callback_data="add_token")],
        [InlineKeyboardButton("🔁 Switch Token", callback_data="switch_token_menu")],
        [InlineKeyboardButton("📂 My Repos", callback_data="list_repos")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data="upload_zip_repo")],
        [InlineKeyboardButton("🔍 Search GitHub User", callback_data="search_user"),
         InlineKeyboardButton("🔎 Search Repos (keyword)", callback_data="search_repo")],
        [InlineKeyboardButton("📈 Trending", callback_data="trending"),
         InlineKeyboardButton("🎲 Random Repo", callback_data="random_repo")],
        [InlineKeyboardButton("🧾 Gist Create", callback_data="gist_create"),
         InlineKeyboardButton("📊 GH Stats", callback_data="ghstats")],
    ])
    if is_admin(uid):
        kb.inline_keyboard.append([InlineKeyboardButton("👥 Admin Panel", callback_data="admin_panel")])

    await msg.reply("👋 Welcome! Press a button to run a feature.", reply_markup=kb)
    user_states.pop(uid, None)

# -------- CALLBACK QUERY HANDLER -----------
@app.on_callback_query(filters.create(lambda _, __, cq: True))
async def cb_menu(_, cq: CallbackQuery):
    uid = cq.from_user.id
    data_cb = cq.data or ""

    # Back to main menu
    if data_cb == "start_back":
        await cmd_start(_, cq.message)
        await cq.answer()
        user_states.pop(uid, None)
        admin_states.pop(uid, None)
        return

    # ADD TOKEN flow
    if data_cb == "add_token":
        user_states[uid] = {"action": "awaiting_add_token"}
        await cq.message.edit("🔑 Send your GitHub Personal Access Token (PAT).\n\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # SWITCH TOKEN menu
    if data_cb == "switch_token_menu":
        rec = data.get("tokens", {}).get(str(uid), {})
        saved = rec.get("tokens", {})
        if not saved:
            await cq.answer("You have no tokens saved. Add one first.", show_alert=True)
            return
        kb = []
        for tkn, meta in saved.items():
            name = meta.get("username", "unknown")
            active = rec.get("active") == tkn
            label = f"{name}{' (active)' if active else ''}"
            kb.append([InlineKeyboardButton(label, callback_data=f"switchtoken_btn:{name}")])
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="start_back")])
        await cq.message.edit("🔁 Choose a token to activate:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    if data_cb.startswith("switchtoken_btn:"):
        name = data_cb.split(":",1)[1]
        rec = data.get("tokens", {}).get(str(uid), {})
        found = None
        for tkn, meta in rec.get("tokens", {}).items():
            if meta.get("username") == name:
                found = tkn
                break
        if not found:
            await cq.answer("Token not found.", show_alert=True)
            return
        data["tokens"][str(uid)]["active"] = found
        save_data(data)
        await cq.message.edit(f"✅ Active token switched to **{name}**.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # LIST REPOS
    if data_cb == "list_repos":
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await cq.answer("No active token. Add one first.", show_alert=True)
            return
        await cq.message.edit("📚 Fetching your repositories...", reply_markup=None)
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=200", token)
            if st != 200:
                await cq.message.edit("❌ Failed to fetch repos (token may be invalid).", reply_markup=make_back_keyboard())
                return
            if not repos:
                await cq.message.edit("⚠️ No repositories found.", reply_markup=make_back_keyboard())
                return
            kb = []
            for r in repos:
                kb.append([InlineKeyboardButton(r.get("name"), callback_data=f"myrepo:{r.get('full_name')}")])
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            await cq.message.edit("📚 Your repos:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    if data_cb.startswith("myrepo:"):
        full_name = data_cb.split(":", 1)[1]
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        await cq.answer("Preparing download...")
        if not token:
            # Public repo fallback
            url = f"https://github.com/{full_name}/archive/refs/heads/main.zip"
            await cq.message.edit(f"Public repo download link:\n{url}", reply_markup=make_back_keyboard())
            return
        # Try downloading repo ZIP
        zip_url = f"https://api.github.com/repos/{full_name}/zipball"
        # Send a direct download link for the ZIP (with token in header, can't send direct URL with token)
        # Best: send public URL or fail gracefully
        try:
            # Download the zip file
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"token {token}"}
                async with session.get(zip_url, headers=headers) as resp:
                    if resp.status != 200:
                        await cq.message.edit("❌ Failed to download repo archive. Maybe private or token invalid.", reply_markup=make_back_keyboard())
                        return
                    data_bytes = await resp.read()
                    # Send file
                    await cq.message.reply_document(data_bytes, file_name=f"{full_name.replace('/', '_')}.zip")
                    await cq.answer()
                    await cq.message.edit(f"✅ Downloaded repository `{full_name}`.", reply_markup=make_back_keyboard())
        except Exception as e:
            await cq.message.edit(f"❌ Error: {e}", reply_markup=make_back_keyboard())
        return

    # UPLOAD ZIP placeholder
    if data_cb == "upload_zip_repo":
        user_states[uid] = {"action": "awaiting_zip_upload"}
        await cq.message.edit("📤 Please send the ZIP file now.\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # SEARCH USER
    if data_cb == "search_user":
        user_states[uid] = {"action": "awaiting_search_user"}
        await cq.message.edit("🔎 Send a GitHub username or profile URL (e.g. https://github.com/username).\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # SEARCH REPO
    if data_cb == "search_repo":
        user_states[uid] = {"action": "awaiting_search_repo"}
        await cq.message.edit("🔎 Send a keyword to search public repositories.\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # TRENDING placeholder
    if data_cb == "trending":
        await cq.answer("Fetching trending...", show_alert=False)
        await cq.message.edit("🔝 Trending repos feature coming soon!", reply_markup=make_back_keyboard())
        return

    # RANDOM REPO placeholder
    if data_cb == "random_repo":
        await cq.answer("Finding a random popular repo...", show_alert=False)
        await cq.message.edit("🎲 Random repo feature coming soon!", reply_markup=make_back_keyboard())
        return

    # GIST CREATE
    if data_cb == "gist_create":
        user_states[uid] = {"action": "awaiting_gist_create", "public": True}
        await cq.message.edit("🧾 Send gist content (plain text or JSON for multiple files).\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # GH STATS placeholder
    if data_cb == "ghstats":
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await cq.answer("No active token.", show_alert=True)
            return
        async with aiohttp.ClientSession() as session:
            st, stats = await gh_request(session, "GET", "https://api.github.com/rate_limit", token)
            if st != 200:
                await cq.answer("Failed to fetch stats.", show_alert=True)
                return
            limit = stats.get("rate", {})
            text = (
                f"GitHub API Rate Limits:\n"
                f"Limit: {limit.get('limit')}\n"
                f"Remaining: {limit.get('remaining')}\n"
                f"Reset: <code>{limit.get('reset')}</code>"
            )
            await cq.message.edit(text, reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # ADMIN PANEL
    if data_cb == "admin_panel":
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        kb = [
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list_users")],
            [InlineKeyboardButton("📂 Manage User Repos", callback_data="admin_manage_user")],
            [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user")],
            [InlineKeyboardButton("◀️ Back", callback_data="start_back")]
        ]
        await cq.message.edit("👑 Admin Panel", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    # ADMIN LIST USERS
    if data_cb == "admin_list_users":
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        users = data.get("tokens", {})
        if not users:
            await cq.message.edit("No users found.", reply_markup=make_back_keyboard())
            return
        kb = []
        for u in users.keys():
            uname = users[u].get("first_name", "unknown")
            active_tkn = users[u].get("active")
            gh_name = "N/A"
            if active_tkn:
                gh_name = users[u]["tokens"].get(active_tkn, {}).get("username", "N/A")
            kb.append([InlineKeyboardButton(f"{uname} ({gh_name})", callback_data=f"admin_user:{u}")])
        kb.append([InlineKeyboardButton("◀️ Back", callback_data="admin_panel")])
        await cq.message.edit("👥 Users:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    # ADMIN USER selected - manage repos
    if data_cb.startswith("admin_user:"):
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        target_uid = data_cb.split(":",1)[1]
        admin_states[uid] = {"target_uid": target_uid}
        kb = [
            [InlineKeyboardButton("📂 List Repos", callback_data=f"admin_list_repos:{target_uid}")],
            [InlineKeyboardButton("➕ Create Repo", callback_data=f"admin_create_repo:{target_uid}")],
            [InlineKeyboardButton("◀️ Back", callback_data="admin_list_users")]
        ]
        await cq.message.edit(f"Manage user {target_uid}:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    # ADMIN LIST REPOS FOR USER
    if data_cb.startswith("admin_list_repos:"):
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        target_uid = data_cb.split(":",1)[1]
        user_rec = data.get("tokens", {}).get(target_uid, {})
        active_tkn = user_rec.get("active")
        if not active_tkn:
            await cq.message.edit("User has no active token.", reply_markup=make_back_keyboard())
            return
        token = active_tkn
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=200", token)
            if st != 200:
                await cq.message.edit("Failed to fetch repos.", reply_markup=make_back_keyboard())
                return
            if not repos:
                await cq.message.edit("User has no repos.", reply_markup=make_back_keyboard())
                return
            kb = []
            for r in repos:
                full_name = r.get("full_name")
                kb.append([InlineKeyboardButton(r.get("name"), callback_data=f"admin_repo_download:{target_uid}:{full_name}")])
            kb.append([InlineKeyboardButton("◀️ Back", callback_data=f"admin_user:{target_uid}")])
            await cq.message.edit(f"Repos for user {target_uid}:", reply_markup=InlineKeyboardMarkup(kb))
        await cq.answer()
        return

    # ADMIN REPO DOWNLOAD
    if data_cb.startswith("admin_repo_download:"):
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        parts = data_cb.split(":", 2)
        target_uid, full_name = parts[1], parts[2]
        user_rec = data.get("tokens", {}).get(target_uid, {})
        active_tkn = user_rec.get("active")
        if not active_tkn:
            await cq.message.edit("User has no active token.", reply_markup=make_back_keyboard())
            return
        token = active_tkn
        await cq.answer("Downloading repo...")
        zip_url = f"https://api.github.com/repos/{full_name}/zipball"
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"token {token}"}
                async with session.get(zip_url, headers=headers) as resp:
                    if resp.status != 200:
                        await cq.message.edit("Failed to download repo archive.", reply_markup=make_back_keyboard())
                        return
                    data_bytes = await resp.read()
                    await cq.message.reply_document(data_bytes, file_name=f"{full_name.replace('/', '_')}.zip")
                    await cq.answer()
                    await cq.message.edit(f"✅ Downloaded repo `{full_name}`.", reply_markup=make_back_keyboard())
        except Exception as e:
            await cq.message.edit(f"Error: {e}", reply_markup=make_back_keyboard())
        return

    # ADMIN CREATE REPO (start input)
    if data_cb.startswith("admin_create_repo:"):
        if not is_admin(uid):
            await cq.answer("Unauthorized", show_alert=True)
            return
        target_uid = data_cb.split(":",1)[1]
        admin_states[uid] = {"action": "admin_awaiting_create_repo", "target_uid": target_uid}
        await cq.message.edit("Send the new repo name.\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
        await cq.answer()
        return

    # ADMIN BAN USER (placeholder)
    if data_cb == "admin_ban_user":
        await cq.answer("Ban user feature coming soon.", show_alert=True)
        return

    # Unknown callback
    await cq.answer("Unknown command.", show_alert=True)

# -------- MESSAGE HANDLER FOR STATES ----------
@app.on_message(filters.private & filters.create(not_banned_filter))
async def on_message(_, msg: Message):
    uid = msg.from_user.id
    text = msg.text or ""

    # Handle back keyword
    if text.lower() == "back":
        user_states.pop(uid, None)
        admin_states.pop(uid, None)
        await cmd_start(_, msg)
        return

    state = user_states.get(uid)
    admin_state = admin_states.get(uid)

    if state:
        action = state.get("action")

        if action == "awaiting_add_token":
            token = text.strip()
            # Validate token by fetching user info
            async with aiohttp.ClientSession() as session:
                st, user = await gh_request(session, "GET", "https://api.github.com/user", token)
                if st != 200:
                    await msg.reply("❌ Invalid token. Try again or send 'Back' to cancel.", reply_markup=make_back_keyboard())
                    return
                username = user.get("login")
                if not username:
                    await msg.reply("❌ Could not fetch username from token. Try again or send 'Back' to cancel.", reply_markup=make_back_keyboard())
                    return

            # Save token for user
            data.setdefault("tokens", {})
            rec = data["tokens"].setdefault(str(uid), {"first_name": msg.from_user.first_name or "", "tokens": {}, "active": None})
            rec["tokens"][token] = {"username": username}
            rec["active"] = token
            save_data(data)
            user_states.pop(uid, None)
            await msg.reply(f"✅ Token saved and activated for GitHub user `{username}`.", reply_markup=make_back_keyboard())
            return

        if action == "awaiting_search_user":
            username = extract_github_username_from_text(text.strip())
            if not username:
                await msg.reply("❌ Invalid GitHub username or URL. Send again or 'Back' to cancel.", reply_markup=make_back_keyboard())
                return
            user_states.pop(uid, None)
            # Fetch user's public repos
            async with aiohttp.ClientSession() as session:
                st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{username}/repos?per_page=50")
                if st != 200:
                    await msg.reply("❌ Failed to fetch user repos.", reply_markup=make_back_keyboard())
                    return
                if not repos:
                    await msg.reply("⚠️ User has no public repositories.", reply_markup=make_back_keyboard())
                    return
                kb = []
                for r in repos:
                    full_name = r.get("full_name")
                    kb.append([InlineKeyboardButton(r.get("name"), callback_data=f"searchuser_repo:{full_name}")])
                kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
                await msg.reply(f"Repositories of user `{username}`:", reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "awaiting_search_repo":
            keyword = text.strip()
            if not keyword:
                await msg.reply("❌ Please send a keyword or 'Back' to cancel.", reply_markup=make_back_keyboard())
                return
            user_states.pop(uid, None)
            async with aiohttp.ClientSession() as session:
                st, result = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={keyword}&per_page=20")
                if st != 200:
                    await msg.reply("❌ Search failed.", reply_markup=make_back_keyboard())
                    return
                items = result.get("items", [])
                if not items:
                    await msg.reply("⚠️ No repositories found for that keyword.", reply_markup=make_back_keyboard())
                    return
                kb = []
                for r in items:
                    full_name = r.get("full_name")
                    kb.append([InlineKeyboardButton(r.get("name"), callback_data=f"searchrepo_repo:{full_name}")])
                kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
                await msg.reply(f"Repositories matching `{keyword}`:", reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "awaiting_gist_create":
            content = text.strip()
            if not content:
                await msg.reply("❌ Send gist content or 'Back' to cancel.", reply_markup=make_back_keyboard())
                return
            # Create gist with active token
            rec = data.get("tokens", {}).get(str(uid), {})
            token = rec.get("active")
            if not token:
                await msg.reply("❌ No active token. Add one first.", reply_markup=make_back_keyboard())
                return
            user_states.pop(uid, None)
            async with aiohttp.ClientSession() as session:
                payload = {
                    "files": {
                        "file1.txt": {"content": content}
                    },
                    "public": True,
                    "description": "Created by Telegram Bot"
                }
                st, resp = await gh_request(session, "POST", "https://api.github.com/gists", token, json=payload)
                if st != 201:
                    await msg.reply("❌ Failed to create gist.", reply_markup=make_back_keyboard())
                    return
                gist_url = resp.get("html_url")
                await msg.reply(f"✅ Gist created: {gist_url}", reply_markup=make_back_keyboard())
            return

        if action == "awaiting_zip_upload":
            # Placeholder: you can implement ZIP upload to GitHub repo here
            await msg.reply("ZIP upload feature is not implemented yet.\nSend 'Back' to cancel.", reply_markup=make_back_keyboard())
            return

        if action == "admin_awaiting_create_repo":
            repo_name = text.strip()
            if not repo_name:
                await msg.reply("❌ Send a valid repository name or 'Back' to cancel.", reply_markup=make_back_keyboard())
                return
            target_uid = admin_state.get("target_uid")
            user_rec = data.get("tokens", {}).get(target_uid, {})
            token = user_rec.get("active")
            if not token:
                await msg.reply("❌ Target user has no active token.", reply_markup=make_back_keyboard())
                admin_states.pop(uid, None)
                return
            # Create repo via API
            async with aiohttp.ClientSession() as session:
                payload = {
                    "name": repo_name,
                    "auto_init": True,
                    "private": False
                }
                st, resp = await gh_request(session, "POST", "https://api.github.com/user/repos", token, json=payload)
                if st != 201:
                    await msg.reply(f"❌ Failed to create repo: {resp}", reply_markup=make_back_keyboard())
                    admin_states.pop(uid, None)
                    return
                await msg.reply(f"✅ Repo `{repo_name}` created for user {target_uid}.", reply_markup=make_back_keyboard())
                admin_states.pop(uid, None)
            return

    # If no states matched, ignore or show main
    # Just ignore other messages for now
    return

# --------- RUN APP ----------
if __name__ == "__main__":
    print("Bot is running...")
    app.run()
