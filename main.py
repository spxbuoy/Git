# main.py
import os
import re
import json
import aiohttp
import asyncio
import tempfile
import zipfile
import base64
import shutil
import datetime
import random
from typing import Any, Dict, List, Tuple, Optional
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# ---------------- CONFIG ----------------
API_ID = int(os.getenv("API_ID", "22222258"))
API_HASH = os.getenv("API_HASH", "60ea076de059a85ccfd68516df08b951")
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMINS = [int(x) for x in os.getenv("ADMINS", "7941175119").split(",") if x.strip()]
DATA_FILE = "data.json"
# ----------------------------------------

# In-memory states
user_states: Dict[int, Any] = {}

# Persistent storage load/save
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"tokens": {}, "banned": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

_data = load_data()
user_tokens: Dict[str, Any] = _data.get("tokens", {})  # {telegram_id: {"tokens": {token: {"username":..}}, "active": token}}
banned_users = set(_data.get("banned", []))

def persist():
    save_data({"tokens": user_tokens, "banned": list(banned_users)})

# ---------- HTTP (GitHub) helpers ----------
async def gh_request(session: aiohttp.ClientSession, method: str, url: str, token: Optional[str] = None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers.update({"Authorization": f"token {token}"})
    headers.update({
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Spilux-GitHub-Bot"
    })
    async with session.request(method, url, headers=headers, **kwargs) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = await resp.text()
        return resp.status, data

async def gh_rate_limit(session: aiohttp.ClientSession, token: Optional[str] = None):
    st, data = await gh_request(session, "GET", "https://api.github.com/rate_limit", token)
    return st, data

async def get_user_info(session: aiohttp.ClientSession, token: str):
    return await gh_request(session, "GET", "https://api.github.com/user", token)

# ---------- Small utilities ----------
def not_banned_filter(_, __, message: Message):
    return (message.from_user and (message.from_user.id not in banned_users))

def extract_github_username_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"github\.com/([A-Za-z0-9\-_.]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_.]{1,39}", text.strip()):
        return text.strip()
    return None

async def write_temp_file(data: bytes, suffix: str = "") -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    await asyncio.to_thread(lambda: f.write(data))
    f.close()
    return f.name

async def read_file_bytes(path: str) -> bytes:
    return await asyncio.to_thread(lambda: open(path, "rb").read())

# ---------- Pyrogram client ----------
app = Client("github_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- Start and main menu ----------
@app.on_message(filters.command("start") & filters.private & filters.create(not_banned_filter))
async def start_cmd(client: Client, msg: Message):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Set Token", callback_data="set_token")],
        [InlineKeyboardButton("🔍 Search GitHub User", callback_data="search_user")],
        [InlineKeyboardButton("🔎 Search Repos (keyword)", callback_data="search_repo")],
        [InlineKeyboardButton("📂 My Repos", callback_data="list_repos")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data="upload_zip_repo")],
        [InlineKeyboardButton("🏓 Ping", callback_data="ping")],
    ])
    if msg.from_user.id in ADMINS:
        kb.keyboard.append([InlineKeyboardButton("👁 View Users (admin)", callback_data="admin_view_users")])
        kb.keyboard.append([InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")])
        kb.keyboard.append([InlineKeyboardButton("🛠 Exec API (admin)", callback_data="exec_api")])
    await msg.reply("👋 Welcome — choose an action:", reply_markup=kb)

# ---------- Main menu callbacks ----------
@app.on_callback_query(filters.regex("^(set_token|search_user|search_repo|list_repos|upload_zip_repo|ping|admin_view_users|broadcast|exec_api)$"))
async def main_menu_cb(client: Client, cb: CallbackQuery):
    action = cb.data
    uid = cb.from_user.id

    if action == "set_token":
        user_states[uid] = {"action": "set_token"}
        await cb.message.edit("🔑 Please send your GitHub Personal Access Token (PAT). Required scopes: `repo` and `gist` (optional).", reply_markup=None)

    elif action == "search_user":
        user_states[uid] = {"action": "search_user"}
        await cb.message.edit("🔎 Send a GitHub username or paste their GitHub profile URL (e.g. `https://github.com/username`).", reply_markup=None)

    elif action == "search_repo":
        user_states[uid] = {"action": "search_repo"}
        await cb.message.edit("🔎 Send a keyword to search public repositories (e.g. `telegram bot`).", reply_markup=None)

    elif action == "list_repos":
        info = user_tokens.get(str(uid))
        token = info.get("active") if info else None
        if not token:
            await cb.message.edit("❌ You have no active token. Use Set Token first.", reply_markup=None)
            return
        await cb.answer("Fetching your repos...", show_alert=False)
        async with aiohttp.ClientSession() as session:
            st, user = await get_user_info(session, token)
            if st != 200:
                await cb.message.edit("❌ Token invalid or GitHub unreachable. Use Set Token again.", reply_markup=None)
                return
            username = user.get("login")
            st2, repos = await gh_request(session, "GET", f"https://api.github.com/user/repos?per_page=200", token)
            if st2 != 200:
                await cb.message.edit("❌ Failed to fetch repos.", reply_markup=None)
                return
            if not repos:
                await cb.message.edit("⚠️ No repositories found.", reply_markup=None)
                return
            buttons = [[InlineKeyboardButton(r.get("name"), callback_data=f"myrepo:{r.get('full_name')}")] for r in repos]
            buttons.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            await cb.message.edit(f"📚 Repos for **{username}**:", reply_markup=InlineKeyboardMarkup(buttons))

    elif action == "upload_zip_repo":
        user_states[uid] = {"action": "awaiting_zip_upload"}
        await cb.message.edit("📤 Please send your ZIP file now (private chat). I'll extract and ask for target repo (owner/repo).", reply_markup=None)

    elif action == "ping":
        await cb.answer("Pong! ✅", show_alert=False)

    elif action == "admin_view_users":
        if uid not in ADMINS:
            await cb.answer("Unauthorized", show_alert=True)
            return
        rows = []
        for suid, info in user_tokens.items():
            sample_username = None
            tokens = info.get("tokens", {})
            for tkn, meta in (tokens.items() if isinstance(tokens, dict) else []):
                sample_username = meta.get("username")
                break
            label = sample_username or f"User {suid}"
            rows.append([InlineKeyboardButton(f"{label} ({suid})", callback_data=f"admin_view_user:{suid}")])
        if not rows:
            await cb.message.edit("No users saved.", reply_markup=None)
            return
        rows.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
        await cb.message.edit("👥 Registered users (admin):", reply_markup=InlineKeyboardMarkup(rows))

    elif action == "broadcast":
        if uid not in ADMINS:
            await cb.answer("Unauthorized", show_alert=True)
            return
        user_states[uid] = {"action": "broadcast"}
        await cb.message.edit("📢 Send the text you want to broadcast to all known users.", reply_markup=None)

    elif action == "exec_api":
        if uid not in ADMINS:
            return await cb.answer("Unauthorized", show_alert=True)
        user_states[uid] = {"action": "exec_api"}
        await cb.message.edit("🛠 Admin: send a raw HTTP call in the format `METHOD /path` (e.g. `GET /rate_limit`), optionally followed by a JSON body on next line.", reply_markup=None)

@app.on_callback_query(filters.regex("^start_back$"))
async def back_to_start_cb(client: Client, cb: CallbackQuery):
    await cb.message.edit("Back to main. Use /start or press buttons again.", reply_markup=None)

# ---------- Repo actions for registered user ----------
@app.on_callback_query(filters.regex("^myrepo:"))
async def myrepo_cb(client: Client, cb: CallbackQuery):
    repo_full = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    token = user_tokens.get(str(uid), {}).get("active")
    if not token:
        await cb.message.edit("❌ No active token. Use Set Token first.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download ZIP", callback_data=f"download_repo:{repo_full}")],
        [InlineKeyboardButton("🗑 Delete Repo", callback_data=f"delete_repo:{repo_full}")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data=f"upload_zip_to_repo:{repo_full}")],
        [InlineKeyboardButton("📝 Create/Edit File", callback_data=f"editfile_repo:{repo_full}")],
        [InlineKeyboardButton("🔁 Fork", callback_data=f"forkrepo:{repo_full}")],
        [InlineKeyboardButton("📊 Insights", callback_data=f"repo_insights:{repo_full}")],
        [InlineKeyboardButton("◀️ Back", callback_data="start_back")]
    ])
    await cb.message.edit(f"Repo: `{repo_full}` — choose an action:", reply_markup=kb)

