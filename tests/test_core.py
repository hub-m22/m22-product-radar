"""Автотесты критической логики: нормализация, парсеры, сопоставление, сигналы, рекомендации (на тестовой базе, отдельно от реальных данных)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("PYTHONUTF8", "1")

from radar import db, matching, normalize, recommendations, signals  # noqa: E402
from radar.collectors import m22 as m22c  # noqa: E402
from radar.collectors import radiosync as rsc  # noqa: E402
from radar.collectors.common import upsert_competitor_product, upsert_m22_product  # noqa: E402
from radar.collectors.competitor_generic import parse_catalog_page, parse_product_page  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.sqlite3"
    c = db.connect(path)
    db.migrate(c)
    yield c
    c.close()


# ---------- нормализация ----------
@pytest.mark.parametrize("text,expected", [("32 500 ₽", 32500.0), ("32500,00", 32500.0), ("от 4 500 руб.", 4500.0), ("12.500 руб", 12500.0), ("1 500 ₽ за шт", 1500.0), ("нет", None), ("", None)])
def test_parse_price(text, expected):
    assert normalize.parse_price(text) == expected


@pytest.mark.parametrize("name,key", [("Радиогид система Radiosync SGTR02, на 25 персон", "SGTR02"), ("Радиогид система Radiosync UG-10", "UG10"), ("Аудиогид Radiosync AG-300", "AG300"),
                                      ("Retekess TT106 tour guide", "TT106"), ("Радиогид Reinvox DUO", "REINVOX-DUO"), ("Sennheiser HDE 2020-D-II", "HDE2020")])
def test_model_key(name, key):
    assert normalize.model_key(name) == key


def test_category_and_kind():
    assert normalize.classify_category("Радиогид система Radiosync SGTR03, на 25 персон, с докстанцией и кейсом", None, "Радиогиды и аудиогиды") == "radiogid"
    assert normalize.classify_category("Кейс для радиогид систем Radiosync SGTR03 (на 60 устройств)", None, "Радиогиды и аудиогиды") == "charging_cases"
    assert normalize.classify_category("Комплект для синхронного перевода Radiosync SGTR02, на 25 персон") == "sync_translation"
    assert normalize.classify_category("Одноразовые стерео наушники Radiosync X22387B", None, "Радиогиды и аудиогиды") == "disposable_headphones"
    assert normalize.detect_kind("Передатчик для радиогид системы SGTR13") == "transmitter"
    assert normalize.detect_kind("Приёмник для радиогид системы Radiosync SGTR02") == "receiver"
    assert normalize.detect_kind("Радиогид система Radiosync SGTR02") == "system"
    assert normalize.detect_capacity("Радиогид система Radiosync SGTR02, на 25 персон", "Комплект 5/10/15/25 экскурсантов") == 25
    assert normalize.detect_capacity("Радиогид система Radiosync SGTR02", "Комплект 5/10/15/25 экскурсантов") is None


def test_clean_text_removes_tilda_fillers():
    assert normalize.clean_text("Кейс (60 слотов)ㅤㅤㅤ") == "Кейс (60 слотов)"
    assert normalize.clean_text(123) == "123"


# ---------- парсеры ----------
def test_m22_parser_fixture():
    html = (FIX / "m22_product.html").read_text(encoding="utf-8")
    d = m22c.parse_product_page(html, "https://m22.ru/catalog/sistemi-radiogid/sistema-radiogid-sgtr02/radiogid-sistema-radiosync-sgtr02")
    assert d["name"].startswith("Радиогид система Radiosync SGTR02")
    assert d["price"] == 32500.0
    assert d["sku"] == "SGTR02-X"
    assert "Комплектация" in d["specs"]
    assert len(d["variants"]) >= 4


def test_radiosync_parser_fixture():
    html = (FIX / "radiosync_product.html").read_text(encoding="utf-8")
    d = rsc.parse_product_page(html, "https://radiosync.ru/comparisontable/tproduct/448439933172-radiogid-sistema-radiosync-sgtr02")
    assert d["price"] == 32500.0
    assert d["high_price"] == 110000.0
    assert len(d["variants"]) == 4 and d["variants"][0]["url"].endswith("#RTKGTR11305131")


def test_generic_parser_jsonld_and_heuristics():
    html = """<html><head><script type="application/ld+json">{"@type":"Product","name":"Радиогид X1","offers":{"@type":"Offer","price":"12 500","priceCurrency":"RUB"}}</script></head><body><h1>Радиогид X1</h1></body></html>"""
    items = parse_product_page(html, "https://x.ru/p")
    assert items[0]["name"] == "Радиогид X1" and items[0]["price"] == 12500.0
    cat = """<html><body><div class="item"><h3><a href="/a">Радиогид Alpha 10 персон</a></h3><span class="price"><span>45 000</span><span> ₽</span></span></div>
             <div class="item"><a href="/b"><img alt="Приёмник Beta"></a><span>3 900 руб.</span></div><div><a href="/x">Главная</a> 100 ₽ доставка</div></body></html>"""
    items = parse_catalog_page(cat, "https://x.ru/cat")
    names = {i["name"]: i["price"] for i in items}
    assert names["Радиогид Alpha 10 персон"] == 45000.0 and names["Приёмник Beta"] == 3900.0
    assert "Главная" not in names


# ---------- хранение, дубли, история ----------
def _m22(conn, name, price, sku=None, site="m22.ru", url=None, cap=None):
    upsert_m22_product(conn, {"site": site, "url": url or f"https://{site}/{name}", "name": name, "brand": "Radiosync", "price": price, "sku": sku, "site_category_path": "Радиогиды",
                              "specs": {}, "kit": {}, "variants": [], "images": [], "fetched_at": "2026-09-01T00:00:00Z"}, None)
    return db.row(conn, "SELECT * FROM m22_products WHERE url=?", (url or f"https://{site}/{name}",))


def test_upsert_dedup_and_price_history(conn):
    p = _m22(conn, "Радиогид система Radiosync SGTR02, на 10 персон", 50500, "RTKGTR113010131")
    assert p["capacity"] == 10 and p["model_key"] == "SGTR02" and p["category_slug"] == "radiogid"
    _m22(conn, "Радиогид система Radiosync SGTR02, на 10 персон", 50500, "RTKGTR113010131")
    assert db.row(conn, "SELECT COUNT(*) n FROM m22_products")["n"] == 1
    assert db.row(conn, "SELECT COUNT(*) n FROM m22_price_history")["n"] == 1
    _m22(conn, "Радиогид система Radiosync SGTR02, на 10 персон", 52000, "RTKGTR113010131")
    assert db.row(conn, "SELECT COUNT(*) n FROM m22_price_history")["n"] == 2


def _competitor(conn, name="Конкурент", site="https://c.ru"):
    conn.execute("INSERT INTO competitors(name, website, types_json) VALUES(?,?,'[\"direct_seller\"]')", (name, site))
    return db.row(conn, "SELECT id FROM competitors WHERE website=?", (site,))["id"]


# ---------- сопоставление и ценовые сигналы ----------
def test_matching_and_price_vs_market(conn):
    m = _m22(conn, "Радиогид система Radiosync SGTR02, на 10 персон", 50500, "RTKGTR113010131")
    ids = []
    for i, price in enumerate([40000, 41000, 42000]):
        cid = _competitor(conn, f"К{i}", f"https://c{i}.ru")
        pid, _ = upsert_competitor_product(conn, cid, None, {"url": f"https://c{i}.ru/p", "name": "Радиогид система Retekess TT106, на 10 персон", "price": price, "fetched_at": "2026-09-01T00:00:00Z"}, None)
        ids.append(pid)
    res = matching.run_matching(conn)
    assert res["created"] >= 3
    mt = db.rows(conn, "SELECT * FROM product_matches WHERE m22_product_id=?", (m["id"],))
    assert mt and all(x["match_type"] in ("direct_analog", "functional") for x in mt)
    assert all(x["confidence"] >= 0.6 for x in mt)
    n = signals.detect_price_vs_market(conn)
    assert n == 1
    s = db.row(conn, "SELECT * FROM signals WHERE type='m22_price_above_market'")
    assert s and "дороже" in s["title"] and s["m22_product_id"] == m["id"]
    # защита от повторов
    assert signals.detect_price_vs_market(conn) == 0
    # рекомендация формируется только при ≥3 сопоставимых
    assert recommendations.generate(conn) >= 1
    r = db.row(conn, "SELECT * FROM recommendations WHERE m22_product_id=?", (m["id"],))
    assert r and r["priority"] in ("P1", "P2") and "рассмотреть возможность" not in r["action"].lower()


def test_price_vs_market_needs_min_comparables(conn):
    m = _m22(conn, "Радиогид система Radiosync SGTR02, на 10 персон", 50500, "RTKGTR113010131")
    cid = _competitor(conn)
    upsert_competitor_product(conn, cid, None, {"url": "https://c.ru/p", "name": "Радиогид система Retekess TT106, на 10 персон", "price": 30000, "fetched_at": "2026-09-01T00:00:00Z"}, None)
    matching.run_matching(conn)
    assert signals.detect_price_vs_market(conn) == 0  # один слабый сигнал — не повод для вывода


def test_cross_site_discrepancy_by_sku(conn):
    _m22(conn, "Радиогид система Radiosync SGTR02, на 15 персон", 70300, "RTKGTR113015131", "m22.ru")
    _m22(conn, "Радиогид система Radiosync SGTR02 — RTKGTR113015131", 65000, "RTKGTR113015131", "radiosync.ru")  # разница ≥1 % и ≥500 ₽; округление (70 300 vs 70 200) сигналом не считается
    assert signals.detect_cross_site(conn) == 1
    s = db.row(conn, "SELECT * FROM signals WHERE type='cross_site_discrepancy'")
    assert "70 300" in s["title"] and "70 200" in s["title"] and s["confidence"] >= 0.9
    assert signals.detect_cross_site(conn) == 0


def test_competitor_price_change_signal(conn):
    cid = _competitor(conn)
    item = {"url": "https://c.ru/p", "name": "Радиогид система Retekess TT106", "price": 40000, "fetched_at": "2026-09-01T00:00:00Z"}
    upsert_competitor_product(conn, cid, None, item, 1)
    conn.execute("UPDATE competitor_price_history SET observed_at='2026-08-01 00:00:00'")
    upsert_competitor_product(conn, cid, None, {**item, "price": 34000}, 2)
    assert signals.detect_competitor_price_changes(conn) == 1
    s = db.row(conn, "SELECT * FROM signals WHERE type='competitor_price_change'")
    assert "снизил" in s["title"] and "15%" in s["title"]


def test_demand_change_signal(conn):
    conn.execute("INSERT INTO search_queries(query, category_slug, is_seed) VALUES('радиогид','radiogid',1)")
    qid = db.row(conn, "SELECT id FROM search_queries")["id"]
    vals = [10] * 8 + [20] * 4
    for i, v in enumerate(vals):
        d = f"2026-{(i // 4) + 5:02d}-{(i % 4) * 7 + 1:02d}"
        conn.execute("INSERT INTO demand_observations(query_id, source, period_start, period_end, value, unit) VALUES(?,?,?,?,?,?)", (qid, "google_trends", d, d, v, "index"))
    assert signals.detect_demand(conn) >= 1
    s = db.row(conn, "SELECT * FROM signals WHERE type='demand_change'")
    assert "вырос" in s["title"] and s["fact_kind"] == "inference"


def test_migrations_idempotent(tmp_path):
    path = tmp_path / "m.sqlite3"
    c = db.connect(path)
    assert db.migrate(c)
    assert db.migrate(c) == []
    c.close()
