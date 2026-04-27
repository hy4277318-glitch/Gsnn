import sqlite3
import datetime
import random
import string
import threading
import os

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# =========================
# CONFIG
# =========================
TOKEN = "8306448784:AAFXG39OKoGXPR-Z0ddGNFb4VmAbePvkN-I"
ADMIN_ID = 6676943475
UPI_ID = "himanshuji90million@fam"

# =========================
# FLASK (Render Fix)
# =========================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot Running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# =========================
# DATABASE
# =========================
conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, category TEXT, approved INTEGER, code TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS payments (user_id INTEGER, utr TEXT, date TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS videos (user_id INTEGER, link TEXT, date TEXT)")

# =========================
# AUTO DELETE OLD DATA (7 DAYS)
# =========================
def clean_old():
    while True:
        today = datetime.datetime.now()
        limit = today - datetime.timedelta(days=7)

        cur.execute("DELETE FROM payments WHERE date < ?", (str(limit),))
        cur.execute("DELETE FROM videos WHERE date < ?", (str(limit.date()),))
        conn.commit()

        import time
        time.sleep(86400)

threading.Thread(target=clean_old, daemon=True).start()

# =========================
# START
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["🔥 10–500 Followers (₹15)"],
        ["⚡ 500–1000 Followers (₹50)"],
        ["🚀 1000–10000 Followers (₹200)"]
    ]
    await update.message.reply_text("Choose category 👇", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

# =========================
# CATEGORY (LIMIT 50)
# =========================
async def category(update, context):
    uid = update.message.chat_id
    cat = update.message.text

    cur.execute("SELECT COUNT(*) FROM users WHERE category=?", (cat,))
    if cur.fetchone()[0] >= 50:
        await update.message.reply_text("❌ Category full (50 users)")
        return

    fee = "15" if "15" in cat else "50" if "50" in cat else "200"

    cur.execute("INSERT OR REPLACE INTO users VALUES (?, ?, 0, '')", (uid, cat))
    conn.commit()

    btn = [[InlineKeyboardButton("💰 Pay Now", callback_data=f"pay_{fee}")]]
    await update.message.reply_text(f"{cat}\nFee ₹{fee}", reply_markup=InlineKeyboardMarkup(btn))

# =========================
# PAY
# =========================
async def pay(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text("💳 Pay using UPI 👇")
    await q.message.reply_text(f"💰 `{UPI_ID}`", parse_mode="Markdown")

    btn = [[InlineKeyboardButton("📤 Submit UTR", callback_data="submit_pay")]]
    await q.message.reply_text("After payment click:", reply_markup=InlineKeyboardMarkup(btn))

# =========================
# SUBMIT UTR
# =========================
async def submit_pay(update, context):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    context.user_data["mode"] = "utr"

    await q.message.reply_text("Send your UTR number")

# =========================
# TEXT HANDLER
# =========================
async def text(update, context):
    uid = update.message.chat_id
    msg = update.message.text

    if msg in ["🔥 10–500 Followers (₹15)", "⚡ 500–1000 Followers (₹50)", "🚀 1000–10000 Followers (₹200)"]:
        await category(update, context)
        return

    if msg == "📤 Submit Video":
        cur.execute("SELECT approved FROM users WHERE id=?", (uid,))
        d = cur.fetchone()

        if not d or int(d[0]) != 1:
            await update.message.reply_text("❌ You are not approved yet")
            return

        context.user_data.clear()
        context.user_data["mode"] = "video"

        await update.message.reply_text("📸 Send Instagram Reel/Post link")
        return

    # VIDEO
    if context.user_data.get("mode") == "video":

        if not ("instagram.com/reel/" in msg or "instagram.com/p/" in msg):
            await update.message.reply_text("❌ Invalid Instagram link")
            return

        today = str(datetime.date.today())

        cur.execute("SELECT * FROM videos WHERE user_id=? AND date=?", (uid, today))
        if cur.fetchone():
            await update.message.reply_text("⚠️ Already submitted today")
            return

        cur.execute("INSERT INTO videos VALUES (?, ?, ?)", (uid, msg, today))
        conn.commit()

        context.user_data.clear()

        await update.message.reply_text("✅ Video submitted")
        await context.bot.send_message(ADMIN_ID, f"📸 Insta Video\nUser: {uid}\nLink: {msg}")
        return

    # UTR
    if context.user_data.get("mode") == "utr":
        now = str(datetime.datetime.now())

        cur.execute("INSERT INTO payments VALUES (?, ?, ?)", (uid, msg, now))
        conn.commit()

        context.user_data.clear()

        btn = [[InlineKeyboardButton("Approve", callback_data=f"approve_{uid}")]]
        await context.bot.send_message(ADMIN_ID, f"💰 Payment\nUser: {uid}\nUTR: {msg}\nTime: {now}", reply_markup=InlineKeyboardMarkup(btn))
        await update.message.reply_text("✅ Payment submitted")

# =========================
# APPROVE
# =========================
async def approve(update, context):
    q = update.callback_query
    await q.answer()

    if q.from_user.id != ADMIN_ID:
        return

    uid = int(q.data.split("_")[1])

    code = "FreeSpons-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))

    cur.execute("UPDATE users SET approved=1, code=? WHERE id=?", (code, uid))
    conn.commit()

    kb = [["📤 Submit Video"]]

    await context.bot.send_message(uid, "🎉 Approved!")
    await context.bot.send_message(uid, f"🔑 `{code}`", parse_mode="Markdown")
    await context.bot.send_message(uid, "Use this code in caption", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

    await q.edit_message_text("Approved")

# =========================
# DASHBOARD
# =========================
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_ID:
        return

    today = datetime.date.today()
    buttons = []

    for i in range(7):
        day = today - datetime.timedelta(days=i)
        buttons.append([InlineKeyboardButton(str(day), callback_data=f"day_{day}")])

    await update.message.reply_text("📊 Select Day:", reply_markup=InlineKeyboardMarkup(buttons))

async def day_data(update, context):
    q = update.callback_query
    await q.answer()

    date = q.data.split("_")[1]

    # payments
    cur.execute("SELECT * FROM payments WHERE date LIKE ?", (f"{date}%",))
    pays = cur.fetchall()

    # videos
    cur.execute("SELECT * FROM videos WHERE date=?", (date,))
    vids = cur.fetchall()

    msg = f"📅 {date}\n\n💰 Payments:\n"
    for p in pays:
        msg += f"{p[0]} | {p[1]}\n"

    msg += "\n📸 Videos:\n"
    for v in vids:
        msg += f"{v[0]} | {v[1]}\n"

    await q.message.reply_text(msg)

# =========================
# PARTICIPATION
# =========================
async def participation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat_id != ADMIN_ID:
        return

    cur.execute("SELECT category, COUNT(*) FROM users GROUP BY category")
    data = cur.fetchall()

    msg = "📊 Today Participation:\n\n"
    for d in data:
        msg += f"{d[0]} → {d[1]}\n"

    await update.message.reply_text(msg)

# =========================
# MAIN
# =========================
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("dashboard", dashboard))
app.add_handler(CommandHandler("participation", participation))
app.add_handler(CallbackQueryHandler(day_data, pattern="day_"))
app.add_handler(CommandHandler("list", list_cmd))
app.add_handler(CommandHandler("data", data_cmd))
app.add_handler(CallbackQueryHandler(pay, pattern="pay_"))
app.add_handler(CallbackQueryHandler(submit_pay, pattern="submit_pay"))
app.add_handler(CallbackQueryHandler(approve, pattern="approve_"))
app.add_handler(MessageHandler(filters.TEXT, text))

app.run_polling()
