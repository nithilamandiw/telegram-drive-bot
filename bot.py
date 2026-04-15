import os
import json
import re
import logging
import subprocess
import asyncio
import uuid
import time
import httpx
import requests
from datetime import datetime
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
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
ANALYTICS_FILE = "analytics.json"
URL_PATTERN = re.compile(r"(https?://\S+)", re.IGNORECASE)
MAX_URL_DOWNLOAD_SIZE = int(os.getenv("MAX_URL_DOWNLOAD_SIZE", str(2 * 1024 * 1024 * 1024)))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TransferCancelled(Exception):
    pass


def _callback_store(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data.setdefault("callback_payloads", {})


def make_callback_data(context: ContextTypes.DEFAULT_TYPE, action: str, **payload) -> str:
    token = uuid.uuid4().hex[:16]
    _callback_store(context)[token] = {"action": action, **payload}
    return f"cb_{token}"


def resolve_callback_data(context: ContextTypes.DEFAULT_TYPE, data: str):
    if not data or not data.startswith("cb_"):
        return None
    token = data[3:]
    return _callback_store(context).get(token)


def progress_bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def format_size(size_bytes) -> str:
    if size_bytes is None:
        return "Unknown size"
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "Unknown size"

    units = ["B", "KB", "MB", "GB", "TB"]
    unit_idx = 0
    while size >= 1024 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    return f"{size:.1f} {units[unit_idx]}"


def format_bytes_stats(num_bytes) -> str:
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return "0.00 MB"

    gb = value / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.2f} GB"

    mb = value / (1024 ** 2)
    return f"{mb:.2f} MB"


def default_analytics_data() -> dict:
    return {
        "total_uploads": 0,
        "total_downloads": 0,
        "total_size": 0,
        "daily": {},
        "types": {}
    }


def load_analytics() -> dict:
    if not os.path.exists(ANALYTICS_FILE):
        return default_analytics_data()

    try:
        with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load analytics file, using defaults: {e}")
        return default_analytics_data()

    defaults = default_analytics_data()
    if not isinstance(data, dict):
        return defaults

    merged = {
        "total_uploads": int(data.get("total_uploads", defaults["total_uploads"]) or 0),
        "total_downloads": int(data.get("total_downloads", defaults["total_downloads"]) or 0),
        "total_size": int(data.get("total_size", defaults["total_size"]) or 0),
        "daily": data.get("daily", defaults["daily"]) if isinstance(data.get("daily", {}), dict) else {},
        "types": data.get("types", defaults["types"]) if isinstance(data.get("types", {}), dict) else {},
    }
    return merged


def save_analytics(data: dict):
    tmp_file = f"{ANALYTICS_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, ANALYTICS_FILE)


def update_upload_analytics(filename: str, file_size: int):
    data = load_analytics()
    size = int(file_size or 0)

    data["total_uploads"] = int(data.get("total_uploads", 0)) + 1
    data["total_size"] = int(data.get("total_size", 0)) + size

    today = datetime.now().strftime("%Y-%m-%d")
    daily = data.setdefault("daily", {})
    today_stats = daily.setdefault(today, {"uploads": 0, "size": 0})
    today_stats["uploads"] = int(today_stats.get("uploads", 0)) + 1
    today_stats["size"] = int(today_stats.get("size", 0)) + size

    ext = os.path.splitext((filename or "").lower())[1].lstrip(".") or "no_ext"
    types = data.setdefault("types", {})
    types[ext] = int(types.get(ext, 0)) + 1

    save_analytics(data)


def update_download_analytics():
    data = load_analytics()
    data["total_downloads"] = int(data.get("total_downloads", 0)) + 1
    save_analytics(data)


def extract_first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    if not match:
        return None
    # Trim trailing punctuation that often appears in chat messages.
    return match.group(1).rstrip(").,]}>\"'")


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    raw_name = os.path.basename(parsed.path or "")
    decoded = unquote(raw_name).strip()
    if not decoded:
        return f"url_file_{uuid.uuid4().hex[:8]}.bin"

    safe_name = decoded.replace("/", "_").replace("\\", "_")
    return safe_name or f"url_file_{uuid.uuid4().hex[:8]}.bin"


async def get_url_content_length(url: str) -> int:
    def _head() -> int:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        content_length = resp.headers.get("Content-Length")
        if not content_length:
            return 0
        try:
            return max(0, int(content_length))
        except (TypeError, ValueError):
            return 0

    try:
        return await asyncio.to_thread(_head)
    except Exception:
        return 0


async def download_url_with_requests(
    url: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None,
    should_cancel=None,
    task_state=None,
) -> bool:
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def emit(event: str, *payload):
        loop.call_soon_threadsafe(queue.put_nowait, (event, *payload))

    def worker():
        downloaded = 0
        try:
            if task_state is not None:
                task_state["url"] = url
                task_state["file_path"] = local_path
                task_state["downloaded"] = 0

            with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=True) as response:
                response.raise_for_status()
                with open(local_path, "wb") as dst:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if should_cancel and should_cancel():
                            emit("cancel")
                            return

                        while task_state and task_state.get("paused"):
                            if should_cancel and should_cancel():
                                emit("cancel")
                                return
                            time.sleep(0.25)

                        if not chunk:
                            continue

                        dst.write(chunk)
                        downloaded += len(chunk)

                        if task_state is not None:
                            task_state["downloaded"] = downloaded

                        if MAX_URL_DOWNLOAD_SIZE > 0 and downloaded > MAX_URL_DOWNLOAD_SIZE:
                            emit("too_large", downloaded)
                            return

                        emit("progress", downloaded)

            emit("done", downloaded)
        except requests.exceptions.Timeout:
            emit("error", "Timeout while downloading URL")
        except requests.exceptions.RequestException as e:
            emit("error", f"Connection failed: {e}")
        except Exception as e:
            emit("error", str(e))

    worker_task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        while True:
            event, *payload = await queue.get()

            if event == "progress":
                downloaded = int(payload[0])
                if progress_callback:
                    await progress_callback(downloaded, file_size)
                continue

            if event == "cancel":
                raise TransferCancelled("Download cancelled by user")

            if event == "too_large":
                downloaded = int(payload[0])
                raise RuntimeError(
                    f"File exceeds max allowed size ({format_bytes_stats(MAX_URL_DOWNLOAD_SIZE)}). "
                    f"Downloaded: {format_bytes_stats(downloaded)}"
                )

            if event == "error":
                raise RuntimeError(payload[0])

            if event == "done":
                downloaded = int(payload[0])
                if progress_callback and file_size:
                    await progress_callback(file_size, file_size)
                elif progress_callback and downloaded:
                    await progress_callback(downloaded, downloaded)
                return True
    finally:
        await worker_task


