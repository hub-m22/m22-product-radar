"""Начальные справочники: категории контура, реестр источников (описания), настройки."""
from __future__ import annotations

import sqlite3

from . import db
from .normalize import CATEGORY_NAMES

CATEGORY_ORDER = [
    "radiogid", "audiogid", "sync_translation", "disposable_headphones", "reusable_headphones", "microphones_guide",
    "charging_cases", "kits_solutions", "rental", "industrial_tours", "museum_equipment", "conference_delegations",
    "staff_call", "intercom_events", "voice_amplifier", "radio_walkie", "substitutes_apps", "adjacent_new",
]

SOURCE_DESCRIPTIONS: list[dict] = [
    dict(key="competitors", name="Сайты конкурентов (страницы мониторинга)", kind="competitor", url=None, mvp_suitability="ready",
         data_available="Названия, цены, наличие, описания товаров на страницах, добавленных в мониторинг", official_api="нет",
         needs_parsing=1, needs_auth=0, cost="бесплатно", limits="Соблюдение robots.txt, задержка ≥2 с между запросами к одному домену",
         update_frequency="ежедневно", stability="зависит от сайта; JS-магазины и маркетплейсы недоступны", risks="Изменение вёрстки; блокировки при частых запросах",
         how_to_connect="Добавьте страницу в разделе «Конкуренты» → «Добавить страницу мониторинга»"),
    dict(key="google_trends", name="Google Trends (pytrends)", kind="demand", url="https://trends.google.com/trends/", mvp_suitability="ready",
         data_available="Относительный индекс интереса 0–100 по запросам (RU), связанные запросы", official_api="нет официального API; неофициальная библиотека pytrends",
         needs_parsing=0, needs_auth=0, cost="бесплатно", limits="Жёсткие лимиты частоты (HTTP 429); сравнение до 5 запросов в группе; индекс относительный, не абсолютные объёмы",
         update_frequency="еженедельно", stability="средняя", risks="Неофициальный доступ, возможны временные блокировки; нарушение — только частота запросов",
         how_to_connect="Работает без ключей; при 429 — повтор через несколько часов, либо импорт CSV, выгруженного вручную из trends.google.com"),
    dict(key="wordstat", name="Яндекс Wordstat", kind="demand", url="https://wordstat.yandex.ru", mvp_suitability="needs_key",
         data_available="Абсолютное число показов в месяц по запросу, история по месяцам, регионы, похожие запросы", official_api="да — Wordstat API (https://yandex.ru/dev/wordstat/), требуется OAuth-токен Яндекс ID и подключение в кабинете",
         needs_parsing=0, needs_auth=1, cost="бесплатно для владельца аккаунта, лимиты на число запросов", limits="Требуется авторизация; веб-интерфейс закрыт без входа",
         update_frequency="ежемесячно", stability="высокая при наличии токена", risks="Парсинг веб-интерфейса нарушает условия использования — не применяется",
         how_to_connect="1) Получить OAuth-токен Яндекс ID для приложения с доступом к Wordstat API; 2) записать в .env: YANDEX_WORDSTAT_TOKEN=...; 3) до этого — ручной импорт CSV/XLSX из Wordstat в разделе «Поисковый спрос»"),
    dict(key="wordstat_import", name="Wordstat — ручной импорт CSV/XLSX", kind="import", url=None, mvp_suitability="manual_import",
         data_available="Запрос, показы в месяц, период — из файла выгрузки", official_api="—", needs_parsing=0, needs_auth=0, cost="бесплатно",
         limits="Ручной шаг", update_frequency="по мере импорта", stability="высокая", risks="нет",
         how_to_connect="Раздел «Поисковый спрос» → «Импорт» → файл с колонками: query; impressions; period (YYYY-MM)"),
    dict(key="yandex_suggest", name="Яндекс Подсказки (suggest)", kind="demand", url="https://suggest.yandex.ru", mvp_suitability="ready",
         data_available="Популярные формулировки запросов (без объёмов) — для расширения семантики и обнаружения новых потребностей", official_api="публичный endpoint подсказок",
         needs_parsing=0, needs_auth=0, cost="бесплатно", limits="Без объёмов; частота — умеренная", update_frequency="еженедельно", stability="средняя", risks="Неофициальный endpoint",
         how_to_connect="Работает без ключей"),
    dict(key="wildberries", name="Wildberries (поиск каталога)", kind="marketplace", url="https://www.wildberries.ru", mvp_suitability="ready",
         data_available="Товары, продавцы, цены по поисковым запросам (публичный JSON поиска)", official_api="публичного API нет; JSON-endpoint поиска витрины",
         needs_parsing=0, needs_auth=0, cost="бесплатно", limits="Неофициальный endpoint, может меняться", update_frequency="ежедневно", stability="средняя",
         risks="Изменение endpoint; ограничение частоты", how_to_connect="Работает без ключей; при ошибках — обновить endpoint в radar/collectors/wildberries.py"),
    dict(key="ozon", name="Ozon", kind="marketplace", url="https://www.ozon.ru", mvp_suitability="blocked",
         data_available="Товары и цены (только через браузер)", official_api="Seller API только для продавцов (нужен ключ)", needs_parsing=1, needs_auth=1,
         cost="бесплатно для продавца", limits="Анти-бот защита на витрине", update_frequency="—", stability="низкая", risks="Обход защиты не допускается",
         how_to_connect="Вариант 1: ручной импорт CSV с ценами; вариант 2: ключ Ozon Seller API (Client-Id, Api-Key) для аналитики по своим товарам"),
    dict(key="yandex_market", name="Яндекс Маркет", kind="marketplace", url="https://market.yandex.ru", mvp_suitability="blocked",
         data_available="Товары и цены (витрина)", official_api="Partner API для продавцов", needs_parsing=1, needs_auth=1, cost="—", limits="Капча/анти-бот на витрине",
         update_frequency="—", stability="низкая", risks="Обход защиты не допускается", how_to_connect="Ручной импорт CSV или Partner API для собственного магазина"),
    dict(key="zakupki", name="ЕИС zakupki.gov.ru (тендеры)", kind="tenders", url="https://zakupki.gov.ru", mvp_suitability="manual_import",
         data_available="Закупки по ключевым словам: предмет, заказчик, НМЦК, дата", official_api="официальный FTP открытых данных (ftp.zakupki.gov.ru) в XML; веб-поиск защищён",
         needs_parsing=1, needs_auth=0, cost="бесплатно", limits="Веб-интерфейс блокирует автоматические запросы; FTP — большие объёмы XML",
         update_frequency="ежедневно", stability="средняя", risks="Блокировка при частых запросах", how_to_connect="Ручной импорт CSV выгрузки из ЕИС или подключение FTP-загрузчика по XML (следующий этап)"),
    dict(key="customs", name="Таможенная статистика ФТС", kind="customs", url="https://customs.gov.ru", mvp_suitability="paid",
         data_available="Только агрегированная статистика по кодам ТН ВЭД; данные по поставкам — у платных провайдеров (ВЭД-Стат, ImportGenius и др.)",
         official_api="нет", needs_parsing=1, needs_auth=0, cost="платно (провайдеры)", limits="Нет детализации по товарам без платной подписки", update_frequency="ежемесячно",
         stability="—", risks="Юридически — только легальные провайдеры", how_to_connect="Подписка у провайдера таможенных данных, затем импорт CSV"),
    dict(key="alibaba", name="Alibaba.com", kind="manufacturer", url="https://www.alibaba.com", mvp_suitability="blocked",
         data_available="Товары фабрик, ценовые диапазоны, MOQ", official_api="нет публичного", needs_parsing=1, needs_auth=1, cost="—", limits="Анти-бот, JS-рендер",
         update_frequency="—", stability="низкая", risks="Обход защиты не допускается", how_to_connect="Ручной импорт CSV по итогам проверки поставщиков"),
    dict(key="1688", name="1688.com", kind="manufacturer", url="https://www.1688.com", mvp_suitability="blocked",
         data_available="Оптовые цены фабрик (CNY)", official_api="нет", needs_parsing=1, needs_auth=1, cost="—", limits="Требует авторизации/капчи",
         update_frequency="—", stability="низкая", risks="Обход защиты не допускается", how_to_connect="Ручной импорт CSV"),
    dict(key="made_in_china", name="Made-in-China.com", kind="manufacturer", url="https://www.made-in-china.com", mvp_suitability="manual_import",
         data_available="Товары фабрик и ценовые диапазоны (USD)", official_api="нет", needs_parsing=1, needs_auth=0, cost="бесплатно", limits="Анти-бот на части страниц",
         update_frequency="ежемесячно", stability="средняя", risks="Изменение вёрстки", how_to_connect="Добавить страницы поиска как страницы мониторинга (kind=catalog) или импорт CSV"),
    dict(key="serp", name="Поисковая выдача Яндекс/Google", kind="serp", url=None, mvp_suitability="manual_import",
         data_available="Кто занимает выдачу по запросам, новые домены", official_api="Яндекс XML (требует регистрацию и IP), Google Custom Search API (ключ, платно после 100/день)",
         needs_parsing=1, needs_auth=1, cost="ограниченно бесплатно", limits="Капча при автоматических запросах к обычной выдаче", update_frequency="еженедельно", stability="низкая",
         risks="Обход капчи не допускается", how_to_connect="Подключить Яндекс XML (https://xml.yandex.ru) или Google CSE API и указать ключи в .env"),
    dict(key="manufacturers", name="Сайты зарубежных производителей", kind="manufacturer", url=None, mvp_suitability="ready",
         data_available="Новые модели, характеристики, цены (USD/EUR) на статичных страницах", official_api="нет", needs_parsing=1, needs_auth=0, cost="бесплатно",
         limits="Часть сайтов на JS", update_frequency="еженедельно", stability="средняя", risks="Изменение вёрстки", how_to_connect="Добавить страницы каталога производителя в мониторинг"),
]


def seed(conn: sqlite3.Connection) -> None:
    for i, slug in enumerate(CATEGORY_ORDER):
        conn.execute(
            "INSERT INTO categories(slug, name_ru, in_scope, sort_order) VALUES(?,?,1,?) ON CONFLICT(slug) DO UPDATE SET name_ru=excluded.name_ru",
            (slug, CATEGORY_NAMES[slug], i * 10),
        )
    for s in SOURCE_DESCRIPTIONS:
        s = dict(s)
        key = s.pop("key")
        existing = conn.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()
        if existing:
            # обновляем только описательные поля, не трогая статус запуска
            s.pop("status", None)
            db.upsert_source(conn, key, **s)
        else:
            db.upsert_source(conn, key, status="unknown", **s)
    if db.get_setting(conn, "owner_default") is None:
        db.set_setting(conn, "owner_default", "Продуктовая команда")
    if db.get_setting(conn, "owners") is None:
        db.set_setting(conn, "owners", "Собственник;Продуктовая команда;Маркетинг;Продажи;Закупки")
