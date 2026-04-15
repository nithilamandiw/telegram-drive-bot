# Telegram Drive Bot

A private Telegram bot that uploads files to Google Drive and provides a file-browser style UI to manage them.

Built with `python-telegram-bot` and Google Drive API, with optional local Telegram Bot API (Docker) for reliable large-file support.

## Features
- Upload files/media from Telegram directly to Google Drive
- Large file support using local Telegram Bot API (`--local`)
- Download + upload progress bar with percentage and MB tracking
- Fast chunked/resumable Google Drive upload
- Private bot access restricted to `OWNER_ID`
- `/files` interactive browser with pagination
- Inline file details view (name, size, link)
- Delete file from Drive with inline controls
- Back navigation in file browser
- `/storage` command to view Drive quota usage (total/used/free)

## Commands
- `/files` → Browse files in your configured Drive folder
- `/storage` → Show Google Drive storage usage
- Send a file/photo/video/audio/voice message → Upload to Google Drive

## Installation
1. Clone the repository
```bash
git clone https://github.com/YOUR_USERNAME/telegram-drive-bot.git
cd telegram-drive-bot
```

2. Create and activate virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies
```bash
pip install -r requirements.txt
```

4. Add Google OAuth files
- Place `client_secrets.json` in the project root
- Keep `settings.yaml` configured for your Google app

## Environment Variables (`.env`)
Create `.env` in project root:

```env
TELEGRAM_TOKEN=
DRIVE_FOLDER_ID=
OWNER_ID=
MAX_PARALLEL_UPLOADS=3
```

- `TELEGRAM_TOKEN`: Token from `@BotFather`
- `DRIVE_FOLDER_ID`: Google Drive folder ID (`root` to use My Drive root)
- `OWNER_ID`: Your Telegram numeric user ID (only this user can use the bot)
- `MAX_PARALLEL_UPLOADS`: Optional upload concurrency limit

## Running Local Telegram Bot API (Docker)
For large files, run local Bot API server with `--local`:

```bash
mkdir -p /home/azureuser/telegram-api-data

docker run -d \
  --name telegram-bot-api \
  -p 8081:8081 \
  -v /home/azureuser/telegram-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest \
  --api-id <YOUR_API_ID> \
  --api-hash <YOUR_API_HASH> \
  --local \
  --http-port 8081 \
  --dir /var/lib/telegram-bot-api
```

Check status:
```bash
docker ps
```

## Running the Bot
```bash
python bot.py
```

On first run, Google OAuth may open a browser flow and create `saved_creds.json`.

## Usage
1. Open your bot in Telegram.
2. Send a file/media message.
3. Bot downloads and uploads to Drive with progress updates.
4. Use `/files` to browse uploaded files with inline buttons.
5. Tap a file to view details.
6. Use `🗑 Delete` to remove it (with confirmation flow in UI) or `🔙 Back` to return to list.
7. Use `/storage` anytime to check Drive usage.

## Notes / Limitations
- Bot is private: only `OWNER_ID` can use commands and actions.
- Large file support depends on running local Telegram Bot API (`--local`).
- CDN fallback is limited for large files; local API is required for best reliability.
- Keep sensitive files private:
  - `.env`
  - `client_secrets.json`
  - `saved_creds.json`

## Troubleshooting
### 1) “File too big” / download fails
- Ensure local Telegram Bot API container is running.
- Verify it was started with `--local`.
- Confirm bot points to local API URL (`http://localhost:8081/bot`).

### 2) Local API not working
- Check container logs:
```bash
docker logs telegram-bot-api --tail 100
```
- Verify port mapping `8081:8081` and volume mount path are correct.
- Confirm API ID/API hash are valid.

### 3) Docker issues
- Container not running:
```bash
docker ps -a
```
- Restart container:
```bash
docker restart telegram-bot-api
```
- Permission/path issues: verify mounted host directory exists and is readable by your bot process.

## Project Structure
```text
telegram-drive-bot/
├── bot.py
├── settings.yaml
├── requirements.txt
├── .env.example
├── client_secrets.json
├── saved_creds.json
└── README.md
```
