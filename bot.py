import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# ── Load env ─────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "root")

logging.basicConfig(level=logging.INFO)

# ── Google Drive Auth ────────────────────
def get_drive():
    gauth = GoogleAuth(settings_file="settings.yaml")

    # Load creds from Railway / VPS env (if exists)
    creds_json = os.getenv("GOOGLE_CREDS")
    if creds_json:
        with open("saved_creds.json", "w") as f:
            f.write(creds_json)

    # Use command line auth (no browser needed)
    gauth.CommandLineAuth()
    return GoogleDrive(gauth)

drive = get_drive()

# ── Upload to Drive ──────────────────────
def upload_to_drive(filepath, filename):
    file = drive.CreateFile({
        "title": filename,
        "parents": [{"id": DRIVE_FOLDER_ID}]
    })
    file.SetContentFile(filepath)
    file.Upload()

    file.InsertPermission({
        "type": "anyone",
        "value": "anyone",
        "role": "reader"
    })

    return f"https://drive.google.com/file/d/{file['id']}/view"


# ── Fake upload progress ─────────────────
async def fake_upload_progress(message):
    msg = await message.reply_text("☁️ Uploading... 0%")

    for i in range(10, 101, 10):
        await asyncio.sleep(0.4)
        await msg.edit_text(f"☁️ Uploading... {i}%")

    return msg


# ── Handle incoming file ─────────────────
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # Detect file type
    if message.document:
        tg_file = await message.document.get_file()
        filename = message.document.file_name
    elif message.photo:
        tg_file = await message.photo[-1].get_file()
        filename = f"photo_{tg_file.file_id}.jpg"
    elif message.video:
        tg_file = await message.video.get_file()
        filename = message.video.file_name or f"video_{tg_file.file_id}.mp4"
    elif message.audio:
        tg_file = await message.audio.get_file()
        filename = message.audio.file_name or f"audio_{tg_file.file_id}.mp3"
    elif message.voice:
        tg_file = await message.voice.get_file()
        filename = f"voice_{tg_file.file_id}.ogg"
    else:
        await message.reply_text("Send a file/photo/video/audio.")
        return

    # ── Download progress ──
    progress_msg = await message.reply_text("⬇️ Downloading... 0%")

    local_path = f"/tmp/{filename}"

    # Simulated download progress
    for i in range(10, 101, 10):
        await asyncio.sleep(0.2)
        await progress_msg.edit_text(f"⬇️ Downloading... {i}%")

    # Actual download
    await tg_file.download_to_drive(local_path)

    await progress_msg.edit_text("✅ Download complete!")

    # ── Upload progress ──
    upload_msg = await fake_upload_progress(message)

    try:
        link = upload_to_drive(local_path, filename)

        await upload_msg.edit_text(
            f"✅ Uploaded!\n\n🔗 {link}"
        )

    except Exception as e:
        await message.reply_text(f"❌ Upload failed: {str(e)}")

    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


# ── Run bot ─────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    print("🤖 Bot is running...")
    app.run_polling()