import os
import requests
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

# ───── PROGRESS BAR ─────
def progress_bar(percent):
    filled = percent // 10
    return "█" * filled + "░" * (10 - filled)

# ───── HANDLE FILE ─────
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message.document:
        await message.reply_text("Send a file.")
        return

    file = message.document
    file_id = file.file_id
    file_name = file.file_name
    file_size = file.file_size

    local_path = f"./{file_name}"

    progress_msg = await message.reply_text(f"⬇️ Downloading: {file_name}")

    try:
        # ✅ ALWAYS use SDK to get file
        tg_file = await context.bot.get_file(file_id)

        file_path = tg_file.file_path

        if not file_path:
            await message.reply_text("❌ Invalid file path")
            return

        # `get_file` may return an absolute URL; convert to relative Telegram file path.
        prefixes = [
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/",
            f"http://127.0.0.1:8081/file/bot{TELEGRAM_TOKEN}/",
            f"http://localhost:8081/file/bot{TELEGRAM_TOKEN}/",
        ]
        for prefix in prefixes:
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
                break
        file_path = file_path.lstrip("/")

        # 🔥 BIG FILE → LOCAL API
        if file_size > 20 * 1024 * 1024:
            print("🚀 Using LOCAL API")
            download_url = f"http://127.0.0.1:8081/file/bot{TELEGRAM_TOKEN}/{file_path}"

        else:
            print("🌐 Using TELEGRAM CDN")
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

        r = requests.get(download_url, stream=True, timeout=120)

        if r.status_code != 200:
            await message.reply_text(f"❌ Download failed ({r.status_code})")
            return

        downloaded = 0

        with open(local_path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    percent = int(downloaded * 100 / file_size)
                    mb_done = downloaded / (1024 * 1024)
                    mb_total = file_size / (1024 * 1024)

                    try:
                        await progress_msg.edit_text(
                            f"⬇️ Downloading...\n"
                            f"{percent}%\n"
                            f"{mb_done:.2f}/{mb_total:.2f} MB"
                        )
                    except:
                        pass

    except Exception as e:
        await message.reply_text(f"❌ Download error: {str(e)}")
        return

    await progress_msg.edit_text("☁️ Uploading to Google Drive...")

    # ✅ Upload
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
