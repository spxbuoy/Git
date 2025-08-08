import os
import re
import json
import aiohttp
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ------------- CONFIG -------------
API_ID = int(os.getenv("API_ID", "20660797"))
API_HASH = os.getenv("API_HASH", "755e5cdf9bade62a75211e7a57f25601")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8448084086:AAGT6-0n3g3OkQDYxxlyXdJESsYmjX9_ISA")
ADMINS = [int(x) for x in os.getenv("ADMINS", "7941175119").split(",") if x.strip()]
DATA_FILE = "data.json"
ITEMS_PER_PAGE = 6
# ----------------------------------

# Load and save data helpers
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"tokens": {}, "banned": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

data = load_data()

# States for users/admins awaiting input
user_states = {}
admin_states = {}

# Filter to ignore banned users
def not_banned_filter(_, __, message: Message):
    return message.from_user and (message.from_user.id not in set(data.get("banned", [])))

# Helper: extract github username from input
def extract_github_username(text: str):
    if not text:
        return None
    m = re.search(r"github\.com/([A-Za-z0-9\-_.]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_.]{1,39}", text.strip()):
        return text.strip()
    return None

# Helper: build back button keyboard
def back_button(text="◀️ Back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data="start")]])

# Helper: paginated keyboard builder
def paginate_buttons(items, prefix, page=0, per_page=ITEMS_PER_PAGE):
    max_page = (len(items) - 1) // per_page
    page = max(0, min(page, max_page))
    buttons = []
    for item in items[page*per_page:(page+1)*per_page]:
        buttons.append([InlineKeyboardButton(item["label"], callback_data=f"{prefix}:{item['data']}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_page:{page-1}"))
    if page < max_page:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_page:{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="start")])
    return InlineKeyboardMarkup(buttons)

# GitHub API request wrapper
async def gh_request(session, method, url, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"token {token}"
    headers["Accept"] = "application/vnd.github.v3+json"
    headers["User-Agent"] = "Spilux-GitHub-Bot"
    async with session.request(method, url, headers=headers, **kwargs) as resp:
        try:
            return resp.status, await resp.json()
        except:
            return resp.status, await resp.text()

app = Client("github_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --------------------------------
# MAIN MENU
# --------------------------------
@app.on_message(filters.private & filters.command("start") & filters.create(not_banned_filter))
async def start_menu(_, msg: Message):
    uid = msg.from_user.id
    data.setdefault("tokens", {})
    rec = data["tokens"].setdefault(str(uid), {"first_name": msg.from_user.first_name or "", "tokens": {}, "active": None})
    rec.setdefault("first_name", msg.from_user.first_name or "")
    save_data(data)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Add Token", callback_data="token_add")],
        [InlineKeyboardButton("🔁 Switch Token", callback_data="token_switch_list")],
        [InlineKeyboardButton("🗑 Remove Token", callback_data="token_remove_list")],
        [InlineKeyboardButton("📂 My Repos", callback_data="myrepos_page:0")],
        [InlineKeyboardButton("📤 Upload ZIP to Repo", callback_data="upload_zip")],
        [InlineKeyboardButton("🔍 Search GitHub User", callback_data="search_user")],
        [InlineKeyboardButton("🔎 Search Repos (keyword)", callback_data="search_repo")],
        [InlineKeyboardButton("📈 Trending Repos", callback_data="trending")],
        [InlineKeyboardButton("🎲 Random Repo", callback_data="random_repo")],
        [InlineKeyboardButton("🧾 Create Gist", callback_data="gist_create")],
        [InlineKeyboardButton("⭐ Star a Repo", callback_data="star_repo_prompt")],
        [InlineKeyboardButton("📊 GitHub API Stats", callback_data="gh_stats")]
    ])
    if uid in ADMINS:
        kb.keyboard.append([InlineKeyboardButton("👥 Admin Panel", callback_data="admin_panel")])

    await msg.reply("👋 Welcome to Spilux GitHub Bot! Select an option:", reply_markup=kb)

