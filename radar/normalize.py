"""Нормализация: цены, ключи моделей, категории, тип товара, вместимость комплектов."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

PRICE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?|\d{1,3}(?:\.\d{3})+|\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|р\.|RUB|rub)", re.I)


def parse_price(text: str | None) -> float | None:
    """Извлекает цену в рублях из строки вроде '32 500 ₽' или '32500,00'."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    if not s:
        return None
    m = PRICE_RE.search(s)
    candidate = m.group(1) if m else s
    candidate = candidate.replace(" ", "").replace(" ", "").replace(" ", "")
    # 32500,00 / 32500.00 / 32.500
    if re.fullmatch(r"\d+[.,]\d{1,2}", candidate):
        candidate = candidate.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", candidate):
        candidate = re.sub(r"[.,]", "", candidate)
    else:
        candidate = re.sub(r"[^\d.]", "", candidate)
    try:
        value = float(candidate)
    except ValueError:
        return None
    if value <= 0 or value > 1e8:
        return None
    return value


def clean_text(s: str | None) -> str:
    if s is None or s == "":
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    for ch in (" ", "ㅤ", "ᅠ", " ", " "):
        s = s.replace(ch, " ")
    s = s.replace("​", "")
    return re.sub(r"\s+", " ", s).strip()


BRANDS = [
    "radiosync", "kromix", "retekess", "okayo", "sennheiser", "williams sound", "williams av", "listen tech",
    "listentech", "conference pro", "conferencepro", "reinvox", "crystalsound", "crystal sound", "touraudio", "mipro", "takstar", "bosch", "shure", "gid-lux", "гид-люкс", "гид люкс", "vox tour", "voxtour",
    "tourtalk", "tour talk", "axitour", "beyerdynamic", "hixio", "synco", "baofeng",
    "pyle", "ankuka", "anleon", "wpt", "ekkotek", "auvisio", "soundpeats", "tonline", "тонлайн", "wintal",
]


def detect_brand(name: str, brand_hint: str | None = None) -> str | None:
    if brand_hint and brand_hint.strip():
        return brand_hint.strip()
    low = name.lower()
    for b in BRANDS:
        if b in low:
            return b.title() if b.isascii() else b
    return None


MODEL_PATTERNS = [
    r"\bSGTR[\s-]?(\d{2}[A-Z]?)\b",
    r"\bUG[\s-]?(\d{2})\b",
    r"\bAG[\s-]?(\d{3})\b",
    r"\bTT[\s-]?(\d{3}[A-Z]?)\b",
    r"\bT(\d{3}[A-Z]?)\b",
    r"\bRS[\s-]?(\d{2})\b",
    r"\bRG[\s-]?(\d{2,4}[A-Z]?)\b",
    r"\bWT[\s-]?(\d{3,4}[A-Z]?)\b",
    r"\bEJ[\s-]?(\d{3,4}[A-Z]?)\b",
    r"\bMT[\s-]?(\d{3,4}[A-Z]?)\b",
    r"\bDR[\s-]?(\d{3,4}[A-Z]?)\b",
    r"\bK[\s-]?(\d{4}[A-Z]?)\b",
    r"\bX22(\d{2,4}[A-Z]?)\b",
    r"\b(\d{3,4}[A-Z]?)\b",
]


