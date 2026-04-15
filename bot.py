import os
import logging
import time
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.http import MediaFileUpload

# ── Load environment ─────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "root")

logging.basicConfig(level=logging.INFO)

# ── Google Drive Auth ────────────────────
def get_drive():
    gauth = GoogleAuth(settings_file="settings.yaml")

    # Load saved credentials (VPS)
    if os.path.exists("saved_creds.json"):
        gauth.LoadCredentialsFile("saved_creds.json")

    if gauth.credentials is None:
        print("⚠️ First-time auth required (run locally)")
        gauth.CommandLineAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    gauth.SaveCredentialsFile("saved_creds.json")

    return GoogleDrive(gauth)

drive = get_drive()

# ── Upload with REAL progress ────────────
def upload_to_drive_with_progress(filepath, filename, progress_message):
    service = drive.auth.service

    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID]
    }

    media = MediaFileUpload(filepath, resumable=True)

    request = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    )

    response = None
    start_time = time.time()

    while response is None:
        status, response = request.next_chunk()

        if status:
            progress = int(status.progress() * 100)

            uploaded_bytes = status.resumable_progress
            total_bytes = status.total_size or 1

            uploaded_mb = uploaded_bytes / (1024 * 1024)
            total_mb = total_bytes / (1024 * 1024)

            elapsed = time.time() - start_time
            speed = uploaded_mb / elapsed if elapsed > 0 else 0

            remaining_mb = total_mb - uploaded_mb
            eta = remaining_mb / speed if speed > 0 else 0

            # Update message
            progress_message.edit_text(
                f"☁️ Uploading... {progress}%\n"
                f"{uploaded_mb:.2f} / {total_mb:.2f} MB\n"
                f"⚡ {speed:.2f} MB/s | ⏱ {int(eta)}s left"
            )

    file_id = response.get("id")
    return f"https://drive.google.com/file/d/{file_id}/view"

# ── Handle incoming files ────────────────
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.document:
        tg_file = await message.document.get_file()
        filename = message.document.file_name
        file_size = message.document.file_size

    elif message.video:
        tg_file = await message.video.get_file()
        filename = message.video.file_name or "video.mp4"
        file_size = message.video.file_size

    elif message.photo:
        tg_file = await message.photo[-1].get_file()
        filename = f"photo_{tg_file.file_id}.jpg"
        file_size = message.photo[-1].file_size

    else:
        await message.reply_text("Send a file/photo/video.")
        return

    total_mb = file_size / (1024 * 1024)

    # ── Download ──
    download_msg = await message.reply_text(
        f"⬇️ Downloading...\n0 / {total_mb:.2f} MB"
    )

    local_path = f"/tmp/{filename}"

    await tg_file.download_to_drive(local_path)

    await download_msg.edit_text(
        f"✅ Downloaded {total_mb:.2f} MB"
    )

    # ── Upload ──
    upload_msg = await message.reply_text("☁️ Starting upload...")

    try:
        link = upload_to_drive_with_progress(local_path, filename, upload_msg)

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
        filters.Document.ALL | filters.VIDEO | filters.PHOTO,
        handle_file
    ))

    print("🤖 Bot is running...")
    app.run_polling()