def get_category(filename: str) -> str:
    ext = os.path.splitext((filename or "").lower())[1]
    if ext in {".mp4", ".mkv", ".avi"}:
        return "Videos"
    if ext in {".zip", ".rar", ".7z"}:
        return "Archives"
    if ext in {".psd", ".ai"}:
        return "Design"
    if ext in {".jpg", ".jpeg", ".png"}:
        return "Images"
    if ext in {".pdf", ".docx"}:
        return "Documents"
    return "Others"


def clean_drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


async def get_public_permission_id(service, file_id: str):
    perms = await asyncio.to_thread(
        service.permissions().list(
            fileId=file_id,
            fields="permissions(id,type,role)"
        ).execute
    )
    for perm in perms.get("permissions", []):
        if perm.get("type") == "anyone" and perm.get("role") == "reader":
            return perm.get("id")
    return None


async def ensure_category_folder(context: ContextTypes.DEFAULT_TYPE, service, category: str) -> str:
    folders_cache = context.bot_data.setdefault("folders", {})
    if category in folders_cache:
        return folders_cache[category]

    query = (
        f"'{DRIVE_FOLDER_ID}' in parents and "
        f"mimeType='application/vnd.google-apps.folder' and "
        f"name='{escape_drive_query_value(category)}' and trashed=false"
    )
    result = await asyncio.to_thread(
        service.files().list(
            q=query,
            fields="files(id,name)",
            pageSize=1
        ).execute
    )
    items = result.get("files", [])
    if items:
        folder_id = items[0]["id"]
        folders_cache[category] = folder_id
        return folder_id

    created = await asyncio.to_thread(
        service.files().create(
            body={
                "name": category,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [DRIVE_FOLDER_ID],
            },
            fields="id"
        ).execute
    )
    folder_id = created["id"]
    folders_cache[category] = folder_id
    return folder_id


async def is_file_public(service, file_id: str) -> bool:
    return await get_public_permission_id(service, file_id) is not None


async def ensure_public_permission(service, file_id: str) -> str:
    existing = await get_public_permission_id(service, file_id)
    if existing:
        return existing
    created = await asyncio.to_thread(
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id"
        ).execute
    )
    return created["id"]


async def revoke_public_after_delay(context: ContextTypes.DEFAULT_TYPE, file_id: str, permission_id: str, delay_seconds: int):
    try:
        await asyncio.sleep(delay_seconds)
        drive = context.bot_data.get("drive")
        if not drive:
            return
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)
        await asyncio.to_thread(
            service.permissions().delete(
                fileId=file_id,
                permissionId=permission_id
            ).execute
        )
        logger.info(f"Expired public link revoked for file: {file_id}")
    except Exception as e:
        logger.warning(f"Failed to revoke expiring link for {file_id}: {e}")


async def build_upload_action_keyboard(context: ContextTypes.DEFAULT_TYPE, service, file_id: str):
    is_public = await is_file_public(service, file_id)
    toggle_button = (
        InlineKeyboardButton("🔒 Make Private", callback_data=make_callback_data(context, "private", file_id=file_id))
        if is_public else
        InlineKeyboardButton("🌐 Make Public", callback_data=make_callback_data(context, "public", file_id=file_id))
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_drive_file_url(file_id))],
        [toggle_button],
        [
            InlineKeyboardButton("⏱ 1h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=3600)),
            InlineKeyboardButton("⏱ 24h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=86400)),
        ],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=make_callback_data(context, "rename", file_id=file_id)),
            InlineKeyboardButton("🗑 Delete", callback_data=make_callback_data(context, "delete_upload", file_id=file_id))
        ]
    ])


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
    context: ContextTypes.DEFAULT_TYPE,
    drive,
    filepath: str,
    filename: str,
    file_size: int,
    progress_callback=None,
    should_cancel=None
):
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    media = MediaFileUpload(
        filepath,
        resumable=True,
        chunksize=5 * 1024 * 1024
    )
    category = get_category(filename)
    parent_folder_id = await ensure_category_folder(context, service, category)
    metadata = {
        "name": filename,
        "parents": [parent_folder_id]
    }

    request = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,webViewLink"
    )

    response = None
    while response is None:
        if should_cancel and should_cancel():
            raise TransferCancelled("Upload cancelled by user")

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

    file_id = response["id"]
    return file_id, clean_drive_file_url(file_id)