def model_key(name: str, sku: str | None = None) -> str | None:
    """Нормализованный ключ модели: 'SGTR02', 'UG10', 'TT106', 'AG300' и т.п."""
    text = clean_text(name).upper()
    m = re.search(r"\b(SGTR)\s?-?\s?(\d{2})\s?([A-Z])?\b", text)
    if m:
        return f"SGTR{m.group(2)}{m.group(3) or ''}"
    m = re.search(r"\b(UG)\s?-?\s?(\d{2})\b", text)
    if m:
        return f"UG{m.group(2)}"
    m = re.search(r"\b(AG)\s?-?\s?(\d{3})\b", text)
    if m:
        return f"AG{m.group(2)}"
    m = re.search(r"\b(HDE|SR|EK|SK|TT|RS|RG|WT|EJ|MT|DR|X22|RT|BF|UV|CL|SWB|STDB|STRB|MC|CP|PW|MS|WS|LT|LR|DLT|DLR|ATS|FT|ES)\s?-?\s?(\d{2,5}[A-Z]{0,2})\b", text)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    m = re.search(r"\b(T|K|R|W|E)\s?-?\s?(\d{3,5}[A-Z]{0,2})\b", text)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    m = re.search(r"\b([A-Z]{2,6})\s?-?\s?(\d{2,5}[A-Z]{0,2})\b", text)
    if m and m.group(1) not in {"USB", "AUX", "LED", "IP", "ГГЦ", "МГЦ", "MHZ", "GHZ", "ДО", "НА", "ИЗ", "ОТ", "ДЛЯ", "ПО", "VHF", "UHF", "FM", "AM", "RF"}:
        return f"{m.group(1)}{m.group(2)}"
    # модели-слова после бренда (Reinvox DUO / PRO / AiR и т.п.)
    m = re.search(r"\b(REINVOX|RETEKESS|OKAYO|SENNHEISER|WILLIAMS|LISTEN|MIPRO|TAKSTAR|CRYSTALSOUND|TOURTALK|AXITOUR)\s+([A-Z][A-Z0-9-]{1,12})\b", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    if sku and re.search(r"[A-Z]", clean_text(sku).upper()):
        return clean_text(sku).upper()[:32]
    return None


# --- Категории контура исследования (slug -> ключевые слова) ---
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("staff_call", ["кнопк вызова", "кнопка вызова", "кнопки вызова", "вызова персонала", "вызов персонала", "вызова официанта", "вызова медсестры", "вызова мед", "пейджер", "пейджинг", "paging",
                    "табло оповещ", "оповещения о входе", "оповещение о входе", "оповещения клиентов", "оповещения о готовности", "хост-ресивер", "система вызова", "call button", "calling system", "nurse call", "часы-пейджер", "датчик движения"]),
    ("sync_translation", ["синхронн", "перевод", "interpret", "translation", "переводчик", "делегац", "integrus", "кабина", "кабины", "кабин ", "шушотаж", "тифло"]),
    ("audiogid", ["аудиогид", "audio guide", "audioguide", "audiogid", "триггер", "trigger tag", "мультимедиа гид", "автогид"]),
    ("disposable_headphones", ["одноразов", "disposable", "гигиеническ", "чехлы для наушник", "чехлы на наушник", "чехол для наушник", "губчат", "накладки на амбушюр"]),
    ("reusable_headphones", ["многоразов", "накладные наушники", "наушник", "headphone", "earphone", "earpiece"]),
    ("charging_cases", ["кейс", "док-станц", "докстанц", "dock", "case", "зарядн", "charger", "charging", "сумк", "шкаф", "бокс для зарядки", "usb порт"]),
    ("voice_amplifier", ["голосовой усилитель", "усилитель голоса", "усилители голоса", "мегафон", "voice amplifier", "громкоговорител"]),
    ("microphones_guide", ["микрофон", "гарнитура", "headset", "microphone", "mic ", "петличн", "головной", "lavalier"]),
    ("rental", ["аренда", "арендовать", "аренду", "прокат", "rent", "rental"]),
    ("kits_solutions", ["комплексное решение", "готовое решение", "готовые решения", "готовые комплекты", "прайм", "профи", "комплект для", "solution", "решение"]),
    ("intercom_events", ["интерком", "intercom", "xtalk", "служебная связь"]),
    ("radio_walkie", ["раци", "walkie", "baofeng", "retevis rt", "тангент", "программатор"]),
    ("adjacent_new", ["bluetooth", "блютуз"]),
    ("radiogid", ["радиогид", "радио гид", "радио-гид", "radioguide", "radio guide", "tour guide", "экскурсионн", "передатчик", "приёмник", "приемник", "transmitter", "receiver", "шептал", "whisper", "гид-переводчик", "радиосистема для экскурс", "радиосистемы для экскурс", "тургид",
                  "система гида", "гида-экскурсовода", "шепчущ", "оборудование для гид", "групповое общение", "tt1", "tt0"]),
]

