#!/usr/bin/env bash
# Первичная установка M22 Product Radar на VPS (Ubuntu/Debian) в Docker с автозапуском.
# Запуск на сервере от root или через sudo:  bash scripts/vps_install.sh
# Что делает: ставит Docker, включает его автозапуск при загрузке, собирает и запускает контейнер (restart: always),
# ставит nginx с паролем перед радаром. Данные (база, копии) — в ./data на хосте.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "== Устанавливаю Docker"
  curl -fsSL https://get.docker.com | sh
fi
echo "== Автозапуск Docker при загрузке сервера"
systemctl enable --now docker

if [ ! -f .env ]; then
  cp .env.example .env
  SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))' 2>/dev/null || head -c 48 /dev/urandom | base64)
  sed -i "s|^RADAR_SECRET=.*|RADAR_SECRET=$SECRET|" .env
  echo "== Создан .env. Задайте пароль входа: RADAR_PASSWORD=... в .env (сейчас пусто — вход без пароля!)"
fi
mkdir -p data/logs data/backups data/exports data/raw

echo "== Сборка и запуск контейнера (restart: always — поднимается сам после перезагрузки и сбоя)"
docker compose up -d --build
sleep 8
docker compose ps
curl -fsS http://127.0.0.1:8022/health && echo

if command -v apt-get >/dev/null 2>&1 && [ "${SKIP_NGINX:-0}" != "1" ]; then
  echo "== nginx перед радаром (порт 80; для HTTPS добавьте certbot: apt install certbot python3-certbot-nginx && certbot --nginx)"
  apt-get install -y -q nginx
  cat > /etc/nginx/sites-available/m22-radar <<'NGINX'
server {
  listen 80 default_server;
  server_name _;
  client_max_body_size 50m;
  location / {
    proxy_pass http://127.0.0.1:8022;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300;
  }
}
NGINX
  ln -sf /etc/nginx/sites-available/m22-radar /etc/nginx/sites-enabled/m22-radar
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl enable --now nginx && systemctl reload nginx
fi
echo "== Готово. Радар: http://<ip-сервера>/  (вход по RADAR_PASSWORD из .env)."
echo "   Обновление: bash scripts/vps_update.sh   Журнал: docker compose logs -f --tail=200"
