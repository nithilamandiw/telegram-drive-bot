from __future__ import annotations

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
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
from aiohttp import web

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
OAUTH_SERVER_PORT = int(os.getenv("OAUTH_SERVER_PORT", "8080"))

# Local Bot API server (optional — for large file support via Docker)
USE_LOCAL_API = os.getenv("USE_LOCAL_API", "false").lower() in ("true", "1", "yes")
LOCAL_API_URL = "http://localhost:8081/bot"
LOCAL_FILE_URL = "http://localhost:8081/file/bot"
LOCAL_API_DIR = "/var/lib/telegram-bot-api"  # path inside container
VOLUME_HOST_PATH = "/home/azureuser/telegram-api-data"
MAX_PARALLEL_UPLOADS = int(os.getenv("MAX_PARALLEL_UPLOADS", "3"))
ANALYTICS_FILE = "analytics.json"
USERS_FILE = "users.json"
ADMINS_FILE = "admins.json"
USER_CREDS_DIR = "user_creds"
SCOPES = ["https://www.googleapis.com/auth/drive"]
URL_PATTERN = re.compile(r"(https?://\S+)", re.IGNORECASE)
DRIVE_FILE_ID_PATTERN = re.compile(r"(?:/d/|id=)([a-zA-Z0-9_-]+)")
MAX_URL_DOWNLOAD_SIZE = int(os.getenv("MAX_URL_DOWNLOAD_SIZE", str(2 * 1024 * 1024 * 1024)))
DOWNLOADS_DIR = "./downloads"


def _build_oauth_client_config():
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }

COMMANDS = {
    "connect": ("\U0001f517 /connect", "Connect your Google account"),
    "disconnect": ("\U0001f50c /disconnect", "Disconnect your Google account"),
    "upload": ("\U0001f4e4 Send file", "Upload files to Drive"),
    "files": ("\U0001f4c1 /files", "View Drive files"),
    "newfolder": ("\U0001f4c2 /newfolder <name>", "Create a new folder"),
    "recent": ("\U0001f55b /recent", "View recently uploaded files"),
    "search": ("\U0001f50d /search <name>", "Search files"),
    "storage": ("\U0001f4be /storage", "View storage usage"),
    "stats": ("\U0001f4ca /stats", "Storage stats"),
    "analytics": ("\U0001f4c8 /analytics", "Upload/download analytics"),
    "trash": ("\U0001f5d1 /trash", "View trash"),
    "url": ("\U0001f310 /url <link>", "Upload from URL"),
    "get": ("\U0001f4e5 /get <drive_link>", "Download from Drive"),
    "adduser": ("\u2795 /adduser <id>", "Add user"),
    "removeuser": ("\u2796 /removeuser <id>", "Remove user"),
    "addadmin": ("\U0001f451 /addadmin <id>", "Add admin"),
    "removeadmin": ("\u274c /removeadmin <id>", "Remove admin"),
}

COMMAND_PERMISSIONS = {
    "connect": "upload",
    "disconnect": "upload",
    "upload": "upload",
    "files": "files",
    "newfolder": "newfolder",
    "recent": "recent",
    "search": "search",
    "storage": "storage",
    "stats": "stats",
    "analytics": "analytics",
    "trash": "trash",
    "url": "upload",
    "get": "files",
    "adduser": "adduser",
    "removeuser": "removeuser",
    "addadmin": "addadmin",
    "removeadmin": "removeadmin",
}

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


async def notify(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not OWNER_ID:
        return
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=text)
    except Exception as e:
        logger.warning(f"Notification send failed: {e}")


async def get_drive_storage(user_id: int):
    try:
        service = get_user_service(user_id)
    except Exception:
        return 0, 0

    about = await asyncio.to_thread(
        service.about().get(fields="storageQuota").execute
    )
    quota = about.get("storageQuota", {})
    used = int(quota.get("usage", 0) or 0)
    total = int(quota.get("limit", 0) or 0)
    return used, total


async def check_expired_links(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    exp_links = context.bot_data.setdefault("exp_links", {})

    expired_ids = [file_id for file_id, expiry in list(exp_links.items()) if now >= float(expiry)]
    for file_id in expired_ids:
        await notify(context, f"⏰ Link expired for file: {file_id}")
        exp_links.pop(file_id, None)


async def check_storage(context: ContextTypes.DEFAULT_TYPE):
    # Check storage for the owner if connected
    if not OWNER_ID or not user_has_credentials(OWNER_ID):
        return
    try:
        used, total = await get_drive_storage(OWNER_ID)
    except Exception as e:
        logger.warning(f"Storage check failed: {e}")
        return

    if total <= 0:
        return

    percent = (used / total) * 100
    if percent < 90:
        return

    now = time.time()
    last_alert = float(context.bot_data.get("last_storage_alert", 0) or 0)
    if now - last_alert < 3600:
        return

    await notify(
        context,
        f"⚠️ Storage {percent:.0f}% full\n"
        f"Used: {used / 1e9:.2f} GB / {total / 1e9:.2f} GB"
    )
    context.bot_data["last_storage_alert"] = now


def load_id_set(file_path: str) -> set[int]:
    if not os.path.exists(file_path):
        return set()
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            return set()
        return {int(x) for x in raw}
    except Exception as e:
        logger.warning(f"Failed to load {file_path}: {e}")
        return set()


def save_id_set(file_path: str, values: set[int]):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(values)), f)
    except Exception as e:
        logger.warning(f"Failed to save {file_path}: {e}")


def load_allowed_users() -> set[int]:
    return load_id_set(USERS_FILE)


def save_allowed_users(users: set[int]):
    save_id_set(USERS_FILE, users)


def load_admin_users() -> set[int]:
    return load_id_set(ADMINS_FILE)


def save_admin_users(admins: set[int]):
    save_id_set(ADMINS_FILE, admins)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    admins = context.bot_data.get("admins", set())
    if not isinstance(admins, set):
        admins = set(admins or [])
        context.bot_data["admins"] = admins
    return user_id in admins


