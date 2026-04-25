# 🚀 Google Drive Bot

A powerful Telegram bot to upload, manage, and share files directly with Google Drive — with **per-user OAuth2 authentication**, parallel uploads, file management UI, role-based access, and more.

---

## ✨ Features

### 🔐 Per-User Google OAuth2

* Each user connects their **own Google account** via `/connect`
* Credentials stored securely per user (`user_creds/{user_id}.json`)
* Auto token refresh
* `/disconnect` to revoke access

---

### 📤 Upload System

* Upload files directly from Telegram
* Supports **large files (UP TO 2GB)** using Local Bot API
* Parallel uploads (configurable)
* Progress bar with:

  * Percentage
  * Speed
  * ETA
* Cancel upload/download

---

### ☁️ Google Drive Integration

* Upload files to user's own Google Drive
* Generate shareable links
* Clone Drive files (no re-upload needed)
* Public / Private sharing
* Expiring links (1h / 24h)

---

### 📁 File Manager (/files)

* Paginated file listing
* File size display
* Buttons UI:

  * Open file
  * Delete
  * Rename
  * Make public/private
* Clean navigation system

---

### 🔍 Search System

* Search files by name
* Smart suggestions

---

### 🌐 URL Upload

* Upload files from direct links — just send a URL

---

### 📥 Drive Download

```
/get <drive_link>
```

* Download files from Drive back to Telegram

---

### 🔁 Duplicate Detection

* Detects duplicate files using:

  * File name
  * File size
* Options:

  * Skip
  * Replace

---

### 🧠 Smart Routing

Bot automatically detects:

* Files → Upload
* URLs → Download + Upload
* Drive links → Clone

---

### 👥 Role-Based Access System

| Feature             | Owner | Admin | User |
| ------------------- | ----- | ----- | ---- |
| Upload files        | ✅     | ✅     | ✅    |
| View files (/files) | ✅     | ✅     | ❌    |
| Delete files        | ✅     | ✅     | ❌    |
| Rename files        | ✅     | ✅     | ❌    |
| Share settings      | ✅     | ✅     | ❌    |
| Expiring links      | ✅     | ✅     | ❌    |
| Add/remove users    | ✅     | ❌     | ❌    |
| Add/remove admins   | ✅     | ❌     | ❌    |

---

### 📊 Stats & Analytics

* Storage usage and free space
* File count
* Upload/download history
* Top file types

---

### 🔔 Notifications

* Upload finished
* Storage almost full
* Link expiration alerts

---

## ⚙️ Setup

### 1. Clone repository

```bash
git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd telegram-drive-bot
```

### 2. Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Google Cloud Setup

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Create an **OAuth 2.0 Client ID** of type **Web application**
3. Add your redirect URI under **Authorized redirect URIs**:
   * Local testing: `http://localhost:8080/oauth/callback`
   * Production: `https://your-domain.com/oauth/callback`
4. Enable the **Google Drive API** in your project
5. Download the client credentials JSON

### 4. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `OWNER_ID` | Your Telegram user ID (full permissions) |
| `GOOGLE_CLIENT_ID` | OAuth client ID from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret from Google Cloud |
| `OAUTH_REDIRECT_URI` | Callback URL (e.g. `http://localhost:8080/oauth/callback`) |
| `OAUTH_SERVER_PORT` | Port for the OAuth callback server (default: `8080`) |
| `USE_LOCAL_API` | Set to `true` to use local Telegram Bot API for large files |
| `MAX_PARALLEL_UPLOADS` | Max concurrent uploads (default: `3`) |

### 5. Run the bot

```bash
python bot.py
```

---

## 🐳 Local Telegram API (for large files)

For files over 20MB, run the Telegram Bot API server locally via Docker:

```bash
docker run -d -p 8081:8081 \
-e TELEGRAM_API_ID=YOUR_ID \
-e TELEGRAM_API_HASH=YOUR_HASH \
-v telegram-data:/var/lib/telegram-bot-api \
aiogram/telegram-bot-api \
--local \
--http-port=8081 \
--dir=/var/lib/telegram-bot-api \
--temp-dir=/tmp/telegram-bot-api
```

Then set `USE_LOCAL_API=true` in your `.env`.

---

## 📌 Commands

| Command | Description |
| --- | --- |
| `/start` | Welcome message |
| `/connect` | Connect your Google Drive account |
| `/disconnect` | Remove stored Google credentials |
| `/commands` | Show available commands (role-based) |
| `/files` | Browse your Drive files |
| `/search <query>` | Search files by name |
| `/get <name/id/link>` | Get a file from Drive |
| `/storage` | View Drive storage usage |
| `/stats` | View Drive stats |
| `/analytics` | View upload/download analytics |
| `/adduser <id>` | Add authorized user (owner only) |
| `/removeuser <id>` | Remove user (owner only) |
| `/addadmin <id>` | Add admin (owner only) |
| `/removeadmin <id>` | Remove admin (owner only) |

---

## 🔁 Run 24/7 (Recommended)

### Using tmux

```bash
tmux
python bot.py
```

Detach: `CTRL + B → D`

### Using systemd (Production)

```bash
sudo systemctl start telegram-bot
sudo systemctl enable telegram-bot
```

---

## 🧠 Tech Stack

* Python (`python-telegram-bot`)
* Google Auth + Google Drive API v3
* aiohttp (OAuth callback server)
* Docker (Local Bot API)
* tmux / systemd

---

## 📂 Project Structure

```
├── bot.py              # Main bot application
├── requirements.txt    # Python dependencies
├── .env                # Environment variables (not tracked)
├── .env.example        # Environment template
├── .gitignore          # Git ignore rules
├── analytics.json      # Upload/download stats (auto-generated)
├── allowed_users.json  # Authorized user list (auto-generated)
├── admin_users.json    # Admin user list (auto-generated)
└── user_creds/         # Per-user OAuth tokens (not tracked)
```

---

## ⚠️ Notes

* Only OWNER can manage users/admins
* Local Bot API required for files over 20MB
* Each user must `/connect` before uploading
* Credentials are stored locally — keep `user_creds/` secure

---

## 💡 Future Improvements

* Web dashboard
* Multi-drive support
* AI file naming
* Advanced analytics

---

## 👤 Author

**Nithila Mandiw**

---

## ⭐ Support

If you like this project, give it a ⭐ on GitHub!
