import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# 1. LOAD SECRETS
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. SETUP AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

# 3. CONFIGURE LOGGING
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- AI SYSTEM PROMPT ---
SYSTEM_PROMPT = """
You are a scheduling assistant. Extract details from the user's message into JSON.
Current Date/Time: {current_time}
Default Duration: 3 hours.
Default Venue: Home.
Default Reminder: 15 mins before start.
Output Format: JSON with keys: task_name, start_time, end_time, project, tag, venue, reminder_time.
"""

# --- BOT FUNCTIONS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 System Online. Tell me what to schedule!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.message.from_user.id
    
    print(f"\n📩 MESSAGE RECEIVED from {user_id}: {user_text}")

    # 1. Show "Typing..." status
    await update.message.chat.send_action(action="typing")

    # 2. Prepare the Prompt
    try:
        # This fixes the error you saw!
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prompt = SYSTEM_PROMPT.format(current_time=now) + f"\nUser Input: {user_text}"
        
        print("🤖 Asking Gemini...")
        
        # 3. Call Gemini API
        response = model.generate_content(prompt)
        print("✅ Gemini Responded!")
        
        # 4. Clean the JSON
        raw_text = response.text
        clean_json = raw_text.replace("```json", "").replace("```", "").strip()
        print(f"📄 Raw JSON from AI: {clean_json}")

        # 5. Parse JSON
        data = json.loads(clean_json)
        
        # 6. Create the Reply
        reply = (
            f"✅ Plan Generated:\n"
            f"Task: {data.get('task_name')}\n"
            f"Start: {data.get('start_time')}\n"
            f"End: {data.get('end_time')}\n"
            f"Project: {data.get('project', 'No Project')}\n"
            f"Reminder: {data.get('reminder_time')}\n\n"
            f"(Reply 'Save' to confirm)"
        )
        
        print("📨 Sending Reply to Telegram...")
        await update.message.reply_text(reply)
        print("🚀 Reply Sent Successfully!")

    except json.JSONDecodeError:
        print(f"❌ JSON ERROR. The AI replied with: {raw_text}")
        await update.message.reply_text("⚠️ Technical Error: AI returned invalid data. Check terminal.")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {e}")
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

# --- LOCAL POLLING (FOR TESTING) ---
if __name__ == "__main__":
    print("🚀 Running locally via Polling...")
    
    # 1. Build the Bot
    local_bot = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # 2. Add Handlers
    local_bot.add_handler(CommandHandler("start", start))
    local_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # 3. Run
    local_bot.run_polling()