#!/usr/bin/env bash
# Полный деплой content-bot на чистый Ubuntu VDS (Selectel), запускать от root.
#
# Секреты НЕ хранятся в этом файле — передаются переменными окружения при запуске:
#   BOT_TOKEN=... OWNER_TELEGRAM_ID=... ANTHROPIC_API_KEY=... bash selectel_setup.sh
#
# Обязательные переменные: BOT_TOKEN, OWNER_TELEGRAM_ID, ANTHROPIC_API_KEY
# Необязательные (есть разумные дефолты): SEED_ADMIN_IDS, SERVER_IP, DOMAIN,
#   ANTHROPIC_BASE_URL, AI_MODEL, REPO_URL, APP_DIR
set -euo pipefail

: "${BOT_TOKEN:?Задайте BOT_TOKEN=...}"
: "${OWNER_TELEGRAM_ID:?Задайте OWNER_TELEGRAM_ID=...}"
: "${ANTHROPIC_API_KEY:?Задайте ANTHROPIC_API_KEY=...}"

REPO_URL="${REPO_URL:-https://github.com/ErnestC77/content-bot.git}"
APP_DIR="${APP_DIR:-/opt/content-bot}"
SERVER_IP="${SERVER_IP:-$(curl -s https://api.ipify.org)}"
DOMAIN="${DOMAIN:-$(echo "$SERVER_IP" | tr '.' '-').sslip.io}"
SEED_ADMIN_IDS="${SEED_ADMIN_IDS:-}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 24)}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 24)}"
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.aitunnel.ru}"
AI_MODEL="${AI_MODEL:-claude-sonnet-5}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"

echo "==> Обновление системы и установка зависимостей"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg git ufw nginx certbot python3-certbot-nginx

echo "==> Установка Docker (если ещё не установлен)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "==> Проверка доступности Telegram API (Selectel иногда блокирует часть IP Telegram)"
if ! curl -s --max-time 5 https://api.telegram.org >/dev/null; then
  echo "api.telegram.org недоступен напрямую — прибиваю рабочий IP в /etc/hosts"
  for ip in 149.154.167.220 149.154.175.55 149.154.171.5; do
    if curl -s --max-time 5 --resolve "api.telegram.org:443:${ip}" https://api.telegram.org >/dev/null; then
      grep -q "api.telegram.org" /etc/hosts && sed -i '/api.telegram.org/d' /etc/hosts
      echo "${ip} api.telegram.org" >> /etc/hosts
      echo "Использую IP ${ip} для api.telegram.org"
      break
    fi
  done
fi

echo "==> Клонирование репозитория"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> Запись .env (право доступа только root)"
umask 077
cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
OWNER_TELEGRAM_ID=${OWNER_TELEGRAM_ID}
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
DATABASE_URL=postgresql+asyncpg://content_bot:${DB_PASSWORD}@db:5432/content_bot
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}
AI_PROVIDER=anthropic
AI_MODEL=${AI_MODEL}
DEFAULT_CHANNEL_ID=
APP_HOST=0.0.0.0
APP_PORT=8000
TIMEZONE=Europe/Moscow
DAILY_CHECK_TIME=10:00
BOT_MODE=webhook
WEBHOOK_URL=https://${DOMAIN}
WEBHOOK_SECRET=${WEBHOOK_SECRET}
SEED_ADMIN_IDS=${SEED_ADMIN_IDS}
EOF

echo "==> Пароль БД через override-файл (не трогая сам репозиторий)"
cat > docker-compose.override.yml <<EOF
services:
  db:
    environment:
      POSTGRES_USER: content_bot
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: content_bot
EOF

echo "==> Открываю порты в файрволе (SSH + HTTP/HTTPS)"
ufw allow OpenSSH || true
ufw allow 80/tcp || true
ufw allow 443/tcp || true
yes | ufw enable || true

echo "==> Сборка и запуск (app + postgres)"
docker compose up -d --build

echo "==> Настройка nginx (reverse proxy на 127.0.0.1:8000)"
cat > /etc/nginx/sites-available/content-bot <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sf /etc/nginx/sites-available/content-bot /etc/nginx/sites-enabled/content-bot
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==> Выпуск TLS-сертификата (Let's Encrypt)"
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "admin@${DOMAIN}" --redirect || \
  echo "certbot не смог выпустить сертификат автоматически — проверьте вручную: certbot --nginx -d ${DOMAIN}"

echo ""
echo "===================== ГОТОВО ====================="
echo "Домен:          https://${DOMAIN}"
echo "Панель:         https://${DOMAIN}/webapp"
echo "ADMIN_USERNAME: ${ADMIN_USERNAME}"
echo "ADMIN_PASSWORD: ${ADMIN_PASSWORD}"
echo "===================================================="
echo "Проверка health: curl -s https://${DOMAIN}/health"
echo "Логи бота:       docker compose -f ${APP_DIR}/docker-compose.yml logs -f app"