def is_user(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    users = context.bot_data.get("users", set())
    if not isinstance(users, set):
        users = set(users or [])
        context.bot_data["users"] = users
    return user_id in users


def get_role(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(user_id):
        return "owner"
    if is_admin(user_id, context):
        return "admin"
    if is_user(user_id, context):
        return "user"
    return None


def has_permission(user_id: int, context: ContextTypes.DEFAULT_TYPE, action: str) -> bool:
    role = get_role(user_id, context)
    permissions = {
        "upload": ["owner", "admin", "user"],
        "files": ["owner", "admin", "user"],
        "delete": ["owner", "admin", "user"],
        "rename": ["owner", "admin", "user"],
        "share": ["owner", "admin", "user"],
        "search": ["owner", "admin", "user"],
        "storage": ["owner", "admin", "user"],
        "stats": ["owner", "admin", "user"],
        "analytics": ["owner", "admin", "user"],
        "newfolder": ["owner", "admin", "user"],
        "recent": ["owner", "admin", "user"],
        "trash": ["owner", "admin", "user"],
        "adduser": ["owner", "admin"],
        "removeuser": ["owner", "admin"],
        "addadmin": ["owner"],
        "removeadmin": ["owner"],
    }
    return role in permissions.get(action, [])


def is_allowed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Backward-compatible helper: any recognized role can pass.
    return has_permission(user_id, context, "upload")


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


def extract_drive_file_id(url: str) -> str | None:
    match = DRIVE_FILE_ID_PATTERN.search(url or "")
    if not match:
        return None
    return match.group(1)


def is_google_drive_link(url: str) -> bool:
    netloc = (urlparse(url).netloc or "").lower()
    return "drive.google.com" in netloc


async def clone_drive_file(user_id: int, source_file_id: str):
    service = get_user_service(user_id)

    original = await asyncio.to_thread(
        service.files().get(fileId=source_file_id, fields="id,name").execute
    )
    original_name = original.get("name", "Unnamed file")

    copied = await asyncio.to_thread(
        service.files().copy(
            fileId=source_file_id,
            body={"name": original_name},
            fields="id,name"
        ).execute
    )

    new_id = copied.get("id")
    if not new_id:
        raise RuntimeError("Drive copy returned no file id")

    return {
        "id": new_id,
        "name": copied.get("name", original_name),
    }


def sanitize_filename(name: str, fallback: str = "file") -> str:
    safe = re.sub(r"[^a-zA-Z0-9._ -]", "_", (name or "")).strip().strip(".")
    return safe[:120] if safe else fallback


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


async def revoke_public_after_delay(context: ContextTypes.DEFAULT_TYPE, user_id: int, file_id: str, permission_id: str, delay_seconds: int):
    try:
        await asyncio.sleep(delay_seconds)
        if not user_has_credentials(user_id):
            return
        service = get_user_service(user_id)
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


async def send_uploaded_ui(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    file_id: str,
    filename: str,
    message_to_edit=None,
):
    service = get_user_service(user_id)

    # Keep link generation centralized for all uploaded/cloned success UI.
    _ = clean_drive_file_url(file_id)
    upload_keyboard = await build_upload_action_keyboard(context, service, file_id)

    if message_to_edit is not None:
        await message_to_edit.edit_text("✅ Uploaded!", reply_markup=upload_keyboard)
        return

    target_message = update.message if update.message else (update.callback_query.message if update.callback_query else None)
    if target_message:
        await target_message.reply_text("✅ Uploaded!", reply_markup=upload_keyboard)


async def check_duplicate(service, filename: str, file_size: int | None):
    if not filename or file_size is None:
        return None

    try:
        local_size = int(file_size)
    except (TypeError, ValueError):
        return None

    if local_size <= 0:
        return None

    normalized_name = (filename or "").strip()
    query = (
        f"name contains '{escape_drive_query_value(normalized_name)}' and "
        f"trashed=false"
    )

    result = await asyncio.to_thread(
        service.files().list(
            q=query,
            fields="files(id,name,size)",
            pageSize=100
        ).execute
    )

    for item in result.get("files", []):
        drive_name = (item.get("name") or "").strip()
        if drive_name != normalized_name:
            continue

        size_raw = item.get("size")
        if size_raw is None:
            continue

        try:
            drive_size = int(size_raw)
            if drive_size == local_size:
                return item.get("id")
        except (TypeError, ValueError):
            continue

    return None


def build_duplicate_keyboard(task_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭ Skip", callback_data=f"dup_skip_{task_id}"),
            InlineKeyboardButton("♻️ Replace", callback_data=f"dup_replace_{task_id}")
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


def get_user_creds_path(user_id: int) -> str:
    return os.path.join(USER_CREDS_DIR, f"{user_id}.json")


def user_has_credentials(user_id: int) -> bool:
    return os.path.exists(get_user_creds_path(user_id))


def get_user_service(user_id: int):
    creds_path = get_user_creds_path(user_id)
    if not os.path.exists(creds_path):
        raise RuntimeError("Google account not connected. Use /connect to get started.")

    creds = Credentials.from_authorized_user_file(creds_path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        with open(creds_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def require_connection(update: Update, user_id: int) -> bool:
    """Returns True if user is connected. Sends error message and returns False otherwise."""
    if user_has_credentials(user_id):
        return True
    msg = update.message if update.message else (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text("\u274c You haven't connected your Google account yet. Use /connect to get started.")
    return False


async def connect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if not is_allowed(user.id, context):
        await update.message.reply_text("\u274c Unauthorized access")
        return

    flow = Flow.from_client_config(
        _build_oauth_client_config(),
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=str(user.id),
    )

    await update.message.reply_text(
        "Click the link below to connect your Google Drive:\n\n"
        f"[Authorize Google Drive]({auth_url})\n\n"
        "After authorizing, you'll receive a confirmation message here.",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def disconnect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if not is_allowed(user.id, context):
        await update.message.reply_text("\u274c Unauthorized access")
        return

    creds_path = get_user_creds_path(user.id)
    if os.path.exists(creds_path):
        os.remove(creds_path)
        await update.message.reply_text("\u2705 Google account disconnected successfully.")
    else:
        await update.message.reply_text("\u26a0\ufe0f No Google account is connected.")


async def handle_oauth_callback(request):
    code = request.query.get("code")
    state = request.query.get("state")

    if not code or not state:
        return web.Response(text="Missing code or state parameter.", status=400)

    try:
        user_id = int(state)
    except ValueError:
        return web.Response(text="Invalid state parameter.", status=400)

    try:
        flow = Flow.from_client_config(
            _build_oauth_client_config(),
            scopes=SCOPES,
            redirect_uri=OAUTH_REDIRECT_URI,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        os.makedirs(USER_CREDS_DIR, exist_ok=True)
        creds_path = get_user_creds_path(user_id)
        with open(creds_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

        bot = request.app.get("telegram_bot")
        if bot:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text="\u2705 Google account connected successfully!\n\nYou can now upload files to your Google Drive."
                )
            except Exception as e:
                logger.warning(f"Failed to notify user {user_id}: {e}")

        return web.Response(
            text="<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                 "<h2>\u2705 Google account connected!</h2>"
                 "<p>You can close this tab and return to Telegram.</p></body></html>",
            content_type="text/html",
        )
    except Exception as e:
        logger.error(f"OAuth callback failed for user {user_id}: {e}")
        return web.Response(text=f"Authorization failed: {e}", status=500)


async def newfolder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new folder in the user's Google Drive."""
    user = update.effective_user
    if not user or not has_permission(user.id, context, "newfolder"):
        await update.message.reply_text("\u274c Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    if not context.args:
        await update.message.reply_text("\u26a0\ufe0f Usage: /newfolder <name>")
        return

    folder_name = " ".join(context.args)
    service = get_user_service(user.id)
    if not service:
        await update.message.reply_text("\u274c Could not connect to Google Drive. Try /connect again.")
        return

    try:
        file_metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        folder = service.files().create(body=file_metadata, fields="id, name, webViewLink").execute()
        link = folder.get("webViewLink", "")
        await update.message.reply_text(
            f"\u2705 Folder created!\n\n"
            f"\U0001f4c2 {folder_name}\n"
            f"\U0001f517 [Open folder]({link})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to create folder for user {user.id}: {e}")
        await update.message.reply_text(f"\u274c Failed to create folder: {e}")


async def recent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recently uploaded files from the user's Google Drive."""
    user = update.effective_user
    if not user or not has_permission(user.id, context, "recent"):
        await update.message.reply_text("\u274c Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    service = get_user_service(user.id)
    if not service:
        await update.message.reply_text("\u274c Could not connect to Google Drive. Try /connect again.")
        return

    try:
        results = service.files().list(
            pageSize=10,
            orderBy="createdTime desc",
            q="trashed = false",
            fields="files(id, name, size, mimeType, createdTime, webViewLink)",
        ).execute()
        files = results.get("files", [])

        if not files:
            await update.message.reply_text("\U0001f4ed No files found in your Drive.")
            return

        lines = ["\U0001f55b **Recent Files:**\n"]
        for f in files:
            name = f.get("name", "Unknown")
            size = int(f.get("size", 0))
            size_str = f"{size / (1024 * 1024):.2f} MB" if size > 0 else "---"
            link = f.get("webViewLink", "")
            lines.append(f"\U0001f4c4 [{name}]({link})")
            lines.append(f"   {size_str}")
            lines.append("")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to get recent files for user {user.id}: {e}")
        await update.message.reply_text(f"\u274c Failed to get recent files: {e}")


async def trash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trashed files from the user's Google Drive."""
    user = update.effective_user
    if not user or not has_permission(user.id, context, "trash"):
        await update.message.reply_text("\u274c Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    service = get_user_service(user.id)
    if not service:
        await update.message.reply_text("\u274c Could not connect to Google Drive. Try /connect again.")
        return

    try:
        results = service.files().list(
            pageSize=15,
            q="trashed = true",
            fields="files(id, name, size, mimeType)",
        ).execute()
        files = results.get("files", [])

        if not files:
            await update.message.reply_text("\U0001f5d1 Trash is empty!")
            return

        lines = ["\U0001f5d1 **Trash:**\n"]
        buttons = []
        for f in files:
            name = f.get("name", "Unknown")
            size = int(f.get("size", 0))
            size_str = f"{size / (1024 * 1024):.2f} MB" if size > 0 else "folder"
            lines.append(f"\U0001f4c4 {name} ({size_str})")
            buttons.append([
                InlineKeyboardButton(f"\u267b\ufe0f Restore {name[:20]}", callback_data=f"cb_restore_{f['id']}"),
                InlineKeyboardButton(f"\U0001f5d1 Delete {name[:20]}", callback_data=f"cb_permdelete_{f['id']}"),
            ])

        buttons.append([InlineKeyboardButton("\U0001f5d1 Empty Trash", callback_data="cb_emptytrash")])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error(f"Failed to get trash for user {user.id}: {e}")
        await update.message.reply_text(f"\u274c Failed to get trash: {e}")


async def upload_to_drive(
    user_id: int,
    filepath: str,
    filename: str,
    file_size: int,
    progress_callback=None,
    should_cancel=None
):
    if should_cancel and should_cancel():
        raise TransferCancelled("Upload cancelled by user")

    service = get_user_service(user_id)

    def _upload_file():
        file_metadata = {"name": filename}
        media = MediaFileUpload(filepath, resumable=True)
        request = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
        return response.get("id")

    file_id = await asyncio.to_thread(_upload_file)

    if progress_callback and file_size:
        await progress_callback(file_size, file_size)

    if not file_id:
        raise RuntimeError("Upload completed but file id is missing")

    await asyncio.to_thread(
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"}
        ).execute
    )

    return file_id, clean_drive_file_url(file_id)


async def download_via_local_api(
    bot,
    file_id: str,
    local_path: str,
    file_size: int = 0,
    progress_callback=None,
    should_cancel=None,
    task_state=None
) -> bool:
    """Download file using the bot's built-in local API support."""
    try:
        # Use the bot's get_file which properly handles local mode
        file_obj = await bot.get_file(file_id)
        file_path = file_obj.file_path

        if not file_path:
            logger.warning("Local API getFile returned no file_path")
            return False

        # In local mode, file_path may include the full URL prefix
        # Extract just the container path (e.g. /var/lib/telegram-bot-api/...)
        if LOCAL_API_DIR in file_path:
            file_path = file_path[file_path.index(LOCAL_API_DIR):]

        # Remap container path to host volume path
        host_file_path = file_path.replace(LOCAL_API_DIR, VOLUME_HOST_PATH, 1)
        if task_state is not None:
            task_state["url"] = host_file_path
            task_state["file_path"] = local_path

        logger.info(f"Reading directly from volume: {host_file_path}")

        fix_volume_permissions()
        if not os.path.exists(host_file_path):
            # If file not found at remapped path, use bot's download_to_drive
            logger.info("File not at volume path, using bot download_to_drive...")
            await file_obj.download_to_drive(local_path)
            if progress_callback and file_size:
                await progress_callback(file_size, file_size)
            logger.info(f"Downloaded via download_to_drive: {local_path}")
            return True

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
        logger.info(f"Copied from volume: {local_path}")
        return True

    except TransferCancelled:
        raise
    except Exception as e:
        logger.warning(f"Local download failed: {e}")
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


async def build_main_menu(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Build the main menu keyboard based on user permissions."""
    role = get_role(user_id, context)
    if not role:
        return None, None

    # Check if user has Google connected
    creds_path = get_user_creds_path(user_id)
    connected = os.path.exists(creds_path)

    greeting = (
        "\U0001f44b *Welcome to Drive Upload Bot!*\n\n"
        "Send me any file, photo, video or audio and\n"
        "I'll upload it to your Google Drive.\n\n"
    )

    if connected:
        greeting += "\u2705 *Google Drive connected*\n"
    else:
        greeting += "\u26a0\ufe0f *Google Drive not connected*\n"

    greeting += f"\U0001f464 Role: *{role.capitalize()}*"

    buttons = []

    # Row 1: Connect/Disconnect
    if connected:
        buttons.append([
            InlineKeyboardButton("\U0001f50c Disconnect", callback_data="menu_disconnect"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("\U0001f517 Connect Google", callback_data="menu_connect"),
        ])

    # Row 2-3: File management (all connected users)
    if connected:
        buttons.append([
            InlineKeyboardButton("\U0001f4c1 My Files", callback_data="menu_files"),
            InlineKeyboardButton("\U0001f55b Recent", callback_data="menu_recent"),
        ])
        buttons.append([
            InlineKeyboardButton("\U0001f50d Search", callback_data="menu_search"),
            InlineKeyboardButton("\U0001f4c2 New Folder", callback_data="menu_newfolder"),
        ])
        buttons.append([
            InlineKeyboardButton("\U0001f4be Storage", callback_data="menu_storage"),
            InlineKeyboardButton("\U0001f4ca Stats", callback_data="menu_stats"),
        ])
        buttons.append([
            InlineKeyboardButton("\U0001f4c8 Analytics", callback_data="menu_analytics"),
            InlineKeyboardButton("\U0001f5d1 Trash", callback_data="menu_trash"),
        ])

    # Admin section
    if has_permission(user_id, context, "adduser"):
        buttons.append([
            InlineKeyboardButton("\u2795 Add User", callback_data="menu_adduser"),
            InlineKeyboardButton("\u2796 Remove User", callback_data="menu_removeuser"),
        ])

    if has_permission(user_id, context, "addadmin"):
        buttons.append([
            InlineKeyboardButton("\U0001f451 Add Admin", callback_data="menu_addadmin"),
            InlineKeyboardButton("\u274c Remove Admin", callback_data="menu_removeadmin"),
        ])

    keyboard = InlineKeyboardMarkup(buttons)
    return greeting, keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    role = get_role(user.id, context)
    if not role:
        await update.message.reply_text(
            "\u274c You don't have access to this bot.\n"
            "Ask the bot owner to add you with /adduser."
        )
        return

    text, keyboard = await build_main_menu(user.id, context)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu button presses."""
    query = update.callback_query
    user = update.effective_user

    if not user:
        return

    role = get_role(user.id, context)
    if not role:
        await query.answer("Unauthorized", show_alert=True)
        return

    action = (query.data or "").replace("menu_", "")
    await query.answer()

    if action == "connect":
        # Trigger connect flow
        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [OAUTH_REDIRECT_URI],
                }
            },
            scopes=SCOPES,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            redirect_uri=OAUTH_REDIRECT_URI,
            state=str(user.id),
        )
        await query.edit_message_text(
            "\U0001f517 Click the link below to connect your Google Drive:\n\n"
            f"[Authorize Google Drive]({auth_url})\n\n"
            "After authorizing, send /start to return to the menu.",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if action == "disconnect":
        creds_path = get_user_creds_path(user.id)
        if os.path.exists(creds_path):
            os.remove(creds_path)
        text, keyboard = await build_main_menu(user.id, context)
        await query.edit_message_text(
            "\u2705 Google account disconnected.\n\n" + text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    if action == "back":
        text, keyboard = await build_main_menu(user.id, context)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if action == "files":
        if not await require_connection(update, user.id):
            return
        context.user_data["_oauth_user_id"] = user.id
        text, keyboard = await build_files_page(context, user.id, page=1, page_size=10)
        back_btn = [InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")]
        if keyboard:
            rows = list(keyboard.inline_keyboard) + [back_btn]
            keyboard = InlineKeyboardMarkup(rows)
        else:
            keyboard = InlineKeyboardMarkup([back_btn])
        await query.edit_message_text(text=text, reply_markup=keyboard)
        return

    if action == "recent":
        if not await require_connection(update, user.id):
            return
        service = get_user_service(user.id)
        if not service:
            await query.edit_message_text("\u274c Drive connection error. Try /connect.")
            return
        try:
            results = service.files().list(
                pageSize=10, orderBy="createdTime desc", q="trashed = false",
                fields="files(id, name, size, mimeType, createdTime, webViewLink)",
            ).execute()
            files_list = results.get("files", [])
            if not files_list:
                text = "\U0001f4ed No files found."
            else:
                lines = ["\U0001f55b *Recent Files:*\n"]
                for f in files_list:
                    name = f.get("name", "Unknown")
                    size = int(f.get("size", 0))
                    size_str = f"{size / (1024 * 1024):.2f} MB" if size > 0 else "---"
                    link = f.get("webViewLink", "")
                    lines.append(f"\U0001f4c4 [{name}]({link})")
                    lines.append(f"   {size_str}")
                    lines.append("")
                text = "\n".join(lines)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")
            ]])
            await query.edit_message_text(text, parse_mode="Markdown",
                                          disable_web_page_preview=True, reply_markup=keyboard)
        except Exception as e:
            await query.edit_message_text(f"\u274c Error: {e}")
        return

    if action == "search":
        await query.edit_message_text(
            "\U0001f50d *Search Files*\n\n"
            "Type your search query using:\n"
            "`/search filename`\n\n"
            "Then press /start to return to the menu.",
            parse_mode="Markdown",
        )
        return

    if action == "newfolder":
        await query.edit_message_text(
            "\U0001f4c2 *Create Folder*\n\n"
            "Type the folder name using:\n"
            "`/newfolder My Folder Name`\n\n"
            "Then press /start to return to the menu.",
            parse_mode="Markdown",
        )
        return

    if action == "storage":
        if not await require_connection(update, user.id):
            return
        service = get_user_service(user.id)
        if not service:
            await query.edit_message_text("\u274c Drive connection error.")
            return
        try:
            about = await asyncio.to_thread(service.about().get(fields="storageQuota").execute)
            quota = about.get("storageQuota", {})
            total = int(quota.get("limit", 0))
            used = int(quota.get("usage", 0))
            free = max(total - used, 0) if total else 0
            to_gb = lambda b: b / (1024 ** 3)
            if total > 0:
                pct = (used / total) * 100
                bar_len = 20
                filled = int(bar_len * used / total)
                bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
                text = (
                    "\U0001f4be *Google Drive Storage*\n\n"
                    f"Used: `{to_gb(used):.2f} GB` / `{to_gb(total):.2f} GB`\n"
                    f"Free: `{to_gb(free):.2f} GB`\n\n"
                    f"`[{bar}]` {pct:.1f}%"
                )
            else:
                text = f"\U0001f4be *Storage*\n\nUsed: `{to_gb(used):.2f} GB`"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")
            ]])
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await query.edit_message_text(f"\u274c Error: {e}")
        return

    if action == "stats":
        if not await require_connection(update, user.id):
            return
        service = get_user_service(user.id)
        if not service:
            await query.edit_message_text("\u274c Drive connection error.")
            return
        try:
            results = service.files().list(
                q="trashed = false", pageSize=1000,
                fields="files(size, mimeType)",
            ).execute()
            files_list = results.get("files", [])
            total_files = len(files_list)
            total_size = sum(int(f.get("size", 0)) for f in files_list)
            types = {}
            for f in files_list:
                mt = f.get("mimeType", "unknown").split("/")[0]
                types[mt] = types.get(mt, 0) + 1
            lines = [
                "\U0001f4ca *Drive Stats*\n",
                f"Total files: *{total_files}*",
                f"Total size: *{total_size / (1024**3):.2f} GB*\n",
                "*By type:*",
            ]
            for t, c in sorted(types.items(), key=lambda x: -x[1]):
                lines.append(f"  {t}: {c}")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")
            ]])
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await query.edit_message_text(f"\u274c Error: {e}")
        return

    if action == "analytics":
        analytics = load_analytics()
        text = (
            "\U0001f4c8 *Analytics*\n\n"
            f"Total uploads: *{analytics.get('total_uploads', 0)}*\n"
            f"Total downloads: *{analytics.get('total_downloads', 0)}*\n"
            f"Total data: *{analytics.get('total_size', 0) / (1024**3):.2f} GB*"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")
        ]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if action == "trash":
        if not await require_connection(update, user.id):
            return
        service = get_user_service(user.id)
        if not service:
            await query.edit_message_text("\u274c Drive connection error.")
            return
        try:
            results = service.files().list(
                pageSize=15, q="trashed = true",
                fields="files(id, name, size, mimeType)",
            ).execute()
            files_list = results.get("files", [])
            if not files_list:
                text = "\U0001f5d1 Trash is empty!"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")
                ]])
            else:
                lines = ["\U0001f5d1 *Trash:*\n"]
                btns = []
                for f in files_list:
                    name = f.get("name", "Unknown")
                    size = int(f.get("size", 0))
                    size_str = f"{size / (1024 * 1024):.2f} MB" if size > 0 else "folder"
                    lines.append(f"\U0001f4c4 {name} ({size_str})")
                    btns.append([
                        InlineKeyboardButton(f"\u267b\ufe0f {name[:18]}", callback_data=f"cb_restore_{f['id']}"),
                        InlineKeyboardButton(f"\U0001f5d1 {name[:18]}", callback_data=f"cb_permdelete_{f['id']}"),
                    ])
                btns.append([InlineKeyboardButton("\U0001f5d1 Empty Trash", callback_data="cb_emptytrash")])
                btns.append([InlineKeyboardButton("\u25c0\ufe0f Back to Menu", callback_data="menu_back")])
                text = "\n".join(lines)
                keyboard = InlineKeyboardMarkup(btns)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await query.edit_message_text(f"\u274c Error: {e}")
        return

    if action in ("adduser", "removeuser", "addadmin", "removeadmin"):
        cmd = f"/{action}"
        await query.edit_message_text(
            f"\u2699\ufe0f *{action.replace('add', 'Add ').replace('remove', 'Remove ').title()}*\n\n"
            f"Use: `{cmd} <telegram_user_id>`\n\n"
            "Then press /start to return to the menu.",
            parse_mode="Markdown",
        )
        return