CATEGORY_NAMES = {
    "radiogid": "Радиогиды (экскурсионные радиосистемы)",
    "staff_call": "Вызов персонала, пейджеры, табло",
    "audiogid": "Аудиогиды",
    "sync_translation": "Системы синхронного перевода",
    "disposable_headphones": "Одноразовые наушники",
    "reusable_headphones": "Многоразовые наушники и гарнитуры",
    "microphones_guide": "Микрофоны для экскурсоводов",
    "charging_cases": "Зарядные кейсы, док-станции, сумки",
    "kits_solutions": "Готовые комплекты и B2B-решения",
    "rental": "Аренда экскурсионного оборудования",
    "industrial_tours": "Промышленные экскурсии",
    "museum_equipment": "Оборудование для музеев и выставок",
    "conference_delegations": "Конференции и делегации",
    "intercom_events": "Интерком для мероприятий",
    "voice_amplifier": "Голосовые усилители для гида",
    "radio_walkie": "Рации для гидов и мероприятий",
    "substitutes_apps": "Технологические заменители (приложения, QR, Bluetooth)",
    "adjacent_new": "Смежные и новые направления",
}


def _match_rules(text: str) -> str | None:
    for slug, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return slug
    return None


HEAD_RULES = [
    ("staff_call", ("кнопка вызова", "кнопки вызова", "комплект вызова", "комплекты вызова", "система вызова", "системы вызова", "пейджер", "часы-пейджер", "часы пейджер", "табло", "комплект оповещения",
                    "система оповещения", "хост-ресивер", "беспроводной приёмник сигнала", "беспроводной приемник сигнала", "система беспроводных вызовов")),
    ("radiogid", ("радиогид", "радио-гид", "радио гид", "экскурсионная система", "tour guide", "радиосистема для экскурс", "система гид", "гид-переводчик", "шептал",
                  "беспроводная система гид", "оборудование для гид", "шепчущ", "система для экскурс", "экскурсионное оборудование")),
    ("sync_translation", ("комплект для синхронного", "система синхронного", "оборудование для синхронного", "синхронный перевод", "система перевода")),
    ("audiogid", ("аудиогид", "audio guide", "audioguide", "мультимедиа гид")),
    ("rental", ("аренда", "прокат")),
    ("kits_solutions", ("комплексное решение", "готовое решение")),
    ("voice_amplifier", ("усилитель голоса", "голосовой усилитель", "мегафон")),
]


# Подсказки по кодам моделей известных брендов: (регулярное выражение по названию в нижнем регистре, категория, тип изделия).
# Проверяются до общих ключевых слов — у многих продавцов название состоит из одного кода («Reinvox A-200», «SPBAUDIO MAX-1»).
MODEL_HINTS: list[tuple[re.Pattern, str | None, str]] = [(re.compile(p, re.I), c, k) for p, c, k in [
    (r"reinvox\s*(?:it|rt|lt|upl)-?\d", "audiogid", "case_charger"),
    (r"reinvox\s*a-?(?:150|210|410)", "audiogid", "case_charger"),
    (r"reinvox\s*a-?\d", "audiogid", "audioguide"),
    (r"reinvox\s*r-?\d", "radiogid", "receiver"),
    (r"reinvox\s*(?:t|wt)-?\d+t?\b", "radiogid", "transmitter"),
    (r"reinvox\s*s-?\d", "sync_translation", "transmitter"),
    (r"reinvox\s*c-?\d", "charging_cases", "case_charger"),
    (r"reinvox\s*de-?\d", "disposable_headphones", "headphones"),
    (r"reinvox\s*p-?\d", "reusable_headphones", "headphones"),
    (r"reinvox\s*m-?\d", "microphones_guide", "microphone"),
    (r"reinvox\s*(?:cs-?\d|concept|elegant)", None, "other"),
    (r"spbaudio\s*(?:a-1|di-?\d|b-10)", "audiogid", "audioguide"),
    (r"spbaudio\s*m-1|маячок", "audiogid", "accessory"),
    (r"spbaudio\s*b-?\d{2,}", "charging_cases", "case_charger"),
    (r"spbaudio\s*(?:max|ec|eurocab|co-|c-9000|v-1)", "sync_translation", "other"),
    (r"spbaudio\s*tiflo|тифло", "sync_translation", "other"),
    (r"spbaudio\s*nl-|индукционн\w+ петл", "adjacent_new", "other"),
    (r"soolai\s*(?:ag|sa|rft)", "audiogid", "audioguide"),
    (r"synexis\s*c\s?\d", "charging_cases", "case_charger"),
    (r"synexis\s*(?:th|tp)", "radiogid", "transmitter"),
    (r"synexis\s*rp", "radiogid", "receiver"),
    (r"synexis\s*chp|dt394", "reusable_headphones", "headphones"),
    (r"synexis", "radiogid", "system"),
    (r"iris\s*rp", "sync_translation", "receiver"),
    (r"iris\s*(?:ts|ef)", "sync_translation", "transmitter"),
    (r"iris\s*c\s?\d", "charging_cases", "case_charger"),
    (r"beyerdynamic\s*iris", "sync_translation", "system"),
    (r"cromi\s*r-?\d", "sync_translation", "receiver"),
    (r"cromi\s*\d{3}t", "sync_translation", "transmitter"),
    (r"cromi\s*(?:cab|sl\s*in|sm-|h-)", "sync_translation", "other"),
    (r"whisper\s*cube|multi-caisses|кабин[аы]\s+(?:для\s+)?(?:синхронн\w+\s+)?перевод|eurocab|\bec-\d|пульт\w*\s+(?:синхронн\w+\s+)?переводчик", "sync_translation", "other"),
    (r"retekess\s*(?:tt|t1\d\d)", "radiogid", "system"),
    # Retekess: TD — системы вызова/пейджеры, TH — кнопки вызова, FT — FM-передатчики, TA — ушной мониторинг, SU — усилители: вне контура; TR — рации
    (r"\btd\s?\d{3}[a-z]?\b|\bth\s?\d{3}\b", "staff_call", "system"),
    (r"\bft\s?1\d\b|\bpr\s?13\b|\bsu\s?\d{3}\b|\bta\s?\d{3}\b|\bt-ac\d", None, "other"),
    (r"retekess\s*(?:tr|rt)\s?\d{3}|\btr\s?\d{3}\b.*(?:раци|radio)", "radio_walkie", "other"),
    (r"громкоговоритель|мегафон|усилитель голоса|rolton|rоlton|shidu|zoweetek", "voice_amplifier", "other"),
]]
OUT_OF_SCOPE_HEADS = ("видеопроектор", "проектор", "радиоприемник", "радиоприёмник", "кейсы и кофры", "рэковые", "конференц-систем",
                      "кнопка экстренного", "система контроля доступа", "портативное радио", "экранный дисплей", "pos-", "антенна для пейджер")


