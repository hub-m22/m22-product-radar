"""Схлопывание дублей товаров конкурентов по URL страницы товара.

Одна страница товара (monitored_pages.kind='product') должна давать одну позицию. Если под тем же URL есть несколько активных
строк с разными названиями (старое название из ручного исследования и новое со страницы), оставляем строку, снятую последней,
а более информативное старое название (с параметрами вроде «150 м, 18 каналов») переносим в описание, чтобы нормализация
характеристик его видела. Сопоставления и историю цен старой строки не трогаем — строка просто становится неактивной.
Запуск: python scripts/dedupe_competitor_products.py [--dry]
"""
import re
import sys

sys.path.insert(0, ".")
from radar import db

dry = "--dry" in sys.argv
conn = db.connect()
groups = db.rows(conn, """SELECT cp.competitor_id, cp.url, COUNT(*) n FROM competitor_products cp
                          WHERE cp.is_active=1 AND cp.url IN (SELECT url FROM monitored_pages WHERE kind='product')
                          GROUP BY cp.competitor_id, cp.url HAVING COUNT(*) > 1""")
merged = 0
for g in groups:
    rows = db.rows(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND url=? AND is_active=1 ORDER BY COALESCE(fetched_at,'') DESC, id DESC",
                   (g["competitor_id"], g["url"]))
    keep, rest = rows[0], rows[1:]
    extra = [r["name"] for r in rest if r["name"] != keep["name"] and re.search(r"\d", r["name"] or "")]
    desc = keep["description"] or ""
    add = " ; ".join(x for x in extra if x not in desc)
    if add:
        desc = (add + " ; " + desc)[:2500]
    print(f"[{g['competitor_id']}] {g['url'][:70]} -> оставить «{keep['name'][:50]}», выключить {len(rest)}: {[r['name'][:40] for r in rest]}")
    if not dry:
        conn.execute("UPDATE competitor_products SET description=? WHERE id=?", (desc, keep["id"]))
        conn.executemany("UPDATE competitor_products SET is_active=0 WHERE id=?", [(r["id"],) for r in rest])
    merged += len(rest)
if not dry:
    conn.commit()
print("групп:", len(groups), "выключено строк:", merged, "(пробный прогон)" if dry else "")
