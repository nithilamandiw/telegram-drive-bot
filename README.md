# Google Drive Telegram Bot

A Telegram bot that uploads, manages, and shares files from each user's own Google Drive using per-user OAuth2.

## Features

- Per-user Google OAuth2 (`/connect`, `/disconnect`)
- Interactive menu and command-based workflow
- Upload files from Telegram to Google Drive
- Search, list, recent files, folder creation, trash management
- Drive-to-Telegram download (`/get`)
- Role-based access (owner/admin/user)
- Optional local Telegram Bot API for larger file workflows
- Optional background jobs (job queue)

## Commands

| Command | Description |
| --- | --- |
| `/start` | Open main menu |
| `/commands` | Show command list |
| `/connect` | Connect your Google account |
| `/disconnect` | Remove saved Google credentials |
| `/files` | Browse Drive files |
| `/newfolder <name>` | Create folder |
| `/recent` | Show recent uploads |
| `/search <query>` | Search Drive files |
| `/get <name/id/link>` | Download Drive file to Telegram |
| `/storage` | Storage usage |
| `/stats` | Drive stats |
| `/analytics` | Transfer analytics |
| `/trash` | Manage trash |
| `/adduser <id>` | Add authorized user (admin+) |
| `/removeuser <id>` | Remove authorized user (admin+) |
| `/addadmin <id>` | Add admin (owner only) |
| `/removeadmin <id>` | Remove admin (owner only) |

## Local Setup

### 1. Clone and install

```bash
git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd telegram-drive-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Google Cloud OAuth setup

1. Open Google Cloud Console -> APIs & Services -> Credentials.
2. Create **OAuth 2.0 Client ID** with type **Web application**.
3. Add authorized redirect URIs:
   - Local: `http://localhost:8080/oauth/callback`
   - Production: `https://your-domain.com/oauth/callback`
4. Enable **Google Drive API**.
5. If app is in Testing mode, add test users in OAuth consent screen.

### 3. Configure `.env`

```bash
cp .env.example .env
```

Required values:

- `TELEGRAM_TOKEN`
- `OWNER_ID`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `OAUTH_REDIRECT_URI`
- `OAUTH_SERVER_PORT` (default `8080`, production commonly `8081` behind reverse proxy)

Optional:

- `MAX_PARALLEL_UPLOADS`
- `USE_LOCAL_API=true` (if using local Telegram Bot API)

### 4. Run

```bash
python bot.py
```

## VPS Deployment (Production, Lightsail-tested)

This is the fully working method validated with:

- Domain: `your-domain.com`
- Reverse proxy: Caddy
- Local Telegram Bot API port: `8081` (localhost only)
- OAuth callback app port: `8082`
- Public HTTPS endpoint: `https://your-domain.com/oauth/callback`

### 1. Prepare server

```bash
sudo apt update
sudo apt install -y python3 python3-venv git caddy
```

```bash
git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd telegram-drive-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install "python-telegram-bot[job-queue]"
```

### 2. Use a static public IP

In Lightsail:

1. Create and attach a **Static IP** to the instance.
2. Note that static IP value.

### 3. DNS records (A records)

At your DNS provider, point domain to the static IP:

- `A  your-domain.com      -> <STATIC_IP>`
- `A  www.your-domain.com  -> <STATIC_IP>` (optional)

TTL `300` is fine.

Verify:

```bash
dig +short your-domain.com
curl -4 ifconfig.me
```

Both should match your static public IP.

### 4. Lightsail firewall rules

In Lightsail -> Networking -> IPv4 firewall:

- SSH TCP `22` from your IP (recommended)
- HTTP TCP `80` from `Any IPv4 address`
- HTTPS TCP `443` from `Any IPv4 address`
- Do not expose `8081`/`8082` publicly when using Caddy

### 5. UFW on server

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status numbered
```

### 6. Configure app environment

Set `.env` with exact production values:

```env
USE_LOCAL_API=true
OAUTH_SERVER_PORT=8082
OAUTH_REDIRECT_URI=https://your-domain.com/oauth/callback
```

Important: `OAUTH_REDIRECT_URI` must be exact and match Google OAuth settings character-for-character.

### 7. Google OAuth redirect URI

In Google Cloud OAuth client, add:

- `https://your-domain.com/oauth/callback`