# --------------------------------
# CALLBACK QUERY HANDLER
# --------------------------------
@app.on_callback_query()
async def cb_handler(_, cq: CallbackQuery):
    data_cb = cq.data or ""
    uid = cq.from_user.id

    # Route callback data
    if data_cb == "start":
        await start_menu(_, cq.message)
        await cq.answer()
        return

    if data_cb == "token_add":
        user_states[uid] = {"action": "await_add_token"}
        await cq.message.edit("🔑 Please send your GitHub Personal Access Token (PAT).", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "token_switch_list":
        rec = data.get("tokens", {}).get(str(uid), {})
        tokens = rec.get("tokens", {})
        if not tokens:
            await cq.answer("You have no saved tokens.", show_alert=True)
            return
        items = [{"label": f"{meta.get('username', 'unknown')}{' (active)' if rec.get('active')==tkn else ''}", "data": tkn} for tkn, meta in tokens.items()]
        kb = paginate_buttons(items, "token_switch", 0)
        await cq.message.edit("🔁 Select a token to activate:", reply_markup=kb)
        await cq.answer()
        return

    if data_cb.startswith("token_switch:"):
        tkn = data_cb.split(":", 1)[1]
        rec = data.get("tokens", {}).get(str(uid), {})
        if tkn not in rec.get("tokens", {}):
            await cq.answer("Token not found.", show_alert=True)
            return
        data["tokens"][str(uid)]["active"] = tkn
        save_data(data)
        await cq.message.edit("✅ Active token switched.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb.startswith("token_switch_page:"):
        page = int(data_cb.split(":")[1])
        rec = data.get("tokens", {}).get(str(uid), {})
        tokens = rec.get("tokens", {})
        items = [{"label": f"{meta.get('username', 'unknown')}{' (active)' if rec.get('active')==tkn else ''}", "data": tkn} for tkn, meta in tokens.items()]
        kb = paginate_buttons(items, "token_switch", page)
        await cq.message.edit("🔁 Select a token to activate:", reply_markup=kb)
        await cq.answer()
        return

    if data_cb == "token_remove_list":
        rec = data.get("tokens", {}).get(str(uid), {})
        tokens = rec.get("tokens", {})
        if not tokens:
            await cq.answer("You have no saved tokens.", show_alert=True)
            return
        items = [{"label": meta.get("username", "unknown"), "data": tkn} for tkn, meta in tokens.items()]
        kb = paginate_buttons(items, "token_remove", 0)
        await cq.message.edit("🗑 Select a token to remove:", reply_markup=kb)
        await cq.answer()
        return

    if data_cb.startswith("token_remove:"):
        tkn = data_cb.split(":", 1)[1]
        rec = data.get("tokens", {}).get(str(uid), {})
        if tkn not in rec.get("tokens", {}):
            await cq.answer("Token not found.", show_alert=True)
            return
        rec["tokens"].pop(tkn)
        if rec.get("active") == tkn:
            rec["active"] = next(iter(rec["tokens"]), None)
        save_data(data)
        await cq.message.edit("✅ Token removed.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb.startswith("token_remove_page:"):
        page = int(data_cb.split(":")[1])
        rec = data.get("tokens", {}).get(str(uid), {})
        tokens = rec.get("tokens", {})
        items = [{"label": meta.get("username", "unknown"), "data": tkn} for tkn, meta in tokens.items()]
        kb = paginate_buttons(items, "token_remove", page)
        await cq.message.edit("🗑 Select a token to remove:", reply_markup=kb)
        await cq.answer()
        return

    if data_cb.startswith("myrepos_page:"):
        page = int(data_cb.split(":")[1])
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await cq.answer("No active token. Add one first.", show_alert=True)
            return
        await cq.message.edit("📚 Fetching your repositories...", reply_markup=None)
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=100", token)
            if st != 200:
                await cq.message.edit("❌ Failed to fetch repos. Token may be invalid.", reply_markup=back_button())
                return
            if not repos:
                await cq.message.edit("⚠️ No repositories found.", reply_markup=back_button())
                return
            items = [{"label": r.get("name"), "data": r.get("full_name")} for r in repos]
            kb = paginate_buttons(items, "myrepo", page)
            await cq.message.edit("📚 Your Repositories:", reply_markup=kb)
            await cq.answer()
        return

    if data_cb.startswith("myrepo:"):
        full_name = data_cb.split(":", 1)[1]
        await cq.answer("Downloading repo ZIP...")
        await send_repo_zip(cq=cq, full_name=full_name, user_id=uid)
        return

    if data_cb.startswith("myrepo_page:"):
        page = int(data_cb.split(":")[1])
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await cq.answer("No active token. Add one first.", show_alert=True)
            return
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=100", token)
            if st != 200:
                await cq.message.edit("❌ Failed to fetch repos. Token may be invalid.", reply_markup=back_button())
                return
            if not repos:
                await cq.message.edit("⚠️ No repositories found.", reply_markup=back_button())
                return
            items = [{"label": r.get("name"), "data": r.get("full_name")} for r in repos]
            kb = paginate_buttons(items, "myrepo", page)
            await cq.message.edit("📚 Your Repositories:", reply_markup=kb)
            await cq.answer()
        return

    if data_cb == "search_user":
        user_states[uid] = {"action": "search_user"}
        await cq.message.edit("🔎 Send a GitHub username or profile URL to search for repos:", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "search_repo":
        user_states[uid] = {"action": "search_repo"}
        await cq.message.edit("🔎 Send a keyword to search public repositories:", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb.startswith("userrepo_page:"):
        # pagination for user repos (admin or user)
        params = data_cb.split(":")[1]
        username, page = params.split("|")
        page = int(page)
        await send_user_repos(cq, username, page)
        return

    if data_cb.startswith("userrepo:"):
        full_name = data_cb.split(":", 1)[1]
        await cq.answer("Downloading repo ZIP...")
        await send_repo_zip(cq=cq, full_name=full_name, user_id=uid)
        return

    if data_cb.startswith("searchrepo_page:"):
        page = int(data_cb.split(":")[1])
        keyword = user_states.get(uid, {}).get("search_keyword")
        if not keyword:
            await cq.answer("Session expired. Please search again.", show_alert=True)
            return
        await send_search_repos(cq, keyword, page)
        return

    if data_cb.startswith("searchrepo:"):
        full_name = data_cb.split(":", 1)[1]
        await cq.answer("Downloading repo ZIP...")
        await send_repo_zip(cq=cq, full_name=full_name, user_id=uid)
        return

    if data_cb == "random_repo":
        await cq.answer("Fetching random repo...")
        await random_repo(cq)
        return

    if data_cb.startswith("forkrepo:"):
        full_name = data_cb.split(":", 1)[1]
        await fork_repo(cq, full_name, uid)
        return

    if data_cb == "upload_zip":
        user_states[uid] = {"action": "upload_zip"}
        await cq.message.edit("📤 Send the ZIP file to upload to a repo.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "gist_create":
        user_states[uid] = {"action": "gist_create"}
        await cq.message.edit("🧾 Send gist content as plain text or JSON for multiple files.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "gh_stats":
        await cq.answer("Fetching GitHub API rate limits...")
        await gh_stats(cq)
        return

    if data_cb == "star_repo_prompt":
        user_states[uid] = {"action": "star_repo"}
        await cq.message.edit("⭐ Send full repo name (owner/repo) to star:", reply_markup=back_button())
        await cq.answer()
        return

    # ADMIN PANEL
    if data_cb == "admin_panel":
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 List Users", callback_data="admin_list_users_page:0")],
            [InlineKeyboardButton("❌ Ban User", callback_data="admin_ban_user")],
            [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user")],
            [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
            [InlineKeyboardButton("📈 Bot Stats", callback_data="admin_bot_stats")],
            [InlineKeyboardButton("◀️ Back", callback_data="start")],
        ])
        await cq.message.edit("👥 Admin Panel:", reply_markup=kb)
        await cq.answer()
        return

    if data_cb.startswith("admin_list_users_page:"):
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        page = int(data_cb.split(":")[1])
        await admin_list_users(cq, page)
        return

    if data_cb.startswith("admin_view_user:"):
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        user_id_str = data_cb.split(":",1)[1]
        await admin_view_user_repos(cq, user_id_str, 0)
        return

    if data_cb.startswith("admin_view_user_repopage:"):
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        params = data_cb.split(":",1)[1]
        user_id_str, page = params.split("|")
        page = int(page)
        await admin_view_user_repos(cq, user_id_str, page)
        return

    if data_cb.startswith("admin_ban_user_confirm:"):
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        user_id_str = data_cb.split(":",1)[1]
        try:
            uid_ban = int(user_id_str)
        except:
            await cq.answer("Invalid user ID.", show_alert=True)
            return
        if uid_ban in data.get("banned", []):
            await cq.answer("User already banned.", show_alert=True)
            return
        data.setdefault("banned", []).append(uid_ban)
        save_data(data)
        await cq.message.edit(f"✅ User {uid_ban} banned.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb.startswith("admin_unban_user_confirm:"):
        if uid not in ADMINS:
            await cq.answer("Unauthorized", show_alert=True)
            return
        user_id_str = data_cb.split(":",1)[1]
        try:
            uid_unban = int(user_id_str)
        except:
            await cq.answer("Invalid user ID.", show_alert=True)
            return
        if uid_unban not in data.get("banned", []):
            await cq.answer("User not banned.", show_alert=True)
            return
        data["banned"].remove(uid_unban)
        save_data(data)
        await cq.message.edit(f"✅ User {uid_unban} unbanned.", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "admin_ban_user":
        admin_states[uid] = {"action": "ban_user"}
        await cq.message.edit("❌ Send the Telegram user ID to ban:", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "admin_unban_user":
        admin_states[uid] = {"action": "unban_user"}
        await cq.message.edit("✅ Send the Telegram user ID to unban:", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "admin_broadcast":
        admin_states[uid] = {"action": "broadcast"}
        await cq.message.edit("📢 Send the message to broadcast to all users:", reply_markup=back_button())
        await cq.answer()
        return

    if data_cb == "admin_bot_stats":
        user_count = len(data.get("tokens", {}))
        token_count = sum(len(u.get("tokens", {})) for u in data.get("tokens", {}).values())
        banned_count = len(data.get("banned", []))
        txt = (f"📊 Bot Stats:\n\n"
               f"👥 Users: {user_count}\n"
               f"🔑 Tokens: {token_count}\n"
               f"🚫 Banned Users: {banned_count}")
        await cq.message.edit(txt, reply_markup=back_button())
        await cq.answer()
        return

    await cq.answer("Unknown action or expired session.", show_alert=True)

# --------------------------------
# MESSAGE HANDLER (for inputs)
# --------------------------------
@app.on_message(filters.private & filters.create(not_banned_filter))
async def msg_handler(_, msg: Message):
    uid = msg.from_user.id
    state = user_states.get(uid) or admin_states.get(uid)
    text = msg.text or ""

    if not state:
        await msg.reply("❓ Please use the buttons to interact with the bot.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="start")]]))
        return

    action = state.get("action")

    if action == "await_add_token":
        token = text.strip()
        # Validate token by fetching username
        async with aiohttp.ClientSession() as session:
            st, resp = await gh_request(session, "GET", "https://api.github.com/user", token)
            if st != 200 or "login" not in resp:
                await msg.reply("❌ Invalid token or API error. Try again or press Back.", reply_markup=back_button())
                return
            username = resp["login"]
        # Save token
        urec = data["tokens"].setdefault(str(uid), {"tokens": {}, "active": None, "first_name": msg.from_user.first_name or ""})
        urec["tokens"][token] = {"username": username}
        if not urec.get("active"):
            urec["active"] = token
        save_data(data)
        user_states.pop(uid, None)
        await msg.reply(f"✅ Token added for user: {username}\nActive token switched to this if none set.", reply_markup=back_button())
        return

    if action == "search_user":
        username = extract_github_username(text)
        if not username:
            await msg.reply("❌ Invalid GitHub username or URL. Try again or press Back.", reply_markup=back_button())
            return
        async with aiohttp.ClientSession() as session:
            st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{username}/repos?per_page=100")
            if st != 200:
                await msg.reply("❌ User not found or API error. Try again or press Back.", reply_markup=back_button())
                return
            if not repos:
                await msg.reply("⚠️ No repos found for this user.", reply_markup=back_button())
                return
            # Save username for pagination
            user_states[uid] = {"action": "search_user", "username": username}
            # Show first page repos
            await send_user_repos(msg, username, 0)
        return

    if action == "search_repo":
        keyword = text.strip()
        if not keyword:
            await msg.reply("❌ Send a keyword to search or press Back.", reply_markup=back_button())
            return
        user_states[uid] = {"action": "search_repo", "search_keyword": keyword}
        await send_search_repos(msg, keyword, 0)
        return

    if action == "gist_create":
        content = text.strip()
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await msg.reply("❌ No active token found. Add and switch tokens first.", reply_markup=back_button())
            user_states.pop(uid, None)
            return
        try:
            gist_files = json.loads(content)
            if not isinstance(gist_files, dict):
                raise ValueError
        except:
            gist_files = {"file1.txt": {"content": content}}

        post_data = {
            "files": gist_files,
            "public": True,
            "description": "Created by Spilux GitHub Bot"
        }
        async with aiohttp.ClientSession() as session:
            st, resp = await gh_request(session, "POST", "https://api.github.com/gists", token, json=post_data)
            if st != 201:
                await msg.reply(f"❌ Failed to create gist (status {st}).", reply_markup=back_button())
                user_states.pop(uid, None)
                return
            url = resp.get("html_url", "N/A")
            await msg.reply(f"✅ Gist created: {url}", reply_markup=back_button())
            user_states.pop(uid, None)
        return

    if action == "upload_zip":
        if not msg.document or not msg.document.file_name.endswith(".zip"):
            await msg.reply("❌ Please send a ZIP file to upload.", reply_markup=back_button())
            return
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await msg.reply("❌ No active token. Add/switch tokens first.", reply_markup=back_button())
            user_states.pop(uid, None)
            return
        zip_path = await msg.download()
        user_states[uid] = {"action": "upload_zip_repo", "zip_path": zip_path}
        await msg.reply("📝 Now send the full repo name (username/reponame) to upload ZIP to.", reply_markup=back_button())
        return

    if action == "upload_zip_repo":
        full_name = text.strip()
        zip_path = state.get("zip_path")
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token or not zip_path:
            await msg.reply("❌ Missing data. Please start again.", reply_markup=back_button())
            user_states.pop(uid, None)
            return
        await msg.reply(f"📤 Uploading ZIP contents to `{full_name}` repo...")
        await upload_zip_contents(msg, full_name, zip_path, token)
        user_states.pop(uid, None)
        return

    if action == "star_repo":
        full_name = text.strip()
        rec = data.get("tokens", {}).get(str(uid), {})
        token = rec.get("active")
        if not token:
            await msg.reply("❌ No active token to star repo.", reply_markup=back_button())
            user_states.pop(uid, None)
            return
        async with aiohttp.ClientSession() as session:
            st, _ = await gh_request(session, "PUT", f"https://api.github.com/user/starred/{full_name}", token, headers={"Content-Length": "0"})
            if st == 204:
                await msg.reply(f"⭐ Starred repo {full_name} successfully.", reply_markup=back_button())
            else:
                await msg.reply(f"❌ Failed to star repo (status {st}).", reply_markup=back_button())
        user_states.pop(uid, None)
        return

    # Admin actions input
    if uid in ADMINS:
        if action == "ban_user":
            try:
                to_ban = int(text.strip())
            except:
                await msg.reply("❌ Invalid user ID. Try again or press Back.", reply_markup=back_button())
                return
            if to_ban in data.get("banned", []):
                await msg.reply("User already banned.", reply_markup=back_button())
                admin_states.pop(uid, None)
                return
            data.setdefault("banned", []).append(to_ban)
            save_data(data)
            await msg.reply(f"✅ User {to_ban} banned.", reply_markup=back_button())
            admin_states.pop(uid, None)
            return

        if action == "unban_user":
            try:
                to_unban = int(text.strip())
            except:
                await msg.reply("❌ Invalid user ID. Try again or press Back.", reply_markup=back_button())
                return
            if to_unban not in data.get("banned", []):
                await msg.reply("User not banned.", reply_markup=back_button())
                admin_states.pop(uid, None)
                return
            data["banned"].remove(to_unban)
            save_data(data)
            await msg.reply(f"✅ User {to_unban} unbanned.", reply_markup=back_button())
            admin_states.pop(uid, None)
            return

        if action == "broadcast":
            broadcast_msg = text.strip()
            admin_states.pop(uid, None)
            count_sent = 0
            for user_id_str in data.get("tokens", {}).keys():
                try:
                    await app.send_message(int(user_id_str), f"📢 Broadcast from Admin:\n\n{broadcast_msg}")
                    count_sent += 1
                except:
                    pass
            await msg.reply(f"✅ Broadcast sent to {count_sent} users.", reply_markup=back_button())
            return

    # Unknown input
    await msg.reply("❓ Please use the buttons to interact with the bot.", reply_markup=back_button())

# --------------------------------
# UTILITIES
# --------------------------------
async def send_user_repos(dest, username, page):
    async with aiohttp.ClientSession() as session:
        st, repos = await gh_request(session, "GET", f"https://api.github.com/users/{username}/repos?per_page=100")
        if st != 200 or not repos:
            await dest.edit("❌ Failed to fetch user repos or none found.", reply_markup=back_button())
            return
        items = [{"label": r.get("name"), "data": r.get("full_name")} for r in repos]
        kb = paginate_buttons(items, f"userrepo_page:{username}|", page)
        await dest.edit(f"📚 Repositories for user <b>{username}</b>:", reply_markup=kb)

async def send_search_repos(dest, keyword, page):
    async with aiohttp.ClientSession() as session:
        st, resp = await gh_request(session, "GET", f"https://api.github.com/search/repositories?q={keyword}&per_page=50")
        if st != 200:
            await dest.edit("❌ GitHub API error.", reply_markup=back_button())
            return
        items_raw = resp.get("items", [])
        if not items_raw:
            await dest.edit("⚠️ No repositories found.", reply_markup=back_button())
            return
        # Store keyword in user state for pagination
        uid = dest.chat.id
        user_states[uid] = {"action": "search_repo", "search_keyword": keyword}
        items = [{"label": r.get("full_name"), "data": r.get("full_name")} for r in items_raw]
        kb = paginate_buttons(items, "searchrepo_page:", page)
        await dest.edit(f"🔎 Search results for: <b>{keyword}</b>", reply_markup=kb)

async def send_repo_zip(cq: CallbackQuery, full_name: str, user_id: int, token_override=None):
    rec = data.get("tokens", {}).get(str(user_id), {})
    token = token_override or rec.get("active")
    if not token:
        await cq.message.edit("❌ No active token. Add and switch token first.", reply_markup=back_button())
        return
    owner, repo = full_name.split("/")
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball"
    headers = {"Authorization": f"token {token}", "User-Agent": "Spilux-GitHub-Bot"}

    await cq.message.edit(f"⏳ Downloading ZIP for `{full_name}`...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(zip_url, headers=headers) as r:
                if r.status != 200:
                    await cq.message.edit(f"❌ Failed to download ZIP (status {r.status})", reply_markup=back_button())
                    return
                zip_bytes = await r.read()
    except Exception as e:
        await cq.message.edit(f"❌ Download error: {e}", reply_markup=back_button())
        return

    try:
        await cq.message.reply_document(zip_bytes, filename=f"{repo}.zip", caption=f"📥 Repo ZIP: {full_name}")
        await cq.message.edit("✅ Here is your ZIP file.", reply_markup=back_button())
    except Exception as e:
        await cq.message.edit(f"❌ Failed to send ZIP: {e}", reply_markup=back_button())

async def upload_zip_contents(msg, full_name, zip_path, token):
    import zipfile
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            files_to_upload = {}
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                with zf.open(name) as f:
                    files_to_upload[name] = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.reply(f"❌ Failed to read ZIP: {e}", reply_markup=back_button())
        return

    if "/" not in full_name:
        await msg.reply("❌ Repo name format invalid. Use username/reponame.", reply_markup=back_button())
        return

    owner, repo = full_name.split("/", 1)

    async with aiohttp.ClientSession() as session:
        # Get branch ref
        st, ref_data = await gh_request(session, "GET", f"https://api.github.com/repos/{full_name}/git/refs/heads/main", token)
        if st != 200:
            st, ref_data = await gh_request(session, "GET", f"https://api.github.com/repos/{full_name}/git/refs/heads/master", token)
            if st != 200:
                await msg.reply("❌ Repo branch 'main' or 'master' not found.", reply_markup=back_button())
                return

        commit_sha = ref_data.get("object", {}).get("sha")
        if not commit_sha:
            await msg.reply("❌ Failed to get latest commit SHA.", reply_markup=back_button())
            return

        # Get tree SHA from commit
        st, commit_data = await gh_request(session, "GET", f"https://api.github.com/repos/{full_name}/git/commits/{commit_sha}", token)
        if st != 200:
            await msg.reply("❌ Failed to get commit data.", reply_markup=back_button())
            return
        tree_sha = commit_data.get("tree", {}).get("sha")
        if not tree_sha:
            await msg.reply("❌ Failed to get tree SHA.", reply_markup=back_button())
            return

        # Create blobs
        blobs = []
        for filename, content in files_to_upload.items():
            st, blob_resp = await gh_request(session, "POST", f"https://api.github.com/repos/{full_name}/git/blobs", token,
                                            json={"content": content, "encoding": "utf-8"})
            if st != 201:
                await msg.reply(f"❌ Failed to create blob for {filename}.", reply_markup=back_button())
                return
            blobs.append({"path": filename, "mode": "100644", "type": "blob", "sha": blob_resp.get("sha")})

        # Create new tree
        st, tree_resp = await gh_request(session, "POST", f"https://api.github.com/repos/{full_name}/git/trees", token,
                                        json={"base_tree": tree_sha, "tree": blobs})
        if st != 201:
            await msg.reply("❌ Failed to create new tree.", reply_markup=back_button())
            return
        new_tree_sha = tree_resp.get("sha")

        # Create new commit
        commit_message = f"Upload ZIP files via bot"
        st, new_commit = await gh_request(session, "POST", f"https://api.github.com/repos/{full_name}/git/commits", token,
                                        json={"message": commit_message, "tree": new_tree_sha, "parents": [commit_sha]})
        if st != 201:
            await msg.reply("❌ Failed to create commit.", reply_markup=back_button())
            return
        new_commit_sha = new_commit.get("sha")

        # Update branch ref
        st, update_ref = await gh_request(session, "PATCH", f"https://api.github.com/repos/{full_name}/git/refs/heads/main", token,
                                        json={"sha": new_commit_sha})
        if st != 200:
            # try master branch
            st, update_ref = await gh_request(session, "PATCH", f"https://api.github.com/repos/{full_name}/git/refs/heads/master", token,
                                            json={"sha": new_commit_sha})
            if st != 200:
                await msg.reply("❌ Failed to update branch reference.", reply_markup=back_button())
                return

        await msg.reply("✅ ZIP contents uploaded successfully!", reply_markup=back_button())

async def random_repo(cq: CallbackQuery):
    async with aiohttp.ClientSession() as session:
        st, resp = await gh_request(session, "GET", "https://api.github.com/search/repositories?q=stars:>10000&sort=stars&order=desc&per_page=100")
        if st != 200:
            await cq.message.edit("❌ Failed to fetch trending repos.", reply_markup=back_button())
            return
        import random
        repo = random.choice(resp.get("items", []))
        full_name = repo.get("full_name")
        description = repo.get("description", "No description")
        stars = repo.get("stargazers_count", 0)
        forks = repo.get("forks_count", 0)
        html_url = repo.get("html_url")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Download ZIP", callback_data=f"myrepo:{full_name}")],
            [InlineKeyboardButton("🍴 Fork Repo", callback_data=f"forkrepo:{full_name}")],
            [InlineKeyboardButton("◀️ Back", callback_data="start")]
        ])
        txt = (f"🎲 Random Popular Repo:\n\n"
               f"<b>{full_name}</b>\n"
               f"{description}\n\n"
               f"⭐ Stars: {stars} | 🍴 Forks: {forks}\n"
               f"<a href='{html_url}'>GitHub Link</a>")
        await cq.message.edit(txt, reply_markup=kb)

async def fork_repo(cq: CallbackQuery, full_name, user_id):
    rec = data.get("tokens", {}).get(str(user_id), {})
    token = rec.get("active")
    if not token:
        await cq.answer("No active token.", show_alert=True)
        return
    async with aiohttp.ClientSession() as session:
        st, _ = await gh_request(session, "POST", f"https://api.github.com/repos/{full_name}/forks", token)
        if st == 202:
            await cq.answer("🍴 Fork started. Check your repos after some time.", show_alert=True)
        else:
            await cq.answer(f"❌ Failed to fork repo (status {st}).", show_alert=True)

async def gh_stats(cq: CallbackQuery):
    uid = cq.from_user.id
    rec = data.get("tokens", {}).get(str(uid), {})
    token = rec.get("active")
    if not token:
        await cq.message.edit("❌ No active token.", reply_markup=back_button())
        return
    async with aiohttp.ClientSession() as session:
        st, resp = await gh_request(session, "GET", "https://api.github.com/rate_limit", token)
        if st != 200:
            await cq.message.edit("❌ Failed to get stats.", reply_markup=back_button())
            return
        core = resp.get("rate", {})
        txt = (f"📊 GitHub API Rate Limit:\n\n"
               f"Limit: {core.get('limit')}\n"
               f"Remaining: {core.get('remaining')}\n"
               f"Reset: <code>{core.get('reset')}</code>")
        await cq.message.edit(txt, reply_markup=back_button())

# --------------------------------
# ADMIN PANEL FUNCTIONS
# --------------------------------
async def admin_list_users(cq: CallbackQuery, page: int):
    user_tokens = data.get("tokens", {})
    if not user_tokens:
        await cq.message.edit("No users found.", reply_markup=back_button())
        return
    items = []
    for ustr, info in user_tokens.items():
        first_name = info.get("first_name", "unknown")
        token_count = len(info.get("tokens", {}))
        items.append({"label": f"{first_name} (ID: {ustr}) [{token_count} tokens]", "data": ustr})
    kb = paginate_buttons(items, "admin_view_user", page)
    await cq.message.edit("👥 Registered Users:", reply_markup=kb)

async def admin_view_user_repos(cq: CallbackQuery, user_id_str: str, page: int):
    rec = data.get("tokens", {}).get(user_id_str)
    if not rec:
        await cq.message.edit("User not found.", reply_markup=back_button())
        return
    tokens = rec.get("tokens", {})
    active_token = rec.get("active")
    if not tokens:
        await cq.message.edit("User has no saved tokens.", reply_markup=back_button())
        return
    token = tokens.get(active_token)
    if not active_token:
        token = next(iter(tokens), None)
        if not token:
            await cq.message.edit("User has no active token.", reply_markup=back_button())
            return
        active_token = token
    async with aiohttp.ClientSession() as session:
        st, repos = await gh_request(session, "GET", "https://api.github.com/user/repos?per_page=100", active_token)
        if st != 200:
            await cq.message.edit("Failed to fetch user repos or token invalid.", reply_markup=back_button())
            return
        if not repos:
            await cq.message.edit("No repos found for this user.", reply_markup=back_button())
            return
        items = [{"label": r.get("name"), "data": r.get("full_name")} for r in repos]
        kb = paginate_buttons(items, f"admin_view_user_repopage:{user_id_str}|", page)
        await cq.message.edit(f"Repos for user {rec.get('first_name')} (ID {user_id_str}):", reply_markup=kb)

# --------------------------------
# RUN
# --------------------------------
print("Bot is running...")
app.run()
