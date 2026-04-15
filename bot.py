import os
import logging
import subprocess
import asyncio
import httpx
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "root")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Local Bot API server URL (running via Docker)
LOCAL_API_URL = "http://localhost:8081/bot"
LOCAL_API_DIR = "/var/lib/telegram-bot-api"  # path inside container
VOLUME_HOST_PATH = "/home/azureuser/telegram-api-data"
MAX_PARALLEL_UPLOADS = int(os.getenv("MAX_PARALLEL_UPLOADS", "3"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def fix_volume_permissions():
    try:
        subprocess.run(
            ["sudo", "chmod", "-R", "o+rx", "/home/azureuser/telegram-api-data/"],
            check=True, capture_output=True
        )
    except Exception as e:
        logger.warning(f"Could not fix volume permissions: {e}")


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


async def upload_to_drive(
    drive,
    filepath: str,
    filename: str,
    file_size: int,
    progress_callback=None
):
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    media = MediaFileUpload(
        filepath,
        resumable=True,
        chunksize=5 * 1024 * 1024
    )
    metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID]
    }

    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,webViewLink"
    )

    response = None
    while response is None:
        # Run blocking chunk upload in a worker thread so multiple uploads can progress in parallel.
        status, response = await asyncio.to_thread(request.next_chunk)
        if status and progress_callback and file_size:
            uploaded_bytes = int(status.resumable_progress)
            await progress_callback(uploaded_bytes, file_size)

    if progress_callback and file_size:
        await progress_callback(file_size, file_size)

    await asyncio.to_thread(
        service.permissions().create(
            fileId=response["id"],
            body={"type": "anyone", "role": "reader"}
        ).execute
    )

    return response.get("webViewLink") or f"https://drive.google.com/file/d/{response['id']}/view?usp=drivesdk"


async def download_via_local_api(
    file_id: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None
) -> bool:
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

        fix_volume_permissions()
        if not os.path.exists(host_file_path):
            logger.warning(f"File not found on host at: {host_file_path}")
            return False

        downloaded = 0
        chunk_size = 1024 * 1024
        with open(host_file_path, "rb") as src, open(local_path, "wb") as dst:
            while True:
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                dst.write(chunk)
                downloaded += len(chunk)
                if progress_callback and file_size:
                    await progress_callback(downloaded, file_size)

        if progress_callback and file_size:
            await progress_callback(file_size, file_size)
        logger.info(f"✅ Copied from volume: {local_path}")
        return True

    except Exception as e:
        logger.warning(f"Local volume read failed: {e}")
        return False


async def download_via_cdn(
    file_id: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None
) -> bool:
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
        downloaded = 0

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("GET", cdn_url) as response:
                response.raise_for_status()
                with open(local_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and file_size:
                            await progress_callback(downloaded, file_size)

        if progress_callback and file_size:
            await progress_callback(file_size, file_size)

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
    upload_semaphore = context.bot_data.get("upload_semaphore")

    # Access control: only allow the owner to use file handlers.
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

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

    progress_msg = await message.reply_text(
        f"{emoji} {filename}\n"
        f"📦 {size_mb} MB\n\n"
        f"⬇️ Downloading... 0%"
    )

    progress_state = {"download": -1, "upload": -1}

    async def update_download_progress(done: int, total: int):
        if not total:
            return
        percent = min(100, int(done * 100 / total))
        if percent == progress_state["download"] or (percent < 100 and percent - progress_state["download"] < 2):
            return
        progress_state["download"] = percent
        done_mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        try:
            bar = progress_bar(percent)
            await progress_msg.edit_text(
                f"{emoji} {filename}\n"
                f"📦 {size_mb} MB\n\n"
                f"⬇️ Downloading... {percent}%\n"
                f"{bar}\n"
                f"{done_mb:.2f}/{total_mb:.2f} MB"
            )
        except Exception:
            pass

    async def update_upload_progress(done: int, total: int):
        if not total:
            return
        percent = min(100, int(done * 100 / total))
        if percent == progress_state["upload"] or (percent < 100 and percent - progress_state["upload"] < 2):
            return
        progress_state["upload"] = percent
        done_mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        try:
            bar = progress_bar(percent)
            await progress_msg.edit_text(
                f"{emoji} {filename}\n"
                f"📦 {size_mb} MB\n\n"
                f"☁️ Uploading to Google Drive... {percent}%\n"
                f"{bar}\n"
                f"{done_mb:.2f}/{total_mb:.2f} MB"
            )
        except Exception:
            pass

    # Try local API first (supports up to 2GB)
    downloaded = await download_via_local_api(
        file_id,
        local_path,
        file_size=file_size or 0,
        progress_callback=update_download_progress
    )

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
        downloaded = await download_via_cdn(
            file_id,
            local_path,
            file_size=file_size or 0,
            progress_callback=update_download_progress
        )

    if not downloaded:
        await message.reply_text("❌ Could not download the file. Please try again.")
        return

    # Upload to Google Drive
    await update_upload_progress(0, file_size or 0)
    try:
        if upload_semaphore:
            await progress_msg.edit_text(
                f"{emoji} {filename}\n"
                f"📦 {size_mb} MB\n\n"
                f"⏳ Waiting for upload slot..."
            )

            async with upload_semaphore:
                await update_upload_progress(0, file_size or 0)
                link = await upload_to_drive(
                    drive,
                    local_path,
                    filename,
                    file_size=file_size or 0,
                    progress_callback=update_upload_progress
                )
        else:
            link = await upload_to_drive(
                drive,
                local_path,
                filename,
                file_size=file_size or 0,
                progress_callback=update_upload_progress
            )
        await progress_msg.edit_text(
            f"✅ *Uploaded!*\n"
            f"🔗 [View on Google Drive]({link})",
            parse_mode="Markdown"
        )
        logger.info(f"Uploaded: {filename} ({size_mb} MB)")
    except Exception as e:
        logger.error(f"Drive upload failed: {e}")
        await progress_msg.edit_text(f"❌ Google Drive upload failed:\n`{str(e)}`", parse_mode="Markdown")
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


def main():
    fix_volume_permissions()
    logger.info("Authenticating with Google Drive...")
    drive = get_drive()
    logger.info("✅ Google Drive ready")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .base_url(LOCAL_API_URL)   # Use local API server
        .local_mode(True)          # Enable local mode for large files
        .concurrent_updates(True)
        .build()
    )

    app.bot_data["drive"] = drive
    app.bot_data["upload_semaphore"] = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    logger.info(f"🤖 Bot is running... (parallel uploads: {MAX_PARALLEL_UPLOADS})")
    app.run_polling()


if __name__ == "__main__":
    main()
