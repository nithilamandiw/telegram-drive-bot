import os
import logging
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "root")

# Local Bot API server URL (running via Docker)
LOCAL_API_URL = "http://localhost:8081/bot"
LOCAL_API_DIR = "/var/lib/telegram-bot-api"  # path inside container
VOLUME_HOST_PATH = "/home/azureuser/telegram-api-data"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_drive():
    gauth = GoogleAuth(settings_file="settings.yaml")
    gauth.LoadCredentialsFile("saved_creds.json")
    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    gauth.SaveCredentialsFile("saved_creds.json")
    return GoogleDrive(gauth)


def upload_to_drive(drive, filepath, filename):
    file_metadata = {
        "title": filename,
        "parents": [{"id": DRIVE_FOLDER_ID}]
    }
    drive_file = drive.CreateFile(file_metadata)
    drive_file.SetContentFile(filepath)
    drive_file.Upload()
    drive_file.InsertPermission({
        "type": "anyone",
        "value": "anyone",
        "role": "reader"
    })
    link = f"https://drive.google.com/file/d/{drive_file['id']}/view?usp=drivesdk"
    return link


async def download_via_local_api(file_id: str, local_path: str) -> bool:
    """Read file directly from the Docker volume on disk — no HTTP needed."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{LOCAL_API_URL}{TELEGRAM_TOKEN}/getFile",
                json={"file_id": file_id}
            )
            data = resp.json()

        if not data.get("ok"):
            logger.warning(f"Local API getFile failed: {data}")
            return False

        file_path = data["result"]["file_path"]
        # file_path is like: /var/lib/telegram-bot-api/1875002742:.../documents/file_3.psd
        # Remap it to the host volume path
        host_file_path = file_path.replace(LOCAL_API_DIR, VOLUME_HOST_PATH, 1)

        logger.info(f"Reading directly from volume: {host_file_path}")

        if not os.path.exists(host_file_path):
            logger.warning(f"File not found on host at: {host_file_path}")
            return False

        import shutil
        shutil.copy2(host_file_path, local_path)
        logger.info(f"✅ Copied from volume: {local_path}")
        return True

    except Exception as e:
        logger.warning(f"Local volume read failed: {e}")
        return False


async def download_via_cdn(file_id: str, local_path: str) -> bool:
    """Fallback: standard Telegram CDN (20MB limit only).
    We call the PUBLIC api.telegram.org here — NOT the local server —
    because the local server's file_path is an absolute filesystem path
    that the public CDN doesn't understand.
    """
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Get file info from PUBLIC Telegram API
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                json={"file_id": file_id}
            )
            data = resp.json()

        if not data.get("ok"):
            logger.warning(f"CDN getFile failed: {data}")
            return False

        cdn_file_path = data["result"]["file_path"]
        cdn_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{cdn_file_path}"

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("GET", cdn_url) as response:
                response.raise_for_status()
                with open(local_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)

        logger.info(f"✅ Downloaded via CDN: {local_path}")
        return True

    except Exception as e:
        logger.warning(f"CDN download failed: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello! I'm your *Google Drive Upload Bot*.\n\n"
        "Send me any file, photo, video, audio or voice message "
        "and I'll upload it to your Google Drive! 🚀\n\n"
        "✅ Supports files up to *2GB* via local API server.",
        parse_mode="Markdown"
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    drive = context.bot_data.get("drive")

    # Detect file type
    if message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or f"file_{file_id[:8]}"
        file_size = message.document.file_size
        emoji = "📄"
    elif message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = f"photo_{file_id[:8]}.jpg"
        file_size = photo.file_size
        emoji = "🖼"
    elif message.video:
        file_id = message.video.file_id
        filename = message.video.file_name or f"video_{file_id[:8]}.mp4"
        file_size = message.video.file_size
        emoji = "🎥"
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or f"audio_{file_id[:8]}.mp3"
        file_size = message.audio.file_size
        emoji = "🎵"
    elif message.voice:
        file_id = message.voice.file_id
        filename = f"voice_{file_id[:8]}.ogg"
        file_size = message.voice.file_size
        emoji = "🎤"
    else:
        await message.reply_text("❓ Please send a file, photo, video, audio or voice message.")
        return

    size_mb = round(file_size / (1024 * 1024), 2) if file_size else "?"
    local_path = f"/tmp/{filename}"

    await message.reply_text(
        f"{emoji} *{filename}*\n"
        f"📦 `{size_mb} MB`\n\n"
        f"⬇️ Downloading...",
        parse_mode="Markdown"
    )

    # Try local API first (supports up to 2GB)
    downloaded = await download_via_local_api(file_id, local_path)

    # Fallback to CDN only for files under 20MB
    if not downloaded:
        if file_size and file_size > 20 * 1024 * 1024:
            await message.reply_text(
                f"❌ *Download failed!*\n\n"
                f"This file is `{size_mb} MB` — too large for the CDN fallback.\n"
                f"Make sure your local API Docker container is running:\n"
                f"`docker ps`",
                parse_mode="Markdown"
            )
            logger.error(f"Local API failed and file is too large for CDN ({size_mb} MB)")
            return

        logger.info("Falling back to Telegram CDN (file is under 20MB)...")
        downloaded = await download_via_cdn(file_id, local_path)

    if not downloaded:
        await message.reply_text("❌ Could not download the file. Please try again.")
        return

    # Upload to Google Drive
    status_msg = await message.reply_text("☁️ Uploading to Google Drive...")
    try:
        link = upload_to_drive(drive, local_path, filename)
        await status_msg.edit_text(
            f"✅ *Uploaded!*\n"
            f"🔗 [View on Google Drive]({link})",
            parse_mode="Markdown"
        )
        logger.info(f"Uploaded: {filename} ({size_mb} MB)")
    except Exception as e:
        logger.error(f"Drive upload failed: {e}")
        await status_msg.edit_text(f"❌ Google Drive upload failed:\n`{str(e)}`", parse_mode="Markdown")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


def main():
    logger.info("Authenticating with Google Drive...")
    drive = get_drive()
    logger.info("✅ Google Drive ready")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .base_url(LOCAL_API_URL)   # Use local API server
        .local_mode(True)          # Enable local mode for large files
        .build()
    )

    app.bot_data["drive"] = drive

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    logger.info("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
