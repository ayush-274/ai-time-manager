import os
import json
import logging
import pytz
import smtplib
import io
import matplotlib
matplotlib.use('Agg') # Required for servers (Render) to generate images without a screen
import matplotlib.pyplot as plt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
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
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

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

# --- EMAIL LOGIC ---
def send_email(subject, body, images=None):
    if not EMAIL_USER or not EMAIL_PASSWORD:
        print("⚠️ Email credentials missing. Skipping.")
        return

    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_USER
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))

    # Attach Graphs if they exist
    if images:
        for i, img_bytes in enumerate(images):
            img = MIMEImage(img_bytes.getvalue())
            img.add_header('Content-ID', f'<graph_{i}>')
            msg.attach(img)

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.send_message(msg)
            print(f"📧 Email Sent: {subject}")
    except Exception as e:
        print(f"❌ Email Failed: {e}")

async def send_daily_report():
    print("📝 Generating Daily Report...")
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    
    # Range: Today 00:00 to 23:59 (Converted to UTC for DB Query)
    start_utc = now.replace(hour=0, minute=0, second=0).astimezone(pytz.utc).isoformat()
    end_utc = now.replace(hour=23, minute=59, second=59).astimezone(pytz.utc).isoformat()
    
    tasks = supabase.table("events").select("*").gte("start_time", start_utc).lte("start_time", end_utc).execute().data
    
    completed = [t for t in tasks if t['status'] == 'Completed']
    pending = [t for t in tasks if t['status'] == 'Pending']
    
    html_body = f"""
    <h2>📅 Daily Summary: {now.strftime('%b %d')}</h2>
    <p><b>Total Tasks:</b> {len(tasks)} | <b>Completed:</b> {len(completed)}</p>
    <hr>
    <h3 style="color:green">✅ Done</h3>
    <ul>{''.join([f"<li>{t['task_name']}</li>" for t in completed])}</ul>
    <h3 style="color:red">⏳ Pending</h3>
    <ul>{''.join([f"<li>{t['task_name']}</li>" for t in pending])}</ul>
    """
    
    send_email(f"Daily Report: {len(completed)}/{len(tasks)} Tasks Done", html_body)

async def send_weekly_report():
    print("📊 Generating Weekly Report...")
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)
    seven_days_ago = now - timedelta(days=7)
    
    start_utc = seven_days_ago.astimezone(pytz.utc).isoformat()
    
    # Fetch last 7 days of COMPLETED tasks
    tasks = supabase.table("events").select("*").gte("start_time", start_utc).eq("status", "Completed").execute().data
    
    if not tasks:
        send_email("Weekly Report: No Data", "No tasks completed this week.")
        return

    # --- ANALYSIS ---
    # 1. Tasks per Day
    daily_counts = {}
    # 2. Time per Project
    project_counts = {}
    
    for t in tasks:
        # Parse time
        dt = datetime.fromisoformat(t['start_time']).astimezone(ist)
        day_name = dt.strftime("%a") # Mon, Tue...
        daily_counts[day_name] = daily_counts.get(day_name, 0) + 1
        
        proj = t['project']
        project_counts[proj] = project_counts.get(proj, 0) + 1

    # --- PLOTTING ---
    images = []
    
    # Graph 1: Bar Chart (Tasks per Day)
    plt.figure(figsize=(6, 4))
    plt.bar(daily_counts.keys(), daily_counts.values(), color='skyblue')
    plt.title('Tasks Completed per Day')
    buf1 = io.BytesIO()
    plt.savefig(buf1, format='png')
    buf1.seek(0)
    images.append(buf1)
    plt.close()

    # Graph 2: Pie Chart (Project Split)
    plt.figure(figsize=(6, 4))
    plt.pie(project_counts.values(), labels=project_counts.keys(), autopct='%1.1f%%')
    plt.title('Project Distribution')
    buf2 = io.BytesIO()
    plt.savefig(buf2, format='png')
    buf2.seek(0)
    images.append(buf2)
    plt.close()

    html_body = f"""
    <h2>📊 Weekly Analytics ({seven_days_ago.strftime('%b %d')} - {now.strftime('%b %d')})</h2>
    <p>You crushed <b>{len(tasks)} tasks</b> this week!</p>
    <p>See your performance graphs below:</p>
    <br><img src="cid:graph_0"><br><hr><br><img src="cid:graph_1">
    """
    
    send_email(f"Weekly Report: {len(tasks)} Tasks Crushed! 🚀", html_body, images)

# --- SCHEDULER & REMINDERS ---
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

# --- BOT LOGIC ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 System Online! Schedule me.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    user_id = update.message.from_user.id
    await update.message.chat.send_action(action="typing")

    if user_text.lower().endswith("done"):
        # ... (Same Done Logic as before) ...
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

# --- CLOUD LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await ptb_app.initialize()
    await ptb_app.start()
    
    render_url = os.getenv("RENDER_EXTERNAL_URL") 
    if render_url:
        await ptb_app.bot.set_webhook(f"{render_url}/webhook")
    
    # SETUP SCHEDULER
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    
    # 1. Reminders (Every 1 min)
    scheduler.add_job(check_reminders, 'interval', minutes=1, args=[ptb_app.bot])
    
    # 2. Daily Report (Every Night 11:30 PM)
    scheduler.add_job(send_daily_report, 'cron', hour=23, minute=30)
    
    # 3. Weekly Report (Every Sunday 11:30 PM)
    scheduler.add_job(send_weekly_report, 'cron', day_of_week='sun', hour=23, minute=30)
    
    scheduler.start()
    app.state.ptb_app = ptb_app
    yield
    scheduler.shutdown()
    await ptb_app.stop()
    await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    ptb_app = request.app.state.ptb_app
    update = Update.de_json(await request.json(), ptb_app.bot)
    await ptb_app.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def health_check():
    return {"status": "active"}

# --- LOCAL RUNNER ---
if __name__ == "__main__":
    print("🚀 Running Locally...")
    async def local_post_init(app: Application):
        scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        scheduler.add_job(check_reminders, 'interval', minutes=1, args=[app.bot])
        
        # Uncomment this line to test the email immediately (runs in 10 seconds)
        # scheduler.add_job(send_daily_report, 'date', run_date=datetime.now() + timedelta(seconds=10))
        
        scheduler.start()
    
    local_app = Application.builder().token(TELEGRAM_TOKEN).post_init(local_post_init).build()
    local_app.add_handler(CommandHandler("start", start))
    local_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    local_app.run_polling()