OUT_OF_SCOPE_ANY = re.compile(r"аудиоконференц|конференц[- ]?систем|видеоконференц|лазерн\w+ указк|проектор|видеостен|led[- ]экран|светодиодн\w+ экран|микшерн|рэков|кофр|"
                              r"\bfm[- ]радио|\bam\s?fm\b|вызов гостя|оповещения клиентов|звуковое оборудование|аудиооборудование", re.I)


def model_hint(name: str) -> tuple[str | None, str] | None:
    low = clean_text(name).lower()
    if OUT_OF_SCOPE_ANY.search(low):
        return None, "other"
    if re.search(r"комплект радиокласса|\bset-?\d+\s*\+\s*\d|радиоклас", low):
        return "radiogid", "system"
    for rx, cat, kind in MODEL_HINTS:
        if rx.search(low):
            return cat, kind
    return None


def classify_category(name: str, description: str | None = None, site_path: str | None = None) -> str | None:
    """Категория: сначала по началу названия (тип изделия), затем по коду модели, ключевым словам названия, разделу сайта, описанию."""
    head = clean_text(name).lower()[:45]
    if head.startswith(OUT_OF_SCOPE_HEADS):
        return None
    if site_path and site_path.lower().startswith(("рации", "рация")):
        return "radio_walkie"  # раздел сайта M22 «Рации и аксессуары»: гарнитуры, тангенты, докстанции для раций — тоже к рациям
    if head.startswith(("аренда", "аренд", "прокат")):
        return "rental"
    if re.match(r"^(?:нет бренда\s+)?(?:\w+\s+){0,2}наушник", head) or head.startswith(("одноразов", "многоразов")):
        return "disposable_headphones" if "одноразов" in head else "reusable_headphones"
    for slug, starts in HEAD_RULES:
        if head.startswith(starts):
            return slug
    hint = model_hint(name)
    if hint is not None:
        return hint[0]
    for text in (name, site_path, description):
        if not text:
            continue
        slug = _match_rules(text.lower())
        if slug:
            return slug
    return None


