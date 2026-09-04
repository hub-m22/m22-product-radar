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