async def download_via_local_api(
    file_id: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None,
    should_cancel=None,
    task_state=None
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
        if task_state is not None:
            task_state["url"] = host_file_path
            task_state["file_path"] = local_path

        logger.info(f"Reading directly from volume: {host_file_path}")

        fix_volume_permissions()
        if not os.path.exists(host_file_path):
            logger.warning(f"File not found on host at: {host_file_path}")
            return False

        downloaded = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        if task_state is not None:
            task_state["downloaded"] = downloaded

        if file_size and downloaded >= file_size:
            if progress_callback:
                await progress_callback(file_size, file_size)
            return True

        chunk_size = 1024 * 1024
        with open(host_file_path, "rb") as src, open(local_path, "ab" if downloaded > 0 else "wb") as dst:
            if downloaded > 0:
                src.seek(downloaded)
            while True:
                if task_state and task_state.get("cancel"):
                    raise TransferCancelled("Download cancelled by user")
                if task_state and task_state.get("paused"):
                    await asyncio.sleep(1)
                    continue
                chunk = src.read(chunk_size)
                if not chunk:
                    break
                if should_cancel and should_cancel():
                    raise TransferCancelled("Download cancelled by user")
                dst.write(chunk)
                downloaded += len(chunk)
                if task_state is not None:
                    task_state["downloaded"] = downloaded
                if progress_callback and file_size:
                    await progress_callback(downloaded, file_size)

        if progress_callback and file_size:
            await progress_callback(file_size, file_size)
        logger.info(f"✅ Copied from volume: {local_path}")
        return True

    except TransferCancelled:
        raise
    except Exception as e:
        logger.warning(f"Local volume read failed: {e}")
        return False


async def download_via_cdn(
    file_id: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None,
    should_cancel=None,
    task_state=None
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

        downloaded = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        if task_state is not None:
            task_state["url"] = cdn_url
            task_state["file_path"] = local_path
            task_state["downloaded"] = downloaded

        if file_size and downloaded >= file_size:
            if progress_callback:
                await progress_callback(file_size, file_size)
            return True

        headers = {}
        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"

        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("GET", cdn_url, headers=headers) as response:
                if response.status_code == 416:
                    if progress_callback and file_size:
                        await progress_callback(file_size, file_size)
                    return True
                if downloaded > 0 and response.status_code == 200:
                    # Server ignored range; restart from scratch safely.
                    downloaded = 0
                    if os.path.exists(local_path):
                        os.remove(local_path)
                response.raise_for_status()
                with open(local_path, "ab" if downloaded > 0 else "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        if task_state and task_state.get("cancel"):
                            raise TransferCancelled("Download cancelled by user")
                        if task_state and task_state.get("paused"):
                            await asyncio.sleep(1)
                            continue
                        if should_cancel and should_cancel():
                            raise TransferCancelled("Download cancelled by user")
                        f.write(chunk)
                        downloaded += len(chunk)
                        if task_state is not None:
                            task_state["downloaded"] = downloaded
                        if progress_callback and file_size:
                            await progress_callback(downloaded, file_size)

        if progress_callback and file_size:
            await progress_callback(file_size, file_size)

        logger.info(f"✅ Downloaded via CDN: {local_path}")
        return True

    except TransferCancelled:
        raise
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


async def storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    try:
        drive = context.bot_data.get("drive")
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)
        about = await asyncio.to_thread(
            service.about().get(fields="storageQuota").execute
        )
        quota = about.get("storageQuota", {})

        total = int(quota.get("limit", 0))
        used = int(quota.get("usage", 0))
        free = max(total - used, 0) if total else 0

        to_gb = lambda b: b / (1024 ** 3)

        if total > 0:
            text = (
                "💾 *Google Drive Storage*\n\n"
                f"Total: `{to_gb(total):.2f} GB`\n"
                f"Used: `{to_gb(used):.2f} GB`\n"
                f"Free: `{to_gb(free):.2f} GB`"
            )
        else:
            text = (
                "💾 *Google Drive Storage*\n\n"
                "Total: `Unlimited`\n"
                f"Used: `{to_gb(used):.2f} GB`\n"
                "Free: `Unlimited`"
            )

        await message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Storage command failed: {e}")
        await message.reply_text(f"❌ Could not fetch storage info:\n`{str(e)}`", parse_mode="Markdown")


async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    try:
        drive = context.bot_data.get("drive")
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)

        about = await asyncio.to_thread(
            service.about().get(fields="storageQuota").execute
        )
        quota = about.get("storageQuota", {})
        usage = int(quota.get("usage", 0))
        limit = int(quota.get("limit", 0))
        free = max(limit - usage, 0) if limit > 0 else 0

        total_files = 0
        page_token = None
        while True:
            resp = await asyncio.to_thread(
                service.files().list(
                    q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
                    fields="nextPageToken,files(id)",
                    pageSize=1000,
                    pageToken=page_token
                ).execute
            )
            total_files += len(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        used_text = format_bytes_stats(usage)
        limit_text = "Unlimited" if limit <= 0 else format_bytes_stats(limit)
        free_text = "Unlimited" if limit <= 0 else format_bytes_stats(free)

        text = (
            "📊 Drive Stats\n"
            f"Used: {used_text} / {limit_text}\n"
            f"Free: {free_text}\n"
            f"Files: {total_files}"
        )
        await message.reply_text(text)
    except Exception as e:
        logger.error(f"Stats command failed: {e}")
        await message.reply_text(f"❌ Could not fetch stats:\n`{str(e)}`", parse_mode="Markdown")


async def analytics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    data = load_analytics()
    today_key = datetime.now().strftime("%Y-%m-%d")
    today_data = data.get("daily", {}).get(today_key, {"uploads": 0, "size": 0})

    types = data.get("types", {})
    top_types = sorted(types.items(), key=lambda x: x[1], reverse=True)[:5]
    types_text = "\n".join([f"{ext}: {count}" for ext, count in top_types]) if top_types else "No uploads yet"

    text = (
        "📊 Usage Stats\n\n"
        f"Uploads: {int(data.get('total_uploads', 0))}\n"
        f"Downloads: {int(data.get('total_downloads', 0))}\n"
        f"Total uploaded: {format_bytes_stats(int(data.get('total_size', 0)))}\n\n"
        "📅 Today:\n"
        f"Uploads: {int(today_data.get('uploads', 0))}\n"
        f"Size: {format_bytes_stats(int(today_data.get('size', 0)))}\n\n"
        "📁 Top file types:\n"
        f"{types_text}"
    )

    await message.reply_text(text)


async def files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    try:
        text, keyboard = await build_files_page(context, page=1, page_size=10)
        await message.reply_text(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Files command failed: {e}")
        await message.reply_text(f"❌ Could not fetch files:\n`{str(e)}`", parse_mode="Markdown")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    raw_query = " ".join(context.args).strip()
    if not raw_query:
        await message.reply_text("Usage: /search <query>")
        return

    query = " ".join(raw_query.split())
    session_id = str(context.user_data.get("search_session_counter", 0) + 1)
    context.user_data["search_session_counter"] = int(session_id)
    context.user_data.setdefault("search_sessions", {})[session_id] = {
        "query": query,
        "tokens": [None],  # page 1 starts with no pageToken
    }

    try:
        drive = context.bot_data.get("drive")
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)

        safe_query = escape_drive_query_value(query.lower().strip())
        suggestion_result = await asyncio.to_thread(
            service.files().list(
                q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false and name contains '{safe_query}'",
                orderBy="createdTime desc",
                pageSize=11,  # 10 suggestions + 1 extra to know if there are more
                fields="nextPageToken,files(id,name)"
            ).execute
        )

        items = suggestion_result.get("files", [])
        if not items:
            await message.reply_text(f"🔎 No files found for: {query}")
            return

        suggestions = items[:10]
        has_more = len(items) > 10 or bool(suggestion_result.get("nextPageToken"))

        keyboard_rows = []
        for item in suggestions:
            file_id = item.get("id")
            name = item.get("name", "Unnamed file")
            if file_id:
                label = name if len(name) <= 40 else f"{name[:37]}..."
                keyboard_rows.append([
                    InlineKeyboardButton(
                        label,
                        callback_data=make_callback_data(context, "open_file_search_suggestion", file_id=file_id, session_id=session_id)
                    )
                ])

        if has_more:
            keyboard_rows.append([
                InlineKeyboardButton(
                    "📚 View all results",
                    callback_data=make_callback_data(context, "search_page", session_id=session_id, page=1)
                )
            ])

        sent = await message.reply_text(
            f"🔎 Suggestions for: {query}",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
            disable_web_page_preview=True
        )
        context.user_data.setdefault("search_message_sessions", {})[sent.message_id] = session_id
    except Exception as e:
        logger.error(f"Search command failed: {e}")
        await message.reply_text(f"❌ Search failed:\n`{str(e)}`", parse_mode="Markdown")


async def build_files_page(context: ContextTypes.DEFAULT_TYPE, page: int, page_size: int = 10):
    drive = context.bot_data.get("drive")
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    # Simulated paging: fetch a bounded latest set, then slice by page index.
    result = await asyncio.to_thread(
        service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false",
            orderBy="createdTime desc",
            pageSize=150,
            fields="files(id,name)"
        ).execute
    )
    all_items = result.get("files", [])

    if not all_items:
        return "📂 No files found in the configured Drive folder.", None

    total_pages = max(1, (len(all_items) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    items = all_items[start:end]

    lines = [f"📂 Select a file\nPage {page}/{total_pages}\n"]
    keyboard_rows = []
    for item in items:
        file_id = item.get("id", "")
        name = item.get("name", "Unnamed file")
        if file_id:
            display_name = name if len(name) <= 40 else f"{name[:37]}..."
            keyboard_rows.append([
                InlineKeyboardButton(
                    display_name,
                    callback_data=make_callback_data(context, "open_file_from_files", file_id=file_id, page=page)
                )
            ])

    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=make_callback_data(context, "files_page", page=page - 1)))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("➡️ Next", callback_data=make_callback_data(context, "files_page", page=page + 1)))
    if buttons:
        keyboard_rows.append(buttons)
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    return "\n".join(lines), keyboard


