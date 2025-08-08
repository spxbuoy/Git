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
import math
import random
from typing import Any, Dict, Optional, Tuple, List
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ---------------- CONFIG ----------------
import os

API_ID = int(os.getenv("API_ID", "20660797"))
API_HASH = os.getenv("API_HASH", "755e5cdf9bade62a75211e7a57f25601")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8448084086:AAGT6-0n3g3OkQDYxxlyXdJESsYmjX9_ISA")
ADMINS = [int(x) for x in os.getenv("ADMINS", "7941175119").split(",") if x.strip()]
DATA_FILE = "data.json"
ITEMS_PER_PAGE = 6
# ----------------------------------------

# ---------- Persistent storage ----------
def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"tokens": {}, "banned": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)

data = load_data()

# ---------- In-memory states ----------
user_states: Dict[int, Dict[str, Any]] = {}
admin_states: Dict[int, Dict[str, Any]] = {}

# ---------- Helpers ----------
def not_banned_filter(_, __, message: Message):
    return (message.from_user and (message.from_user.id not in set(data.get("banned", []))))

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

async def write_temp_file(data: bytes, suffix: str = "") -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    await asyncio.to_thread(lambda: f.write(data))
    f.close()
    return f.name

async def read_file_bytes(path: str) -> bytes:
    return await asyncio.to_thread(lambda: open(path, "rb").read())