# ---------- Admin view user's repos ----------
@app.on_callback_query(filters.regex("^admin_view_user:"))
async def admin_view_user_cb(client: Client, cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Unauthorized", show_alert=True)
    target_uid = cb.data.split(":", 1)[1]
    info = user_tokens.get(target_uid)
    if not info:
        return await cb.message.edit("❌ This user has no saved tokens.")
    token = info.get("active")
    if not token:
        return await cb.message.edit("❌ The user has no active token.")
    async with aiohttp.ClientSession() as session:
        st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=200", token)
        if st != 200:
            return await cb.message.edit("❌ Failed to fetch user's repos (token may be invalid).")
        kb = [[InlineKeyboardButton(r.get("full_name"), callback_data=f"admin_repo:{target_uid}:{r.get('full_name')}")] for r in repos]
        kb.append([InlineKeyboardButton("◀️ Back", callback_data="admin_view_users")])
        await cb.message.edit(f"Repos for user {target_uid}:", reply_markup=InlineKeyboardMarkup(kb))

@app.on_callback_query(filters.regex("^admin_repo:"))
async def admin_repo_cb(client: Client, cb: CallbackQuery):
    _, target_uid, full_repo = cb.data.split(":", 2)
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Unauthorized", show_alert=True)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download ZIP", callback_data=f"admin_download:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("🗑 Delete Repo", callback_data=f"admin_delete:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data=f"admin_uploadzip:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("📝 Create/Edit File", callback_data=f"admin_editfile:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("➕ Add Collaborator", callback_data=f"admin_addcollab:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("➖ Remove Collaborator", callback_data=f"admin_delcollab:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("🔒 Set private/public", callback_data=f"admin_setvis:{target_uid}:{full_repo}")],
        [InlineKeyboardButton("◀️ Back", callback_data=f"admin_view_user:{target_uid}")]
    ])
    await cb.message.edit(f"Admin actions for `{full_repo}`:", reply_markup=kb)

# ---------- Download (user/admin) ----------
@app.on_callback_query(filters.regex("^(download_repo:|admin_download:)"))
async def download_repo_cb(client: Client, cb: CallbackQuery):
    if cb.data.startswith("admin_download:"):
        _, remaining = cb.data.split(":", 1)
        target_uid, full_repo = remaining.split(":", 1)
        token = user_tokens.get(target_uid, {}).get("active")
    else:
        full_repo = cb.data.split(":", 1)[1]
        token = user_tokens.get(str(cb.from_user.id), {}).get("active")
    if not token:
        await cb.message.edit("❌ No token available for this operation.")
        return
    await cb.answer("Preparing download...", show_alert=False)
    async with aiohttp.ClientSession() as session:
        st, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{full_repo}", token)
        if st != 200:
            return await cb.message.edit("❌ Repo not found or inaccessible.")
        default_branch = repo_info.get("default_branch", "main")
        archive_url = f"https://api.github.com/repos/{full_repo}/zipball/{default_branch}"
        headers = {"Authorization": f"token {token}", "User-Agent": "Spilux-GitHub-Bot"}
        async with session.get(archive_url, headers=headers) as resp:
            if resp.status != 200:
                return await cb.message.edit("❌ Failed to download repository archive.")
            data = await resp.read()
            tmp_file = await write_temp_file(data, suffix=".zip")
            await cb.message.reply_document(document=tmp_file, caption=f"📦 Archive for `{full_repo}`")
            os.unlink(tmp_file)
            await cb.message.edit("✅ Download sent.")

