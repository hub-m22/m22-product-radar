# Развёртывание M22 Product Radar

## Локально (Windows)
1. Установить Python 3.11+ (winget install Python.Python.3.12).
2. `python -m pip install -r requirements.txt`
3. `copy .env.example .env` — при необходимости изменить порт, пороги, расписание.
4. `python -m radar init` → `python -m radar collect all` → `python -m radar analyze` → `python -m radar report`.
5. `scripts\run.bat` — веб-интерфейс на http://127.0.0.1:8022 с планировщиком внутри процесса.
6. Автозапуск: Планировщик заданий Windows → «При входе в систему» → `scripts\run.bat`. Если сервер не должен работать постоянно — задача «Ежедневно 06:00» → `scripts\collect.bat`.

## Сервер (Linux, systemd)
1. `git clone`/копия в `/opt/m22-product-radar`, `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. `.env`: `RADAR_HOST=0.0.0.0`, `RADAR_PORT=8022`, `RADAR_PDF_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (`apt install fonts-dejavu-core`).
3. `sudo cp scripts/m22-radar.service /etc/systemd/system/ && sudo systemctl enable --now m22-radar`.
4. Доступ снаружи — только через обратный прокси с авторизацией (nginx `auth_basic`); встроенной аутентификации нет.

## Резервные копии
- `python -m radar backup` → `data/backups/radar_<дата>.sqlite3`; автоматически — при еженедельном отчёте.
- Кнопка «Скачать резервную копию базы» в разделе «Источники».
- Экспорт таблиц CSV/XLSX — раздел соответствующего экрана или `/export/<таблица>.<csv|xlsx>`.

## Подключение доступов
| Доступ | Где взять | Куда записать |
|---|---|---|
| Яндекс Wordstat API | Yandex Cloud → сервисный аккаунт с ролью `search-api.webSearch.user` → API-ключ (платно, ~20 ₽/запрос) | `.env`: `YANDEX_WORDSTAT_TOKEN=` (сборщик — следующий этап; сейчас — импорт CSV/XLSX) |
| Wordstat вручную | wordstat.yandex.ru (вход Яндекс ID) → «Скачать» CSV/XLSX | раздел «Поисковый спрос» → Импорт |
| Ozon / Яндекс Маркет | Seller API (Client-Id/Api-Key) / Partner API — только по своим товарам | ручной импорт цен конкурентов (CSV) |
| Yandex Search API (выдача) | Yandex Cloud, платно | следующий этап |
| Таможенные данные | ВЭД-Стат / ImportGenius / Volza — подписка | импорт CSV |
| AI (Anthropic) | ключ API | `.env`: `ANTHROPIC_API_KEY=` — в текущей версии не используется |

## Docker на VPS (основной вариант) — автозапуск

Один раз на сервере (Ubuntu/Debian, от root):
```bash
git clone <репозиторий> /opt/m22-product-radar && cd /opt/m22-product-radar
bash scripts/vps_install.sh        # Docker + автозапуск Docker + контейнер (restart: always) + nginx
nano .env                          # RADAR_PASSWORD=<пароль входа>, RADAR_SECRET уже сгенерирован
docker compose up -d               # применить .env
```

Как устроен автозапуск, три уровня:
1. **Docker при загрузке сервера** — `systemctl enable docker` (делает скрипт установки).
2. **Контейнер** — `restart: always` в `docker-compose.yml`: поднимается при старте Docker, после перезагрузки VPS и после любого падения; `healthcheck` по `/health` показывает состояние в `docker compose ps`.
3. **Скрипты сбора и анализа** — планировщик внутри процесса радара (`radar/scheduler.py`): сайты M22 ежедневно 05:00, конкуренты 06:00, спрос и отчёт с резервной копией по понедельникам, анализ после каждого сбора; время московское (`TZ=Europe/Moscow` в контейнере). Если сервер был выключен и последний сбор старше 26 часов, через 3 минуты после старта идёт догоняющий полный сбор. Отдельный cron на хосте не нужен. Расписание меняется в `.env` (`RADAR_*_CRON_*`), запустить вручную — раздел «Управление» или `docker compose exec radar python -m radar full-update`.

Перенос данных с рабочей машины: скопировать `data/radar.sqlite3` (и при желании `data/backups`) в `/opt/m22-product-radar/data/` **до** первого запуска. Папка `data/` монтируется с хоста и переживает пересборку контейнера.

Повседневное:
```bash
bash scripts/vps_update.sh                 # обновить до новой версии (git pull + пересборка), данные не трогаются
docker compose logs -f --tail=200          # журнал
docker compose ps                          # состояние (healthy / restarting)
docker compose restart                     # перезапуск вручную; кнопка «Перезапустить радар» в разделе «Управление» делает то же
docker compose exec radar python -m radar backup   # резервная копия в data/backups
```

HTTPS: `apt install certbot python3-certbot-nginx && certbot --nginx -d radar.m22.ru`. Порт 8022 наружу не открывать: в compose он привязан к `127.0.0.1` сервера, снаружи только nginx.

Пример nginx с паролем (порт 8022 наружу не открывать):
```nginx
server {
  listen 443 ssl; server_name radar.m22.ru;
  auth_basic "M22 Product Radar"; auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd -c /etc/nginx/.htpasswd owner
  location / { proxy_pass http://127.0.0.1:8022; proxy_set_header Host $host; }
}
```
