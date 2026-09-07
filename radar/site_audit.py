"""Аудит собственных сайтов M22 (m22.ru, radiosync.ru): ошибки карточек, расхождения цен и наличия, дубли,
единообразие названий, опечатки и типографика, структура каталога. Результат — список замечаний с приоритетом
и конкретной рекомендацией, что исправить. Считается на лету по данным последнего сбора.

Приоритеты: P1 — теряем деньги или доверие (разные цены, нет цены, нет в наличии у ключевых товаров, дубли с разной ценой);
P2 — мешает продаже и поиску (нет на втором сайте, пустые характеристики, разнобой названий моделей, ошибки в ключевых полях);
P3 — косметика (типографика, опечатки, склеенные слова в микроразметке, неровные названия характеристик).
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

from . import db
from .normalize import CATEGORY_NAMES

PRIORITY_NAMES = {"P1": "Срочно", "P2": "Важно", "P3": "Косметика"}
GROUPS = {
    "price": "Цены и наличие",
    "dup": "Дубли и расхождения между сайтами",
    "card": "Неполные карточки",
    "naming": "Единообразие названий",
    "typo": "Опечатки и типографика",
    "structure": "Структура каталога и характеристик",
    "source": "Доступность страниц для сбора",
}
KEY_CATEGORIES = {"radiogid", "kits_solutions", "sync_translation", "audiogid", "charging_cases"}
# основные категории бизнеса: по умолчанию раздел показывает только их
CORE_CATEGORIES = ["radiogid", "audiogid", "sync_translation", "disposable_headphones", "reusable_headphones", "microphones_guide", "charging_cases", "kits_solutions", "rental"]
BRAND_CATEGORIES = {"radiogid", "audiogid", "charging_cases", "disposable_headphones", "reusable_headphones", "microphones_guide", "kits_solutions", "sync_translation", "voice_amplifier"}

AVAIL = {"InStock": "в наличии", "OutOfStock": "нет в наличии", "PreOrder": "предзаказ", "SoldOut": "распродано", None: "наличие не указано"}

# похожие по написанию латиница/кириллица
HOMOGLYPHS = str.maketrans({"c": "с", "o": "о", "a": "а", "e": "е", "p": "р", "x": "х", "y": "у", "k": "к", "C": "С", "O": "О", "A": "А", "E": "Е", "P": "Р", "X": "Х", "K": "К", "M": "М", "H": "Н", "T": "Т", "B": "В"})

# варианты написания одного термина: каноническая форма -> шаблон
TERM_VARIANTS = [
    ("радиогид-система", re.compile(r"радиогид[\s\-]*[сc]истем\w*", re.I), "Единообразно: «радиогид-система» (через дефис) или «радиогид система» — выбрать одну форму на обоих сайтах."),
    ("докстанция", re.compile(r"док[\s\-]*станци\w*", re.I), "Единообразно: «док-станция» (словарная форма) во всех карточках."),
    ("интерком-система", re.compile(r"интерком[\s\-]*систем\w*", re.I), "Единообразно: «интерком-система» без пробелов вокруг дефиса."),
    ("двусторонняя связь", re.compile(r"дву[хx]?сторонн\w*", re.I), "Единообразно: «двусторонняя связь» (норма); «двухсторонняя» — разговорный вариант."),
    ("приёмник", re.compile(r"при[её]мник\w*", re.I), "Единообразно: «приёмник» с буквой ё во всех названиях и характеристиках (или везде без ё)."),
]
UNIT_RX = re.compile(r"\b(?:на\s+)?(\d{1,3})\s*(персон|человек|чел\.?|слот\w*|устройств\w*|приёмник\w*|приемник\w*)", re.I)


def _rub(v) -> str:
    return "нет" if v is None else f"{v:,.0f} ₽".replace(",", " ")


def _norm_name(s: str) -> str:
    s = (s or "").lower().translate(HOMOGLYPHS)
    s = s.replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9+]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _model_forms(name: str) -> dict[str, str]:
    """{канонический ключ модели: как написано в названии} — для поиска AG300 / AG-300 / SGTR 11."""
    out = {}
    for m in re.finditer(r"\b(SGTR|UG|AG|TAG|RS|RT|BF|UV)\s*-?\s*(\d{2,3}[A-Z]?)\b", name, re.I):
        out[(m.group(1) + m.group(2)).upper()] = m.group(0)
    return out


def _flat_specs(p: dict) -> dict[str, str]:
    out = {}
    for g, v in (db.uj(p.get("specs_json"), {}) or {}).items():
        if isinstance(v, dict):
            out.update({str(k).strip(): str(x).strip() for k, x in v.items()})
        else:
            out[str(g).strip()] = str(v).strip()
    return out


def _spec(p: dict, *names: str) -> str | None:
    fl = {_norm_spec_key(k): v for k, v in _flat_specs(p).items()}
    for n in names:
        if fl.get(_norm_spec_key(n)):
            return fl[_norm_spec_key(n)]
    return None


def _image(p: dict) -> str | None:
    imgs = db.uj(p.get("images_json"), []) or []
    if not imgs:
        return None
    img = imgs[0]
    return f"https://{p['site']}{img}" if img.startswith("/") else img


def _card(p: dict) -> dict:
    """Короткая карточка товара для показа в замечании: фото, ссылка на сайт и в радар, цена, наличие, цвет, раздел."""
    return {"id": p["id"], "name": p["name"], "url": p["url"], "site": p["site"], "sku": p["sku"], "price": p["price"], "availability": AVAIL.get(p["availability"], p["availability"]),
            "image": _image(p), "color": _spec(p, "Цвет"), "section": (p.get("site_category_path") or "").strip(" /"), "desc_len": len(p.get("description") or ""),
            "specs_n": len(_flat_specs(p))}


def _item(priority: str, group: str, what: str, fix: str, *, site: str = "", product_id: int | None = None, name: str = "", url: str = "", sku: str | None = None, evidence: str = "", key: str = "",
          cards: list[dict] | None = None, category: str = "") -> dict:
    return {"key": key or f"{group}:{site}:{product_id}:{what[:60]}", "priority": priority, "priority_name": PRIORITY_NAMES[priority], "group": group, "group_name": GROUPS[group],
            "site": site, "product_id": product_id, "name": name, "url": url, "sku": sku, "what": what, "fix": fix, "evidence": evidence, "cards": cards or [], "category_slug": category}


# ---------------------------------------------------------------- проверки
def check_prices_and_stock(prods: list[dict]) -> list[dict]:
    out = []
    for p in prods:
        if p["parent_url"]:
            continue
        cat = p["category_slug"]
        if p["price"] is None and cat != "rental":
            out.append(_item("P1", "price", "Карточка без цены", "Указать цену или убрать карточку из каталога: карточка без цены не участвует в поиске по цене и не даёт положить товар в корзину.",
                             site=p["site"], product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"noprice:{p['id']}"))
        # «нет в наличии» — не ошибка, а отдельная вкладка «Наличие» (stock_report)
        if p["old_price"] and p["price"] and p["old_price"] <= p["price"]:
            out.append(_item("P2", "price", "Старая цена не выше новой", "Убрать зачёркнутую цену или поставить реальную старую цену — иначе скидка выглядит фальшивой.",
                             site=p["site"], product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], evidence=f"{p['price']:.0f} / старая {p['old_price']:.0f}", key=f"oldprice:{p['id']}"))
        if p["old_price"] and p["old_price"] % 10:
            out.append(_item("P3", "typo", "Старая цена не округлена", "Округлить зачёркнутую цену до десятков/сотен рублей — «139 355 ₽» выглядит как ошибка.",
                             site=p["site"], product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], evidence=f"старая цена {p['old_price']:.0f} ₽", key=f"oldround:{p['id']}"))
    # цена системы ниже цены передатчика или равна цене приёмника той же модели — почти наверняка ошибка
    by_model: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for p in prods:
        if p["model_key"] and p["price"] and not p["parent_url"]:
            by_model[(p["site"], p["model_key"])][p["kind"]].append(p)
    for (site, mk), kinds in by_model.items():
        tx = min((x["price"] for x in kinds.get("transmitter", [])), default=None)
        rx = min((x["price"] for x in kinds.get("receiver", [])), default=None)
        for s in kinds.get("system", []):
            if (tx and s["price"] < tx) or (rx and abs(s["price"] - rx) < 1):
                out.append(_item("P1", "price", "Цена системы ниже цены её компонентов", f"Проверить цену: система {_rub(s['price'])}, передатчик {mk} {_rub(tx)}, приёмник {_rub(rx)}. Похоже, в базовой карточке подставлена цена приёмника.", cards=[_card(s)] + [_card(x) for x in kinds.get("transmitter", [])[:1] + kinds.get("receiver", [])[:1]],
                                 site=site, product_id=s["id"], name=s["name"], url=s["url"], sku=s["sku"], key=f"sysprice:{s['id']}"))
    return out


def check_cross_site(prods: list[dict]) -> list[dict]:
    out = []
    by_sku: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for p in prods:
        if p["sku"] and p["sku"].strip():
            by_sku[p["sku"].strip().upper()][p["site"]].append(p)
    for sku, sites in by_sku.items():
        a, b = sites.get("m22.ru"), sites.get("radiosync.ru")
        if not a or not b:
            continue
        pa, pb = a[0], b[0]
        if pa["price"] and pb["price"] and abs(pa["price"] - pb["price"]) >= 1:
            out.append(_item("P1", "dup", "Разная цена на m22.ru и radiosync.ru", f"Выровнять цену: m22.ru {_rub(pa['price'])}, radiosync.ru {_rub(pb['price'])} (артикул {sku}). Клиент, открывший оба сайта, теряет доверие; в тендерных прайсах цены должны совпадать.", cards=[_card(pa), _card(pb)],
                             site="оба", product_id=pa["id"], name=pa["name"], url=pa["url"], sku=sku, evidence=f"radiosync: {pb['url']}", key=f"xprice:{sku}"))
        if pa["availability"] != pb["availability"] and pa["availability"] and pb["availability"]:
            out.append(_item("P1", "dup", "Разный статус наличия на двух сайтах", f"На m22.ru — {AVAIL.get(pa['availability'])}, на radiosync.ru — {AVAIL.get(pb['availability'])}. Свести остатки к одному источнику (учётная система → оба сайта).", cards=[_card(pa), _card(pb)],
                             site="оба", product_id=pa["id"], name=pa["name"], url=pa["url"], sku=sku, evidence=f"radiosync: {pb['url']}", key=f"xstock:{sku}"))
        if _norm_name(pa["name"]) != _norm_name(pb["name"]) and " — " not in pb["name"]:
            out.append(_item("P3", "naming", "Один артикул назван по-разному на двух сайтах", f"m22.ru: «{pa['name']}»; radiosync.ru: «{pb['name']}». Привести к одному названию — так проще искать и сверять прайсы.", cards=[_card(pa), _card(pb)],
                             site="оба", product_id=pa["id"], name=pa["name"], url=pa["url"], sku=sku, key=f"xname:{sku}"))
    # есть только на одном сайте (товары бренда Radiosync в основных категориях)
    m22_skus = {p["sku"].strip().upper() for p in prods if p["site"] == "m22.ru" and p["sku"]}
    rs_skus = {p["sku"].strip().upper() for p in prods if p["site"] == "radiosync.ru" and p["sku"]}
    for p in prods:
        if p["parent_url"] or not p["sku"] or p["category_slug"] not in BRAND_CATEGORIES or (p["brand"] or "").lower() != "radiosync":
            continue
        sku = p["sku"].strip().upper()
        if p["site"] == "radiosync.ru" and sku not in m22_skus:
            out.append(_item("P2", "dup", "Есть на radiosync.ru, нет на m22.ru", "Добавить карточку на m22.ru (основной магазин) или убрать с radiosync.ru, если товар снят.",
                             site="radiosync.ru", product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"only_rs:{sku}"))
        elif p["site"] == "m22.ru" and sku not in rs_skus and p["category_slug"] in ("radiogid", "audiogid", "charging_cases", "kits_solutions", "sync_translation"):
            out.append(_item("P3" if p["category_slug"] == "charging_cases" and not re.search(r"кейс|док", p["name"], re.I) else "P2", "dup", "Есть на m22.ru, нет на radiosync.ru", "Товар бренда Radiosync отсутствует на сайте бренда: добавить карточку на radiosync.ru или объяснить в карточке m22.ru, где купить.",
                             site="m22.ru", product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"only_m22:{sku}"))
    # дубли внутри одного сайта: одинаковое название
    by_name: dict[tuple, list[dict]] = defaultdict(list)
    for p in prods:
        if not p["parent_url"]:
            by_name[(p["site"], _norm_name(p["name"]))].append(p)
    for (site, _n), items in by_name.items():
        if len(items) < 2:
            continue
        prices = {x["price"] for x in items}
        skus = ", ".join(str(x["sku"]) for x in items)
        cards = [_card(x) for x in items]
        colors = [c["color"] for c in cards]
        # чем отличаются карточки по характеристикам (кроме цвета)
        specs = [{k: v for k, v in _flat_specs(x).items() if _norm_spec_key(k) != "цвет"} for x in items]
        diff_keys = sorted({k for sp in specs for k in sp if any(sp2.get(k) != sp.get(k) for sp2 in specs)})
        diff_txt = "; ".join(f"{k}: " + " / ".join(str(sp.get(k) or "—") for sp in specs) for k in diff_keys[:6])
        common = {"site": site, "product_id": items[0]["id"], "name": items[0]["name"], "url": items[0]["url"], "sku": skus, "cards": cards, "category": items[0]["category_slug"]}
        price_txt = " / ".join(_rub(x) for x in sorted(p for p in prices if p))
        if all(colors) and len(set(colors)) == len(colors) and len(diff_keys) <= 2:
            out.append(_item("P2", "dup", f"Варианты по цвету ({' / '.join(colors)}) под одним названием",
                             f"Это один товар в {len(items)} цветах, но из названия и списка каталога этого не видно. Дописать цвет в название («…, {colors[0].lower()}») или объединить в одну карточку с выбором цвета."
                             + (f" Цены при этом разные: {price_txt} — если это не намеренно, выровнять." if len(prices) > 1 else ""),
                             evidence=("Отличия в характеристиках: " + diff_txt) if diff_txt else "Характеристики совпадают, отличается только цвет.", key=f"dupcolor:{site}:{_n}", **common))
        elif len(diff_keys) >= 3:
            out.append(_item("P1", "dup", "Разные товары под одним названием",
                             f"Артикулы {skus}: характеристики отличаются существенно ({len(diff_keys)} параметров), значит это разные изделия, а называются одинаково"
                             + (f" и стоят по-разному: {price_txt}" if len(prices) > 1 else "") + ". Переименовать так, чтобы отличие было в названии (например, мощность и число портов), и проверить, что код модели в названии верен.",
                             evidence="Отличия: " + diff_txt, key=f"dupdiff:{site}:{_n}", **common))
        elif len(prices) > 1:
            out.append(_item("P1", "dup", "Одинаковые карточки с разной ценой", f"Артикулы {skus}, цены {price_txt}, характеристики совпадают. Оставить одну карточку или выровнять цену.",
                             evidence=("Отличия: " + diff_txt) if diff_txt else "Характеристики совпадают.", key=f"dupname_price:{site}:{_n}", **common))
        else:
            out.append(_item("P2", "dup", "Две карточки с одинаковым названием и ценой", f"Артикулы {skus}. Покупатель не понимает разницы, поисковики считают дублем. Объединить в одну карточку или дописать отличие в название.",
                             evidence=("Отличия: " + diff_txt) if diff_txt else "Характеристики совпадают полностью.", key=f"dupname:{site}:{_n}", **common))
    # однотипные карточки (одна модель, один тип изделия) заполнены неодинаково: у одной есть характеристика, у другой нет
    by_series: dict[tuple, list[dict]] = defaultdict(list)
    for p in prods:
        if not p["parent_url"] and p["model_key"] and p["site"] == "m22.ru" and p["kind"] in ("system", "receiver", "transmitter"):
            by_series[(p["model_key"], p["kind"], p["category_slug"])].append(p)
    for (mk, kind, _cat), items in by_series.items():
        if len(items) < 2:
            continue
        orig: dict[str, str] = {}
        for x in items:
            for k in _flat_specs(x):
                orig.setdefault(_norm_spec_key(k), k)
        keysets = [set(_norm_spec_key(k) for k in _flat_specs(x)) for x in items]
        union = set().union(*keysets)
        if not union:
            continue
        missing = []
        for x, ks in zip(items, keysets):
            lack = sorted(union - ks)
            if lack and len(lack) <= max(3, len(union) // 3):
                missing.append((x, lack))
        if missing:
            ev = "; ".join(f"«{x['name'][:40]}» (арт. {x['sku']}): нет «{'», «'.join(orig.get(k, k) for k in l[:4])}»" for x, l in missing[:4])
            out.append(_item("P2", "structure", f"Однотипные карточки {mk} заполнены неодинаково", "Заполнить характеристики по одному шаблону для всех карточек модели: у части карточек параметры пропущены, из-за чего сравнение и фильтры работают неровно.",
                             site="m22.ru", product_id=missing[0][0]["id"], name=missing[0][0]["name"], url=missing[0][0]["url"], sku=missing[0][0]["sku"], evidence=ev, key=f"specset:{mk}:{kind}",
                             cards=[_card(x) for x in items[:6]], category=items[0]["category_slug"]))
    return out


def check_cards(prods: list[dict]) -> list[dict]:
    out = []
    parents_with_variants = {p["parent_url"] for p in prods if p["parent_url"]}
    for p in prods:
        if p["parent_url"] or p["category_slug"] == "rental":
            continue
        has_variants = p["url"] in parents_with_variants  # карточка с выбором варианта: артикул у каждого варианта свой
        specs = db.uj(p["specs_json"], {}) or {}
        n_specs = sum(len(v) if isinstance(v, dict) else 1 for v in specs.values())
        images = db.uj(p["images_json"], []) or []
        problems = []
        if not images:
            problems.append(("P2", "Нет фотографий", "Добавить минимум 3 фото: общий вид, в руке/на человеке, комплект."))
        if n_specs == 0 and p["site"] == "m22.ru":
            problems.append(("P2", "Нет характеристик", "Заполнить блок характеристик (дальность, каналы, автономность, вес, гарантия) — по ним сравнивают с конкурентами."))
        elif n_specs == 0 and p["site"] == "radiosync.ru" and p["category_slug"] in ("radiogid", "audiogid", "charging_cases"):
            problems.append(("P3", "Нет характеристик (Tilda)", "Добавить в карточку блок «Характеристики» хотя бы с 5 ключевыми параметрами."))
        if len(p["description"] or "") < 250:
            problems.append(("P2", "Короткое описание", f"Описание {len(p['description'] or '')} знаков: дописать сценарии применения, состав комплекта, отличие от соседних моделей (300–800 знаков)."))
        if (not p["sku"] or not p["sku"].strip()) and not has_variants:
            problems.append(("P2", "Нет артикула", "Указать артикул — без него нельзя сверить товар между сайтами и с прайсом."))
        if p["site"] == "m22.ru" and (not p["site_category_path"] or p["site_category_path"].strip(" /") == ""):
            problems.append(("P2", "Карточка вне раздела каталога", "Привязать товар к разделу и подразделу каталога — сейчас он находится только по прямой ссылке/поиску."))
        elif p["site"] == "m22.ru" and p["site_category_path"].rstrip().endswith("/"):
            problems.append(("P3", "Нет подраздела каталога", "Указать подраздел (сейчас только верхний раздел) — упрощает навигацию и фильтры."))
        if (p["brand"] or "") == "М22":
            problems.append(("P3", "Бренд написан кириллицей «М22»", "В поле «Бренд» использовать латиницу «M22» (или «Radiosync»), иначе фильтр по бренду и поиск работают с двумя разными брендами."))
        for pr, what, fix in problems:
            out.append(_item(pr, "card", what, fix, site=p["site"], product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"card:{p['id']}:{what}"))
    return out


def check_naming(prods: list[dict]) -> list[dict]:
    out = []
    # 1. разное написание одной модели (AG300 / AG-300, UG10 / UG-10, SGTR 11 / SGTR11)
    forms: dict[str, Counter] = defaultdict(Counter)
    examples: dict[tuple, dict] = {}
    for p in prods:
        if p["parent_url"]:
            continue
        for canon, raw in _model_forms(p["name"]).items():
            forms[canon][raw] += 1
            examples.setdefault((canon, raw), p)
    for canon, cnt in forms.items():
        if len(cnt) > 1:
            main = cnt.most_common(1)[0][0]
            for raw, n in cnt.items():
                if raw == main:
                    continue
                ex = examples[(canon, raw)]
                out.append(_item("P2", "naming", f"Модель написана как «{raw}», в остальных карточках — «{main}»",
                                 f"Писать модель одинаково во всех названиях, характеристиках и URL: «{main}» ({cnt[main]} карточек). Разное написание разбивает поиск по сайту и поисковый спрос на два запроса.",
                                 site=ex["site"], product_id=ex["id"], name=ex["name"], url=ex["url"], sku=ex["sku"], evidence=f"{n} карточек с «{raw}»", key=f"model_form:{canon}:{raw}"))
    # 2. варианты терминов
    for canon, rx, fix in TERM_VARIANTS:
        cnt: Counter = Counter()
        ex: dict[str, dict] = {}
        for p in prods:
            if p["parent_url"]:
                continue
            for m in rx.finditer(p["name"] + " | " + (p["description"] or "")[:1500]):
                f = m.group(0).lower()
                f = re.sub(r"(\w)$", r"\1", f)
                base = re.sub(r"(систем|станци|сторонн|мник)\w*$", r"\1", f)
                cnt[base] += 1
                ex.setdefault(base, p)
        if len(cnt) > 1:
            variants = ", ".join(f"«{k}» ×{v}" for k, v in cnt.most_common())
            first = ex[cnt.most_common(1)[0][0]]
            out.append(_item("P3", "naming", f"Термин «{canon}» пишется по-разному", fix + f" Сейчас: {variants}.", site="оба", product_id=None, name="", url="", evidence=f"пример: {first['name']}", key=f"term:{canon}"))
    # 3. единица вместимости: персон / слотов / устройств
    units: Counter = Counter()
    for p in prods:
        if p["parent_url"]:
            continue
        for m in UNIT_RX.finditer(p["name"]):
            u = m.group(2).lower().rstrip(".")
            units[re.sub(r"(персон|человек|чел|слот|устройств|приемник|приёмник)\w*", r"\1", u)] += 1
    if len(units) > 2:
        out.append(_item("P3", "naming", "Вместимость в названиях указана в разных единицах", "Договориться: для систем — «на N персон», для кейсов и докстанций — «на N устройств». Сейчас: " + ", ".join(f"«{k}» ×{v}" for k, v in units.most_common()) + ".",
                         site="оба", key="units"))
    # 4. варианты radiosync названы артикулом вместо вместимости
    bad_variants = [p for p in prods if p["parent_url"] and re.search(r" — [A-Z0-9]{5,}$", p["name"])]
    if bad_variants:
        out.append(_item("P2", "naming", f"Варианты на radiosync.ru подписаны артикулом ({len(bad_variants)} шт.)", "В выборе варианта показывать «на 5 персон / на 10 персон», а не «RTKGTR101110013»: покупатель не знает артикулов. Название варианта задаётся в карточке Tilda.",
                         site="radiosync.ru", product_id=bad_variants[0]["id"], name=bad_variants[0]["name"], url=bad_variants[0]["url"], key="rs_variants"))
    return out


GLUE_RX = re.compile(r"[а-яё][А-ЯЁ]|[a-z][А-ЯЁ]|[а-яё][A-Z]|[.!?:][А-ЯЁA-Z]")
MIXED_WORD_RX = re.compile(r"\b(?=[\w]*[А-Яа-яЁё])(?=[\w]*[A-Za-z])[A-Za-zА-Яа-яЁё]{3,}\b")
KNOWN_MIXED = ("Type-C", "USB", "AUX", "HRTF", "MasterFree", "hands", "Hi-Res", "mini-jack")


def check_typos(prods: list[dict]) -> list[dict]:
    out = []
    vocab: Counter = Counter()
    for p in prods:
        for w in re.findall(r"[а-яё]{4,}", ((p["name"] or "") + " " + (p["description"] or "")).lower()):
            vocab[w] += 1
    for p in prods:
        if p["parent_url"]:
            continue
        name, desc = p["name"] or "", p["description"] or ""
        # название
        if "  " in name or name != name.strip():
            out.append(_item("P3", "typo", "Лишние пробелы в названии", "Убрать двойные и крайние пробелы.", site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], key=f"sp:{p['id']}"))
        if re.search(r"\(\s|\s\)", name):
            out.append(_item("P3", "typo", "Пробелы внутри скобок", "Писать «(на 40 слотов)», без пробелов после «(» и перед «)».", site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], key=f"paren:{p['id']}"))
        if re.search(r"\w\s-\s\w", name):
            out.append(_item("P3", "typo", "Дефис с пробелами в названии", "Для составных слов — дефис без пробелов («интерком-система», «Bluetooth-адаптер»), для пояснений — тире «—».", site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], evidence=name, key=f"dash:{p['id']}"))
        for m in MIXED_WORD_RX.finditer(name + " " + desc[:3000]):
            w = m.group(0)
            if any(k.lower() in w.lower() for k in KNOWN_MIXED):
                continue
            latin = re.findall(r"[A-Za-z]", w)
            if len(latin) <= 2:
                where = "в названии" if w in name else "в описании"
                out.append(_item("P2" if w in name else "P3", "typo", f"Латинская буква в русском слове {where}: «{w}»", f"Заменить латинскую «{''.join(latin)}» на русскую — слово с латиницей не находится поиском и подчёркивается как ошибка.",
                                 site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], evidence=w, key=f"mixed:{p['id']}:{w}"))
        # склеенные предложения/слова в описании (микроразметка без разделителей)
        glued = [m.group(0) for m in GLUE_RX.finditer(desc)]
        glued_words = []
        for w in re.findall(r"[а-яё]{16,}", desc.lower()):
            for i in range(5, len(w) - 4):
                if vocab.get(w[:i], 0) >= 2 and vocab.get(w[i:], 0) >= 2:
                    glued_words.append(f"{w[:i]}|{w[i:]}")
                    break
        if len(glued) >= 2 or glued_words:
            ev = ", ".join(glued[:5]) + (("; слова: " + ", ".join(glued_words[:4])) if glued_words else "")
            out.append(_item("P3", "typo", "Склеенные предложения в описании (микроразметка)", "В описании для поисковиков (JSON-LD / meta description) между абзацами и пунктами списка нет пробелов и точек. Проверить шаблон вывода описания на сайте: добавить пробел или перенос между элементами списка.",
                             site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], evidence=ev, key=f"glue:{p['id']}"))
        if re.search(r"\s[,.;:!?]", desc) or re.search(r",(?=[А-Яа-яA-Za-z])", desc):
            out.append(_item("P3", "typo", "Пробел перед знаком препинания или нет пробела после запятой", "Пройтись по описанию: знаки препинания без пробела перед ними и с пробелом после.",
                             site=p["site"], product_id=p["id"], name=name, url=p["url"], sku=p["sku"], evidence=(lambda m: m.group(0) if m else "")(re.search(r".{0,25}(\s[,.;:!?]|,(?=[А-Яа-яA-Za-z])).{0,25}", desc)), key=f"punct:{p['id']}"))
    return out


def _norm_spec_key(k: str) -> str:
    s = k.lower().translate(HOMOGLYPHS).replace("ё", "е").strip(" :.")
    s = s.replace("кол-во", "количество").replace("приемника", "приемник").replace("передатчика", "передатчик")
    return re.sub(r"\s+", " ", s)


def check_structure(prods: list[dict]) -> list[dict]:
    out = []
    # 1. ключи характеристик: разное написание одного параметра
    forms: dict[str, Counter] = defaultdict(Counter)
    for p in prods:
        if p["site"] != "m22.ru":
            continue
        for g, v in (db.uj(p["specs_json"], {}) or {}).items():
            keys = v.keys() if isinstance(v, dict) else [g]
            for k in keys:
                forms[_norm_spec_key(k)][k] += 1
    for norm, cnt in forms.items():
        if len(cnt) > 1:
            main = cnt.most_common(1)[0][0]
            others = ", ".join(f"«{k}» ×{v}" for k, v in cnt.most_common()[1:])
            out.append(_item("P3", "structure", f"Характеристика «{main}» пишется по-разному", f"Использовать одно название параметра во всех карточках: «{main}» ({cnt[main]} карточек); сейчас также {others}. Разные названия ломают сравнение товаров и фильтры.",
                             site="m22.ru", key=f"speckey:{norm}"))
    # латиница/кириллица внутри названия характеристики (LСD, Мute)
    seen = set()
    for p in prods:
        for g, v in (db.uj(p["specs_json"], {}) or {}).items():
            for k in (v.keys() if isinstance(v, dict) else [g]):
                if k in seen:
                    continue
                for w in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", k):
                    if re.search(r"[А-Яа-яЁё]", w) and re.search(r"[A-Za-z]", w):
                        seen.add(k)
                        out.append(_item("P2", "structure", f"Смешение алфавитов в названии характеристики: «{k}»", f"В слове «{w}» часть букв латинские, часть русские. Переписать одним алфавитом (например, «LCD экран», «Функция Mute»).",
                                         site=p["site"], product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"speckey_mixed:{k}"))
                        break
    # 2. одинаковые модели в разных разделах сайта
    by_series: dict[tuple, Counter] = defaultdict(Counter)
    ex: dict[tuple, dict] = {}
    for p in prods:
        if p["site"] != "m22.ru" or p["parent_url"] or not p["site_category_path"]:
            continue
        key = (p["category_slug"], p["kind"])
        by_series[key][p["site_category_path"]] += 1
        ex.setdefault((key, p["site_category_path"]), p)
    for key, cnt in by_series.items():
        if len(cnt) > 1 and key[0] in ("disposable_headphones", "reusable_headphones", "microphones_guide", "charging_cases"):
            main = cnt.most_common(1)[0][0]
            for path, n in cnt.items():
                if path == main or n > cnt[main] // 2 + 1:
                    continue
                p = ex[(key, path)]
                out.append(_item("P2", "structure", f"Товар лежит в разделе «{path.strip(' /')}», а однотипные — в «{main.strip(' /')}»", "Перенести в общий раздел или продублировать привязку: покупатель ищет все наушники/зарядки в одном месте.",
                                 site="m22.ru", product_id=p["id"], name=p["name"], url=p["url"], sku=p["sku"], key=f"section:{p['id']}"))
    return out


def check_sources(conn: sqlite3.Connection) -> list[dict]:
    out = []
    rows = db.rows(conn, "SELECT source_key, url, error, MAX(ts) ts FROM error_log WHERE source_key IN ('m22.ru','radiosync.ru') AND ts >= datetime('now','-14 days') GROUP BY url ORDER BY ts DESC LIMIT 30")
    robots = [r for r in rows if "robots" in (r["error"] or "")]
    other = [r for r in rows if "robots" not in (r["error"] or "")]
    if robots:
        out.append(_item("P2", "source", f"robots.txt radiosync.ru закрывает {len(robots)} страниц товаров от индексации", "Раздел одноразовых наушников на radiosync.ru запрещён в robots.txt: поисковики его не видят, радар не собирает. Открыть путь в настройках Tilda (SEO → robots.txt) или перенести товары в открытый раздел.",
                         site="radiosync.ru", url=robots[0]["url"], evidence="; ".join(r["url"].split("/")[-1][:40] for r in robots[:5]), key="robots_rs"))
    for r in other[:10]:
        out.append(_item("P2", "source", "Страница товара не разобралась при сборе", f"Открыть страницу вручную: {r['error'][:120]}. Если страница удалена — настроить редирект на замену (иначе 404 в поиске).",
                         site=r["source_key"], url=r["url"], evidence=r["ts"], key=f"err:{r['url']}"))
    return out


AGGREGATE_MIN = 6


def _aggregate(items: list[dict]) -> list[dict]:
    """Массовые однотипные замечания (одно и то же в 6+ карточках) сворачиваются в одну строку со списком карточек:
    это почти всегда одна причина в шаблоне сайта, а не 30 отдельных ошибок."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[it["what"]].append(it)
    out = []
    for what, its in groups.items():
        if len(its) >= AGGREGATE_MIN and its[0]["product_id"] and its[0]["priority"] == "P3" or (what.startswith("Есть на m22.ru") and len(its) >= AGGREGATE_MIN):
            first = its[0]
            names = "; ".join(i["name"][:45] for i in its[:12]) + (f" … и ещё {len(its) - 12}" if len(its) > 12 else "")
            out.append({**first, "what": f"{what} — {len(its)} карточек", "name": f"{len(its)} карточек ({first['site']})", "product_id": None, "url": first["url"], "sku": None,
                        "evidence": names, "key": f"agg:{what}", "agg_ids": [i["product_id"] for i in its], "cards": [c for i in its[:8] for c in i["cards"][:1]]})
        else:
            out.extend(its)
    return out