# ---------- Delete repo ----------
@app.on_callback_query(filters.regex("^(delete_repo:|admin_delete:)"))
async def delete_repo_cb(client: Client, cb: CallbackQuery):
    is_admin = cb.data.startswith("admin_delete:")
    if is_admin:
        _, remaining = cb.data.split(":", 1)
        target_uid, full_repo = remaining.split(":", 1)
    else:
        full_repo = cb.data.split(":", 1)[1]
        target_uid = str(cb.from_user.id)
    user_states[cb.from_user.id] = {"action": "confirm_delete", "target_uid": target_uid, "full_repo": full_repo}
    await cb.message.edit(f"⚠️ Are you sure you want to delete `{full_repo}`? This is irreversible.\n\nSend `yes` to confirm or `no` to cancel.", reply_markup=None)

# ---------- Edit file flow (create/edit) ----------
@app.on_callback_query(filters.regex("^editfile_repo:|^admin_editfile:"))
async def begin_edit_file_cb(client: Client, cb: CallbackQuery):
    if cb.data.startswith("admin_editfile:"):
        _, target_uid, repo_full = cb.data.split(":", 2)
        if cb.from_user.id not in ADMINS:
            return await cb.answer("Unauthorized")
        user_states[cb.from_user.id] = {"action": "edit_file", "repo": repo_full, "target_uid": target_uid, "step": "awaiting_filepath"}
        await cb.message.edit(f"📝 Admin: send the file path (e.g., `folder/file.txt`) to create/edit in `{repo_full}`.", reply_markup=None)
    else:
        repo_full = cb.data.split(":", 1)[1]
        user_states[cb.from_user.id] = {"action": "edit_file", "repo": repo_full, "step": "awaiting_filepath"}
        await cb.message.edit(f"📝 Send the file path (e.g., `folder/file.txt`) to create/edit in `{repo_full}`.", reply_markup=None)

# ---------- Fork repo (user) ----------
@app.on_callback_query(filters.regex("^forkrepo:"))
async def forkrepo_cb(client: Client, cb: CallbackQuery):
    repo_full = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    token = user_tokens.get(str(uid), {}).get("active")
    if not token:
        return await cb.message.edit("❌ No active token.")
    await cb.answer("Forking repository...", show_alert=False)
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "POST", f"https://api.github.com/repos/{repo_full}/forks", token)
        if st in (202, 201):
            return await cb.message.edit(f"✅ Fork request submitted. GitHub may take a few seconds to create the fork in your account.")
        else:
            return await cb.message.edit(f"❌ Fork failed: {st} — {data}")

# ---------- Repo insights ----------
@app.on_callback_query(filters.regex("^repo_insights:"))
async def repo_insights_cb(client: Client, cb: CallbackQuery):
    repo_full = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    token = user_tokens.get(str(uid), {}).get("active")
    # token optional for public data
    async with aiohttp.ClientSession() as session:
        st, repo = await gh_request(session, "GET", f"https://api.github.com/repos/{repo_full}", token)
        if st != 200:
            return await cb.message.edit("❌ Repo not accessible.")
        st_c, contributors = await gh_request(session, "GET", f"https://api.github.com/repos/{repo_full}/contributors?per_page=10", token)
        st_l, langs = await gh_request(session, "GET", f"https://api.github.com/repos/{repo_full}/languages", token)
        text = f"📊 Insights for `{repo_full}`\n\nStars: {repo.get('stargazers_count')}\nForks: {repo.get('forks_count')}\nOpen issues: {repo.get('open_issues_count')}\nDefault branch: {repo.get('default_branch')}\n\nLanguages: {', '.join(langs.keys()) if isinstance(langs, dict) else 'N/A'}\nTop contributors:\n"
        if isinstance(contributors, list):
            for c in contributors[:10]:
                text += f"- {c.get('login')} ({c.get('contributions')} commits)\n"
        await cb.message.edit(text)

# ---------- Upload ZIP to a repo (menu paths) ----------
@app.on_callback_query(filters.regex("^upload_zip_to_repo:|^admin_uploadzip:"))
async def uploadzip_to_repo_cb(client: Client, cb: CallbackQuery):
    if cb.data.startswith("admin_uploadzip:"):
        _, target_uid, full_repo = cb.data.split(":", 2)
        if cb.from_user.id not in ADMINS:
            await cb.answer("Unauthorized", show_alert=True)
            return
        user_states[cb.from_user.id] = {"action": "awaiting_zip_upload_admin", "target_uid": target_uid, "full_repo": full_repo}
        await cb.message.edit(f"📤 Admin: send ZIP to upload into `{full_repo}` (private chat).", reply_markup=None)
    else:
        full_repo = cb.data.split(":", 1)[1]
        user_states[cb.from_user.id] = {"action": "awaiting_zip_upload_for_repo", "full_repo": full_repo}
        await cb.message.edit(f"📤 Send ZIP to upload into `{full_repo}` (private chat).", reply_markup=None)

# ---------- Handle private ZIP uploads (repo-specific) ----------
@app.on_message(filters.private & filters.document & filters.create(not_banned_filter))
async def handle_repo_zip_uploads(client: Client, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)
    if not state:
        return
    if state.get("action") not in ("awaiting_zip_upload_admin", "awaiting_zip_upload_for_repo", "awaiting_zip_upload"):
        return
    if not msg.document.file_name.lower().endswith(".zip"):
        return await msg.reply("❌ Please send a .zip file.")
    path = await msg.download()
    if state.get("action") == "awaiting_zip_upload_for_repo":
        repo_full = state.get("full_repo")
        token = user_tokens.get(str(uid), {}).get("active")
        user_states.pop(uid, None)
        if not token:
            try: os.unlink(path)
            except: pass
            return await msg.reply("❌ No active token.")
        await msg.reply("📂 Uploading ZIP to repository (may take a while)...")
        ok, result = await extract_and_upload_zip(path, repo_full, token)
        return await msg.reply(result)
    if state.get("action") == "awaiting_zip_upload_admin":
        target_uid = state.get("target_uid")
        repo_full = state.get("full_repo")
        token = user_tokens.get(str(target_uid), {}).get("active")
        user_states.pop(uid, None)
        if not token:
            try: os.unlink(path)
            except: pass
            return await msg.reply("❌ Target user has no active token.")
        await msg.reply(f"📂 Uploading ZIP to `{repo_full}` on behalf of user {target_uid}...")
        ok, result = await extract_and_upload_zip(path, repo_full, token)
        return await msg.reply(result)
    if state.get("action") == "awaiting_zip_upload":
        # generic flow: user wants to upload zip then they will provide owner/repo
        zpath = path
        user_states[uid] = {"action": "awaiting_zip_repo_name", "zip_path": zpath}
        return await msg.reply("✅ ZIP received. Now send the GitHub repo full name where you want to upload (owner/repo).")