async def build_search_page(context: ContextTypes.DEFAULT_TYPE, session_id: str, page: int, page_size: int = 10):
    sessions = context.user_data.get("search_sessions", {})
    session = sessions.get(str(session_id))
    if not session:
        return "❌ Search session expired. Run /search again.", None

    query = session.get("query", "")
    tokens = session.setdefault("tokens", [None])

    if page < 1 or (page - 1) >= len(tokens):
        return "❌ Invalid page. Run /search again.", None

    page_token = tokens[page - 1]

    drive = context.bot_data.get("drive")
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    safe_query = escape_drive_query_value(query)
    result = await asyncio.to_thread(
        service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and trashed = false and name contains '{safe_query}'",
            orderBy="createdTime desc",
            pageSize=page_size,
            pageToken=page_token,
            fields="nextPageToken,files(id,name,size,webViewLink)"
        ).execute
    )

    items = result.get("files", [])
    next_token = result.get("nextPageToken")

    # Preserve next-page token chain for button-driven navigation.
    if next_token:
        if len(tokens) == page:
            tokens.append(next_token)
        else:
            tokens[page] = next_token
    elif len(tokens) > page:
        tokens[page] = None

    if not items and page == 1:
        return f"🔎 No files found for: {query}", None

    lines = [f"🔎 Search results for: {query}\nPage {page}\n"]
    keyboard_rows = []
    for item in items:
        file_id = item.get("id", "")
        name = item.get("name", "Unnamed file")
        if file_id:
            display_name = name if len(name) <= 40 else f"{name[:37]}..."
            keyboard_rows.append([
                InlineKeyboardButton(
                    display_name,
                    callback_data=make_callback_data(
                        context,
                        "open_file_from_search_results",
                        file_id=file_id,
                        session_id=session_id,
                        page=page
                    )
                )
            ])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=make_callback_data(context, "search_page", session_id=session_id, page=page - 1)))
    if next_token:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=make_callback_data(context, "search_page", session_id=session_id, page=page + 1)))
    if nav:
        keyboard_rows.append(nav)

    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    return "\n".join(lines), keyboard


