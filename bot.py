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

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if not message.document:
        await message.reply_text("Send a file.")
        return

    file = message.document
    file_id = file.file_id
    file_name = file.file_name

    local_path = f"./{file_name}"

    progress_msg = await message.reply_text(f"⬇️ Downloading: {file_name}")

    try:
        tg_file = await context.bot.get_file(file_id)

        try:
            # 🔥 TRY LOCAL API FIRST
            await tg_file.download_to_drive(local_path)

        except Exception:
            print("⚠️ Local failed → switching to CDN")

            # 🔥 FORCE TELEGRAM CDN DOWNLOAD
            download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{tg_file.file_path}"

            import requests
            r = requests.get(download_url, stream=True)

            if r.status_code != 200:
                await message.reply_text("❌ Download failed completely")
                return

            with open(local_path, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)

    except Exception as e:
        await message.reply_text(f"❌ Download error: {str(e)}")
        return

    await progress_msg.edit_text("☁️ Uploading to Google Drive...")

    # ✅ Upload to Drive
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