def detect_kind(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("комплексное решение", "готовое решение", "прайм", "профи")):
        return "kit"
    if low.startswith(("радиогид система", "радиогид-система", "система радиогид", "радиогид", "экскурсионная система", "tour guide system", "система синхронного", "комплект для синхронного", "комплект радиогид", "радиосистема")):
        return "system"
    if "аренда" in low or low.startswith("rent"):
        return "rental"
    has_tx = "передатчик" in low or "transmitter" in low
    has_rx = "приёмник" in low or "приемник" in low or "receiver" in low
    head = low.split(" для ")[0].split(" к ")[0]
    # аксессуары: «шнурок для радиогида», «чехол для приёмника», «антенна», «кабель», «адаптер» — по началу названия, до «для»
    if head.startswith(("шнурок", "шейный шнурок", "ремешок", "чехол", "чехлы", "накладк", "амбушюр", "кабель", "шнур", "антенна", "адаптер", "блок питания",
                        "аккумулятор", "батаре", "клипса", "крепление", "держатель", "лямка", "ремень", "лента", "стойка", "подставка", "ветрозащит", "поролон")) \
            or any(w in head for w in (" чехол", " шнурок", " ремешок", " накладк", " амбушюр", " клипса", " крепление", " антенна", " адаптер", " аккумулятор")):
        return "accessory"
    if head.startswith(("передатчик", "transmitter")) and not has_rx:
        return "transmitter"
    if head.startswith(("приёмник", "приемник", "receiver")) and not has_tx:
        return "receiver"
    if (has_tx and has_rx) or (("система" in low or "system" in low or "комплект" in low or "kit" in low or " set" in low) and (has_tx or has_rx)):
        return "system"
    if has_tx:
        return "transmitter"
    if has_rx:
        return "receiver"
    if any(k in low for k in ("кейс", "докстанц", "док-станц", "сумк", "case", "dock", "зарядн", "charger")):
        return "case_charger"
    if "наушник" in low or "headphone" in low or "earphone" in low:
        return "accessory" if low.startswith(("чехл", "накладк", "амбушюр")) else "headphones"
    if "чехл" in low or "накладк" in low or "амбушюр" in low:
        return "accessory"
    if "микрофон" in low or "гарнитур" in low or "microphone" in low or "headset" in low:
        return "microphone"
    if "аудиогид" in low or "audio guide" in low or "audioguide" in low:
        return "audioguide"
    if "триггер" in low or "trigger" in low:
        return "accessory"
    if "аренда" in low or "rent" in low:
        return "rental"
    hint = model_hint(name)
    if hint is not None:
        return hint[1]
    if any(k in low for k in ("система", "system", "радиогид", "radioguide", "tour guide", "комплект", "kit", "set")):
        return "system"
    return "other"


CAPACITY_RE = re.compile(r"(?:на|для|до)\s*(\d{1,3})\s*(?:персон|человек|чел\.?|экскурсант|слушател|участник|приемник|приёмник|устройств|слот)", re.I)
CAPACITY_RE2 = re.compile(r"(\d{1,3})\s*(?:персон|чел|экскурсант|приемник|приёмник|receivers?|слот|устройств)", re.I)
CAPACITY_RE3 = re.compile(r"\b1\s*[\+x×]\s*(\d{1,3})\b|\b(\d{1,3})\s*(?:pcs|шт)\b", re.I)


def detect_capacity(name: str, description: str | None = None) -> int | None:
    """Вместимость комплекта — только из названия (описание перечисляет все варианты и вводит в заблуждение)."""
    for rx in (CAPACITY_RE, CAPACITY_RE2, CAPACITY_RE3):
        m = rx.search(name)
        if m:
            g = next((x for x in m.groups() if x), None)
            if g and g.isdigit():
                return int(g)
    return None


def content_hash(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def is_in_scope(name: str, category_slug: str | None, site_path: str | None = None) -> bool:
    """Контур исследования: экскурсионная деятельность и связанные B2B-решения."""
    if category_slug in (None, "radio_walkie"):
        # рации оставляем как смежную категорию (гиды/мероприятия), но только «для гидов»
        return category_slug == "radio_walkie"
    low = f"{name} {site_path or ''}".lower()
    out_of_scope_markers = ("игровая гарнитура", "пусковое", "пуско-зарядн", "тент", "автомобильн", "автотовар")
    if any(m in low for m in out_of_scope_markers):
        return False
    return True