Common mismatch causes:

- `http` vs `https`
- with/without `www`
- with/without port
- trailing slash mismatch

### 8. Configure Caddy reverse proxy

Create `/etc/caddy/Caddyfile`:

```caddy
your-domain.com, www.your-domain.com {
    reverse_proxy 127.0.0.1:8082
}
```

Apply:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```

Why `127.0.0.1`?

- Caddy and bot run on same VPS.
- Public traffic hits Caddy on `80/443`, then Caddy proxies internally to local OAuth app port `8082`.

### 9. Start local Telegram Bot API container

Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from Telegram:

1. Open `https://my.telegram.org`
2. Sign in with your Telegram phone number
3. Open **API development tools**
4. Create an app (title/short name can be anything)
5. Copy:
   - `api_id` -> `TELEGRAM_API_ID`
   - `api_hash` -> `TELEGRAM_API_HASH`

Keep these values private.

```bash
sudo docker rm -f telegram-bot-api 2>/dev/null || true
sudo mkdir -p /home/ubuntu/telegram-api-data
sudo docker run -d --name telegram-bot-api --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=YOUR_REAL_API_ID \
  -e TELEGRAM_API_HASH=YOUR_REAL_API_HASH \
  -e TELEGRAM_LOCAL=true \
  -v /home/ubuntu/telegram-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest
```

The bot code expects this host path too, so keep a symlink:

```bash
sudo mkdir -p /home/azureuser
sudo rm -rf /home/azureuser/telegram-api-data
sudo ln -s /home/ubuntu/telegram-api-data /home/azureuser/telegram-api-data
ls -ld /home/azureuser/telegram-api-data
```

### 10. Run bot with tmux (no systemd)

```bash
tmux new -s telegrambot
```

Inside tmux:

```bash
cd ~/telegram-drive-bot
source .venv/bin/activate
export USE_LOCAL_API=true
export OAUTH_SERVER_PORT=8082
python bot.py
```

Detach and keep running:

- `Ctrl+b`, then `d`

Manage session:

```bash
tmux ls
tmux attach -t telegrambot
tmux kill-session -t telegrambot
```

### 11. Verify end-to-end

1. App listening:

```bash
ss -ltnp | egrep '8081|8082'
```

2. Local Telegram Bot API check:

```bash
curl -I http://127.0.0.1:8081
```

`404 Not Found` on `/` is expected.

3. Public HTTPS callback check:

```bash
curl -vI https://your-domain.com/oauth/callback
```

`400` is also expected and confirms DNS + TLS + Caddy + app routing are correct.

4. Bot logs (tmux):

```bash
tmux attach -t telegrambot
```

You should see lines like:

- `Using local Telegram Bot API server`
- `OAuth callback server started on port 8082`
- `Bot and OAuth server are running`

### 12. Connect account

In Telegram, run `/connect` and complete Google login.

## Troubleshooting

### `Error 400: redirect_uri_mismatch`

Fix by matching these exactly:

- `.env` `OAUTH_REDIRECT_URI`
- Google OAuth authorized redirect URI

Use production HTTPS URI, not localhost.

### Browser timeout (`ERR_CONNECTION_TIMED_OUT`)

Usually caused by networking layer:

- DNS not pointing to current static IP
- Lightsail firewall not allowing 443/80
- Caddy not running/configured

### `No JobQueue set up`

Install extra dependency:

```bash
pip install "python-telegram-bot[job-queue]"
```

### `Could not fix volume permissions ... /home/azureuser/telegram-api-data/`

Usually means host volume path mismatch. Ensure:

- `/home/ubuntu/telegram-api-data` exists
- `/home/azureuser/telegram-api-data` symlinks to it

## Optional: Local Telegram Bot API (advanced)

Use this only if you need local Telegram Bot API behavior and have configured it intentionally.

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

## Security Notes

- Treat `.env` as secret.
- Never share bot token or OAuth client secret publicly.
- If leaked, rotate immediately:
  - Telegram token in BotFather
  - Google OAuth client secret in Google Cloud
- Keep `user_creds/` protected and excluded from git.

## Project Structure

```text
bot.py
requirements.txt
README.md
.env.example
user_creds/
analytics.json
users.json
admins.json
```

## Author

Nithila Mandiw