# ---------- Text handler for various flows ----------
@app.on_message(filters.private & filters.text & filters.create(not_banned_filter))
async def text_handler(client: Client, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)
    text = msg.text.strip()

    # cancel
    if text.lower() in ("/cancel", "cancel", "❌"):
        user_states.pop(uid, None)
        return await msg.reply("Operation cancelled.")

    # Confirm delete
    if state and state.get("action") == "confirm_delete":
        if text.lower() in ("yes", "y", "confirm"):
            target_uid = state.get("target_uid")
            full_repo = state.get("full_repo")
            user_states.pop(uid, None)
            token = user_tokens.get(str(target_uid), {}).get("active")
            if not token:
                return await msg.reply("❌ Token missing for that user.")
            async with aiohttp.ClientSession() as session:
                st, _ = await gh_request(session, "DELETE", f"https://api.github.com/repos/{full_repo}", token)
                if st == 204:
                    return await msg.reply(f"✅ Repo `{full_repo}` deleted.")
                else:
                    return await msg.reply(f"❌ Failed to delete `{full_repo}`. Status: {st}")
        else:
            user_states.pop(uid, None)
            return await msg.reply("Cancelled deletion.")

    # Set token flow
    if state and state.get("action") == "set_token":
        token = text
        async with aiohttp.ClientSession() as session:
            st, user = await get_user_info(session, token)
            if st != 200:
                user_states.pop(uid, None)
                return await msg.reply("❌ Invalid token or GitHub unreachable. Make sure token has `repo` scope.")
            username = user.get("login", "unknown")
            ukey = str(uid)
            user_tokens.setdefault(ukey, {"tokens": {}, "active": None})
            user_tokens[ukey]["tokens"][token] = {"username": username}
            user_tokens[ukey]["active"] = token
            persist()
            user_states.pop(uid, None)
            return await msg.reply(f"✅ Token saved and set active for `{username}`.")

    # Broadcast
    if state and state.get("action") == "broadcast" and uid in ADMINS:
        content = text
        count = 0
        for suid in list(user_tokens.keys()):
            try:
                await client.send_message(int(suid), content)
                count += 1
            except Exception:
                pass
        user_states.pop(uid, None)
        return await msg.reply(f"📣 Broadcast sent to {count} users.")

    # Exec raw API (admin)
    if state and state.get("action") == "exec_api" and uid in ADMINS:
        # format: METHOD /path (JSON optional next line)
        lines = text.splitlines()
        first = lines[0].strip().split()
        if len(first) < 2:
            user_states.pop(uid, None)
            return await msg.reply("Invalid format. Example: `GET /rate_limit` or `POST /repos/owner/repo/dispatches`")
        method = first[0].upper()
        path = first[1]
        body = None
        if len(lines) > 1:
            try:
                body = json.loads("\n".join(lines[1:]))
            except Exception as e:
                user_states.pop(uid, None)
                return await msg.reply(f"Invalid JSON body: {e}")
        user_states.pop(uid, None)
        # run request using app owner token if provided (we don't have a global token), so ask for user selection if any tokens saved
        # We'll use the admin's active token if they have one; else unauthenticated
        token = user_tokens.get(str(uid), {}).get("active")
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com{path}"
            st, data = await gh_request(session, method, url, token, json=body) if body else await gh_request(session, method, url, token)
            return await msg.reply(f"Status: {st}\n\nResponse:\n`{json.dumps(data, default=str)[:4000]}`")

    # Search user
    if state and state.get("action") == "search_user":
        user_states.pop(uid, None)
        candidate = extract_github_username_from_text(text)
        if not candidate:
            return await msg.reply("❌ Couldn't extract a GitHub username. Send a username like `octocat` or paste `https://github.com/username`.")
        await msg.reply(f"🔎 Searching public repos for `{candidate}` ...")
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{candidate}/repos?per_page=200", None)
            if st != 200:
                return await msg.reply("❌ User not found or GitHub rate limited.")
            if not repos:
                return await msg.reply("⚠️ No public repos found for that user.")
            kb = [[InlineKeyboardButton(r.get("name"), callback_data=f"public_repo:{candidate}:{r.get('full_name')}")] for r in repos]
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await msg.reply(f"📚 Public repos for `{candidate}` — tap to download:", reply_markup=InlineKeyboardMarkup(kb))

    # Search repos by keyword
    if state and state.get("action") == "search_repo":
        user_states.pop(uid, None)
        q = text
        await msg.reply(f"🔎 Searching public repos for `{q}` ... (top 10)")
        async with aiohttp.ClientSession() as session:
            st, data = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={aiohttp.helpers.quote(q)}&per_page=10", None)
            if st != 200:
                return await msg.reply("❌ Search failed or rate-limited.")
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                return await msg.reply("No repos found.")
            kb = []
            for r in items:
                kb.append([InlineKeyboardButton(f"{r.get('full_name')} ⭐{r.get('stargazers_count')}", callback_data=f"public_repo_search:{r.get('full_name')}")])
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await msg.reply("Search results:", reply_markup=InlineKeyboardMarkup(kb))

    # Receiving a zip for the generic flow awaiting_zip_repo_name
    if state and state.get("action") == "awaiting_zip_repo_name":
        repo_full = text
        zip_path = state.get("zip_path")
        token = user_tokens.get(str(uid), {}).get("active")
        user_states.pop(uid, None)
        if not token:
            try: os.unlink(zip_path)
            except: pass
            return await msg.reply("❌ No active token. Set your token first.")
        await msg.reply("📂 Extracting ZIP and uploading to repository (may take some time)...")
        ok, result = await extract_and_upload_zip(zip_path, repo_full, token)
        return await msg.reply(result)

    # Edit file flow
    if state and state.get("action") == "edit_file":
        if state.get("step") == "awaiting_filepath":
            state["filepath"] = text
            state["step"] = "awaiting_filecontent"
            user_states[uid] = state
            return await msg.reply("Now send the file content (plain text).")
        elif state.get("step") == "awaiting_filecontent":
            repo = state.get("repo")
            filepath = state.get("filepath")
            content = text.encode()
            token = None
            if state.get("target_uid"):
                token = user_tokens.get(state.get("target_uid"), {}).get("active")
            else:
                token = user_tokens.get(str(uid), {}).get("active")
            user_states.pop(uid, None)
            if not token:
                return await msg.reply("❌ No active token.")
            async with aiohttp.ClientSession() as session:
                st_repo, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{repo}", token)
                if st_repo != 200:
                    return await msg.reply("❌ Repo inaccessible.")
                branch = repo_info.get("default_branch", "main")
                st_put, put_resp = await put_file_to_repo(session, token, repo, filepath, content, branch, f"Add/update {filepath} via bot")
                if st_put in (200, 201):
                    return await msg.reply("✅ File created/updated.")
                else:
                    return await msg.reply(f"❌ Failed: {st_put} — {put_resp}")

    # If user just pasted a GitHub profile URL spontaneously
    candidate = extract_github_username_from_text(text)
    if candidate:
        # return public repos quick
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{candidate}/repos?per_page=200", None)
            if st != 200:
                return await msg.reply("❌ Couldn't fetch that GitHub user (maybe private or rate-limited).")
            if not repos:
                return await msg.reply("⚠️ No public repos found.")
            kb = [[InlineKeyboardButton(r.get("name"), callback_data=f"public_repo:{candidate}:{r.get('full_name')}")] for r in repos]
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await msg.reply(f"📚 Public repos for `{candidate}` — tap to download:", reply_markup=InlineKeyboardMarkup(kb))

    # fallback
    await msg.reply("I didn't understand. Use /start and the buttons. To search a GitHub user quickly, use the Search GitHub User button or paste a GitHub profile URL.")

