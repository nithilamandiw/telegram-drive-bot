import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# ───── ENV ─────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

# ───── GOOGLE DRIVE ─────
def get_drive():
    gauth = GoogleAuth(settings_file="settings.yaml")

    if os.path.exists("saved_creds.json"):
        gauth.LoadCredentialsFile("saved_creds.json")

    if gauth.credentials is None:
        raise Exception("❌ Missing Google credentials")

    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    gauth.SaveCredentialsFile("saved_creds.json")

    return GoogleDrive(gauth)

drive = get_drive()

# ───── HANDLE FILE ─────
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message.document:
        await message.reply_text("Send a file.")
        return

    file_id = message.document.file_id
    file_name = message.document.file_name

    local_path = f"./{file_name}"

    progress_msg = await message.reply_text(f"⬇️ Downloading: {file_name}")

    try:
        # ✅ SAFE DOWNLOAD (NO requests)
        tg_file = await context.bot.get_file(file_id)

        await tg_file.download_to_drive(local_path)

    except Exception as e:
        await message.reply_text(f"❌ Download failed: {str(e)}")
        return

    await progress_msg.edit_text("☁️ Uploading to Google Drive...")

    # ───── UPLOAD ─────
    try:
        gfile = drive.CreateFile({
            "title": file_name,
            "parents": [{"id": DRIVE_FOLDER_ID}]
        })

        gfile.SetContentFile(local_path)
        gfile.Upload()

        link = gfile["alternateLink"]

        await message.reply_text(f"✅ Uploaded!\n🔗 {link}")

    except Exception as e:
        await message.reply_text(f"❌ Upload failed: {str(e)}")

    finally:
        if os.path.exists(local_path):
            os.remove(local_path)

# ───── RUN BOT ─────
app = ApplicationBuilder() \
    .token(TELEGRAM_TOKEN) \
    .base_url("http://127.0.0.1:8081/bot") \
    .build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

print("🤖 Bot is running...")

app.run_polling()