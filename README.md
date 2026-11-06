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
sudo caddy reload --config /etc/caddy/Caddyfile
```

If Caddy is not already running, start it manually in tmux:

```bash
tmux new -s caddy
sudo caddy run --config /etc/caddy/Caddyfile
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

Update bot from GitHub:

```bash
cd ~/telegram-drive-bot
git pull origin main
tmux new -s telegrambot
source .venv/bin/activate
python bot.py
```

If the tmux session already exists:

```bash
tmux attach -t telegrambot
```

Then stop the running bot with `Ctrl+c`, pull the latest code, start it again, and detach with `Ctrl+b`, then `d`.

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


## New EC2 Method

Use this method to run the bot on an AWS EC2 Ubuntu server. Python runs directly on the server. Docker runs the local Telegram Bot API. Caddy provides HTTPS for Google login.

This is a separate guide from the Lightsail method above. Follow this section from start to finish for a new EC2 server.

### 1. Create the EC2 server

Open AWS Console -> EC2 -> Launch instances. Choose:

| Setting | Value |
| --- | --- |
| Name | `telegram-drive-bot` |
| Operating system | Ubuntu Server 24.04 LTS, standard edition |
| Architecture | 64-bit (x86) |
| Instance type | `t8i.small`, where available |
| Purchase option | On-Demand |
| Number of instances | 1 |
| Network | Default VPC and a public subnet |
| Auto-assign public IP | Enable |
| Storage | 60 GiB, gp3 |
| IOPS | 3000 |
| Throughput | 125 MiB/s |
| Encryption | Enabled, using the default `aws/ebs` key |
| Volume initialization rate | Leave blank |
| Extra file systems | None |

`t3.small` is also a suitable starting option if `t8i.small` is unavailable. Both have 2 GiB RAM. Check the price shown for your region before launching. EC2 compute, disk, public IPv4, and outbound transfers are billed separately. Credits have an expiry date; “Free tier eligible” does not mean unlimited free use.

Create a key pair named `telegram-drive-bot`. Download the `.pem` file and keep it private. These instructions assume it is in your Mac's Downloads folder.

Create a security group with these inbound rules:

| Type | Port | Source |
| --- | --- | --- |
| SSH | 22 | My IP, recommended |
| HTTP | 80 | Anywhere IPv4, `0.0.0.0/0` |
| HTTPS | 443 | Anywhere IPv4, `0.0.0.0/0` |

If your home IP changes, update the SSH rule to your new IP. If you temporarily use Anywhere for SSH, change it later in EC2 -> Security Groups -> Inbound rules. This does not require a server restart.

Do not add public rules for ports `8081` or `8082`.

Launch the instance. Wait until it is Running and its status checks pass.

### 2. Attach an Elastic IP

An Elastic IP is a fixed public address. A normal automatically assigned public IP can change after stopping and starting the instance.

1. In the EC2 sidebar, open Network & Security -> Elastic IPs.
2. Click Allocate Elastic IP address. Keep the defaults and click Allocate.
3. Select the new address and click Associate Elastic IP address.
4. Choose resource type Instance.
5. Select `telegram-drive-bot`.
6. Select its primary private IP if asked.
7. Leave reassociation unchecked and click Associate.

Use this Elastic IP for SSH and DNS from now on. In the commands below, replace `YOUR_ELASTIC_IP` with it. Public IPv4 addresses have a separate charge, including allocated Elastic IPs that are not attached to an instance.

### 3. Connect from your Mac

Run these commands in Terminal on your Mac:

```bash
chmod 400 ~/Downloads/telegram-drive-bot.pem
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i ~/Downloads/telegram-drive-bot.pem ubuntu@YOUR_ELASTIC_IP
```

For a first connection, check the server's host fingerprint where available, then accept the SSH prompt. After login, your terminal prompt starts with `ubuntu@`.

The keepalive options help prevent idle connection timeouts. If you see `Broken pipe` or `Operation timed out`, reconnect using the same command. Your installed packages and saved files remain on the server.

Run all remaining shell commands on the EC2 server unless a step says otherwise.

### 4. Install software and download the project

```bash
sudo apt update
sudo apt install -y python3-venv git docker.io caddy
sudo systemctl enable --now docker caddy

git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd ~/telegram-drive-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install "python-telegram-bot[job-queue]==20.7"
```

If the project folder already exists, enter that folder instead of cloning again. The job queue package enables scheduled background tasks while keeping the project's Telegram library version.