# ---------- App ----------
app = Client("github_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- Start / main menu (buttons for everything) ----------
@app.on_message(filters.private & filters.command("start") & filters.create(not_banned_filter))
async def cmd_start(_, msg: Message):
    uid = msg.from_user.id
    # ensure user record
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
    # Admin area button
    if uid in ADMINS:
        kb.keyboard.append([InlineKeyboardButton("👥 Admin Panel", callback_data="admin_panel")])
    await msg.reply("👋 Welcome — press a button to run a feature.", reply_markup=kb)

# ---------- Generic callback for many buttons ----------
@app.on_callback_query(filters.create(lambda _, __, cq: True))
async def cb_menu(_, cq: CallbackQuery):
    data_cb = cq.data or ""
    uid = cq.from_user.id

    # Add token
    if data_cb == "add_token":
        user_states[uid] = {"action": "awaiting_add_token"}
        await cq.message.edit("🔑 Send your GitHub Personal Access Token (PAT). I'll validate and save it under its GitHub username.", reply_markup=None)
        return

    # Switch token menu (buttons listed by saved GitHub username)
    if data_cb == "switch_token_menu":
        rec = data.get("tokens", {}).get(str(uid), {})
        saved = rec.get("tokens", {})
        if not saved:
            return await cq.answer("You have no tokens saved. Add one first.", show_alert=True)
        kb = []
        for tkn, meta in saved.items():
            name = meta.get("username", "unknown")
            active = rec.get("active") == tkn
            label = f"{name}{' (active)' if active else ''}"
            kb.append([InlineKeyboardButton(label, callback_data=f"switchtoken_btn:{name}")])
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="start_back")])
        await cq.message.edit("🔁 Choose a token to activate:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # handle switch token button selection
    if data_cb.startswith("switchtoken_btn:"):
        name = data_cb.split(":",1)[1]
        rec = data.get("tokens", {}).get(str(uid), {})
        found = None
        for tkn, meta in rec.get("tokens", {}).items():
            if meta.get("username") == name:
                found = tkn; break
        if not found:
            return await cq.answer("Token not found.", show_alert=True)
        data["tokens"][str(uid)]["active"] = found
        save_data(data)
        return await cq.message.edit(f"✅ Active token switched to **{name}**.", reply_markup=None)

    # List repos (for logged-in user)
    if data_cb == "list_repos":
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            return await cq.answer("No active token. Add one first.", show_alert=True)
        await cq.message.edit("📚 Fetching your repositories...", reply_markup=None)
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=200", token)
            if st != 200:
                return await cq.message.edit("❌ Failed to fetch repos (token may be invalid).", reply_markup=None)
            if not repos:
                return await cq.message.edit("⚠️ No repositories found.", reply_markup=None)
            kb = [[InlineKeyboardButton(r.get("name"), callback_data=f"myrepo:{r.get('full_name')}")] for r in repos]
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await cq.message.edit("📚 Your repos:", reply_markup=InlineKeyboardMarkup(kb))

    # Upload ZIP (generic)
    if data_cb == "upload_zip_repo":
        user_states[uid] = {"action": "awaiting_zip_upload"}
        return await cq.message.edit("📤 Please send the ZIP file now.", reply_markup=None)

    # Search user
    if data_cb == "search_user":
        user_states[uid] = {"action":"awaiting_search_user"}
        return await cq.message.edit("🔎 Send a GitHub username or profile URL (e.g. https://github.com/username).", reply_markup=None)

    # Search repo keyword
    if data_cb == "search_repo":
        user_states[uid] = {"action":"awaiting_search_repo"}
        return await cq.message.edit("🔎 Send a keyword to search public repositories (e.g. telegram bot).", reply_markup=None)

    # Trending
    if data_cb == "trending":
        await cq.answer("Fetching trending...", show_alert=False)
        await handle_trending(cq)
        return

    # Random repo
    if data_cb == "random_repo":
        await cq.answer("Finding a random popular repo...", show_alert=False)
        await handle_random_repo(cq)
        return

    # Gist create
    if data_cb == "gist_create":
        user_states[uid] = {"action":"gist_create","public":True}
        return await cq.message.edit("🧾 Send gist content (plain text or JSON for multiple files).", reply_markup=None)

    # GH stats
    if data_cb == "ghstats":
        await cq.answer("Fetching GitHub rate limits...", show_alert=False)
        await handle_ghstats(cq)
        return

    # Back to start
    if data_cb == "start_back":
        return await cq.message.edit("Back to main. Use /start to open the menu.", reply_markup=None)

    # Admin panel
    if data_cb == "admin_panel":
        if uid not in ADMINS:
            return await cq.answer("Unauthorized", show_alert=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 List Users", callback_data="admin_list_users")],
            [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
            [InlineKeyboardButton("🧰 Exec API", callback_data="admin_exec_api")],
            [InlineKeyboardButton("◀️ Back", callback_data="start_back")]
        ])
        return await cq.message.edit("🛡 Admin Panel — choose:", reply_markup=kb)

    # Admin: broadcast
    if data_cb == "admin_broadcast":
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        admin_states[uid] = {"action":"broadcast"}
        return await cq.message.edit("📢 Send the message to broadcast to all known users.", reply_markup=None)

    # Admin: exec api
    if data_cb == "admin_exec_api":
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        admin_states[uid] = {"action":"exec_api"}
        return await cq.message.edit("🛠 Send a raw API call like `GET /rate_limit` or `POST /repos/owner/repo/dispatches` (JSON body on next line).", reply_markup=None)

    # Admin: list users (start page 0)
    if data_cb == "admin_list_users":
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        return await show_admin_users_page(cq.message, page=0)

    # unhandled here; other callbacks processed in cb_router
    await cq.answer()

# ---------- Text handler (handles flows like add token, searches, admin flows, gist create, etc.) ----------
@app.on_message(filters.private & filters.text & filters.create(not_banned_filter))
async def text_handler(_, msg: Message):
    uid = msg.from_user.id
    txt = msg.text.strip()
    # admin states
    astate = admin_states.get(uid)
    ustate = user_states.get(uid)

    # Admin: broadcast
    if astate and astate.get("action") == "broadcast" and uid in ADMINS:
        content = txt
        count = 0
        for suid in list(data.get("tokens", {}).keys()):
            try:
                await app.send_message(int(suid), content)
                count += 1
            except:
                pass
        admin_states.pop(uid, None)
        return await msg.reply(f"📣 Broadcast sent to {count} users.")

    # Admin: exec api
    if astate and astate.get("action") == "exec_api" and uid in ADMINS:
        lines = txt.splitlines()
        first = lines[0].strip().split()
        if len(first) < 2:
            admin_states.pop(uid, None)
            return await msg.reply("Invalid format. Example: `GET /rate_limit`")
        method = first[0].upper(); path = first[1]; body = None
        if len(lines) > 1:
            try: body = json.loads("\n".join(lines[1:]))
            except Exception as e:
                admin_states.pop(uid, None); return await msg.reply(f"Invalid JSON body: {e}")
        admin_states.pop(uid, None)
        token = data.get("tokens", {}).get(str(uid), {}).get("active")
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com{path}"
            st, d = await gh_request(session, method, url, token, json=body) if body else await gh_request(session, method, url, token)
            return await msg.reply(f"Status: {st}\n`{json.dumps(d, default=str)[:3000]}`")

    # Add token flow
    if ustate and ustate.get("action") == "awaiting_add_token":
        token = txt
        async with aiohttp.ClientSession() as session:
            st, user = await gh_request(session, "GET", "https://api.github.com/user", token)
            if st != 200:
                user_states.pop(uid, None)
                return await msg.reply("❌ Invalid token or GitHub unreachable. Ensure token has `repo` scope.")
            gh_login = user.get("login", "unknown")
            key = str(uid)
            data.setdefault("tokens", {})
            data["tokens"].setdefault(key, {"first_name": msg.from_user.first_name or "", "tokens": {}, "active": None})
            data["tokens"][key]["tokens"][token] = {"username": gh_login}
            if not data["tokens"][key].get("active"):
                data["tokens"][key]["active"] = token
            save_data(data)
            user_states.pop(uid, None)
            return await msg.reply(f"✅ Token saved under GitHub username `{gh_login}`. Use Switch Token to change active.")

    # Search user flow
    if ustate and ustate.get("action") == "awaiting_search_user":
        candidate = extract_github_username_from_text(txt)
        user_states.pop(uid, None)
        if not candidate:
            return await msg.reply("❌ Couldn't parse username.")
        await msg.reply(f"🔎 Fetching public repos for `{candidate}`...")
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{candidate}/repos?per_page=200", None)
            if st != 200:
                return await msg.reply("❌ User not found or rate-limited.")
            if not repos:
                return await msg.reply("⚠️ No public repos found.")
            kb = [[InlineKeyboardButton(r.get("name"), callback_data=f"public_repo:{r.get('full_name')}")] for r in repos]
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await msg.reply(f"📚 Public repos for `{candidate}` — tap to download:", reply_markup=InlineKeyboardMarkup(kb))

    # Search repo by keyword flow
    if ustate and ustate.get("action") == "awaiting_search_repo":
        q = txt
        user_states.pop(uid, None)
        await msg.reply(f"🔎 Searching public repos for `{q}` (top 10)...")
        async with aiohttp.ClientSession() as session:
            st, res = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={aiohttp.helpers.quote(q)}&per_page=10", None)
            if st != 200:
                return await msg.reply("❌ Search failed or rate-limited.")
            items = res.get("items", []) if isinstance(res, dict) else []
            if not items:
                return await msg.reply("No repos found.")
            kb = []
            for r in items:
                kb.append([InlineKeyboardButton(f"{r.get('full_name')} ⭐{r.get('stargazers_count')}", callback_data=f"public_repo_search:{r.get('full_name')}")])
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
            return await msg.reply("Search results:", reply_markup=InlineKeyboardMarkup(kb))

    # Zip upload: waiting repo name after uploading zip
    if ustate and ustate.get("action") == "awaiting_zip_repo":
        repo_full = txt
        zip_path = ustate.get("zip_path")
        token = data.get("tokens", {}).get(str(uid), {}).get("active")
        user_states.pop(uid, None)
        if not token:
            try: os.unlink(zip_path)
            except: pass
            return await msg.reply("❌ No active token. Set one first.")
        await msg.reply("📂 Extracting ZIP and uploading to GitHub (may take a while)...")
        ok, result = await extract_and_upload_zip(zip_path, repo_full, token)
        return await msg.reply(result)

    # Admin: set token for user flow
    if astate and astate.get("action") == "set_token_for_user" and uid in ADMINS:
        token = txt
        target = astate.get("target")
        async with aiohttp.ClientSession() as session:
            st, user = await gh_request(session, "GET", "https://api.github.com/user", token)
            if st != 200:
                admin_states.pop(uid, None)
                return await msg.reply("❌ Invalid token.")
            gh_login = user.get("login")
            data.setdefault("tokens", {})
            data["tokens"].setdefault(str(target), {"first_name": None, "tokens": {}, "active": None})
            data["tokens"][str(target)]["tokens"][token] = {"username": gh_login}
            data["tokens"][str(target)]["active"] = token
            save_data(data)
            admin_states.pop(uid, None)
            return await msg.reply(f"✅ Token set for user {target} ({gh_login}).")

    # Gist create continuation
    if ustate and ustate.get("action") == "gist_create":
        content = txt
        public = ustate.get("public", True)
        user_states.pop(uid, None)
        token = data.get("tokens", {}).get(str(uid), {}).get("active")
        if not token:
            return await msg.reply("❌ No active token for creating gists.")
        try:
            if content.strip().startswith("{") and content.strip().endswith("}"):
                files = json.loads(content)
                payload_files = {k: {"content": v} for k, v in files.items()}
            else:
                payload_files = {"gist.txt": {"content": content}}
        except:
            payload_files = {"gist.txt": {"content": content}}
        async with aiohttp.ClientSession() as session:
            st, d = await gh_request(session, "POST", "https://api.github.com/gists", token, json={"public": public, "files": payload_files})
            if st in (200,201):
                return await msg.reply(f"✅ Gist created: {d.get('html_url')}")
            else:
                return await msg.reply(f"❌ Failed to create gist: {st} — {d}")

    # confirm_delete flow
    if ustate and ustate.get("action") == "confirm_delete":
        answer = txt.lower()
        if answer in ("yes","y","confirm"):
            target_uid = ustate.get("target_uid")
            full_repo = ustate.get("full_repo")
            user_states.pop(uid, None)
            token = data.get("tokens", {}).get(str(target_uid), {}).get("active")
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

    # fallback message
    return await msg.reply("I didn't understand. Use /start and press buttons (or send a command).")

# ---------- Document handler for ZIP uploads ----------
@app.on_message(filters.private & filters.document & filters.create(not_banned_filter))
async def zip_document_handler(_, msg: Message):
    uid = msg.from_user.id
    st = user_states.get(uid)
    if not st:
        return
    if st.get("action") == "awaiting_zip_upload":
        if not msg.document.file_name.lower().endswith(".zip"):
            return await msg.reply("❌ Please send a ZIP (.zip) file.")
        path = await msg.download()
        user_states[uid] = {"action":"awaiting_zip_repo", "zip_path": path}
        return await msg.reply("✅ ZIP received. Now send the GitHub repo full name where to upload (owner/repo).")
    if st.get("action") == "awaiting_zip_upload_for_repo":
        # other flows if implemented
        return

# ---------- callback router for repo & admin actions (buttons inside lists) ----------
@app.on_callback_query(filters.create(lambda _, __, cq: True))
async def cb_router(_, cq: CallbackQuery):
    data_cb = cq.data or ""
    uid = cq.from_user.id

    # Public repo download
    if data_cb.startswith("public_repo:") or data_cb.startswith("public_repo_search:"):
        full = data_cb.split(":",1)[1]
        await cq.answer("Downloading public repo archive...", show_alert=False)
        async with aiohttp.ClientSession() as session:
            st, repo = await gh_request(session, "GET", f"https://api.github.com/repos/{full}", None)
            if st != 200:
                return await cq.message.edit("❌ Repo not found or inaccessible.")
            branch = repo.get("default_branch","main")
            archive_url = f"https://api.github.com/repos/{full}/zipball/{branch}"
            async with session.get(archive_url) as resp:
                if resp.status != 200:
                    return await cq.message.edit("❌ Failed to download repository archive.")
                data_bin = await resp.read()
                tmp = await write_temp_file(data_bin, suffix=".zip")
                await cq.message.reply_document(document=tmp, caption=f"📦 Archive for `{full}`")
                try: os.unlink(tmp)
                except: pass
                return await cq.message.edit("✅ Download sent.")

    # My repo actions
    if data_cb.startswith("myrepo:"):
        full = data_cb.split(":",1)[1]
        token = data.get("tokens", {}).get(str(uid), {}).get("active")
        if not token:
            return await cq.message.edit("❌ No active token.")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Download ZIP", callback_data=f"download_repo:{full}")],
            [InlineKeyboardButton("🗑 Delete Repo", callback_data=f"delete_repo:{full}")],
            [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data=f"upload_zip_to_repo:{full}")],
            [InlineKeyboardButton("📝 Edit/Create File", callback_data=f"editfile_repo:{full}")],
            [InlineKeyboardButton("🔁 Fork", callback_data=f"forkrepo:{full}")],
            [InlineKeyboardButton("📊 Insights", callback_data=f"repo_insights:{full}")],
            [InlineKeyboardButton("◀️ Back", callback_data="start_back")]
        ])
        return await cq.message.edit(f"Repo `{full}` — choose an action:", reply_markup=kb)

    # Download repo (user or admin)
    if data_cb.startswith("download_repo:") or data_cb.startswith("admin_download:"):
        if data_cb.startswith("admin_download:"):
            _, target_uid, full = data_cb.split(":",2)
            token = data.get("tokens", {}).get(target_uid, {}).get("active")
        else:
            full = data_cb.split(":",1)[1]
            token = data.get("tokens", {}).get(str(uid), {}).get("active")
        if not token:
            return await cq.message.edit("❌ No token available.")
        await cq.answer("Preparing download...", show_alert=False)
        async with aiohttp.ClientSession() as session:
            st, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{full}", token)
            if st != 200:
                return await cq.message.edit("❌ Repo not found.")
            branch = repo_info.get("default_branch","main")
            archive_url = f"https://api.github.com/repos/{full}/zipball/{branch}"
            headers = {"Authorization": f"token {token}"}
            async with session.get(archive_url, headers=headers) as resp:
                if resp.status != 200:
                    return await cq.message.edit("❌ Failed to download archive.")
                data_bin = await resp.read()
                tmp = await write_temp_file(data_bin, suffix=".zip")
                await cq.message.reply_document(document=tmp, caption=f"📦 Archive for `{full}`")
                try: os.unlink(tmp)
                except: pass
                return await cq.message.edit("✅ Download sent.")

    # Delete repo (ask for confirmation)
    if data_cb.startswith("delete_repo:") or data_cb.startswith("admin_delete:"):
        if data_cb.startswith("admin_delete:"):
            _, target_uid, full = data_cb.split(":",2)
        else:
            full = data_cb.split(":",1)[1]
            target_uid = str(uid)
        user_states[uid] = {"action":"confirm_delete","target_uid":target_uid,"full_repo":full}
        return await cq.message.edit(f"⚠️ Are you sure you want to delete `{full}`? Send `yes` to confirm or `no` to cancel.", reply_markup=None)

    # Edit/Create file flow: prompt path then content
    if data_cb.startswith("editfile_repo:") or data_cb.startswith("admin_editfile:"):
        if data_cb.startswith("admin_editfile:"):
            _, target_uid, repo_full = data_cb.split(":",2)
            admin_states[uid] = {"action":"edit_file","repo":repo_full,"target_uid":target_uid,"step":"awaiting_filepath"}
            return await cq.message.edit(f"📝 Admin: send file path (e.g., folder/file.txt) to create/edit in `{repo_full}`.", reply_markup=None)
        else:
            repo_full = data_cb.split(":",1)[1]
            user_states[uid] = {"action":"edit_file","repo":repo_full,"step":"awaiting_filepath"}
            return await cq.message.edit(f"📝 Send file path (e.g., folder/file.txt) to create/edit in `{repo_full}`.", reply_markup=None)

    # Upload ZIP into repo (from repo menu)
    if data_cb.startswith("upload_zip_to_repo:") or data_cb.startswith("admin_uploadzip:"):
        if data_cb.startswith("admin_uploadzip:"):
            _, target_uid, full_repo = data_cb.split(":",2)
            if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
            admin_states[uid] = {"action":"awaiting_zip_upload_admin","target_uid":target_uid,"full_repo":full_repo}
            return await cq.message.edit(f"📤 Admin: send ZIP to upload into `{full_repo}` (private chat).", reply_markup=None)
        else:
            full_repo = data_cb.split(":",1)[1]
            user_states[uid] = {"action":"awaiting_zip_upload_for_repo","full_repo":full_repo}
            return await cq.message.edit(f"📤 Send ZIP to upload into `{full_repo}` (private chat).", reply_markup=None)

    # Fork
    if data_cb.startswith("forkrepo:"):
        full = data_cb.split(":",1)[1]
        token = data.get("tokens", {}).get(str(uid), {}).get("active")
        if not token: return await cq.message.edit("❌ No active token.")
        await cq.answer("Forking...", show_alert=False)
        async with aiohttp.ClientSession() as session:
            st, d = await gh_request(session, "POST", f"https://api.github.com/repos/{full}/forks", token)
            if st in (201,202): return await cq.message.edit("✅ Fork request submitted.")
            else: return await cq.message.edit(f"❌ Fork failed: {st} — {d}")

    # Repo insights (handled earlier)
    # Admin: view user's repos
    if data_cb.startswith("admin_view:"):
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        target = data_cb.split(":",1)[1]
        rec = data.get("tokens", {}).get(target)
        if not rec: return await cq.message.edit("❌ User has no saved tokens.")
        token = rec.get("active")
        if not token: return await cq.message.edit("❌ User has no active token.")
        await cq.message.edit("📚 Fetching user's repos...")
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=200", token)
            if st != 200: return await cq.message.edit("❌ Failed to fetch user's repos (token invalid?).")
            kb = [[InlineKeyboardButton(r.get("name"), callback_data=f"admin_repo:{target}:{r.get('full_name')}")] for r in repos]
            kb.append([InlineKeyboardButton("◀️ Back", callback_data="admin_list_users")])
            return await cq.message.edit("User repos:", reply_markup=InlineKeyboardMarkup(kb))

    # Admin repo actions (menu)
    if data_cb.startswith("admin_repo:"):
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        _, target, full = data_cb.split(":",2)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Download ZIP", callback_data=f"admin_download:{target}:{full}")],
            [InlineKeyboardButton("🗑 Delete Repo", callback_data=f"admin_delete:{target}:{full}")],
            [InlineKeyboardButton("📤 Upload ZIP", callback_data=f"admin_uploadzip:{target}:{full}")],
            [InlineKeyboardButton("📝 Edit/Create File", callback_data=f"admin_editfile:{target}:{full}")],
            [InlineKeyboardButton("◀️ Back", callback_data=f"admin_view:{target}")]
        ])
        return await cq.message.edit(f"Admin actions for `{full}`:", reply_markup=kb)

    # Admin download handled earlier (admin_download:)
    # Admin delete confirmation
    if data_cb.startswith("admin_delete:"):
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        _, target, full = data_cb.split(":",2)
        admin_states[uid] = {"action":"confirm_admin_delete","target":target,"full":full}
        return await cq.message.edit(f"⚠️ Are you sure you want to delete `{full}` on behalf of {target}? Send `yes` to confirm.", reply_markup=None)

    # Admin switch token (prompt)
    if data_cb.startswith("admin_switch:"):
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        target = data_cb.split(":",1)[1]
        admin_states[uid] = {"action":"set_token_for_user","target":target}
        return await cq.message.edit(f"Send the new GitHub token to set for user {target}.", reply_markup=None)

    # Admin ban user
    if data_cb.startswith("admin_ban:"):
        if uid not in ADMINS: return await cq.answer("Unauthorized", show_alert=True)
        target = data_cb.split(":",1)[1]
        banned = set(data.get("banned", []))
        banned.add(int(target))
        data["banned"] = list(banned)
        save_data(data)
        return await cq.message.edit(f"✅ User {target} banned.")

    # Admin pagination of user list
    if data_cb.startswith("page_admin_users:"):
        page = int(data_cb.split(":",1)[1])
        return await show_admin_users_page(cq.message, page)

    await cq.answer()