async def file_view(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    page: int,
    back_callback: str | None = None,
    delete_callback: str | None = None
):
    query = update.callback_query
    drive = context.bot_data.get("drive")
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    details = await asyncio.to_thread(
        service.files().get(
            fileId=file_id,
            fields="id,name,size"
        ).execute
    )

    name = details.get("name", "Unnamed file")
    size_text = format_size(details.get("size"))
    clean_link = clean_drive_file_url(details["id"])
    public_state = await is_file_public(service, file_id)
    toggle_button = (
        InlineKeyboardButton("🔒 Make Private", callback_data=make_callback_data(context, "private", file_id=file_id))
        if public_state else
        InlineKeyboardButton("🌐 Make Public", callback_data=make_callback_data(context, "public", file_id=file_id))
    )

    back_callback = back_callback or make_callback_data(context, "files_page", page=page)
    delete_callback = delete_callback or make_callback_data(context, "delete_from_files", file_id=file_id, page=page)
    context.user_data["files_current_view"] = {
        "chat_id": query.message.chat_id if query.message else None,
        "message_id": query.message.message_id if query.message else None,
        "file_id": file_id,
        "page": page,
        "back_callback": back_callback,
        "delete_callback": delete_callback
    }

    text = (
        f"📄 {name}\n"
        f"📦 {size_text}\n"
        "🔗 Open using the button below"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_link)],
        [toggle_button],
        [
            InlineKeyboardButton("⏱ 1h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=3600)),
            InlineKeyboardButton("⏱ 24h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=86400)),
        ],
        [InlineKeyboardButton("✏️ Rename", callback_data=make_callback_data(context, "rename", file_id=file_id))],
        [InlineKeyboardButton("🗑 Delete", callback_data=delete_callback)],
        [InlineKeyboardButton("🔙 Back", callback_data=back_callback)]
    ])
    await query.edit_message_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)


async def refresh_file_view_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    file_id: str,
    page: int,
    back_callback: str | None = None,
    delete_callback: str | None = None
):
    drive = context.bot_data.get("drive")
    http = drive.auth.Get_Http_Object()
    service = build("drive", "v3", http=http, cache_discovery=False)

    details = await asyncio.to_thread(
        service.files().get(
            fileId=file_id,
            fields="id,name,size"
        ).execute
    )

    name = details.get("name", "Unnamed file")
    size_text = format_size(details.get("size"))
    clean_link = clean_drive_file_url(details["id"])
    public_state = await is_file_public(service, file_id)
    toggle_button = (
        InlineKeyboardButton("🔒 Make Private", callback_data=make_callback_data(context, "private", file_id=file_id))
        if public_state else
        InlineKeyboardButton("🌐 Make Public", callback_data=make_callback_data(context, "public", file_id=file_id))
    )
    back_callback = back_callback or make_callback_data(context, "files_page", page=page)
    delete_callback = delete_callback or make_callback_data(context, "delete_from_files", file_id=file_id, page=page)

    text = (
        f"📄 {name}\n"
        f"📦 {size_text}\n"
        "🔗 Open using the button below"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_link)],
        [toggle_button],
        [
            InlineKeyboardButton("⏱ 1h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=3600)),
            InlineKeyboardButton("⏱ 24h Link", callback_data=make_callback_data(context, "expire_link", file_id=file_id, duration=86400)),
        ],
        [InlineKeyboardButton("✏️ Rename", callback_data=make_callback_data(context, "rename", file_id=file_id))],
        [InlineKeyboardButton("🗑 Delete", callback_data=delete_callback)],
        [InlineKeyboardButton("🔙 Back", callback_data=back_callback)]
    ])

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


