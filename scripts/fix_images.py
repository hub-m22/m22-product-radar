"""Проверка ссылок на фото товаров конкурентов и подбор замены.

Для каждого активного товара с фото делает лёгкий запрос по ссылке; если сервер не отдаёт картинку (404, HTML вместо картинки),
ищет в сохранённой копии страницы (data/raw) другие изображения и берёт первое рабочее. Для товаров без фото пробует найти его
в сохранённой странице. Ссылки, которые не удалось починить, обнуляются — в матрице покажется «нет фото» вместо битой картинки.
Запуск: python scripts/fix_images.py A   |   python scripts/fix_images.py 2,4
"""
import hashlib
import sys
import time
from urllib.parse import urljoin

import requests

sys.path.insert(0, ".")
from bs4 import BeautifulSoup

from radar import config, db

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
     "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
SKIP = ("logo", "icon", "sprite", "pixel", "banner", ".svg", "data:image", "placeholder", "loader", "captcha", "flag", "payment", "visa", "master")
arg = sys.argv[1] if len(sys.argv) > 1 else "A"
conn = db.connect()
if arg in ("A", "B", "C"):
    ids = [r["id"] for r in db.rows(conn, "SELECT id FROM competitors WHERE is_active=1 AND tier=? AND website NOT LIKE 'tender:%' ORDER BY id", (arg,))]
else:
    ids = [int(x) for x in arg.split(",")]
_cache: dict[str, bool] = {}


def image_ok(url: str) -> bool:
    if url in _cache:
        return _cache[url]
    ok = False
    try:
        r = requests.get(url, headers=H, timeout=12, stream=True)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("image") and int(r.headers.get("content-length") or 1000) > 400
        r.close()
    except Exception:  # noqa: BLE001
        ok = False
    _cache[url] = ok
    time.sleep(0.7)
    return ok


def raw_html(cid: int, url: str) -> str | None:
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    folder = config.RAW_DIR / f"competitor_{cid}"
    if not folder.exists():
        return None
    for day in sorted(folder.iterdir(), reverse=True):
        p = day / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return None


def candidates(html: str, url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for sel, attr in (('meta[property="og:image"]', "content"), ('meta[itemprop="image"]', "content"), ('[itemprop="image"]', "src"), ('link[rel="image_src"]', "href")):
        for el in soup.select(sel):
            v = (el.get(attr) or "").strip()
            if v:
                out.append(urljoin(url, v))
    main = soup.find("main") or soup.body or soup
    for img in main.find_all("img"):
        for a in ("data-src", "data-original", "data-lazy", "data-zoom-image", "data-big", "src"):
            v = (img.get(a) or "").strip()
            if v and not v.startswith("data:"):
                out.append(urljoin(url, v))
    for a in main.find_all("a", href=True):
        h = a["href"].lower()
        if h.endswith((".jpg", ".jpeg", ".png", ".webp")):
            out.append(urljoin(url, a["href"]))
    seen, res = set(), []
    for u in out:
        if u in seen or any(w in u.lower() for w in SKIP):
            continue
        seen.add(u)
        res.append(u)
    return res


for cid in ids:
    rows = db.rows(conn, "SELECT id, url, image_url FROM competitor_products WHERE competitor_id=? AND is_active=1", (cid,))
    fixed = cleared = kept = added = 0
    for r in rows:
        cur = r["image_url"]
        if cur and image_ok(cur):
            kept += 1
            continue
        html = raw_html(cid, r["url"].split("#")[0])
        new = None
        if html:
            for cand in candidates(html, r["url"])[:8]:
                if cand != cur and image_ok(cand):
                    new = cand
                    break
        if new:
            conn.execute("UPDATE competitor_products SET image_url=? WHERE id=?", (new, r["id"]))
            fixed += 1 if cur else 0
            added += 0 if cur else 1
        elif cur:
            conn.execute("UPDATE competitor_products SET image_url=NULL WHERE id=?", (r["id"],))
            cleared += 1
        conn.commit()
    print(f"конкурент {cid}: товаров {len(rows)}, фото рабочих {kept}, заменено битых {fixed}, найдено новых {added}, убрано битых без замены {cleared}", flush=True)
