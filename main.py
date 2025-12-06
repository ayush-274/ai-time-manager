import os
import json
import logging
import pytz
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from supabase import create_client, Client
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import uvicorn

# 1. LOAD SECRETS
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 2. SETUP CLIENTS
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- SYSTEM PROMPT ---
SYSTEM_PROMPT = """
You are a scheduling assistant. 
Current Date/Time in India (IST): {current_time}
Timezone: Asia/Kolkata (UTC+05:30).

Rules:
1. Return 'start_time' and 'end_time' in strict ISO 8601 format with fixed offset +05:30.
2. Default Duration: 3 hours.
3. Default Project: "No Project".
4. Output JSON Keys: task_name, start_time, end_time, project, tag.
"""

# --- DATABASE HELPERS ---
def save_to_supabase(data, user_id):
    try:
        supabase.table("events").insert({
            "user_id": user_id,
            "task_name": data.get("task_name"),
            "start_time": data.get("start_time"),
            "end_time": data.get("end_time"),
            "project": data.get("project"),
            "tag": data.get("tag")
        }).execute()
        return True
    except Exception as e:
        print(f"❌ Database Error: {e}")
        return False

# --- REMINDER LOGIC (THE HEART OF PHASE 3) ---
async def check_reminders(bot):
    """Checks DB every minute for tasks starting in 15 mins"""
    print("⏰ Checking for reminders...")
    try:
        # 1. Calculate time window (Now + 15 mins) in UTC
        # Supabase stores in UTC, so we compare in UTC
        now_utc = datetime.now(pytz.utc)
        target_time = now_utc + timedelta(minutes=15)
        
        # We look for tasks starting between NOW and NOW+16 mins to be safe
        # And make sure we haven't sent a reminder yet
        response = supabase.table("events").select("*").eq("reminder_sent", False).execute()
        
        events = response.data
        
        for event in events:
            # Parse stored time (UTC)
            start_time_str = event['start_time']
            start_dt = datetime.fromisoformat(start_time_str)
            
            # Check if start_dt is close to target_time
            # Logic: If start_time is less than 16 mins away and hasn't passed yet
            time_diff = start_dt - now_utc
            minutes_left = time_diff.total_seconds() / 60
            
            if 0 < minutes_left <= 16:
                # SEND REMINDER
                user_id = event['user_id']
                task = event['task_name']
                print(f"🔔 Sending reminder for {task}")
                
                await bot.send_message(
                    chat_id=user_id, 
                    text=f"🔔 **Reminder:** '{task}' starts in {int(minutes_left)} mins!"
                )
                
                # Mark as sent so we don't spam
                supabase.table("events").update({"reminder_sent": True}).eq("id", event['id']).execute()
                
    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Phase 3 Online! Reminders are active.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.message.from_user.id
    
    await update.message.chat.send_action(action="typing")

    try:
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        formatted_now = now_ist.strftime("%Y-%m-%d %H:%M:%S %Z")
        
        prompt = SYSTEM_PROMPT.format(current_time=formatted_now) + f"\nUser Input: {user_text}"
        response = model.generate_content(prompt)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        save_to_supabase(data, user_id)
        
        # Format for reply
        start_dt = datetime.fromisoformat(data['start_time'])
        readable_start = start_dt.strftime("%b %d, %I:%M %p")
        
        reply = (
            f"✅ **Scheduled!**\n"
            f"📌 **Task:** {data.get('task_name')}\n"
            f"🕒 **Time:** {readable_start}\n"
            f"_(I will remind you 15 mins before)_"
        )
        await update.message.reply_text(reply, parse_mode="Markdown")

    except Exception as e:
        print(f"❌ Error: {e}")
        await update.message.reply_text("⚠️ Error processing request.")

# --- APP LIFESPAN (Starts Scheduler) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize Bot
    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await ptb_app.initialize()
    await ptb_app.start()
    
    # 2. Initialize Scheduler
    scheduler = AsyncIOScheduler()
    # Pass the bot object to the job so it can send messages
    scheduler.add_job(check_reminders, 'interval', minutes=1, args=[ptb_app.bot])
    scheduler.start()
    print("⏰ Scheduler Started (Checking every 1 min)")
    
    yield {'bot_app': ptb_app}
    
    # Cleanup
    scheduler.shutdown()
    await ptb_app.stop()
    await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)

# --- LOCAL RUNNER ---
if __name__ == "__main__":
    # Note: Using uvicorn directly because we need the 'lifespan' to run the scheduler
    # Standard 'python main.py' polling loop doesn't support APScheduler easily without FastAPI
    print("🚀 Running Phase 3 (Scheduler Active)...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)