def run(conn: sqlite3.Connection) -> dict:
    prods = db.rows(conn, "SELECT * FROM m22_products WHERE is_active=1 ORDER BY site, name")
    items = check_prices_and_stock(prods) + check_cross_site(prods) + check_cards(prods) + check_naming(prods) + check_typos(prods) + check_structure(prods) + check_sources(conn)
    items = _aggregate(items)
    dismissed = set(db.uj(db.get_setting(conn, "site_audit_dismissed"), []) or [])
    for it in items:
        it["dismissed"] = it["key"] in dismissed
        if not it.get("category_slug") and it["product_id"]:
            it["category_slug"] = next((p["category_slug"] for p in prods if p["id"] == it["product_id"]), "") or ""
        it["category"] = CATEGORY_NAMES.get(it.get("category_slug") or "", "общее" if not it.get("category_slug") else it["category_slug"])
        if it["product_id"] and not it["cards"]:
            pp = next((p for p in prods if p["id"] == it["product_id"]), None)
            if pp:
                it["cards"] = [_card(pp)]
    order = {"P1": 0, "P2": 1, "P3": 2}
    items.sort(key=lambda x: (order[x["priority"]], x["group_name"], x["site"], x["name"]))
    active = [i for i in items if not i["dismissed"]]
    counts = {p: sum(1 for i in active if i["priority"] == p) for p in ("P1", "P2", "P3")}
    by_site = Counter(i["site"] for i in active)
    by_group = Counter(i["group_name"] for i in active)
    by_cat: dict[str, dict] = {}
    for i in active:
        c = by_cat.setdefault(i.get("category_slug") or "", {"slug": i.get("category_slug") or "", "name": i["category"], "P1": 0, "P2": 0, "P3": 0, "n": 0})
        c[i["priority"]] += 1
        c["n"] += 1
    cat_order = {s: n for n, s in enumerate(CATEGORY_NAMES)}
    cats = sorted(by_cat.values(), key=lambda c: (c["slug"] not in CORE_CATEGORIES, cat_order.get(c["slug"], 99)))
    return {"items": items, "counts": counts, "total": len(active), "dismissed": len(items) - len(active), "by_site": by_site, "by_group": by_group, "cats": cats,
            "products": len([p for p in prods if not p["parent_url"]]), "last_fetch": max((p["fetched_at"] or "" for p in prods), default=None)}