# ---------- Public repo download (non-registered users) ----------
@app.on_callback_query(filters.regex("^public_repo:"))
async def public_repo_cb(client: Client, cb: CallbackQuery):
    _, username, full = cb.data.split(":", 2)
    await cb.answer("Downloading public repo archive...", show_alert=False)
    async with aiohttp.ClientSession() as session:
        st, info = await gh_request(session, "GET", f"https://api.github.com/repos/{full}", None)
        default_branch = "main" if st != 200 else info.get("default_branch", "main")
        archive_url = f"https://api.github.com/repos/{full}/zipball/{default_branch}"
        async with session.get(archive_url) as resp:
            if resp.status != 200:
                return await cb.message.edit("❌ Failed to download repository archive (maybe private or rate-limited).")
            data = await resp.read()
            tmp = await write_temp_file(data, suffix=".zip")
            await cb.message.reply_document(document=tmp, caption=f"📦 Archive for `{full}`")
            os.unlink(tmp)
            await cb.message.edit("✅ Download sent.")

@app.on_callback_query(filters.regex("^public_repo_search:"))
async def public_repo_search_cb(client: Client, cb: CallbackQuery):
    full = cb.data.split(":", 1)[1]
    # same as public repo download but with no username
    await cb.answer("Downloading public repo archive...", show_alert=False)
    async with aiohttp.ClientSession() as session:
        st, info = await gh_request(session, "GET", f"https://api.github.com/repos/{full}", None)
        if st != 200:
            return await cb.message.edit("❌ Repo not accessible.")
        default_branch = info.get("default_branch", "main")
        archive_url = f"https://api.github.com/repos/{full}/zipball/{default_branch}"
        async with session.get(archive_url) as resp:
            if resp.status != 200:
                return await cb.message.edit("❌ Failed to download archive.")
            data = await resp.read()
            tmp = await write_temp_file(data, suffix=".zip")
            await cb.message.reply_document(document=tmp, caption=f"📦 Archive for `{full}`")
            os.unlink(tmp)
            await cb.message.edit("✅ Download sent.")

# ---------- Put file to repo helper ----------
async def put_file_to_repo(session: aiohttp.ClientSession, token: str, repo_fullname: str, rel_path: str, content_bytes: bytes, branch: str, message: str):
    url = f"https://api.github.com/repos/{repo_fullname}/contents/{rel_path}"
    encoded = base64.b64encode(content_bytes).decode()
    st_check, check_data = await gh_request(session, "GET", url, token)
    payload = {"message": message, "content": encoded, "branch": branch}
    if st_check == 200 and isinstance(check_data, dict) and "sha" in check_data:
        payload["sha"] = check_data.get("sha")
    st_put, put_data = await gh_request(session, "PUT", url, token, json=payload)
    return st_put, put_data