async def files_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    try:
        payload = resolve_callback_data(context, query.data or "")
        if not payload:
            await query.answer("Action expired. Please try again.", show_alert=True)
            return

        action = payload.get("action")

        if action == "files_page":
            page = int(payload.get("page", 1))
            text, keyboard = await build_files_page(context, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
            return

        if action == "search_page":
            session_id = str(payload.get("session_id"))
            page = int(payload.get("page", 1))
            text, keyboard = await build_search_page(context, session_id=session_id, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)
            await query.answer()
            return

        if action == "open_file_from_files":
            file_id = payload.get("file_id")
            page = int(payload.get("page", 1))
            await file_view(update, context, file_id=file_id, page=page)
            await query.answer()
            return

        if action == "open_file_search_suggestion":
            file_id = payload.get("file_id")
            session_id = str(payload.get("session_id"))
            await file_view(
                update,
                context,
                file_id=file_id,
                page=1,
                back_callback=make_callback_data(context, "search_page", session_id=session_id, page=1),
                delete_callback=make_callback_data(context, "delete_from_search", file_id=file_id, session_id=session_id, page=1)
            )
            await query.answer()
            return

        if action == "open_file_from_search_results":
            file_id = payload.get("file_id")
            session_id = str(payload.get("session_id"))
            page = int(payload.get("page", 1))
            await file_view(
                update,
                context,
                file_id=file_id,
                page=page,
                back_callback=make_callback_data(context, "search_page", session_id=session_id, page=page),
                delete_callback=make_callback_data(context, "delete_from_search", file_id=file_id, session_id=session_id, page=page)
            )
            await query.answer()
            return

        if action == "delete_from_files":
            file_id = payload.get("file_id")
            page = int(payload.get("page", 1))
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            text, keyboard = await build_files_page(context, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer("✅ File deleted")
            return

        if action == "delete_from_search":
            file_id = payload.get("file_id")
            session_id = str(payload.get("session_id"))
            page = int(payload.get("page", 1))
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            text, keyboard = await build_search_page(context, session_id=session_id, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)
            await query.answer("✅ File deleted")
            return

        if action == "delete_upload":
            file_id = payload.get("file_id")
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            await query.edit_message_text("✅ File deleted from Google Drive.")
            await query.answer("✅ File deleted")
            return

        if action in {"public", "private"}:
            file_id = payload.get("file_id")
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            if action == "public":
                if not await is_file_public(service, file_id):
                    await asyncio.to_thread(
                        service.permissions().create(
                            fileId=file_id,
                            body={"type": "anyone", "role": "reader"}
                        ).execute
                    )
            else:
                perm_id = await get_public_permission_id(service, file_id)
                if perm_id:
                    await asyncio.to_thread(
                        service.permissions().delete(
                            fileId=file_id,
                            permissionId=perm_id
                        ).execute
                    )

            current_view = context.user_data.get("files_current_view", {})
            if current_view.get("file_id") == file_id and current_view.get("message_id") == (query.message.message_id if query.message else None):
                await refresh_file_view_message(
                    context=context,
                    chat_id=current_view["chat_id"],
                    message_id=current_view["message_id"],
                    file_id=file_id,
                    page=int(current_view.get("page", 1)),
                    back_callback=current_view.get("back_callback"),
                    delete_callback=current_view.get("delete_callback")
                )
            else:
                keyboard = await build_upload_action_keyboard(context, service, file_id)
                msg = "🌐 File is now public" if action == "public" else "🔒 File is now private"
                await query.edit_message_text(msg, reply_markup=keyboard)
            await query.answer("🌐 File is now public" if action == "public" else "🔒 File is now private")
            return

        if action == "expire_link":
            file_id = payload.get("file_id")
            duration = int(payload.get("duration", 3600))
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            permission_id = await ensure_public_permission(service, file_id)
            asyncio.create_task(revoke_public_after_delay(context, file_id, permission_id, duration))

            hours_label = "1 hour" if duration == 3600 else ("24 hours" if duration == 86400 else f"{duration}s")
            link = clean_drive_file_url(file_id)
            if query.message:
                await query.message.reply_text(
                    f"🔗 Link valid for {hours_label}\n{link}"
                )
            await query.answer(f"Link enabled for {hours_label}")
            return

        if action == "rename":
            file_id = payload.get("file_id")
            context.user_data["rename_file_id"] = file_id
            current_view = context.user_data.get("files_current_view", {})
            rename_return = None
            if query.message and current_view.get("file_id") == file_id and current_view.get("message_id") == query.message.message_id:
                rename_return = {
                    "chat_id": current_view.get("chat_id"),
                    "message_id": current_view.get("message_id"),
                    "file_id": file_id,
                    "page": int(current_view.get("page", 1)),
                    "back_callback": current_view.get("back_callback"),
                    "delete_callback": current_view.get("delete_callback"),
                }
            context.user_data["rename_return"] = rename_return
            await query.edit_message_text("✏️ Send new file name")
            await query.answer()
            return

        await query.answer("Unknown action", show_alert=True)
    except HttpError as e:
        logger.error(f"Files callback failed (Drive API): {e}")
        await query.answer("❌ Drive API error", show_alert=True)
    except Exception as e:
        logger.error(f"Files callback failed: {e}")
        await query.answer("❌ Action failed", show_alert=True)


async def handle_rename_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        return

    file_id = context.user_data.get("rename_file_id")
    if not file_id:
        return

    new_name = (message.text or "").strip()
    if not new_name:
        await message.reply_text("❌ Name cannot be empty. Send a valid file name.")
        return

    try:
        drive = context.bot_data.get("drive")
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)
        updated = await asyncio.to_thread(
            service.files().update(
                fileId=file_id,
                body={"name": new_name},
                fields="id,name"
            ).execute
        )
        rename_return = context.user_data.get("rename_return")
        context.user_data.pop("rename_file_id", None)
        context.user_data.pop("rename_return", None)

        # Reuse same rename logic: if rename started from file-view, refresh that same message.
        if rename_return:
            await refresh_file_view_message(
                context=context,
                chat_id=rename_return["chat_id"],
                message_id=rename_return["message_id"],
                file_id=rename_return["file_id"],
                page=rename_return["page"],
                back_callback=rename_return.get("back_callback"),
                delete_callback=rename_return.get("delete_callback")
            )
        else:
            await message.reply_text(f"✅ Renamed to: {updated.get('name', new_name)}")
    except Exception as e:
        logger.error(f"Rename failed: {e}")
        context.user_data.pop("rename_file_id", None)
        context.user_data.pop("rename_return", None)
        await message.reply_text(f"❌ Rename failed:\n`{str(e)}`", parse_mode="Markdown")


async def cancel_transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("cancel_"):
        await query.answer()
        return

    task_id = data[len("cancel_"):]
    transfer_tasks = context.bot_data.setdefault("transfer_tasks", {})
    task = transfer_tasks.get(task_id)
    if task:
        task["cancel"] = True
        await query.answer("Cancelling...")
    else:
        await query.answer("Task not found or already finished", show_alert=True)


async def pause_transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("pause_"):
        await query.answer()
        return

    task_id = data[len("pause_"):]
    transfer_tasks = context.bot_data.setdefault("transfer_tasks", {})
    task = transfer_tasks.get(task_id)
    if not task:
        await query.answer("Task not found or already finished", show_alert=True)
        return

    task["paused"] = True
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Resume", callback_data=f"resume_{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
        ]
    ])
    try:
        await query.edit_message_text("⏸ Paused", reply_markup=kb)
    except Exception:
        pass
    await query.answer("Paused")