def export_rows(conn: sqlite3.Connection) -> list[dict]:
    return [{"приоритет": i["priority"], "группа": i["group_name"], "сайт": i["site"], "товар": i["name"], "артикул": i["sku"], "что не так": i["what"], "что сделать": i["fix"],
             "категория": i["category"], "подтверждение": i["evidence"], "ссылка": i["url"], "карточки": " | ".join(f"{c['name']} ({c['url']})" for c in i["cards"]), "скрыто": "да" if i["dismissed"] else ""} for i in run(conn)["items"]]


def stock_report(conn: sqlite3.Connection) -> dict:
    """Что сейчас нельзя купить: нет в наличии, предзаказ, без цены, наличие не указано. По категориям, с фото и ссылками."""
    prods = db.rows(conn, "SELECT * FROM m22_products WHERE is_active=1 AND parent_url IS NULL AND category_slug != 'rental' ORDER BY category_slug, name")
    status_of = {"OutOfStock": "нет в наличии", "SoldOut": "распродано", "PreOrder": "предзаказ", "Discontinued": "снят с продажи"}
    rows = []
    for p in prods:
        st = status_of.get(p["availability"])
        if not st and p["price"] is None:
            st = "без цены"
        if not st and p["availability"] is None:
            st = "наличие не указано"
        if not st:
            continue
        twin = None
        if p["sku"]:
            twin = db.row(conn, "SELECT id, site, url, availability, price FROM m22_products WHERE is_active=1 AND sku=? AND site!=? AND id!=? LIMIT 1", (p["sku"], p["site"], p["id"]))
        last_in = db.row(conn, "SELECT MAX(observed_at) t FROM m22_price_history WHERE product_id=? AND availability='InStock'", (p["id"],))
        rows.append({**_card(p), "status": st, "category_slug": p["category_slug"] or "", "category": CATEGORY_NAMES.get(p["category_slug"] or "", "без категории"), "kind": p["kind"],
                     "twin": ({**twin, "availability": AVAIL.get(twin["availability"], twin["availability"])} if twin else None), "last_in_stock": (last_in or {}).get("t"),
                     "core": (p["category_slug"] in CORE_CATEGORIES)})
    cats: dict[str, dict] = {}
    cat_order = {sl: n for n, sl in enumerate(CATEGORY_NAMES)}
    for r in rows:
        c = cats.setdefault(r["category_slug"], {"slug": r["category_slug"], "name": r["category"], "core": r["core"], "rows": [], "total": 0})
        c["rows"].append(r)
    totals = {r["category_slug"]: r["n"] for r in db.rows(conn, "SELECT category_slug, COUNT(*) n FROM m22_products WHERE is_active=1 AND parent_url IS NULL GROUP BY 1")}
    for c in cats.values():
        c["total"] = totals.get(c["slug"], 0)
    by_status = Counter(r["status"] for r in rows)
    return {"cats": sorted(cats.values(), key=lambda c: (not c["core"], cat_order.get(c["slug"], 99))), "rows": rows, "by_status": by_status,
            "by_site": Counter(r["site"] for r in rows), "total": len(rows), "products": len(prods)}