### 5. Point your domain to EC2

Use a domain or subdomain you control. This guide uses `bot.example.com` as a placeholder. Replace it everywhere with your real domain.

If you forgot your old domain, open Google Cloud Console -> Google Auth Platform -> Clients. Open the Web application client matching `GOOGLE_CLIENT_ID` in your old `.env`. Check Authorized redirect URIs. An entry such as `https://bot.example.com/oauth/callback` shows the domain you previously used.

At your DNS provider, create or update this record:

| Type | Name | Value |
| --- | --- | --- |
| A | `bot` | Your Elastic IP |

Use TTL `300` if available. If you use Cloudflare, choose DNS only while setting up. Update or remove any old `AAAA` record for this hostname if it points to another server.

Check DNS from EC2:

```bash
getent ahostsv4 bot.example.com
```

The result should show your Elastic IP. DNS changes may take time to appear.

### 6. Check Google Cloud settings

In the Google Cloud project used by this bot:

1. Enable the Google Drive API.
2. Open Google Auth Platform -> Clients.
3. Create or open an OAuth client with application type Web application.
4. Add this Authorized redirect URI, using your real domain:

```text
https://bot.example.com/oauth/callback
```

5. Save the settings.
6. If the OAuth app is in Testing mode, add the Google accounts that will use the bot as test users under Audience.

The redirect URI must match the server's `.env` exactly, including `https`, the hostname, and `/oauth/callback`.

### 7. Create the server's .env file

Your Mac's `.env` is not automatically copied to EC2. Create a separate file on the server:

```bash
cd ~/telegram-drive-bot
nano .env
```

Enter your own values:

```dotenv
TELEGRAM_TOKEN=YOUR_BOTFATHER_TOKEN
OWNER_ID=YOUR_TELEGRAM_USER_ID
GOOGLE_CLIENT_ID=YOUR_GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=YOUR_GOOGLE_CLIENT_SECRET
OAUTH_REDIRECT_URI=https://bot.example.com/oauth/callback
OAUTH_SERVER_PORT=8082
USE_LOCAL_API=true
MAX_PARALLEL_UPLOADS=5
```

In nano, press Ctrl+O, then Enter to save. Press Ctrl+X to exit.

```bash
chmod 600 .env
```

`MAX_PARALLEL_UPLOADS=5` allows up to five simultaneous Drive uploads. Watch memory and free disk space when several large transfers run together. Environment changes take effect after restarting the Python bot.

Normal `ls` hides `.env`. Use `ls -a` to check that it exists. Do not share its contents. Replace any credentials that have been exposed before using them here.

### 8. Configure Caddy for HTTPS

```bash
sudo nano /etc/caddy/Caddyfile
```

Replace the contents with your real domain and this proxy setting:

```caddy
bot.example.com {
    reverse_proxy 127.0.0.1:8082
}
```

Save and exit, then run:

```bash
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

Caddy obtains an HTTPS certificate automatically when DNS and ports 80/443 are correct. A `502 Bad Gateway` response can occur until the Python bot starts.

### 9. Create Telegram API credentials

Open `https://my.telegram.org` in your browser and sign in. Open API development tools. Create an application if you do not already have one, then get its `api_id` and `api_hash`.

These are different from your BotFather bot token. Keep all three private.

On EC2, create a separate Docker environment file:

```bash
nano ~/telegram-api.env
```

Enter:

```dotenv
TELEGRAM_API_ID=YOUR_API_ID
TELEGRAM_API_HASH=YOUR_API_HASH
TELEGRAM_LOCAL=1
```

Save and exit, then run:

```bash
chmod 600 ~/telegram-api.env
```

### 10. Start the Docker Telegram API server

This method mounts the Docker data directly at `/home/azureuser/telegram-api-data`, which is the host path currently expected by `bot.py`. It does not need the symlink used in the Lightsail guide.

```bash
sudo mkdir -p /home/azureuser/telegram-api-data
sudo systemctl enable --now docker

sudo docker run -d \
  --name telegram-bot-api \
  --restart unless-stopped \
  --env-file /home/ubuntu/telegram-api.env \
  -p 127.0.0.1:8081:8081 \
  -v /home/azureuser/telegram-api-data:/var/lib/telegram-bot-api \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  aiogram/telegram-bot-api:latest
```

The first run downloads the image. Check the result:

```bash
sudo docker ps
sudo docker logs --tail 30 telegram-bot-api
curl -i http://127.0.0.1:8081
```

