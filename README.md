# Telegram Drive Bot

A private Telegram bot that receives files/messages and uploads them to your Google Drive.

It uses a **local Telegram Bot API server (Docker)** to support large files (well beyond the public 20 MB cloud fetch limit).

## Features
- Uploads to Google Drive from Telegram:
  - Documents
  - Photos
  - Videos
  - Audio
  - Voice messages
- Large file support via local Telegram Bot API + local mode
- Download and upload progress in Telegram (percentage + progress bar)
- Parallel uploads (configurable)
- Private bot mode using `OWNER_ID` (only one user can use the bot)

## Requirements
- Python 3.10+
- Docker (for local Telegram Bot API server)
- A Telegram bot token from `@BotFather`
- Google Cloud OAuth client credentials (`client_secrets.json`)
- Google Drive API enabled in your Google Cloud project

## Installation
1. Clone the repo
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

4. Create `.env`
```bash
cp .env.example .env
```

5. Add Google credentials
- Put your `client_secrets.json` in the project root.
- Ensure `settings.yaml` matches your OAuth setup.

## Environment Variables (.env)
Add/update the following in `.env`:

```env
TELEGRAM_TOKEN=123456:ABCDEF_your_bot_token
DRIVE_FOLDER_ID=root
OWNER_ID=123456789
MAX_PARALLEL_UPLOADS=3
```

- `TELEGRAM_TOKEN`: Bot token from BotFather
- `DRIVE_FOLDER_ID`: Target Drive folder ID (`root` for My Drive root)
- `OWNER_ID`: Your Telegram numeric user ID (only this user can use the bot)
- `MAX_PARALLEL_UPLOADS`: Max simultaneous uploads to Drive

## Running Local Bot API (Docker)
Run Telegram Bot API server locally in **local mode** with persistent storage:

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

Verify it is running:
```bash
docker ps
```

## Running the Bot
1. Start the bot:
```bash
python bot.py
```

2. First run will open Google OAuth flow (if `saved_creds.json` is not present).

3. After auth completes, bot starts polling and is ready.

## Usage
1. Open your bot in Telegram.
2. Send any supported file/media.
3. Bot will:
   - validate access (`OWNER_ID` only)
   - download file (local API first, CDN fallback for small files)
   - upload to Google Drive
   - reply with a shareable Drive link

## Notes / Limitations
- Public Telegram CDN fallback works only for small files (around 20 MB). Large files depend on your local Bot API server.
- Keep these files private and never commit them:
  - `.env`
  - `client_secrets.json`
  - `saved_creds.json`
- The bot currently runs in polling mode.
- `fix_volume_permissions()` may require `sudo` privileges depending on your server setup.

## Deploy to Railway (optional)
If you still deploy there for non-local workloads:
1. Push code to GitHub.
2. Create project in [railway.app](https://railway.app).
3. Set env vars (`TELEGRAM_TOKEN`, `DRIVE_FOLDER_ID`, `OWNER_ID`, etc.).

## Project Structure
```text
telegram-drive-bot/
├── bot.py
├── settings.yaml
├── requirements.txt
├── Procfile
├── .env.example
└── README.md
```
