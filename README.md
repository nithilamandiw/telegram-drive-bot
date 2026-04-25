# 🚀 Google Drive Bot

A powerful Telegram bot to upload, manage, and share files directly with Google Drive — with **per-user OAuth2 authentication**, interactive button menus, parallel uploads, role-based access, and more.

---

## ✨ Features

### 🔐 Per-User Google OAuth2

* Each user connects their **own Google account** via the menu
* Credentials stored securely per user (`user_creds/{user_id}.json`)
* Auto token refresh
* Disconnect anytime from the menu

---

### 🎛️ Interactive Button Menu

Send `/start` to get a fully interactive button menu:

```
📱 Main Menu
├── 🔗 Connect / 🔌 Disconnect
├── 📁 My Files    │  🕐 Recent
├── 🔍 Search      │  📂 New Folder
├── 💾 Storage     │  📊 Stats
├── 📈 Analytics   │  🗑️ Trash
├── ➕ Add User    │  ➖ Remove User    (admin+)
└── 👑 Add Admin   │  ❌ Remove Admin   (owner)
```

Every sub-screen has a **◀️ Back to Menu** button for easy navigation.

---

### 📤 Upload System

* Upload files directly from Telegram
* Supports **large files (up to 2GB)** using Local Bot API
* Parallel uploads (configurable)
* Progress bar with percentage, speed, ETA
* Pause / Cancel uploads

---

### ☁️ Google Drive Integration

* Upload files to user's own Google Drive
* Generate shareable links
* Clone Drive files (no re-upload needed)
* Public / Private sharing
* Expiring links (1h / 24h)

---

### 📁 File Manager

* Paginated file listing with buttons
* File size display
* Open, Delete, Rename, Make public/private
* Clean inline button navigation

---

### 📂 Folder Management

* Create new folders with `/newfolder <name>`
* Direct link to the created folder

---

### 🕐 Recent Files

* View last 10 uploaded files with links and sizes

---

### 🗑️ Trash Management

* View trashed files
* Restore individual files
* Permanently delete files
* Empty entire trash

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

* Detects duplicate files by name and size
* Options: Skip or Replace

---

### 👥 Role-Based Access System

Since each user connects their **own Google Drive**, all users have full control over their own files:

| Feature             | Owner | Admin | User |
| ------------------- | ----- | ----- | ---- |
| Upload files        | ✅     | ✅     | ✅    |
| View files (/files) | ✅     | ✅     | ✅    |
| Delete/Rename       | ✅     | ✅     | ✅    |
| Share/Links         | ✅     | ✅     | ✅    |
| Search/Recent       | ✅     | ✅     | ✅    |
| Storage/Stats       | ✅     | ✅     | ✅    |
| Create folders      | ✅     | ✅     | ✅    |
| Trash management    | ✅     | ✅     | ✅    |
| Add/remove users    | ✅     | ✅     | ❌    |
| Add/remove admins   | ✅     | ❌     | ❌    |

---

### 📊 Stats & Analytics

* Storage usage with visual progress bar
* File count by type
* Upload/download history
* Total data transferred

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
5. Add test users under **OAuth consent screen** (if app is in Testing mode)

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
| `OAUTH_REDIRECT_URI` | Callback URL (e.g. `https://your-domain.com/oauth/callback`) |
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
docker run -d --name telegram-bot-api --restart always \
  -p 8081:8081 \
  -e TELEGRAM_API_ID=YOUR_API_ID \
  -e TELEGRAM_API_HASH=YOUR_API_HASH \
  -e TELEGRAM_LOCAL=true \
  -v /home/azureuser/telegram-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest \
  --local \
  --http-port=8081 \
  --dir=/var/lib/telegram-bot-api \
  --temp-dir=/tmp/telegram-bot-api
```

Get your API ID and Hash from [my.telegram.org](https://my.telegram.org).

Then set `USE_LOCAL_API=true` in your `.env`.

---

## 🌐 VPS Deployment (Production)

### 1. Install dependencies

```bash
sudo apt update && sudo apt install -y python3 python3-venv git docker.io
```

### 2. Clone and setup

```bash
git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd telegram-drive-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Setup Caddy (HTTPS reverse proxy)

Google OAuth requires HTTPS redirect URIs. Caddy auto-provisions SSL certificates:

```bash
# Install Caddy
sudo apt install -y caddy

# Configure
echo 'your-domain.com {
    reverse_proxy localhost:8080
}' | sudo tee /etc/caddy/Caddyfile

sudo ufw allow 80
sudo ufw allow 443
sudo systemctl restart caddy
```

### 4. Setup systemd service

```bash
sudo tee /etc/systemd/system/telegram-bot.service > /dev/null << EOF
[Unit]
Description=Telegram Drive Upload Bot
After=network.target docker.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/telegram-drive-bot
ExecStart=$HOME/telegram-drive-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

### 5. Monitor logs

```bash
sudo journalctl -u telegram-bot -f
```

---

## 📌 Commands

| Command | Description |
| --- | --- |
| `/start` | Interactive button menu |
| `/connect` | Connect your Google Drive account |
| `/disconnect` | Remove stored Google credentials |
| `/commands` | Show available commands (text list) |
| `/files` | Browse your Drive files |
| `/newfolder <name>` | Create a new folder |
| `/recent` | View last 10 uploaded files |
| `/search <query>` | Search files by name |
| `/get <name/id/link>` | Get a file from Drive |
| `/storage` | View Drive storage usage |
| `/stats` | View Drive stats |
| `/analytics` | View upload/download analytics |
| `/trash` | View and manage trashed files |
| `/adduser <id>` | Add authorized user (admin+) |
| `/removeuser <id>` | Remove user (admin+) |
| `/addadmin <id>` | Add admin (owner only) |
| `/removeadmin <id>` | Remove admin (owner only) |

---

## 🧠 Tech Stack

* Python (`python-telegram-bot`)
* Google Auth + Google Drive API v3
* aiohttp (OAuth callback server)
* Docker (Local Bot API for large files)
* Caddy (HTTPS reverse proxy)
* systemd (process management)

---

## 📂 Project Structure

```
├── bot.py              # Main bot application
├── deploy.sh           # VPS deployment script
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

* Admins can manage users; only OWNER can manage admins
* Local Bot API (Docker) required for files over 20MB
* Each user must connect their Google account before uploading
* Credentials are stored locally — keep `user_creds/` secure
* Google OAuth requires HTTPS redirect URI in production (use Caddy)

---

## 👤 Author

**Nithila Mandiw**

---

## ⭐ Support

If you like this project, give it a ⭐ on GitHub!
