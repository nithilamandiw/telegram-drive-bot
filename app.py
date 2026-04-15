import os
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from googleapiclient.discovery import build
from pydrive2.auth import GoogleAuth


load_dotenv()
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "root")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Telegram Drive Bot Dashboard")


def get_drive_http():
    """Reuse the same Google Drive auth flow used by the bot."""
    gauth = GoogleAuth(settings_file="settings.yaml")
    gauth.LoadCredentialsFile("saved_creds.json")

    if gauth.credentials is None:
        raise RuntimeError("Missing saved_creds.json. Run bot.py once to authenticate.")
    if gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    gauth.SaveCredentialsFile("saved_creds.json")
    return gauth.Get_Http_Object()


def drive_service():
    http = get_drive_http()
    return build("drive", "v3", http=http, cache_discovery=False)


def human_size(size_value):
    try:
        size = float(size_value)
    except (TypeError, ValueError):
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.2f} {units[idx]}"


@app.get("/")
async def home(request: Request):
    try:
        svc = drive_service()

        files = []
        page_token = None
        while True:
            result = await asyncio.to_thread(
                svc.files().list(
                    q=f"'{DRIVE_FOLDER_ID}' in parents and trashed=false",
                    fields="nextPageToken, files(id,name,size,webViewLink,createdTime)",
                    orderBy="createdTime desc",
                    pageSize=100,
                    pageToken=page_token,
                ).execute
            )
            files.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        for f in files:
            f["size_human"] = human_size(f.get("size"))

        return templates.TemplateResponse(
            "index.html",
            {"request": request, "files": files, "folder_id": DRIVE_FOLDER_ID},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load files: {e}")


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    temp_path = BASE_DIR / f".tmp_{file.filename}"
    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        svc = drive_service()
        metadata = {"name": file.filename, "parents": [DRIVE_FOLDER_ID]}

        # Use MediaFileUpload through google client request body helper
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(temp_path), resumable=False)
        await asyncio.to_thread(
            svc.files().create(body=metadata, media_body=media, fields="id").execute
        )

        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


@app.post("/delete/{file_id}")
async def delete_file(file_id: str, mode: str = Form("trash")):
    """mode=trash (default) or mode=delete"""
    try:
        svc = drive_service()
        if mode == "delete":
            await asyncio.to_thread(svc.files().delete(fileId=file_id).execute)
        else:
            await asyncio.to_thread(
                svc.files().update(fileId=file_id, body={"trashed": True}).execute
            )
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