async def resume_transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or user.id != OWNER_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("resume_"):
        await query.answer()
        return

    task_id = data[len("resume_"):]
    transfer_tasks = context.bot_data.setdefault("transfer_tasks", {})
    task = transfer_tasks.get(task_id)
    if not task:
        await query.answer("Task not found or already finished", show_alert=True)
        return

    task["paused"] = False
    await query.answer("Resumed")


async def run_transfer_pipeline(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    filename: str,
    file_size: int,
    emoji: str,
    download_runner,
):
    message = update.message
    drive = context.bot_data.get("drive")
    upload_semaphore = context.bot_data.get("upload_semaphore")

    known_size = int(file_size or 0)
    size_label = f"{known_size / (1024 * 1024):.2f} MB" if known_size else "Unknown"
    local_path = f"/tmp/{filename}"
    task_id = uuid.uuid4().hex[:16]

    transfer_tasks = context.bot_data.setdefault("transfer_tasks", {})
    transfer_tasks[task_id] = {
        "paused": False,
        "cancel": False,
        "downloaded": 0,
        "file_path": local_path,
        "url": None,
        "stage": "download",
    }

    def transfer_keyboard():
        task = context.bot_data.get("transfer_tasks", {}).get(task_id, {})
        if task.get("stage") == "download":
            if task.get("paused"):
                return InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("▶️ Resume", callback_data=f"resume_{task_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
                    ]
                ])
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏸ Pause", callback_data=f"pause_{task_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")
                ]
            ])
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{task_id}")]
        ])

    def is_cancelled() -> bool:
        return bool(context.bot_data.get("transfer_tasks", {}).get(task_id, {}).get("cancel", False))

    def cleanup_local_file():
        if os.path.exists(local_path):
            os.remove(local_path)

    progress_msg = await message.reply_text(
        f"{emoji} {filename}\n"
        f"📦 {size_label}\n\n"
        f"⬇️ Downloading... 0%",
        reply_markup=transfer_keyboard()
    )

    progress_state = {
        "download_start_time": None,
        "upload_start_time": None,
        "download_last_update": 0.0,
        "upload_last_update": 0.0,
    }

    async def update_download_progress(done: int, total: int):
        now = time.time()
        if progress_state["download_start_time"] is None:
            progress_state["download_start_time"] = now

        if now - progress_state["download_last_update"] < 1.0 and done != total:
            return
        progress_state["download_last_update"] = now

        done_mb = done / (1024 * 1024)
        elapsed = max(now - progress_state["download_start_time"], 0.001)
        speed_text = "--"
        eta_text = "--"
        if done > 0 and elapsed >= 1:
            speed_bps = done / elapsed
            if speed_bps > 0:
                speed_text = f"{(speed_bps / (1024 * 1024)):.2f} MB/s"
                if total > 0:
                    remaining_bytes = max(total - done, 0)
                    eta_text = f"{int(round(remaining_bytes / speed_bps))}s"

        try:
            if total > 0:
                percent = min(100, int(done * 100 / total))
                total_mb = total / (1024 * 1024)
                bar = progress_bar(percent)
                text = (
                    f"{emoji} {filename}\n"
                    f"📦 {size_label}\n\n"
                    f"⬇️ Downloading... {percent}%\n"
                    f"{bar}\n"
                    f"{done_mb:.2f}/{total_mb:.2f} MB\n\n"
                    f"🚀 Speed: {speed_text}\n"
                    f"⏱ ETA: {eta_text}"
                )
            else:
                text = (
                    f"{emoji} {filename}\n"
                    f"📦 {size_label}\n\n"
                    f"⬇️ Downloading...\n"
                    f"{done_mb:.2f} MB downloaded\n\n"
                    f"🚀 Speed: {speed_text}\n"
                    f"⏱ ETA: --"
                )

            await progress_msg.edit_text(text, reply_markup=transfer_keyboard())
        except Exception:
            pass

    async def update_upload_progress(done: int, total: int):
        if total <= 0:
            return

        now = time.time()
        if progress_state["upload_start_time"] is None:
            progress_state["upload_start_time"] = now

        percent = min(100, int(done * 100 / total))
        if percent < 100 and (now - progress_state["upload_last_update"] < 1.0):
            return
        progress_state["upload_last_update"] = now

        done_mb = done / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        elapsed = now - progress_state["upload_start_time"]
        speed_text = "--"
        eta_text = "--"
        if elapsed >= 1 and done > 0:
            speed_bps = done / elapsed
            if speed_bps > 0:
                speed_mb = speed_bps / (1024 * 1024)
                remaining_bytes = max(total - done, 0)
                eta_seconds = int(round(remaining_bytes / speed_bps))
                speed_text = f"{speed_mb:.2f} MB/s"
                eta_text = f"{eta_seconds}s"

        try:
            bar = progress_bar(percent)
            await progress_msg.edit_text(
                f"{emoji} {filename}\n"
                f"📦 {size_label}\n\n"
                f"☁️ Uploading to Google Drive... {percent}%\n"
                f"{bar}\n"
                f"{done_mb:.2f}/{total_mb:.2f} MB\n\n"
                f"🚀 Speed: {speed_text}\n"
                f"⏱ ETA: {eta_text}",
                reply_markup=transfer_keyboard()
            )
        except Exception:
            pass

    try:
        downloaded = await download_runner(
            local_path,
            known_size,
            update_download_progress,
            is_cancelled,
            context.bot_data.get("transfer_tasks", {}).get(task_id),
        )
    except TransferCancelled:
        cleanup_local_file()
        await progress_msg.edit_text("❌ Cancelled")
        context.bot_data.get("transfer_tasks", {}).pop(task_id, None)
        return
    except Exception as e:
        cleanup_local_file()
        context.bot_data.get("transfer_tasks", {}).pop(task_id, None)
        await message.reply_text(f"❌ Download failed:\n`{str(e)}`", parse_mode="Markdown")
        return

    if not downloaded:
        await message.reply_text("❌ Could not download the file. Please try again.")
        context.bot_data.get("transfer_tasks", {}).pop(task_id, None)
        return

    if known_size <= 0:
        known_size = int(context.bot_data.get("transfer_tasks", {}).get(task_id, {}).get("downloaded", 0) or 0)
        size_label = f"{known_size / (1024 * 1024):.2f} MB" if known_size else "Unknown"

    try:
        update_download_analytics()
    except Exception as e:
        logger.warning(f"Failed to update download analytics: {e}")

    if task_id in context.bot_data.get("transfer_tasks", {}):
        context.bot_data["transfer_tasks"][task_id]["stage"] = "upload"
    await update_upload_progress(0, known_size)

    try:
        if upload_semaphore:
            await progress_msg.edit_text(
                f"{emoji} {filename}\n"
                f"📦 {size_label}\n\n"
                f"⏳ Waiting for upload slot...",
                reply_markup=transfer_keyboard()
            )

            async with upload_semaphore:
                await update_upload_progress(0, known_size)
                uploaded_file_id, _ = await upload_to_drive(
                    context,
                    drive,
                    local_path,
                    filename,
                    file_size=known_size,
                    progress_callback=update_upload_progress,
                    should_cancel=is_cancelled
                )
        else:
            uploaded_file_id, _ = await upload_to_drive(
                context,
                drive,
                local_path,
                filename,
                file_size=known_size,
                progress_callback=update_upload_progress,
                should_cancel=is_cancelled
            )

        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)
        upload_keyboard = await build_upload_action_keyboard(context, service, uploaded_file_id)

        await progress_msg.edit_text("✅ Uploaded!", reply_markup=upload_keyboard)
        try:
            update_upload_analytics(filename, known_size)
        except Exception as e:
            logger.warning(f"Failed to update upload analytics: {e}")
        logger.info(f"Uploaded: {filename} ({size_label})")
    except TransferCancelled:
        cleanup_local_file()
        await progress_msg.edit_text("❌ Cancelled")
        return
    except Exception as e:
        logger.error(f"Drive upload failed: {e}")
        await progress_msg.edit_text(f"❌ Google Drive upload failed:\n`{str(e)}`", parse_mode="Markdown")
    finally:
        cleanup_local_file()
        context.bot_data.get("transfer_tasks", {}).pop(task_id, None)