The container should show Up, with `127.0.0.1:8081->8081/tcp`. Logs should include `--local`. A `404` response on `/` is normal.

Run `docker run` only once. If the container already exists, inspect its logs and start it with `sudo docker start telegram-bot-api` if needed. Do not delete its data directory to solve a container-name conflict.

If moving from a working old server, stop the old bot first. Follow Telegram's migration requirements: use `logOut` on the cloud Bot API before switching from cloud to local, or `close` on the old local API when moving between local servers. See the [Telegram Bot API documentation](https://core.telegram.org/bots/api#logout).

### 11. Test the Python bot

```bash
cd ~/telegram-drive-bot
source .venv/bin/activate
python bot.py
```

Leave this terminal open. Look for:

```text
Using local Telegram Bot API server
OAuth callback server started on port 8082
Bot and OAuth server are running
```

In Telegram:

1. Send `/start` to your bot.
2. Send `/connect` and complete Google login.
3. Send a small file and check that it appears in your Drive.
4. Test a larger file through the local Telegram API.

From another SSH terminal, check HTTPS:

```bash
curl -i https://bot.example.com/oauth/callback
```

A `400` response with `Missing code or state parameter` is expected without a Google login. It confirms the request reaches the callback handler.

After testing, press Ctrl+C in the terminal running `python bot.py`. Wait for it to stop before starting the service below. Do not run two copies of the bot at the same time.

### 12. Keep the bot running with systemd

Use systemd to start the bot after reboot and restart it after a crash. This is an alternative to the Lightsail guide's tmux method. Do not also start the bot in tmux.

```bash
sudo nano /etc/systemd/system/telegram-bot.service
```

Paste:

```ini
[Unit]
Description=Telegram Drive Bot
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram-drive-bot
ExecStart=/home/ubuntu/telegram-drive-bot/.venv/bin/python -u bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and exit, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot
sudo systemctl status telegram-bot --no-pager
sudo journalctl -u telegram-bot -n 50 --no-pager
```

The service should show active (running). It loads `.env` from the project folder through the application's existing dotenv setup. You can now disconnect SSH.

Useful commands:

```bash
# Follow live bot logs. Ctrl+C stops viewing logs, not the bot.
sudo journalctl -u telegram-bot -f

# Restart after changing .env.
sudo systemctl restart telegram-bot

# Stop the bot.
sudo systemctl stop telegram-bot

# Start it again.
sudo systemctl start telegram-bot
```

### 13. Update the project later

```bash
sudo systemctl stop telegram-bot
cd ~/telegram-drive-bot
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install "python-telegram-bot[job-queue]==20.7"
sudo systemctl start telegram-bot
sudo journalctl -u telegram-bot -n 50 --no-pager
```

If the pull or dependency installation fails, resolve that error before continuing with the remaining update steps.

### 14. Backups and common problems

Keep private backups of `.env`, `user_creds/`, `users.json`, `admins.json`, and `analytics.json`. These files are not all recovered by cloning the repository. Without saved Google credentials, users must connect again with `/connect`. Files already uploaded to Google Drive remain there.

Check disk usage regularly. Both temporary transfers and the Telegram API data use disk space:

```bash
df -h
sudo docker system df
```

Set an AWS monthly budget alert and check credit expiry in Billing. Budget alerts notify you; they do not automatically stop spending.

| Problem | What to check |
| --- | --- |
| SSH times out | Instance status, Elastic IP, port 22 rule, and your current IP if using My IP |
| Google `redirect_uri_mismatch` | Use the exact same HTTPS callback URL in Google Cloud and server `.env` |
| Website times out | DNS records, security group ports 80/443, and Caddy status |
| `502 Bad Gateway` | Python service status, logs, and `OAUTH_SERVER_PORT=8082` |
| Container keeps restarting | `sudo docker logs --tail 50 telegram-bot-api`; check API ID and hash privately |
| Port already in use or conflicting polling | Stop the extra manual, tmux, old-server, or systemd copy of the bot |
| Missing Google account connection | Run `/connect` in Telegram |
| Transfers fail | Bot logs, Docker logs, free disk space, and Docker volume path |

For Caddy errors, use `sudo journalctl -u caddy -n 50 --no-pager`. For bot errors, use `sudo journalctl -u telegram-bot -n 50 --no-pager`. Hide tokens and secrets before sharing logs.


## Author

Nithila Mandiw