# ---------- Admin: show paginated users ----------
async def show_admin_users_page(message_obj, page: int = 0):
    caller = message_obj.from_user.id
    if caller not in ADMINS:
        return await message_obj.reply("Unauthorized.")
    users_map = data.get("tokens", {})
    items = list(users_map.items())  # [(tgid, rec)]
    total = len(items)
    total_pages = max(1, math.ceil(total / ITEMS_PER_PAGE))
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1
    start = page * ITEMS_PER_PAGE
    chunk = items[start:start+ITEMS_PER_PAGE]
    text = f"👥 Registered users (Page {page+1}/{total_pages})\n\n"
    kb = []
    for tgid, rec in chunk:
        first = rec.get("first_name") or ""
        gh = None
        for tkn, meta in rec.get("tokens", {}).items():
            gh = meta.get("username"); break
        ghlabel = gh or "No GitHub linked"
        text += f"**{first}** — `{tgid}`\n🌐 {ghlabel}\n\n"
        kb.append([InlineKeyboardButton("🔍 View Repos", callback_data=f"admin_view:{tgid}"),
                   InlineKeyboardButton("🔁 Switch Token", callback_data=f"admin_switch:{tgid}"),
                   InlineKeyboardButton("⛔ Ban", callback_data=f"admin_ban:{tgid}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"page_admin_users:{page-1}"))
    if page < total_pages - 1: nav.append(InlineKeyboardButton("Next ➡", callback_data=f"page_admin_users:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
    try:
        await message_obj.edit(text, reply_markup=InlineKeyboardMarkup(kb))
    except:
        await message_obj.reply(text, reply_markup=InlineKeyboardMarkup(kb))

# ---------- ZIP extract & upload helper ----------
async def put_file_to_repo(session: aiohttp.ClientSession, token: str, repo_fullname: str, rel_path: str, content: bytes, branch: str, message: str):
    url = f"https://api.github.com/repos/{repo_fullname}/contents/{rel_path}"
    encoded = base64.b64encode(content).decode()
    st_check, check_data = await gh_request(session, "GET", url, token)
    payload = {"message": message, "content": encoded, "branch": branch}
    if st_check == 200 and isinstance(check_data, dict) and "sha" in check_data:
        payload["sha"] = check_data.get("sha")
    st_put, put_data = await gh_request(session, "PUT", url, token, json=payload)
    return st_put, put_data

async def extract_and_upload_zip(zip_path: str, repo_fullname: str, token: str) -> Tuple[bool,str]:
    tmpdir = tempfile.mkdtemp(prefix="zip_extract_")
    try:
        def extract():
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(tmpdir)
        await asyncio.to_thread(extract)
        entries = os.listdir(tmpdir)
        root_dir = tmpdir
        if len(entries) == 1 and os.path.isdir(os.path.join(tmpdir, entries[0])):
            root_dir = os.path.join(tmpdir, entries[0])
        pushed = 0; errors = []
        async with aiohttp.ClientSession() as session:
            st_repo, repo_info = await gh_request(session, "GET", f"https://api.github.com/repos/{repo_fullname}", token)
            if st_repo != 200:
                return False, "❌ Repo not found or inaccessible (check owner/repo and permissions)."
            branch = repo_info.get("default_branch", "main")
            for root, _, files in os.walk(root_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(abs_path, root_dir).replace("\\","/")
                    content = await read_file_bytes(abs_path)
                    st_put, put_resp = await put_file_to_repo(session, token, repo_fullname, rel_path, content, branch, f"Add {rel_path} (upload via bot)")
                    if st_put in (200,201):
                        pushed += 1
                    else:
                        errors.append({"file": rel_path, "status": st_put})
        summary = f"✅ Uploaded {pushed} files to `{repo_fullname}`."
        if errors:
            summary += f"\n⚠️ {len(errors)} files failed. First error: {errors[0]}"
        return True, summary
    except Exception as e:
        return False, f"❌ Failed to process ZIP: {e}"
    finally:
        try: os.remove(zip_path)
        except: pass
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except: pass

# ---------- Extra handlers for trending/random/ghstats used by callbacks earlier ----------
async def handle_trending(cq: CallbackQuery):
    await cq.message.edit("🔝 Fetching trending repos...")
    days = 7
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    q = f"created:>{since}"
    async with aiohttp.ClientSession() as session:
        st, data_resp = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={aiohttp.helpers.quote(q)}&sort=stars&order=desc&per_page=10", None)
        if st != 200:
            return await cq.message.edit("❌ Failed to fetch trending (rate-limited).")
        items = data_resp.get("items", []) if isinstance(data_resp, dict) else []
        if not items:
            return await cq.message.edit("No trending repos found.")
        kb = []
        text = ""
        for r in items:
            name = r.get("full_name"); stars = r.get("stargazers_count")
            kb.append([InlineKeyboardButton(f"{name} ⭐{stars}", callback_data=f"public_repo_search:{name}")])
            text += f"{name} — ⭐{stars}\n"
        kb.append([InlineKeyboardButton("◀️ Back", callback_data="start_back")])
        return await cq.message.edit(text, reply_markup=InlineKeyboardMarkup(kb))

async def handle_random_repo(cq: CallbackQuery):
    await cq.message.edit("🎲 Selecting a random popular repo...")
    async with aiohttp.ClientSession() as session:
        st, data_resp = await gh_request(session, "GET", "https://api.github.com/search/repositories?q=stars:>5000&sort=stars&order=desc&per_page=100", None)
        if st != 200:
            return await cq.message.edit("❌ Failed to fetch repositories.")
        items = data_resp.get("items", []) if isinstance(data_resp, dict) else []
        if not items:
            return await cq.message.edit("No repos found.")
        choice = random.choice(items)
        text = f"Random repo: {choice.get('full_name')}\n⭐ {choice.get('stargazers_count')}\n{choice.get('html_url')}"
        return await cq.message.edit(text)

async def handle_ghstats(cq: CallbackQuery):
    async with aiohttp.ClientSession() as session:
        st, d = await gh_request(session, "GET", "https://api.github.com/rate_limit", None)
        if st != 200:
            return await cq.message.edit("Failed to fetch rate limit.")
        core = d.get("resources", {}).get("core", {})
        search = d.get("resources", {}).get("search", {})
        text = f"GitHub Rate Limits\nCore: {core.get('remaining')}/{core.get('limit')} (resets {core.get('reset')})\nSearch: {search.get('remaining')}/{search.get('limit')}"
        return await cq.message.edit(text)

# ---------- Startup ----------
if __name__ == "__main__":
    print("✅ SPILUX GITHUB BOT — main.py starting")
    app.run()
