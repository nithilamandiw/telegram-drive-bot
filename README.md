# 📁 Telegram → Google Drive Bot

A Telegram bot that automatically uploads any file sent to it straight into your Google Drive.

## Features
- 📄 Documents
- 🖼 Photos
- 🎥 Videos
- 🎵 Audio files
- 🎤 Voice messages

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/telegram-drive-bot.git
cd telegram-drive-bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Add your secrets
```bash
cp .env.example .env
```
Edit `.env` and fill in:
- `TELEGRAM_TOKEN` — from @BotFather on Telegram
- `DRIVE_FOLDER_ID` — Google Drive folder ID (or leave as `root`)

### 4. Add Google credentials
Place your `client_secrets.json` (downloaded from Google Cloud Console) in the project folder.

### 5. Run locally (first time — generates saved_creds.json)
```bash
python bot.py
```
A browser will open for Google login. After that, `saved_creds.json` is saved automatically.

## Deploy to Railway

1. Push your code to GitHub (make sure `saved_creds.json` is included for Railway)
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables: `TELEGRAM_TOKEN` and `DRIVE_FOLDER_ID`
4. Done! 🚀

## Project Structure
```
telegram-drive-bot/
├── bot.py              # Main bot code
├── settings.yaml       # PyDrive2 config
├── requirements.txt    # Python dependencies
├── Procfile            # Railway start command
├── .env.example        # Environment variable template
├── .gitignore          # Files to exclude from git
└── README.md           # This file
```

## ⚠️ Never commit these files
- `.env`
- `client_secrets.json`
- `saved_creds.json`
# telegram-drive-bot
