"""Сопоставление товаров по фотографии: перцептивные хеши (pHash + dHash).

Идея: у одинаковых товаров (тот же OEM-приёмник под разными марками) фото с сайтов часто совпадают почти точно.
Совпадение хешей при малом расстоянии Хэмминга — сильный признак того же изделия («фото совпадает»).
Похожие, но не одинаковые фото (расстояние побольше) — только подсказка «возможно, тот же товар», сопоставление по ним не создаётся.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import time
from urllib.parse import urlparse

import numpy as np
import requests
from PIL import Image

from . import db

log = logging.getLogger(__name__)

SAME_MAX = 6        # pHash ≤ 6 бит из 64 и dHash ≤ 10 — фото одинаковое
SIMILAR_MAX = 12    # pHash ≤ 12 — похожее (подсказка)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
           "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
_last: dict[str, float] = {}


def _dct_matrix(n: int = 32) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi / n * (i + 0.5) * k)
    m[0, :] *= 1 / np.sqrt(2)
    return m * np.sqrt(2 / n)


_DCT = _dct_matrix(32)


def phash(img: Image.Image) -> int:
    g = img.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    a = np.asarray(g, dtype=np.float64)
    d = _DCT @ a @ _DCT.T
    low = d[:8, :8].copy()
    med = np.median(low[1:, 1:]) if low.size else 0.0
    bits = (low > med).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def dhash(img: Image.Image) -> int:
    g = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    a = np.asarray(g, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _trim_white(img: Image.Image) -> Image.Image:
    """Обрезает белые/прозрачные поля: одна и та же картинка на сайтах бывает с разными отступами."""
    try:
        rgb = img.convert("RGB")
        a = np.asarray(rgb, dtype=np.int16)
        mask = (a.sum(axis=2) < 3 * 245)
        ys, xs = np.where(mask)
        if len(ys) < 50:
            return img
        y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
        if (y1 - y0) < 20 or (x1 - x0) < 20:
            return img
        return rgb.crop((x0, y0, x1 + 1, y1 + 1))
    except Exception:  # noqa: BLE001
        return img


def fetch_image(url: str, timeout: int = 20) -> Image.Image | None:
    dom = urlparse(url).netloc
    wait = _last.get(dom, 0) + 0.6 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last[dom] = time.monotonic()
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
        return None
    img = Image.open(io.BytesIO(r.content))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    return img


def _m22_image(images_json: str | None, url: str | None) -> str | None:
    imgs = db.uj(images_json, []) or []
    img = imgs[0] if imgs and isinstance(imgs[0], str) else (imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else None)
    if not img:
        return None
    if not img.startswith("http"):
        img = "https://m22.ru" + img if img.startswith("/") else img
    return img


def compute_hashes(conn: sqlite3.Connection, owner: str = "all", force: bool = False, limit: int | None = None) -> dict:
    """Скачивает фото и считает хеши для товаров M22 (owner=m22) и конкурентов (owner=comp)."""
    todo: list[tuple[str, int, str]] = []
    if owner in ("all", "m22"):
        for r in db.rows(conn, "SELECT id, images_json, url FROM m22_products WHERE is_active=1 AND in_scope=1"):
            u = _m22_image(r["images_json"], r["url"])
            if u:
                todo.append(("m22", r["id"], u))
    if owner in ("all", "comp"):
        for r in db.rows(conn, "SELECT cp.id, cp.image_url FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=1 AND c.is_active=1 AND cp.category_slug IS NOT NULL AND cp.image_url IS NOT NULL AND cp.image_url!=''"):
            todo.append(("comp", r["id"], r["image_url"]))
    done = errors = skipped = 0
    for own, pid, url in todo:
        if limit and done + errors >= limit:
            break
        prev = db.row(conn, "SELECT url, status FROM image_hashes WHERE owner=? AND product_id=?", (own, pid))
        if prev and prev["url"] == url and prev["status"] == "ok" and not force:
            skipped += 1
            continue
        # одинаковый url у другого товара — переиспользуем хеш
        same = db.row(conn, "SELECT phash, dhash, width, height FROM image_hashes WHERE url=? AND status='ok' LIMIT 1", (url,))
        try:
            if same:
                ph, dh, w, h = same["phash"], same["dhash"], same["width"], same["height"]
            else:
                img = fetch_image(url)
                if img is None:
                    raise ValueError("не картинка или недоступна")
                w, h = img.size
                img = _trim_white(img)
                ph, dh = f"{phash(img):016x}", f"{dhash(img):016x}"
            conn.execute("""INSERT INTO image_hashes(owner, product_id, url, phash, dhash, width, height, status, error, fetched_at) VALUES(?,?,?,?,?,?,?,'ok',NULL,datetime('now'))
                            ON CONFLICT(owner, product_id) DO UPDATE SET url=excluded.url, phash=excluded.phash, dhash=excluded.dhash, width=excluded.width, height=excluded.height, status='ok', error=NULL, fetched_at=excluded.fetched_at""",
                         (own, pid, url, ph, dh, w, h))
            done += 1
        except Exception as exc:  # noqa: BLE001
            conn.execute("""INSERT INTO image_hashes(owner, product_id, url, status, error, fetched_at) VALUES(?,?,?,'error',?,datetime('now'))
                            ON CONFLICT(owner, product_id) DO UPDATE SET url=excluded.url, status='error', error=excluded.error, fetched_at=excluded.fetched_at""",
                         (own, pid, url, str(exc)[:200]))
            errors += 1
        if (done + errors) % 25 == 0:
            conn.commit()
    conn.commit()
    return {"queued": len(todo), "hashed": done, "errors": errors, "skipped": skipped}


def compare(conn: sqlite3.Connection) -> dict:
    """Сравнивает все фото конкурентов со всеми фото M22 и пишет image_matches (same / similar)."""
    m22 = db.rows(conn, "SELECT h.product_id, h.phash, h.dhash, p.category_slug FROM image_hashes h JOIN m22_products p ON p.id=h.product_id WHERE h.owner='m22' AND h.status='ok'")
    comp = db.rows(conn, "SELECT h.product_id, h.phash, h.dhash, cp.category_slug FROM image_hashes h JOIN competitor_products cp ON cp.id=h.product_id WHERE h.owner='comp' AND h.status='ok' AND cp.is_active=1")
    conn.execute("DELETE FROM image_matches")
    same = similar = 0
    m22v = [(r["product_id"], int(r["phash"], 16), int(r["dhash"], 16), r["category_slug"]) for r in m22]
    for c in comp:
        cph, cdh = int(c["phash"], 16), int(c["dhash"], 16)
        for mid, mph, mdh, mcat in m22v:
            pd = hamming(cph, mph)
            if pd > SIMILAR_MAX:
                continue
            dd = hamming(cdh, mdh)
            if pd <= SAME_MAX and dd <= 10:
                verdict = "same"
                same += 1
            elif pd <= SIMILAR_MAX and dd <= 16:
                verdict = "similar"
                similar += 1
            else:
                continue
            conn.execute("INSERT OR REPLACE INTO image_matches(m22_product_id, competitor_product_id, phash_dist, dhash_dist, verdict) VALUES(?,?,?,?,?)",
                         (mid, c["product_id"], pd, dd, verdict))
    conn.commit()
    return {"m22_images": len(m22v), "competitor_images": len(comp), "same": same, "similar": similar}


def apply_to_matches(conn: sqlite3.Connection) -> dict:
    """Переносит совпадения по фото в product_matches: одинаковое фото — сопоставление «same_photo» с уверенностью 0.9
    (или усиление существующего); похожее фото — только пометка в причинах существующего сопоставления."""
    created = boosted = noted = 0
    for im in db.rows(conn, "SELECT * FROM image_matches"):
        ex = db.row(conn, "SELECT id, confidence, reasons_json, review_status, match_type FROM product_matches WHERE m22_product_id=? AND competitor_product_id=?",
                    (im["m22_product_id"], im["competitor_product_id"]))
        if im["verdict"] == "same":
            reason = f"фото совпадает с фото товара M22 (отличие {im['phash_dist']}/64 бит) — почти наверняка то же изделие"
            if ex:
                if ex["review_status"] == "rejected":
                    continue
                reasons = db.uj(ex["reasons_json"], []) or []
                if not any("фото совпадает" in r for r in reasons):
                    reasons.append(reason)
                conf = max(ex["confidence"] or 0, 0.9)
                conn.execute("UPDATE product_matches SET confidence=?, reasons_json=?, needs_review=0, match_type=CASE WHEN match_type IN ('functional','accessory','adjacent') THEN 'same_photo' ELSE match_type END, updated_at=datetime('now') WHERE id=?",
                             (conf, db.j(reasons), ex["id"]))
                boosted += 1
            else:
                conn.execute("INSERT INTO product_matches(m22_product_id, competitor_product_id, match_type, confidence, method, reasons_json, needs_review) VALUES(?,?,?,?,?,?,?)",
                             (im["m22_product_id"], im["competitor_product_id"], "same_photo", 0.9, "photo", db.j([reason]), 0))
                created += 1
        else:
            if ex and ex["review_status"] != "rejected":
                reasons = db.uj(ex["reasons_json"], []) or []
                note = f"фото похоже на фото товара M22 (отличие {im['phash_dist']}/64 бит) — возможно, то же изделие, проверьте"
                if not any("фото похоже" in r for r in reasons):
                    reasons.append(note)
                    conn.execute("UPDATE product_matches SET reasons_json=?, updated_at=datetime('now') WHERE id=?", (db.j(reasons), ex["id"]))
                    noted += 1
    conn.commit()
    return {"created": created, "boosted": boosted, "noted": noted}


def photo_matches_for(conn: sqlite3.Connection, m22_product_id: int | None = None, competitor_product_id: int | None = None) -> list[dict]:
    where, params = [], []
    if m22_product_id:
        where.append("im.m22_product_id=?")
        params.append(m22_product_id)
    if competitor_product_id:
        where.append("im.competitor_product_id=?")
        params.append(competitor_product_id)
    return db.rows(conn, f"""SELECT im.*, m.name AS m22_name, m.price AS m22_price, m.url AS m22_url, cp.name AS comp_name, cp.price AS comp_price, cp.url AS comp_url, cp.image_url AS comp_image,
                             COALESCE(c.group_name, c.name) AS seller, c.id AS competitor_id
                             FROM image_matches im JOIN m22_products m ON m.id=im.m22_product_id JOIN competitor_products cp ON cp.id=im.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id
                             WHERE {' AND '.join(where) or '1=1'} ORDER BY im.verdict, im.phash_dist""", params)
