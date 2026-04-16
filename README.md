# Telegram Drive Bot

A Telegram bot that uploads user files and URLs to Google Drive, with role-based access control, file browsing, search, rename/delete actions, and basic analytics.

## Features

- Upload Telegram files to Google Drive (document/photo/video/audio/voice)
- Upload from URL by sending a link in chat
- Clone Google Drive links by sending a Drive URL in chat
- Browse Drive files with inline buttons (`/files`)
- Search files (`/search <query>`)
- Download from Drive (`/get <name|id|drive_link>`)
- Public/private toggle and expiring links (1h / 24h)
- Duplicate detection (Skip/Replace)
- Owner/Admin/User roles with permissions
- Upload/download analytics (`analytics.json`)
- Optional parallel uploads via semaphore

## Project Files

- `bot.py` - main bot implementation
- `requirements.txt` - Python dependencies
- `settings.yaml` - PyDrive2 auth settings
- `client_secrets.json` - Google OAuth client credentials
- `saved_creds.json` - generated OAuth token cache (after first auth)
- `Procfile` - process entry (`worker: python bot.py`)

## Requirements

- Python 3.10+
- Telegram bot token (from BotFather)
- Google account + Google Drive API enabled project
- OAuth client secret JSON for desktop app (`client_secrets.json`)

Optional for large Telegram files (>20MB):

- Local Telegram Bot API server running in Docker and mapped to host paths used by `bot.py`

## Installation

1. Clone the repository and move into the project folder.
2. Create and activate a virtual environment.
3. Install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment Variables

Create a `.env` file in the project root:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
OWNER_ID=123456789
DRIVE_FOLDER_ID=root
MAX_PARALLEL_UPLOADS=3
MAX_URL_DOWNLOAD_SIZE=2147483648
```

Notes:

- `OWNER_ID` is mandatory for admin notifications and owner-only commands.
- `DRIVE_FOLDER_ID` defaults to `root`.
- `MAX_URL_DOWNLOAD_SIZE` is in bytes (default is 2GB).

## Google Drive Setup

1. In Google Cloud Console:
   - Enable Google Drive API.
   - Create OAuth client credentials (Desktop App).
2. Download credentials JSON and save it as `client_secrets.json` in project root.
3. Keep `settings.yaml` as configured:
   - Uses file backend
   - Reads `client_secrets.json`
   - Stores auth tokens in `saved_creds.json`

First run will open an OAuth flow. After successful login, `saved_creds.json` will be created and reused.

## Run

```bash
python bot.py
```

## Commands

- `/start` - intro message
- `/commands` - show commands available to your role
- `/storage` - Drive storage quota
- `/stats` - Drive usage + file count
- `/analytics` - upload/download analytics
- `/files` - browse files with inline actions
- `/search <query>` - search files
- `/get <name|id|drive_link>` - download and send file from Drive
- `/adduser <user_id>` - owner only
- `/removeuser <user_id>` - owner only
- `/addadmin <user_id>` - owner only
- `/removeadmin <user_id>` - owner only

## Permissions Model

- Owner: full access
- Admin: upload/files/delete/rename/share
- User: upload only

Data files used for access control:

- `users.json`
- `admins.json`

## Local Telegram Bot API (Large File Support)

This bot is configured to use a local Bot API endpoint in `bot.py`:

- `LOCAL_API_URL = "http://localhost:8081/bot"`
- `LOCAL_API_DIR = "/var/lib/telegram-bot-api"`
- `VOLUME_HOST_PATH = "/home/azureuser/telegram-api-data"`

If your environment differs, update these constants in `bot.py`.

Without local API:

- CDN fallback works only for smaller files (around 20MB limit).

## Deployment

`Procfile` is included for worker-based platforms:

```Procfile
worker: python bot.py
```

## Security Notes

- Do not commit real `.env`, `client_secrets.json`, or `saved_creds.json` to public repos.
- Restrict bot usage using owner/admin/user controls.

## Troubleshooting

- `Unauthorized access`:
  - Ensure your Telegram user ID is configured as owner/admin/user.
- Drive auth errors:
  - Verify Drive API is enabled and `client_secrets.json` is valid.
- Large file download/upload fails:
  - Confirm local Telegram Bot API server is running and path mappings match `bot.py`.
- URL upload rejected for size:
  - Increase `MAX_URL_DOWNLOAD_SIZE` in `.env`.