# ---------- Extract and upload ZIP helper ----------
async def extract_and_upload_zip(zip_path: str, repo_fullname: str, token: str) -> Tuple[bool, str]:
    tmpdir = tempfile.mkdtemp(prefix="zip_extract_")
    try:
        def extract():
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(tmpdir)
        await asyncio.to_thread(extract)

        # flatten single-folder archives
        entries = os.listdir(tmpdir)
        if len(entries) == 1 and os.path.isdir(os.path.join(tmpdir, entries[0])):
            tmpdir = os.path.join(tmpdir, entries[0])

        async with aiohttp.ClientSession() as session:
            st_repo, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{repo_fullname}", token)
            if st_repo != 200:
                return False, "❌ Repository not found or inaccessible (check owner/repo and permissions)."
            branch = repo_info.get("default_branch", "main")
            pushed = 0
            errors = []
            for root, _, files in os.walk(tmpdir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(abs_path, tmpdir).replace("\\", "/")
                    content = await read_file_bytes(abs_path)
                    message = f"Add {rel_path} (uploaded via bot)"
                    st_put, put_resp = await put_file_to_repo(session, token, repo_fullname, rel_path, content, branch, message)
                    if st_put in (200, 201):
                        pushed += 1
                    else:
                        errors.append({"file": rel_path, "status": st_put, "resp": put_resp})
            summary = f"✅ Uploaded {pushed} files to `{repo_fullname}`."
            if errors:
                summary += f"\n⚠️ {len(errors)} files failed. First error: {errors[0]}"
            return True, summary
    except Exception as e:
        return False, f"❌ Failed to process ZIP: {e}"
    finally:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except:
            pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except:
            pass

# ---------- Add / remove collaborator (admin) ----------
@app.on_callback_query(filters.regex("^admin_addcollab:|^admin_delcollab:"))
async def admin_collab_cb(client: Client, cb: CallbackQuery):
    is_add = cb.data.startswith("admin_addcollab:")
    _, target_uid, full_repo = cb.data.split(":", 2)
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Unauthorized", show_alert=True)
    user_states[cb.from_user.id] = {"action": "admin_collab", "add": is_add, "target_uid": target_uid, "repo": full_repo}
    await cb.message.edit(f"Send the GitHub username to {'add as' if is_add else 'remove as'} collaborator on `{full_repo}`.", reply_markup=None)

# ---------- Set repo visibility ----------
@app.on_callback_query(filters.regex("^admin_setvis:"))
async def admin_setvis_cb(client: Client, cb: CallbackQuery):
    _, target_uid, full_repo = cb.data.split(":", 2)
    if cb.from_user.id not in ADMINS:
        return await cb.answer("Unauthorized", show_alert=True)
    user_states[cb.from_user.id] = {"action": "admin_setvis", "target_uid": target_uid, "repo": full_repo}
    await cb.message.edit(f"Send `private` or `public` to set visibility for `{full_repo}`.", reply_markup=None)

# ---------- Mass download / backup user ----------
@app.on_callback_query(filters.regex("^admin_backupuser:"))
async def admin_backupuser_cb(client: Client, cb: CallbackQuery):
    # optional shortcut not wired in menu; admin can trigger via command in future
    await cb.answer("Use /backupuser <telegram_id> to backup a user's repos (admin command).", show_alert=True)

# ---------- Admin commands via text for collaborator, setvis, backup, massdownload ----------
@app.on_message(filters.private & filters.text & filters.create(not_banned_filter))
async def admin_text_actions(client: Client, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)
    text = msg.text.strip()
    if not state:
        return  # nothing to do here; other handler will catch general flows
    # collaborator flow
    if state.get("action") == "admin_collab":
        username = text
        add = state.get("add", True)
        target_uid = state.get("target_uid")
        repo = state.get("repo")
        token = user_tokens.get(target_uid, {}).get("active")
        user_states.pop(uid, None)
        if not token:
            return await msg.reply("❌ Target user has no active token.")
        async with aiohttp.ClientSession() as session:
            if add:
                st, data = await gh_request(session, "PUT", f"https://api.github.com/repos/{repo}/collaborators/{username}", token, json={"permission":"push"})
            else:
                st, data = await gh_request(session, "DELETE", f"https://api.github.com/repos/{repo}/collaborators/{username}", token)
            if st in (201, 204, 200):
                return await msg.reply(f"✅ Success. Status: {st}")
            else:
                return await msg.reply(f"❌ Failed: {st} — {data}")

    # set visibility
    if state.get("action") == "admin_setvis":
        choice = text.lower()
        target_uid = state.get("target_uid")
        repo = state.get("repo")
        token = user_tokens.get(target_uid, {}).get("active")
        user_states.pop(uid, None)
        if not token:
            return await msg.reply("❌ Target user has no active token.")
        if choice not in ("public", "private"):
            return await msg.reply("Send exactly `private` or `public`.")
        is_private = True if choice == "private" else False
        async with aiohttp.ClientSession() as session:
            st, data = await gh_request(session, "PATCH", f"https://api.github.com/repos/{repo}", token, json={"private": is_private})
            if st == 200:
                return await msg.reply(f"✅ Repo visibility updated to `{choice}`.")
            else:
                return await msg.reply(f"❌ Failed: {st} — {data}")

# ---------- Push file (upload) from Telegram doc to repo ----------
@app.on_message(filters.private & filters.document & filters.create(not_banned_filter))
async def pushfile_handler(client: Client, msg: Message):
    # If the user_state expects a pushfile: {action: 'pushfile', repo: 'owner/repo'}
    uid = msg.from_user.id
    state = user_states.get(uid)
    if not state:
        return
    if state.get("action") != "pushfile":
        return
    if not msg.document:
        return await msg.reply("Send a document (file) to upload.")
    repo = state.get("repo")
    path = state.get("path")  # may be None; ask later
    # download file
    fpath = await msg.download()
    if not path:
        # ask for destination path inside repo
        user_states[uid] = {"action": "pushfile_confirm", "repo": repo, "local_path": fpath}
        return await msg.reply("✅ File received. Now send the path inside the repo (e.g. `folder/filename.bin`) where I should create it.")
    # else handled elsewhere

@app.on_message(filters.private & filters.text & filters.create(not_banned_filter))
async def pushfile_confirm(client: Client, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)
    if not state:
        return
    if state.get("action") != "pushfile_confirm":
        return
    repo = state.get("repo")
    local_path = state.get("local_path")
    dest = msg.text.strip()
    token = user_tokens.get(str(uid), {}).get("active")
    user_states.pop(uid, None)
    if not token:
        try: os.unlink(local_path)
        except: pass
        return await msg.reply("❌ No active token.")
    # read file and upload
    try:
        content = await read_file_bytes(local_path)
    except Exception as e:
        return await msg.reply(f"Failed reading file: {e}")
    async with aiohttp.ClientSession() as session:
        st_repo, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{repo}", token)
        if st_repo != 200:
            return await msg.reply("❌ Repo inaccessible.")
        branch = repo_info.get("default_branch", "main")
        st_put, put_resp = await put_file_to_repo(session, token, repo, dest, content, branch, f"Add {dest} via Telegram bot")
        try: os.unlink(local_path)
        except: pass
        if st_put in (200, 201):
            return await msg.reply("✅ File uploaded successfully.")
        else:
            return await msg.reply(f"❌ Upload failed: {st_put} — {put_resp}")

# ---------- Mass download / backup user command (admin) ----------
@app.on_message(filters.command("backupuser") & filters.private)
async def cmd_backupuser(client: Client, msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.reply("Unauthorized.")
    if len(msg.command) < 2:
        return await msg.reply("Usage: /backupuser <telegram_id>")
    target = msg.command[1]
    info = user_tokens.get(target)
    if not info:
        return await msg.reply("User has no tokens saved.")
    token = info.get("active")
    if not token:
        return await msg.reply("User has no active token.")
    await msg.reply("📦 Preparing backup of all user's repos (may take a while)...")
    async with aiohttp.ClientSession() as session:
        st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=500", token)
        if st != 200:
            return await msg.reply("Failed to fetch repos.")
        tmpdir = tempfile.mkdtemp(prefix="backup_")
        try:
            files = []
            for r in repos:
                full = r.get("full_name")
                default_branch = r.get("default_branch", "main")
                archive_url = f"https://api.github.com/repos/{full}/zipball/{default_branch}"
                async with session.get(archive_url, headers={"Authorization": f"token {token}"}) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        fname = os.path.join(tmpdir, f"{full.replace('/', '_')}.zip")
                        await asyncio.to_thread(lambda d=data, p=fname: open(p, "wb").write(d))
                        files.append(fname)
            # create combined zip
            outzip = os.path.join(tempfile.gettempdir(), f"backup_{target}_{int(datetime.datetime.utcnow().timestamp())}.zip")
            with zipfile.ZipFile(outzip, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(f, arcname=os.path.basename(f))
            await msg.reply_document(document=outzip, caption=f"Backup for user {target}")
            try: os.unlink(outzip)
            except: pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- Create Gist (user) ----------
@app.on_message(filters.command("gistcreate") & filters.private)
async def cmd_gistcreate(client: Client, msg: Message):
    # usage: reply with /gistcreate public|secret or just /gistcreate then send content in next message
    uid = msg.from_user.id
    args = msg.command[1:] if len(msg.command) > 1 else []
    public = True
    if args and args[0].lower() in ("secret", "private"):
        public = False
    user_states[uid] = {"action": "gist_create", "public": public}
    await msg.reply("Send the gist content. If you want multiple files, send JSON like `{\"file1.txt\": \"content\", \"file2.py\": \"print(1)\"}`. Otherwise send plain text and I'll create `gist.txt`.")

@app.on_message(filters.private & filters.text & filters.create(not_banned_filter))
async def gist_create_handler(client: Client, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid)
    if not state or state.get("action") != "gist_create":
        return  # other handler will process
    content = msg.text
    public = state.get("public", True)
    user_states.pop(uid, None)
    token = user_tokens.get(str(uid), {}).get("active")
    if not token:
        return await msg.reply("❌ No active token for creating gists. Save a token first.")
    # build gist payload
    try:
        if content.strip().startswith("{") and content.strip().endswith("}"):
            files = json.loads(content)
            payload_files = {k: {"content": v} for k, v in files.items()}
        else:
            payload_files = {"gist.txt": {"content": content}}
    except Exception:
        payload_files = {"gist.txt": {"content": content}}
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "POST", "https://api.github.com/gists", token, json={"public": public, "files": payload_files})
        if st in (201, 200):
            return await msg.reply(f"✅ Gist created: {data.get('html_url')}")
        else:
            return await msg.reply(f"❌ Failed to create gist: {st} — {data}")

# ---------- Trending repos (top weekly by stars) ----------
@app.on_message(filters.command("trending") & filters.private)
async def cmd_trending(client: Client, msg: Message):
    # fetch repos created in last 7 days sorted by stars
    days = 7
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    q = f"created:>{since}"
    await msg.reply(f"🔝 Trending repos since {since} (top 10):")
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={aiohttp.helpers.quote(q)}&sort=stars&order=desc&per_page=10", None)
        if st != 200:
            return await msg.reply("❌ Failed to fetch trending (rate-limited).")
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            return await msg.reply("No trending repos found.")
        kb = []
        text = ""
        for r in items:
            name = r.get("full_name")
            stars = r.get("stargazers_count")
            kb.append([InlineKeyboardButton(f"{name} ⭐{stars}", callback_data=f"public_repo_search:{name}")])
            text += f"{name} — ⭐{stars}\n"
        kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
        await msg.reply(text, reply_markup=InlineKeyboardMarkup(kb))

# ---------- Search repo compare and insights commands ----------
@app.on_message(filters.command("compare") & filters.private)
async def cmd_compare(client: Client, msg: Message):
    if len(msg.command) < 3:
        return await msg.reply("Usage: /compare owner1/repo1 owner2/repo2")
    repo1 = msg.command[1]
    repo2 = msg.command[2]
    async with aiohttp.ClientSession() as session:
        st1, r1 = await gh_request(session, "GET", f"https://api.github.com/repos/{repo1}", None)
        st2, r2 = await gh_request(session, "GET", f"https://api.github.com/repos/{repo2}", None)
        if st1 != 200 or st2 != 200:
            return await msg.reply("One of the repos is not accessible.")
        text = f"Compare `{repo1}` vs `{repo2}`\n\n{repo1} — ⭐{r1.get('stargazers_count')} | Forks: {r1.get('forks_count')}\n{repo2} — ⭐{r2.get('stargazers_count')} | Forks: {r2.get('forks_count')}"
        await msg.reply(text)

# ---------- Repo insights (command) ----------
@app.on_message(filters.command("repoinsights") & filters.private)
async def cmd_repoinsights(client: Client, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Usage: /repoinsights owner/repo")
    repo = msg.command[1]
    async with aiohttp.ClientSession() as session:
        st, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{repo}", None)
        if st != 200:
            return await msg.reply("Repo inaccessible.")
        stc, contribs = await gh_request(session, "GET", f"https://api.github.com/repos/{repo}/contributors?per_page=10", None)
        stl, langs = await gh_request(session, "GET", f"https://api.github.com/repos/{repo}/languages", None)
        text = f"📊 Insights for `{repo}`\nStars: {repo_info.get('stargazers_count')}\nForks: {repo_info.get('forks_count')}\nOpen issues: {repo_info.get('open_issues_count')}\nDefault branch: {repo_info.get('default_branch')}\nLanguages: {', '.join(langs.keys()) if isinstance(langs, dict) else 'N/A'}\nTop contributors:\n"
        if isinstance(contribs, list):
            for c in contribs[:10]:
                text += f"- {c.get('login')} ({c.get('contributions')})\n"
        await msg.reply(text)

# ---------- Fork repo command ----------
@app.on_message(filters.command("forkrepo") & filters.private)
async def cmd_forkrepo(client: Client, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Usage: /forkrepo owner/repo")
    repo = msg.command[1]
    uid = msg.from_user.id
    token = user_tokens.get(str(uid), {}).get("active")
    if not token:
        return await msg.reply("❌ No active token.")
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "POST", f"https://api.github.com/repos/{repo}/forks", token)
        if st in (202, 201):
            return await msg.reply("✅ Fork request submitted.")
        else:
            return await msg.reply(f"❌ Fork failed: {st} — {data}")

# ---------- Random repo ----------
@app.on_message(filters.command("randomrepo") & filters.private)
async def cmd_randomrepo(client: Client, msg: Message):
    # search popular repos and pick random
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "GET", "https://api.github.com/search/repositories?q=stars:>5000&sort=stars&order=desc&per_page=100", None)
        if st != 200:
            return await msg.reply("Failed to fetch repositories.")
        items = data.get("items", [])
        if not items:
            return await msg.reply("No repos found.")
        choice = random.choice(items)
        text = f"Random repo: {choice.get('full_name')}\n⭐ {choice.get('stargazers_count')}\n{choice.get('html_url')}"
        return await msg.reply(text)

# ---------- WhoIs / userinfo ----------
@app.on_message(filters.command("userinfo") & filters.private)
async def cmd_userinfo(client: Client, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Usage: /userinfo username")
    username = msg.command[1]
    async with aiohttp.ClientSession() as session:
        st, data = await gh_request(session, "GET", f"https://api.github.com/users/{username}", None)
        if st != 200:
            return await msg.reply("User not found.")
        text = f"👤 {data.get('login')}\nName: {data.get('name')}\nBio: {data.get('bio')}\nLocation: {data.get('location')}\nPublic repos: {data.get('public_repos')}\nFollowers: {data.get('followers')}  Following: {data.get('following')}\nCreated at: {data.get('created_at')}\nURL: {data.get('html_url')}"
        return await msg.reply(text)

# ---------- GH stats (rate limit) ----------
@app.on_message(filters.command("ghstats") & filters.private)
async def cmd_ghstats(client: Client, msg: Message):
    async with aiohttp.ClientSession() as session:
        st, data = await gh_rate_limit(session, None)
        if st != 200:
            return await msg.reply("Failed to fetch rate limit.")
        core = data.get("resources", {}).get("core", {})
        search = data.get("resources", {}).get("search", {})
        text = f"GitHub Rate Limits\nCore: {core.get('remaining')}/{core.get('limit')} (resets {core.get('reset')})\nSearch: {search.get('remaining')}/{search.get('limit')}"
        return await msg.reply(text)

# ---------- WhoIs style extra command (most-starred repo) ----------
@app.on_message(filters.command("whois") & filters.private)
async def cmd_whois(client: Client, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Usage: /whois username")
    username = msg.command[1]
    async with aiohttp.ClientSession() as session:
        st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{username}/repos?per_page=200", None)
        if st != 200:
            return await msg.reply("User not found.")
        if not repos:
            return await msg.reply("No public repos.")
        top = sorted(repos, key=lambda r: r.get("stargazers_count", 0), reverse=True)[0]
        text = f"{username}'s top repo: {top.get('full_name')} — ⭐{top.get('stargazers_count')}\n{top.get('html_url')}"
        return await msg.reply(text)

# ---------- Misc: ban/unban (admin) ----------
@app.on_message(filters.command("ban") & filters.private)
async def cmd_ban(client: Client, msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.reply("Unauthorized.")
    if len(msg.command) < 2:
        return await msg.reply("Usage: /ban <telegram_id>")
    try:
        target = int(msg.command[1])
        banned_users.add(target)
        persist()
        return await msg.reply(f"Banned {target}.")
    except:
        return await msg.reply("Invalid ID.")

@app.on_message(filters.command("unban") & filters.private)
async def cmd_unban(client: Client, msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.reply("Unauthorized.")
    if len(msg.command) < 2:
        return await msg.reply("Usage: /unban <telegram_id>")
    try:
        target = int(msg.command[1])
        banned_users.discard(target)
        persist()
        return await msg.reply(f"Unbanned {target}.")
    except:
        return await msg.reply("Invalid ID.")

# ---------- Helper: ensure token saving & active switching commands ----------
@app.on_message(filters.command("mytokens") & filters.private)
async def cmd_mytokens(client: Client, msg: Message):
    uid = str(msg.from_user.id)
    info = user_tokens.get(uid)
    if not info:
        return await msg.reply("No tokens saved.")
    text = "Your tokens:\n"
    for i, (tkn, meta) in enumerate(info.get("tokens", {}).items(), start=1):
        active = " (active)" if info.get("active") == tkn else ""
        text += f"{i}. {meta.get('username')}{active}\n"
    await msg.reply(text)

@app.on_message(filters.command("setactive") & filters.private)
async def cmd_setactive(client: Client, msg: Message):
    args = msg.command
    if len(args) < 2:
        return await msg.reply("Usage: /setactive <token_index>")
    idx = int(args[1]) - 1
    uid = str(msg.from_user.id)
    info = user_tokens.get(uid)
    if not info:
        return await msg.reply("No tokens saved.")
    tokens = list(info.get("tokens", {}).keys())
    if idx < 0 or idx >= len(tokens):
        return await msg.reply("Invalid index.")
    info["active"] = tokens[idx]
    persist()
    return await msg.reply("Active token changed.")

# ---------- Final startup ----------
if __name__ == "__main__":
    print("✅ SPILUX GITHUB BOT ONLINE — full-featured")
    app.run()
