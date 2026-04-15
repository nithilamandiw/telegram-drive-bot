import os
import logging
import subprocess
import asyncio
import httpx
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

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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


def clean_drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


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


async def build_upload_action_keyboard(service, file_id: str):
    is_public = await is_file_public(service, file_id)
    toggle_button = (
        InlineKeyboardButton("🔒 Make Private", callback_data=f"private_{file_id}")
        if is_public else
        InlineKeyboardButton("🌐 Make Public", callback_data=f"public_{file_id}")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_drive_file_url(file_id))],
        [toggle_button],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{file_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete_{file_id}")
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

    file_id = response["id"]
    return file_id, clean_drive_file_url(file_id)


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
                    callback_data=f"file_{file_id}_{page}"
                )
            ])

    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page - 1}"))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"page_{page + 1}"))
    if buttons:
        keyboard_rows.append(buttons)
    keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    return "\n".join(lines), keyboard


async def file_view(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, page: int):
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
        InlineKeyboardButton("🔒 Make Private", callback_data=f"private_{file_id}")
        if public_state else
        InlineKeyboardButton("🌐 Make Public", callback_data=f"public_{file_id}")
    )

    context.user_data["files_current_page"] = page
    context.user_data["files_current_file_id"] = file_id

    text = (
        f"📄 {name}\n"
        f"📦 {size_text}\n"
        "🔗 Open using the button below"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_link)],
        [toggle_button],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{file_id}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"delpage_{file_id}_{page}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_{page}")]
    ])
    await query.edit_message_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)


async def refresh_file_view_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    file_id: str,
    page: int
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
        InlineKeyboardButton("🔒 Make Private", callback_data=f"private_{file_id}")
        if public_state else
        InlineKeyboardButton("🌐 Make Public", callback_data=f"public_{file_id}")
    )

    text = (
        f"📄 {name}\n"
        f"📦 {size_text}\n"
        "🔗 Open using the button below"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open File", url=clean_link)],
        [toggle_button],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{file_id}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"delpage_{file_id}_{page}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"back_{page}")]
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
        data = query.data or ""

        if data.startswith("page_"):
            page = int(data.replace("page_", ""))
            text, keyboard = await build_files_page(context, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
            return

        if data.startswith("back_"):
            page = int(data.replace("back_", ""))
            text, keyboard = await build_files_page(context, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
            return

        if data.startswith("file_"):
            payload = data[len("file_"):]
            file_id, page_str = payload.rsplit("_", 1)
            page = int(page_str)
            await file_view(update, context, file_id=file_id, page=page)
            await query.answer()
            return

        if data.startswith("delpage_"):
            payload = data[len("delpage_"):]
            file_id, page_str = payload.rsplit("_", 1)
            page = int(page_str)
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            await asyncio.to_thread(
                service.files().delete(fileId=file_id).execute
            )
            text, keyboard = await build_files_page(context, page=page, page_size=10)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer("✅ File deleted")
            return

        if data.startswith("delete_"):
            payload = data[len("delete_"):]
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            await asyncio.to_thread(
                service.files().delete(fileId=payload).execute
            )
            await query.edit_message_text("✅ File deleted from Google Drive.")
            await query.answer("✅ File deleted")
            return

        if data.startswith("public_"):
            file_id = data[len("public_"):]
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            if not await is_file_public(service, file_id):
                await asyncio.to_thread(
                    service.permissions().create(
                        fileId=file_id,
                        body={"type": "anyone", "role": "reader"}
                    ).execute
                )

            current_page = context.user_data.get("files_current_page")
            current_file_id = context.user_data.get("files_current_file_id")
            if current_page and current_file_id == file_id:
                await file_view(update, context, file_id=file_id, page=int(current_page))
            else:
                keyboard = await build_upload_action_keyboard(service, file_id)
                await query.edit_message_text("🌐 File is now public", reply_markup=keyboard)
            await query.answer("🌐 File is now public")
            return

        if data.startswith("private_"):
            file_id = data[len("private_"):]
            drive = context.bot_data.get("drive")
            http = drive.auth.Get_Http_Object()
            service = build("drive", "v3", http=http, cache_discovery=False)

            perm_id = await get_public_permission_id(service, file_id)
            if perm_id:
                await asyncio.to_thread(
                    service.permissions().delete(
                        fileId=file_id,
                        permissionId=perm_id
                    ).execute
                )

            current_page = context.user_data.get("files_current_page")
            current_file_id = context.user_data.get("files_current_file_id")
            if current_page and current_file_id == file_id:
                await file_view(update, context, file_id=file_id, page=int(current_page))
            else:
                keyboard = await build_upload_action_keyboard(service, file_id)
                await query.edit_message_text("🔒 File is now private", reply_markup=keyboard)
            await query.answer("🔒 File is now private")
            return

        if data.startswith("rename_"):
            file_id = data[len("rename_"):]
            context.user_data["rename_file_id"] = file_id

            # If rename is triggered from file-view UI, remember where to refresh.
            rename_return = None
            if query.message and query.message.reply_markup:
                for row in query.message.reply_markup.inline_keyboard:
                    for btn in row:
                        cb = btn.callback_data or ""
                        if cb.startswith("back_"):
                            try:
                                page = int(cb.replace("back_", ""))
                                rename_return = {
                                    "chat_id": query.message.chat_id,
                                    "message_id": query.message.message_id,
                                    "file_id": file_id,
                                    "page": page
                                }
                            except Exception:
                                pass
                            break
                    if rename_return:
                        break
            context.user_data["rename_return"] = rename_return

            await query.edit_message_text("✏️ Send new file name")
            await query.answer()
            return

        await query.answer()
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
                page=rename_return["page"]
            )
        else:
            await message.reply_text(f"✅ Renamed to: {updated.get('name', new_name)}")
    except Exception as e:
        logger.error(f"Rename failed: {e}")
        context.user_data.pop("rename_file_id", None)
        context.user_data.pop("rename_return", None)
        await message.reply_text(f"❌ Rename failed:\n`{str(e)}`", parse_mode="Markdown")


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
                uploaded_file_id, link = await upload_to_drive(
                    drive,
                    local_path,
                    filename,
                    file_size=file_size or 0,
                    progress_callback=update_upload_progress
                )
        else:
            uploaded_file_id, link = await upload_to_drive(
                drive,
                local_path,
                filename,
                file_size=file_size or 0,
                progress_callback=update_upload_progress
            )
        http = drive.auth.Get_Http_Object()
        service = build("drive", "v3", http=http, cache_discovery=False)
        upload_keyboard = await build_upload_action_keyboard(service, uploaded_file_id)

        await progress_msg.edit_text("✅ Uploaded!", reply_markup=upload_keyboard)
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
    app.add_handler(CommandHandler("storage", storage))
    app.add_handler(CommandHandler("files", files))
    app.add_handler(CallbackQueryHandler(files_callback_handler, pattern=r"^(page_|file_|delpage_|delete_|back_|rename_|public_|private_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rename_input))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE,
        handle_file
    ))

    logger.info(f"🤖 Bot is running... (parallel uploads: {MAX_PARALLEL_UPLOADS})")
    app.run_polling()


if __name__ == "__main__":
    main()
