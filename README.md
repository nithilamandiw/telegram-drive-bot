# 🚀 Google Drive Bot

A powerful Telegram bot to upload, manage, and share files directly with Google Drive — with advanced features like parallel uploads, file management UI, role-based access, and more.

---

## ✨ Features

### 📤 Upload System

* Upload files directly from Telegram
* Supports **large files (>20MB)** using Local Bot API
* Parallel uploads (configurable)
* Progress bar with:

  * Percentage
  * Speed
  * ETA
* Cancel upload/download

---

### ☁️ Google Drive Integration

* Upload files to a specific folder
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

* Upload files from direct links

```
/url https://example.com/file.zip
```

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

### 📊 Stats Dashboard

```
/stats
```

* Total storage used
* Free space
* File count

---

### 📋 Commands List

```
/commands
```

* Dynamically shows commands based on role

---

### 🗑 Trash System

* Safe delete (move to trash)
* Restore support

---

### 🏷 Auto Tagging

* .mp4 → Videos
* .zip → Archives
* .psd → Design

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

---

### 2. Create virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

---

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

### 4. Configure environment

Create `.env` file:

```env
TELEGRAM_TOKEN=your_bot_token
DRIVE_FOLDER_ID=your_drive_folder_id
OWNER_ID=your_telegram_id
```

---

### 5. Google Drive Setup

* Enable Google Drive API
* Download credentials
* Configure `settings.yaml`
* First run will authenticate

---

## 🐳 Local Telegram API (for large files)

Run:

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

---

## ▶️ Run Bot

```bash
python bot.py
```

---

## 🔁 Run 24/7 (Recommended)

### Using tmux

```bash
tmux
python bot.py
```

Detach:

```
CTRL + B → D
```

---

### Using systemd (Production)

```bash
sudo systemctl start telegram-bot
sudo systemctl enable telegram-bot
```

---

## 🚀 Auto Deploy (CI/CD)

* Push to GitHub
* Auto deploy to VPS
* Auto restart bot

---

## 📌 Commands

| Command      | Description             |
| ------------ | ----------------------- |
| /commands    | Show available commands |
| /files       | View files              |
| /stats       | Storage info            |
| /search      | Search files            |
| /url         | Upload from URL         |
| /get         | Download from Drive     |
| /adduser     | Add user                |
| /removeuser  | Remove user             |
| /addadmin    | Add admin               |
| /removeadmin | Remove admin            |

---

## 🧠 Tech Stack

* Python (python-telegram-bot)
* Google Drive API
* Docker (Local Bot API)
* tmux / systemd
* GitHub Actions (CI/CD)

---

## ⚠️ Notes

* Only OWNER can manage users/admins
* Local Bot API required for large files
* Ensure proper permissions in Google Drive

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
