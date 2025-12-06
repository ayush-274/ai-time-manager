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
from thefuzz import process, fuzz 

# 1. CONFIGURATION
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 2. SETUP
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

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

# --- DATABASE ---
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

# --- SCHEDULER ---
async def check_reminders(bot):
    try:
        now_utc = datetime.now(pytz.utc)
        response = supabase.table("events").select("*").eq("reminder_sent", False).execute()
        for event in response.data:
            start_dt = datetime.fromisoformat(event['start_time'])
            minutes_left = (start_dt - now_utc).total_seconds() / 60
            if 0 < minutes_left <= 16:
                await bot.send_message(event['user_id'], f"🔔 **Reminder:** '{event['task_name']}' starts in {int(minutes_left)} mins!")
                supabase.table("events").update({"reminder_sent": True}).eq("id", event['id']).execute()
    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 System Online! I am ready.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    user_id = update.message.from_user.id
    await update.message.chat.send_action(action="typing")

    # DONE LOGIC
    if user_text.lower().endswith("done"):
        try:
            pending = supabase.table("events").select("*").eq("user_id", user_id).eq("status", "Pending").execute().data
            if not pending:
                await update.message.reply_text("🤷‍♂️ No pending tasks.")
                return
            
            search = user_text.lower().replace("done", "").strip()
            task_map = {t['task_name']: t for t in pending}
            match = process.extractOne(search, task_map.keys(), scorer=fuzz.partial_ratio)
            
            if match and match[1] > 70:
                task = task_map[match[0]]
                now_iso = datetime.now(pytz.utc).isoformat()
                supabase.table("events").update({"status": "Completed", "end_time": now_iso}).eq("id", task['id']).execute()
                await update.message.reply_text(f"✅ Marked **'{match[0]}'** as done!")
            else:
                await update.message.reply_text("🤔 Couldn't find that task.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Error: {e}")
        return

    # SCHEDULE LOGIC
    try:
        ist_time = datetime.now(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S %Z")
        prompt = SYSTEM_PROMPT.format(current_time=ist_time) + f"\nUser Input: {user_text}"
        response = model.generate_content(prompt)
        data = json.loads(response.text.replace("```json", "").replace("```", "").strip())
        
        save_to_supabase(data, user_id)
        
        readable = datetime.fromisoformat(data['start_time']).strftime("%b %d, %I:%M %p")
        await update.message.reply_text(f"✅ **Scheduled:** {data['task_name']}\n🕒 {readable}")
    except Exception as e:
        print(f"❌ Error: {e}")
        await update.message.reply_text("⚠️ I didn't understand that.")

# --- CLOUD SETUP (WEBHOOKS) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize Bot
    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await ptb_app.initialize()
    await ptb_app.start()
    
    # 2. Set Webhook (Only on Render)
    render_url = os.getenv("RENDER_EXTERNAL_URL") 
    if render_url:
        print(f"🌍 Setting Webhook to {render_url}/webhook")
        await ptb_app.bot.set_webhook(f"{render_url}/webhook")
    
    # 3. Start Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, 'interval', minutes=1, args=[ptb_app.bot])
    scheduler.start()
    
    # 4. Store bot in app state so the API route can access it
    app.state.ptb_app = ptb_app
    
    yield
    
    scheduler.shutdown()
    await ptb_app.stop()
    await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """The door for Telegram to push messages to us"""
    ptb_app = request.app.state.ptb_app
    update = Update.de_json(await request.json(), ptb_app.bot)
    await ptb_app.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def health_check():
    """Keep-alive endpoint"""
    return {"status": "active"}

# --- LOCAL SETUP (POLLING) ---
if __name__ == "__main__":
    print("🚀 Running Locally...")
    async def local_post_init(app: Application):
        scheduler = AsyncIOScheduler()
        scheduler.add_job(check_reminders, 'interval', minutes=1, args=[app.bot])
        scheduler.start()
    
    local_app = Application.builder().token(TELEGRAM_TOKEN).post_init(local_post_init).build()
    local_app.add_handler(CommandHandler("start", start))
    local_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    local_app.run_polling()