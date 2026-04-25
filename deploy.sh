#!/bin/bash
# ============================================
# Telegram Drive Bot - VPS Deployment Script
# Run this on a fresh Ubuntu VPS
# ============================================

set -e

echo "🚀 Starting deployment..."

# 1. System updates
echo "📦 Updating system..."
sudo apt update && sudo apt upgrade -y

# 2. Install Python, pip, git, Docker
echo "🐍 Installing Python & Docker..."
sudo apt install -y python3 python3-pip python3-venv git docker.io docker-compose
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER

# 3. Clone the repo
echo "📂 Cloning repository..."
cd ~
git clone https://github.com/nithilamandiw/telegram-drive-bot.git
cd telegram-drive-bot

# 4. Create virtual environment
echo "🔧 Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 5. Create .env file (you'll need to fill this in)
echo "📝 Creating .env file..."
cat > .env << 'EOF'
TELEGRAM_TOKEN=YOUR_TELEGRAM_TOKEN
OWNER_ID=YOUR_OWNER_ID
GOOGLE_CLIENT_ID=YOUR_GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=YOUR_GOOGLE_CLIENT_SECRET
OAUTH_REDIRECT_URI=http://YOUR_VPS_IP:8080/oauth/callback
OAUTH_SERVER_PORT=8080
USE_LOCAL_API=true
EOF

echo ""
echo "⚠️  IMPORTANT: Edit .env with your actual values:"
echo "   nano ~/telegram-drive-bot/.env"
echo ""

# 6. Start Telegram Local Bot API (Docker)
echo "🐳 Starting Telegram Local Bot API..."
echo ""
echo "⚠️  You need your Telegram API ID and Hash from https://my.telegram.org"
echo "   Replace YOUR_API_ID and YOUR_API_HASH below, then run:"
echo ""
echo "docker run -d --name telegram-bot-api --restart always \\"
echo "  -p 8081:8081 \\"
echo "  -e TELEGRAM_API_ID=YOUR_API_ID \\"
echo "  -e TELEGRAM_API_HASH=YOUR_API_HASH \\"
echo "  -v telegram-data:/var/lib/telegram-bot-api \\"
echo "  aiogram/telegram-bot-api \\"
echo "  --local \\"
echo "  --http-port=8081 \\"
echo "  --dir=/var/lib/telegram-bot-api \\"
echo "  --temp-dir=/tmp/telegram-bot-api"
echo ""

# 7. Create systemd service for auto-restart
echo "⚙️  Creating systemd service..."
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
Environment=PATH=$HOME/telegram-drive-bot/.venv/bin:/usr/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Next steps:"
echo "  1. Edit .env:           nano ~/telegram-drive-bot/.env"
echo "  2. Start Docker API:    (run the docker command above with your API ID/Hash)"
echo "  3. Open firewall:       sudo ufw allow 8080"
echo "  4. Start the bot:       sudo systemctl start telegram-bot"
echo "  5. Check logs:          sudo journalctl -u telegram-bot -f"
echo ""
echo "🔗 Add this redirect URI to Google Cloud Console:"
echo "   http://YOUR_VPS_IP:8080/oauth/callback"