async def handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    text = (message.text or "").strip()
    url = extract_first_url(text)
    if not url:
        return

    if not user or user.id != OWNER_ID:
        await message.reply_text("❌ Unauthorized access")
        return

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        await message.reply_text("❌ Invalid URL")
        return

    file_size = await get_url_content_length(url)
    if file_size and MAX_URL_DOWNLOAD_SIZE > 0 and file_size > MAX_URL_DOWNLOAD_SIZE:
        await message.reply_text(
            f"❌ File too large:\n"
            f"Size: {format_bytes_stats(file_size)}\n"
            f"Limit: {format_bytes_stats(MAX_URL_DOWNLOAD_SIZE)}"
        )
        return

    filename = filename_from_url(url)

    async def url_download_runner(local_path, known_size, progress_callback, should_cancel, task_state):
        return await download_url_with_requests(
            url=url,
            local_path=local_path,
            file_size=known_size,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            task_state=task_state,
        )

    await run_transfer_pipeline(
        update,
        context,
        filename=filename,
        file_size=file_size,
        emoji="🌐",
        download_runner=url_download_runner,
    )


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("rename_file_id"):
        await handle_rename_input(update, context)
        return
    await handle_url_message(update, context)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

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

    async def telegram_download_runner(local_path, known_size, progress_callback, should_cancel, task_state):
        try:
            downloaded = await download_via_local_api(
                file_id,
                local_path,
                file_size=known_size,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
                task_state=task_state,
            )
        except TransferCancelled:
            raise

        if downloaded:
            return True

        if known_size and known_size > 20 * 1024 * 1024:
            size_mb = known_size / (1024 * 1024)
            raise RuntimeError(
                "Download failed. File is too large for CDN fallback "
                f"({size_mb:.2f} MB). Ensure local API Docker is running."
            )

        logger.info("Falling back to Telegram CDN (file is under 20MB)...")
        return await download_via_cdn(
            file_id,
            local_path,
            file_size=known_size,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
            task_state=task_state,
        )

    await run_transfer_pipeline(
        update,
        context,
        filename=filename,
        file_size=file_size or 0,
        emoji=emoji,
        download_runner=telegram_download_runner,
    )


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
    app.add_handler(CommandHandler("storage", storage))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    app.add_handler(CommandHandler("files", files))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CallbackQueryHandler(pause_transfer_callback, pattern=r"^pause_"))
    app.add_handler(CallbackQueryHandler(resume_transfer_callback, pattern=r"^resume_"))
    app.add_handler(CallbackQueryHandler(cancel_transfer_callback, pattern=r"^cancel_"))
    app.add_handler(CallbackQueryHandler(files_callback_handler, pattern=r"^cb_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    logger.info(f"🤖 Bot is running... (parallel uploads: {MAX_PARALLEL_UPLOADS})")
    app.run_polling()


if __name__ == "__main__":
    main()