async def commands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback text command list."""
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    user_id = user.id
    role = get_role(user_id, context)
    if not role:
        await message.reply_text("\u274c Unauthorized access")
        return

    lines = ["\U0001f4cb Available Commands:", ""]
    for key, (cmd, desc) in COMMANDS.items():
        action = COMMAND_PERMISSIONS.get(key)
        if action and has_permission(user_id, context, action):
            lines.append(cmd)
            lines.append(f"   \u2514 {desc}")
            lines.append("")

    lines.append(f"\U0001f464 Role: {role.capitalize()}")
    lines.append("\nTip: Use /start for the button menu!")
    await message.reply_text("\n".join(lines))


async def storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or not has_permission(user.id, context, "files"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    try:
        service = get_user_service(user.id)
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

    if not user or not has_permission(user.id, context, "files"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    try:
        service = get_user_service(user.id)

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
                    q="'root' in parents and trashed = false",
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


async def find_drive_file(service, query_text: str):
    query_text = (query_text or "").strip()
    if not query_text:
        return None

    # Try as file ID first.
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", query_text):
        try:
            by_id = await asyncio.to_thread(
                service.files().get(
                    fileId=query_text,
                    fields="id,name,size,trashed"
                ).execute
            )
            if not by_id.get("trashed"):
                return {
                    "id": by_id.get("id"),
                    "name": by_id.get("name", "Unnamed file"),
                    "size": int(by_id.get("size", 0) or 0),
                }
        except Exception:
            pass

    safe_query = escape_drive_query_value(query_text)
    result = await asyncio.to_thread(
        service.files().list(
            q=(
                f"name contains '{safe_query}' and "
                f"trashed=false"
            ),
            orderBy="createdTime desc",
            pageSize=1,
            fields="files(id,name,size)"
        ).execute
    )
    items = result.get("files", [])
    if not items:
        return None

    item = items[0]
    return {
        "id": item.get("id"),
        "name": item.get("name", "Unnamed file"),
        "size": int(item.get("size", 0) or 0),
    }


async def download_drive_file(service, file_id: str, local_path: str, progress_callback=None):
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = await asyncio.to_thread(downloader.next_chunk)
            if progress_callback and status:
                try:
                    await progress_callback(int(status.progress() * 100))
                except Exception:
                    pass


async def get_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or not has_permission(user.id, context, "files"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
        return

    query = " ".join(context.args).strip()
    if not query:
        await message.reply_text("Usage: /get <file_name OR file_id>")
        return

    service = get_user_service(user.id)

    found = None
    if is_google_drive_link(query):
        direct_file_id = extract_drive_file_id(query)
        if not direct_file_id:
            await message.reply_text("❌ Invalid Google Drive link")
            return

        try:
            direct_item = await asyncio.to_thread(
                service.files().get(
                    fileId=direct_file_id,
                    fields="id,name,size"
                ).execute
            )
            found = {
                "id": direct_item.get("id"),
                "name": direct_item.get("name", "Unnamed file"),
                "size": int(direct_item.get("size", 0) or 0),
            }
        except HttpError as e:
            logger.error(f"Drive /get direct link failed (API): {e}")
            await message.reply_text("❌ Could not access file from link")
            return
        except Exception as e:
            logger.error(f"Drive /get direct link failed: {e}")
            await message.reply_text("❌ Failed to parse/download from Drive link")
            return
    else:
        try:
            found = await find_drive_file(service, query)
        except Exception as e:
            logger.error(f"Drive search failed in /get: {e}")
            await message.reply_text("❌ Could not search Google Drive")
            return

        if not found or not found.get("id"):
            await message.reply_text("❌ File not found")
            return

    file_id = found["id"]
    file_name = found.get("name", "file")
    safe_name = sanitize_filename(file_name, fallback=f"file_{file_id[:8]}")
    local_path = f"/tmp/get_{uuid.uuid4().hex[:8]}_{safe_name}"

    progress_msg = await message.reply_text(
        f"⬇️ Downloading from Drive... 0%\n📄 {file_name}"
    )
    last_update = {"ts": 0.0}

    async def progress(percent: int):
        now = time.time()
        if percent < 100 and now - last_update["ts"] < 1.0:
            return
        last_update["ts"] = now
        try:
            await progress_msg.edit_text(
                f"⬇️ Downloading from Drive... {percent}%\n"
                f"📄 {file_name}"
            )
        except Exception:
            pass

    try:
        await download_drive_file(service, file_id, local_path, progress_callback=progress)

        with open(local_path, "rb") as fh:
            await message.reply_document(document=fh, filename=file_name)

        try:
            await progress_msg.edit_text(f"✅ Sent: {file_name}")
        except Exception:
            pass
    except HttpError as e:
        logger.error(f"Drive download failed (API): {e}")
        await message.reply_text("❌ Download failed. The file may be inaccessible.")
    except Exception as e:
        logger.error(f"Drive download failed in /get: {e}")
        await message.reply_text("❌ Failed to download/send file")
    finally:
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass


async def analytics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or not has_permission(user.id, context, "files"):
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


async def adduser_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    user_id = user.id if user else 0
    if not has_permission(user_id, context, "adduser"):
        await message.reply_text("❌ Only owner can use this command")
        return

    if not context.args:
        await message.reply_text("Usage: /adduser <user_id>")
        return

    raw_target = (context.args[0] or "").strip()
    if not raw_target.isdigit():
        await message.reply_text("❌ Invalid user_id")
        return

    new_user_id = int(raw_target)
    users_store = context.bot_data.get("users")
    if not isinstance(users_store, set):
        users_store = set(users_store or [])

    users_store.add(new_user_id)
    context.bot_data["users"] = users_store
    save_allowed_users(users_store)

    await message.reply_text(f"✅ User {new_user_id} added")


async def remove_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    user_id = user.id if user else 0
    if not has_permission(user_id, context, "removeuser"):
        await message.reply_text("❌ Only owner can use this command")
        return

    if not context.args:
        await message.reply_text("Usage: /removeuser <user_id>")
        return

    raw_target = (context.args[0] or "").strip()
    if not raw_target.isdigit():
        await message.reply_text("❌ Invalid user_id")
        return

    target_user_id = int(raw_target)
    users_store = context.bot_data.get("users")
    if not isinstance(users_store, set):
        users_store = set(users_store or [])

    existed = target_user_id in users_store
    users_store.discard(target_user_id)
    context.bot_data["users"] = users_store
    save_allowed_users(users_store)

    if existed:
        await message.reply_text("✅ User removed")
    else:
        await message.reply_text("⚠️ User not in list")


async def add_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    user_id = user.id if user else 0
    if not has_permission(user_id, context, "addadmin"):
        await message.reply_text("❌ Only owner can use this command")
        return

    if not context.args:
        await message.reply_text("Usage: /addadmin <user_id>")
        return

    raw_target = (context.args[0] or "").strip()
    if not raw_target.isdigit():
        await message.reply_text("❌ Invalid user_id")
        return

    new_admin_id = int(raw_target)
    admins_store = context.bot_data.get("admins")
    if not isinstance(admins_store, set):
        admins_store = set(admins_store or [])

    admins_store.add(new_admin_id)
    context.bot_data["admins"] = admins_store
    save_admin_users(admins_store)

    await message.reply_text("✅ Admin added")


async def remove_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    user_id = user.id if user else 0
    if not has_permission(user_id, context, "removeadmin"):
        await message.reply_text("❌ Only owner can use this command")
        return

    if not context.args:
        await message.reply_text("Usage: /removeadmin <user_id>")
        return

    raw_target = (context.args[0] or "").strip()
    if not raw_target.isdigit():
        await message.reply_text("❌ Invalid user_id")
        return

    target_admin_id = int(raw_target)
    admins_store = context.bot_data.get("admins")
    if not isinstance(admins_store, set):
        admins_store = set(admins_store or [])

    existed = target_admin_id in admins_store
    admins_store.discard(target_admin_id)
    context.bot_data["admins"] = admins_store
    save_admin_users(admins_store)

    if existed:
        await message.reply_text("✅ Admin removed")
    else:
        await message.reply_text("⚠️ Admin not in list")


async def files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or not has_permission(user.id, context, "files"):
        await message.reply_text("❌ You don't have access to this command")
        return

    if not await require_connection(update, user.id):
        return

    try:
        text, keyboard = await build_files_page(context, user.id, page=1, page_size=10)
        await message.reply_text(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Files command failed: {e}")
        await message.reply_text(f"❌ Could not fetch files:\n`{str(e)}`", parse_mode="Markdown")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user

    if not user or not has_permission(user.id, context, "files"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
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
        service = get_user_service(user.id)

        safe_query = escape_drive_query_value(query.lower().strip())
        suggestion_result = await asyncio.to_thread(
            service.files().list(
                q=f"trashed = false and name contains '{safe_query}'",
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


async def build_files_page(context: ContextTypes.DEFAULT_TYPE, user_id: int, page: int, page_size: int = 10):
    service = get_user_service(user_id)

    # Simulated paging: fetch a bounded latest set, then slice by page index.
    result = await asyncio.to_thread(
        service.files().list(
            q="'root' in parents and trashed = false",
            orderBy="createdTime desc",
            pageSize=150,
            fields="files(id,name)"
        ).execute
    )
    all_items = result.get("files", [])

    if not all_items:
        return "📂 No files found in your Google Drive.", None

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

    service = get_user_service(context.user_data.get("_oauth_user_id", 0))

    safe_query = escape_drive_query_value(query)
    result = await asyncio.to_thread(
        service.files().list(
            q=f"trashed = false and name contains '{safe_query}'",
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
    user_id = update.effective_user.id
    service = get_user_service(user_id)

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
    user_id: int,
    chat_id: int,
    message_id: int,
    file_id: str,
    page: int,
    back_callback: str | None = None,
    delete_callback: str | None = None
):
    service = get_user_service(user_id)

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

    if not user or not has_permission(user.id, context, "upload"):
        await query.answer("Unauthorized", show_alert=True)
        return

    try:
        raw_data = query.data or ""

        # Handle trash callbacks (raw data, not stored)
        if raw_data.startswith("cb_restore_"):
            file_id = raw_data[len("cb_restore_"):]
            service = get_user_service(user.id)
            if service:
                await asyncio.to_thread(
                    service.files().update(fileId=file_id, body={"trashed": False}).execute
                )
                await query.answer("\u2705 File restored!")
                await query.edit_message_text("\u2705 File restored from trash.")
            return

        if raw_data.startswith("cb_permdelete_"):
            file_id = raw_data[len("cb_permdelete_"):]
            service = get_user_service(user.id)
            if service:
                await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
                await query.answer("\u2705 Permanently deleted!")
                await query.edit_message_text("\U0001f5d1 File permanently deleted.")
            return

        if raw_data == "cb_emptytrash":
            service = get_user_service(user.id)
            if service:
                await asyncio.to_thread(service.files().emptyTrash().execute)
                await query.answer("\u2705 Trash emptied!")
                await query.edit_message_text("\U0001f5d1 Trash has been emptied.")
            return

        payload = resolve_callback_data(context, raw_data)
        if not payload:
            await query.answer("Action expired. Please try again.", show_alert=True)
            return

        action = payload.get("action")

        if action in {"delete_from_files", "delete_from_search", "delete_upload"} and not has_permission(user.id, context, "delete"):
            await query.answer("❌ Not allowed", show_alert=True)
            return

        if action == "rename" and not has_permission(user.id, context, "rename"):
            await query.answer("❌ Not allowed", show_alert=True)
            return

        if action in {"public", "private", "expire_link"} and not has_permission(user.id, context, "share"):
            await query.answer("❌ Not allowed", show_alert=True)
            return

        if action in {
            "files_page",
            "search_page",
            "open_file_from_files",
            "open_file_search_suggestion",
            "open_file_from_search_results",
        } and not has_permission(user.id, context, "files"):
            await query.answer("❌ Not allowed", show_alert=True)
            return

        if action == "files_page":
            page = int(payload.get("page", 1))
            context.user_data["_oauth_user_id"] = user.id
            text, keyboard = await build_files_page(context, user.id, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
            return

        if action == "search_page":
            session_id = str(payload.get("session_id"))
            page = int(payload.get("page", 1))
            context.user_data["_oauth_user_id"] = user.id
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
            service = get_user_service(user.id)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            text, keyboard = await build_files_page(context, user.id, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer("✅ File deleted")
            return

        if action == "delete_from_search":
            file_id = payload.get("file_id")
            session_id = str(payload.get("session_id"))
            page = int(payload.get("page", 1))
            service = get_user_service(user.id)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            context.user_data["_oauth_user_id"] = user.id
            text, keyboard = await build_search_page(context, session_id=session_id, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)
            await query.answer("✅ File deleted")
            return

        if action == "delete_upload":
            file_id = payload.get("file_id")
            service = get_user_service(user.id)
            await asyncio.to_thread(service.files().delete(fileId=file_id).execute)
            await query.edit_message_text("✅ File deleted from Google Drive.")
            await query.answer("✅ File deleted")
            return

        if action in {"public", "private"}:
            file_id = payload.get("file_id")
            service = get_user_service(user.id)

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
                    user_id=user.id,
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
            service = get_user_service(user.id)

            permission_id = await ensure_public_permission(service, file_id)
            asyncio.create_task(revoke_public_after_delay(context, user.id, file_id, permission_id, duration))
            context.bot_data.setdefault("exp_links", {})[file_id] = time.time() + duration

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

    if not user or not has_permission(user.id, context, "rename"):
        return

    file_id = context.user_data.get("rename_file_id")
    if not file_id:
        return

    new_name = (message.text or "").strip()
    if not new_name:
        await message.reply_text("❌ Name cannot be empty. Send a valid file name.")
        return

    try:
        service = get_user_service(user.id)
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
                user_id=user.id,
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

    if not user or not has_permission(user.id, context, "upload"):
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


async def duplicate_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or not has_permission(user.id, context, "upload"):
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data or ""
    if not data.startswith("dup_"):
        await query.answer()
        return

    parts = data.split("_")
    if len(parts) != 3:
        await query.answer("Invalid action", show_alert=True)
        return

    action = parts[1]
    task_id = parts[2]
    task_data = context.bot_data.get(task_id)
    if not isinstance(task_data, dict):
        await query.answer("Upload task expired", show_alert=True)
        return

    decision_future = task_data.get("decision_future")
    if decision_future is None:
        await query.answer("Upload task expired", show_alert=True)
        context.bot_data.pop(task_id, None)
        return

    if action == "skip":
        local_path = task_data.get("file_path")
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass

        if not decision_future.done():
            decision_future.set_result("skip")
        context.bot_data.pop(task_id, None)
        await query.edit_message_text("❌ Upload skipped")
        await query.answer("Skipped")
        return

    if action == "replace":
        existing_file_id = task_data.get("existing_file_id")
        if existing_file_id:
            try:
                service = get_user_service(user.id)
                await asyncio.to_thread(
                    service.files().delete(fileId=existing_file_id).execute
                )
            except Exception as e:
                logger.error(f"Failed to delete duplicate before replace: {e}")
                if not decision_future.done():
                    decision_future.set_exception(RuntimeError("Could not replace existing file"))
                context.bot_data.pop(task_id, None)
                await query.edit_message_text("❌ Failed to replace existing file")
                await query.answer("Replace failed", show_alert=True)
                return

        if not decision_future.done():
            decision_future.set_result("replace")
        context.bot_data.pop(task_id, None)
        await query.edit_message_text("♻️ Replacing existing file...")
        await query.answer("Replacing")
        return

    await query.answer("Unknown action", show_alert=True)


async def pause_transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not user or not has_permission(user.id, context, "upload"):
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

    if not user or not has_permission(user.id, context, "upload"):
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
    download_dir: str = "/tmp",
):
    message = update.message if update.message else (update.callback_query.message if update.callback_query else None)
    if not message:
        return

    user = update.effective_user
    user_id = user.id if user else 0
    upload_semaphore = context.bot_data.get("upload_semaphore")

    known_size = int(file_size or 0)
    size_label = f"{known_size / (1024 * 1024):.2f} MB" if known_size else "Unknown"
    os.makedirs(download_dir, exist_ok=True)
    local_path = os.path.join(download_dir, filename)
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

    # Duplicate detection by exact filename and size before upload.
    try:
        service = get_user_service(user_id)
        existing_id = await check_duplicate(service, filename, known_size)
    except Exception as e:
        logger.warning(f"Duplicate check failed, continuing upload: {e}")
        existing_id = None

    if existing_id:
        dup_task_id = uuid.uuid4().hex[:8]
        decision_future = asyncio.get_running_loop().create_future()
        context.bot_data[dup_task_id] = {
            "file_name": filename,
            "file_path": local_path,
            "existing_file_id": existing_id,
            "decision_future": decision_future,
        }

        await progress_msg.edit_text(
            "⚠️ File already exists\n"
            f"Name: {filename}\n"
            f"Size: {format_bytes_stats(known_size)}",
            reply_markup=build_duplicate_keyboard(dup_task_id)
        )

        try:
            decision = await decision_future
        except Exception as e:
            cleanup_local_file()
            context.bot_data.pop(task_id, None)
            await progress_msg.edit_text(f"❌ Duplicate resolution failed:\n`{str(e)}`", parse_mode="Markdown")
            return

        if decision == "skip":
            context.bot_data.get("transfer_tasks", {}).pop(task_id, None)
            return

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
                    user_id,
                    local_path,
                    filename,
                    file_size=known_size,
                    progress_callback=update_upload_progress,
                    should_cancel=is_cancelled
                )
        else:
            uploaded_file_id, _ = await upload_to_drive(
                user_id,
                local_path,
                filename,
                file_size=known_size,
                progress_callback=update_upload_progress,
                should_cancel=is_cancelled
            )

        await send_uploaded_ui(
            update,
            context,
            user_id,
            uploaded_file_id,
            filename,
            message_to_edit=progress_msg,
        )
        await notify(
            context,
            "✅ Upload Finished\n"
            f"📄 {filename}\n"
            f"📦 {known_size / (1024 * 1024):.2f} MB"
        )
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

    if not user or not has_permission(user.id, context, "upload"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
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


async def handle_drive_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    user = update.effective_user

    text = (message.text or "").strip()
    url = extract_first_url(text)
    if not url or not is_google_drive_link(url):
        return False

    if not user or not has_permission(user.id, context, "upload"):
        await message.reply_text("❌ Unauthorized access")
        return True

    if not await require_connection(update, user.id):
        return True

    file_id = extract_drive_file_id(url)
    if not file_id:
        await message.reply_text("❌ Invalid Google Drive link")
        return True

    status_msg = await message.reply_text("📎 Cloning Google Drive file...")
    try:
        cloned = await clone_drive_file(user.id, file_id)
        await send_uploaded_ui(
            update,
            context,
            user.id,
            cloned["id"],
            cloned.get("name", "cloned_file"),
            message_to_edit=status_msg,
        )
    except HttpError as e:
        logger.error(f"Drive clone failed (API): {e}")
        await status_msg.edit_text("❌ Could not clone file. It may be private or inaccessible.")
    except Exception as e:
        logger.error(f"Drive clone failed: {e}")
        await status_msg.edit_text("❌ Failed to clone Google Drive link")

    return True


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("rename_file_id"):
        await handle_rename_input(update, context)
        return

    if await handle_drive_link_message(update, context):
        return

    await handle_url_message(update, context)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # Access control
    user = update.effective_user
    if not user or not has_permission(user.id, context, "upload"):
        await message.reply_text("❌ Unauthorized access")
        return

    if not await require_connection(update, user.id):
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
                context.bot,
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


async def start_oauth_server(telegram_bot):
    """Start the aiohttp web server for OAuth callbacks."""
    oauth_app = web.Application()
    oauth_app["telegram_bot"] = telegram_bot
    oauth_app.router.add_get("/oauth/callback", handle_oauth_callback)
    runner = web.AppRunner(oauth_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", OAUTH_SERVER_PORT)
    await site.start()
    logger.info(f"OAuth callback server started on port {OAUTH_SERVER_PORT}")
    return runner


def main():
    fix_volume_permissions()
    os.makedirs(USER_CREDS_DIR, exist_ok=True)

    builder = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True)
    if USE_LOCAL_API:
        builder = builder.base_url(LOCAL_API_URL).base_file_url(LOCAL_FILE_URL).local_mode(True)
        logger.info("Using local Telegram Bot API server")
    else:
        logger.info("Using standard Telegram API")
    app = builder.build()

    app.bot_data["upload_semaphore"] = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)
    app.bot_data["users"] = load_allowed_users()
    app.bot_data["admins"] = load_admin_users()
    app.bot_data.setdefault("exp_links", {})
    app.bot_data.setdefault("last_storage_alert", 0.0)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("commands", commands_handler))
    app.add_handler(CommandHandler("connect", connect_handler))
    app.add_handler(CommandHandler("disconnect", disconnect_handler))
    app.add_handler(CommandHandler("storage", storage))
    app.add_handler(CommandHandler("stats", stats_handler))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    app.add_handler(CommandHandler("adduser", adduser_handler))
    app.add_handler(CommandHandler("removeuser", remove_user_handler))
    app.add_handler(CommandHandler("addadmin", add_admin_handler))
    app.add_handler(CommandHandler("removeadmin", remove_admin_handler))
    app.add_handler(CommandHandler("get", get_file_handler))
    app.add_handler(CommandHandler("files", files))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("newfolder", newfolder_handler))
    app.add_handler(CommandHandler("recent", recent_handler))
    app.add_handler(CommandHandler("trash", trash_handler))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(pause_transfer_callback, pattern=r"^pause_"))
    app.add_handler(CallbackQueryHandler(resume_transfer_callback, pattern=r"^resume_"))
    app.add_handler(CallbackQueryHandler(cancel_transfer_callback, pattern=r"^cancel_"))
    app.add_handler(CallbackQueryHandler(duplicate_upload_callback, pattern=r"^dup_"))
    app.add_handler(CallbackQueryHandler(files_callback_handler, pattern=r"^cb_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    if app.job_queue:
        app.job_queue.run_repeating(check_expired_links, interval=60, first=60)
        app.job_queue.run_repeating(check_storage, interval=3600, first=120)
    else:
        logger.warning("Job queue unavailable; background notifications are disabled")

    logger.info(f"🤖 Bot is running... (parallel uploads: {MAX_PARALLEL_UPLOADS})")
    return app


if __name__ == "__main__":
    telegram_app = main()

    async def run():
        async with telegram_app:
            oauth_runner = await start_oauth_server(telegram_app.bot)
            try:
                await telegram_app.start()
                await telegram_app.updater.start_polling()
                logger.info("✅ Bot and OAuth server are running")
                stop_event = asyncio.Event()
                await stop_event.wait()
            finally:
                await telegram_app.updater.stop()
                await telegram_app.stop()
                await oauth_runner.cleanup()

    asyncio.run(run())
