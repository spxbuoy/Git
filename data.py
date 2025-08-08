# plugins/getdata.py
from pyrogram import Client, filters
import os

# Replace with your Telegram ID
ADMIN_ID = 7941175119  

@Client.on_message(filters.command("getdata", [".", "/"]))
async def send_data_json(client, message):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("🚫 You are not authorized to use this command.")

    file_path = "data.json"  # Path to your data.json file
    if os.path.exists(file_path):
        await message.reply_document(file_path, caption="📂 Here is the data.json file.")
    else:
        await message.reply("❌ data.json file not found.")
