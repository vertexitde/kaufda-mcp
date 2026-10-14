"""Turn raw kaufDA payloads into compact, model friendly dicts."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

VIEWER_URL = "https://www.kaufda.de/contentViewer/static/{bid}?page={page}"
# kaufDA's clipping of an offer from the brochure page (with price), keyed by offer id.
CUTOUT_URL = "https://content-media.bonial.biz/{oid}/main.jpg"

# Publishers that show up in search results but are not shops.
IGNORED_PUBLISHERS = {"rezepte"}

DEAL_LABELS = {
    "SALES_PRICE": "Angebotspreis",
    "SPECIAL_PRICE": "Sonderpreis",
    "REGULAR_PRICE": "Normalpreis",
    "RECOMMENDED_RETAIL_PRICE": "UVP",
    "OTHER": "Sonstiges",
}


# ---------------------------------------------------------------- time

def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def parse_ts(value: str | None) -> datetime | None:
    """Parse kaufDA timestamps ("2026-10-11T22:00:00.000+0000") into Berlin time."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BERLIN)
    return parsed.astimezone(BERLIN)


def end_day(dt: datetime) -> date:
    # An end of exactly midnight belongs to the previous day (Sa 24:00, not So 00:00).
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return (dt - timedelta(seconds=1)).date()
    return dt.date()


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m.}"


def parse_day(value: str | None) -> date | None:
    """Accept ISO dates and a few German shortcuts."""
    if not value:
        return None
    text = value.strip().lower()
    today = now_berlin().date()
    shortcuts = {"heute": 0, "today": 0, "morgen": 1, "tomorrow": 1, "übermorgen": 2}
    if text in shortcuts:
        return today + timedelta(days=shortcuts[text])
    for weekday_index, name in enumerate(
        ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
    ):
        if text in (name, name[:2]):
            delta = (weekday_index - today.weekday()) % 7
            return today + timedelta(days=delta)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?", text)
    if match:
        candidate = date(today.year, int(match.group(2)), int(match.group(1)))
        if candidate < today - timedelta(days=60):
            candidate = candidate.replace(year=today.year + 1)
        return candidate
    raise ValueError(f"Datum nicht verstanden: {value!r} (erwartet z.B. 2026-10-19, 19.10., heute, morgen, Montag)")


class Validity:
    def __init__(self, start: datetime | None, end: datetime | None):
        self.start = start
        self.end = end

    @classmethod
    def from_strings(cls, start: str | None, end: str | None) -> "Validity":
        return cls(parse_ts(start), parse_ts(end))

    @property
    def known(self) -> bool:
        return self.start is not None or self.end is not None

    def status(self, now: datetime | None = None) -> str:
        now = now or now_berlin()
        if self.end and self.end < now:
            return "abgelaufen"
        if self.start and self.start > now:
            return "demnächst"
        return "aktiv"

    def covers(self, day: date) -> bool:
        if self.start and self.start.date() > day:
            return False
        if self.end and end_day(self.end) < day:
            return False
        return True

    def matches(self, when: str, valid_on: date | None, now: datetime | None = None) -> bool:
        if valid_on is not None:
            return self.covers(valid_on)
        status = self.status(now)
        if status == "abgelaufen":
            return False
        if when == "now":
            return status == "aktiv"
        if when == "upcoming":
            return status == "demnächst"
        return True

    def describe(self, now: datetime | None = None) -> dict[str, Any]:
        if not self.known:
            return {"validity": "unbekannt"}
        out: dict[str, Any] = {}
        parts = []
        if self.start:
            out["valid_from"] = self.start.date().isoformat()
            parts.append(fmt_day(self.start.date()))
        if self.end:
            last = end_day(self.end)
            out["valid_until"] = last.isoformat()
            parts.append(fmt_day(last))
        out["validity"] = " bis ".join(parts)
        now = now or now_berlin()
        today = now.date()
        status = self.status(now)
        out["valid_today"] = status == "aktiv"
        if status == "demnächst" and self.start:
            days = (self.start.date() - today).days
            out["starts_in_days"] = days
            if days == 0:
                when = f"heute ab {self.start:%H:%M} Uhr"
            elif days == 1:
                when = f"ab morgen {fmt_day(self.start.date())}"
            else:
                when = f"ab {fmt_day(self.start.date())} (in {days} Tagen)"
            out["status"] = f"demnächst, {when}"
        elif status == "aktiv" and self.end and end_day(self.end) == today:
            out["status"] = "aktiv, endet heute"
        elif status == "aktiv" and self.end and end_day(self.end) == today + timedelta(days=1):
            out["status"] = "aktiv, endet morgen"
        else:
            out["status"] = status
        return out


# ---------------------------------------------------------------- prices

def money(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def fmt_eur(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}".replace(".", ",") + " €"


_UNIT_ALIASES = {
    "kg": ("kg", 1.0), "g": ("kg", 1000.0),
    "l": ("l", 1.0), "ltr": ("l", 1.0), "ml": ("l", 1000.0), "cl": ("l", 100.0),
    "stück": ("Stück", 1.0), "stk": ("Stück", 1.0), "st": ("Stück", 1.0),
    "wl": ("WL", 1.0), "m": ("m", 1.0), "m²": ("m²", 1.0), "qm": ("m²", 1.0),
}


def parse_unit_price(text: str | None) -> tuple[str, float] | None:
    """'1 kg = 5,16' / '(1 kg = 8.45/6.76)' / '100 g = 1,20' -> ('kg', 5.16)."""
    if not text:
        return None
    t = text.lower()
    t = re.sub(r"(?<!\d)[-–][.,](\d)", r"0,\1", t)   # "-,81" -> "0,81"
    t = re.sub(r"(\d)[.,][-–]", r"\1,00", t)         # "1.-"  -> "1,00"
    t = t.replace("liter", "l").replace(",", ".")
    unit_pattern = r"(kg|ltr|ml|cl|g|l|stück|stk|st|wl|m²|qm|m)"
    m = re.search(rf"(?:(\d+(?:\.\d+)?)\s*)?{unit_pattern}\.?\s*=\s*((?:\d+(?:\.\d+)?)(?:\s*[-–/]\s*\d+(?:\.\d+)?)*)", t)
    if m:
        qty = float(m.group(1) or 1.0)
        unit = m.group(2)
        values = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(3))]
    else:
        m = re.search(rf"((?:\d+(?:\.\d+)?\s*[-–]\s*)*\d+(?:\.\d+)?)\s*(?:€|eur)?\s*/\s*(?:(\d+(?:\.\d+)?)\s*)?"
                      rf"{unit_pattern}\b", t)
        if not m:
            return None
        values = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(1))]
        qty = float(m.group(2) or 1.0)
        unit = m.group(3)
    if not values or qty <= 0 or unit not in _UNIT_ALIASES:
        return None
    base, factor = _UNIT_ALIASES[unit]
    return base, round(min(values) / qty * factor, 2)


def parse_quantity(*texts: str | None) -> tuple[str, float] | None:
    """Package size from free text: '250 g', 'je 1,5 l', '6x20-g-Packung', '12 x 1 l' -> ('kg', 0.25)."""
    t = " ".join(x for x in texts if x).lower().replace(",", ".")
    if not t:
        return None
    units = {"kg": ("kg", 1.0), "g": ("kg", 0.001), "l": ("l", 1.0), "liter": ("l", 1.0), "ml": ("l", 0.001)}
    m = re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*-?\s*(kg|g|ml|liter|l)\b", t)
    if m:
        base, factor = units[m.group(3)]
        return base, int(m.group(1)) * float(m.group(2)) * factor
    m = re.search(r"(\d+(?:\.\d+)?)(?:\s*/\s*\d+(?:\.\d+)?)*\s*-?\s*(kg|g|ml|liter|l)\b", t)
    if m:
        base, factor = units[m.group(2)]
        amount = float(m.group(1)) * factor
        return (base, amount) if amount > 0 else None
    return None


def estimate_unit_price(offer: dict[str, Any]) -> None:
    """Fill unit_price_value from the package size when kaufDA has no Grundpreis (marked as estimated)."""
    if offer.get("unit_price_value") or not offer.get("price"):
        return
    qty = parse_quantity(offer.get("description"), offer.get("product"))
    if qty:
        offer["unit_price_value"] = {"per": qty[0], "eur": round(offer["price"] / qty[1], 2), "estimated": True}


def _conditions(raw: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            for key, val in item.items():
                text = str(val).strip()
                if isinstance(val, bool) or val in (None, [], {}) or text.lower() in ("", "false", "true", "none", "null"):
                    continue
                out.append(text if key == "other" else f"{key}: {text}")
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- conditions, apps, loyalty

# kaufDA mixes real conditions with unit hints ("Je", "Stück") and with labels for the price you pay
# WITHOUT the app ("Ohne PENNY App", "Preis ohne Treuepunkte"). Rules run on norm()'d text, first match wins.
_COND_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("noise", re.compile(r"^(je|jew|jeweils|stueck|stk|st|ab|nur|bis zu|aktion|xxl|einzelpreis|discountpreis|"
                         r"aktionspreis|angebot|pro kg|pro stueck|auch online|je \w+|\w+ je|stueck packung|"
                         r"1 packung|im \w*pack)$")),
    ("no_app", re.compile(r"\bohne\b.*\b(app|karte|card|plus|treuepunkt\w*)\b|^normalpreis$|^regulaerer preis")),
    ("points", re.compile(r"treuepunkt|sammelpunkt")),
    ("online_only", re.compile(r"\bnur\b.*\bonline\b|online exklusiv|nur im onlineshop")),
    ("loyalty", re.compile(r"\bapp\b|apppreis|lidl plus|netto plus|\bcard\b|\bxtra\b|payback|deutschlandcard|"
                           r"kundenkarte|\bcoupon\b|mitgliedspreis|clubpreis")),
    ("multi_buy", re.compile(r"\bab \d+\b|\b\d+ fuer\b|\b\d+ \d+ gratis\b|mindestens|beim kauf von|"
                             r"^\d+ (stueck|dosen|flaschen|packungen|pack|x)$|\b\d+er pack|"
                             r"\b\d+ (kaufen|zahlen|gratis)\b")),
]

_PROGRAMS = [
    ("lidl", "Lidl Plus"), ("penny", "PENNY App"), ("netto marken", "Netto plus"), ("netto", "Netto App"),
    ("kaufland", "Kaufland Card"), ("nahkauf", "REWE App"), ("rewe", "REWE App"), ("marktkauf", "Marktkauf App"),
    ("edeka", "EDEKA App"), ("famila", "famila App"), ("markant", "Markant App"), ("rossmann", "Rossmann App"),
    ("dm", "dm App"), ("aldi", "ALDI App"), ("globus", "Globus App"), ("norma", "NORMA App"),
]
_GENERIC_WORDS = {"app", "apps", "plus", "card", "karte", "kundenkarte", "der", "die", "das", "mit", "von"}


def classify_condition(text: str | None) -> str:
    """noise | no_app | points | online_only | loyalty | multi_buy | other"""
    t = norm(text)
    if not t:
        return "noise"
    for kind, rule in _COND_RULES:
        if rule.search(t):
            return kind
    return "other"


def _split_conditions(raw: Iterable[Any] | None) -> list[str]:
    parts: list[str] = []
    for text in _conditions(raw):
        parts.extend(p.strip(" *.!") for p in re.split(r"[,;]", text) if p.strip(" *.!"))
    return parts


def loyalty_program(retailer: str | None, texts: Iterable[str] = ()) -> str:
    t = norm(" ".join(texts))
    if "payback" in t:
        return "PAYBACK"
    if "deutschlandcard" in t:
        return "DeutschlandCard"
    r = norm(retailer)
    for key, name in _PROGRAMS:
        if r.startswith(key):
            return name
    return f"{retailer} App" if retailer else "App"


def has_app(apps: Iterable[str] | None, retailer: str | None, program: str | None) -> bool:
    """Does the user use the app/card behind this price? apps are free text like 'Lidl Plus', 'Penny', 'alle'."""
    prog = norm(program)
    for entry in apps or []:
        e = norm(entry)
        if not e:
            continue
        if e in ("alle", "all", "alle apps", "jede", "ja"):
            return True
        if prog and e not in _GENERIC_WORDS and (e == prog or e in prog or prog in e):
            return True
        tokens = [t for t in e.split() if t not in _GENERIC_WORDS]
        if retailer and any(store_matches(t, retailer) for t in tokens):
            return True
    return False


def personalize(offer: dict[str, Any], apps: Iterable[str] | None) -> dict[str, Any]:
    """Add your_price: the price this user pays, given the apps/cards they use."""
    app = offer.get("app_price")
    price = offer.get("price")
    program = (app or {}).get("program")
    if app and has_app(apps, offer.get("retailer"), program) and (price is None or app["price"] < price):
        your, basis = app["price"], f"mit {program}"
    elif price is not None:
        your = price
        basis = "ohne App, Normalpreis" if offer.get("app_only") else ("ohne App" if app else None)
    else:
        offer["your_price_note"] = f"Angebot nur mit {program}, Preis ohne App unbekannt"
        return offer
    offer["your_price"] = your
    if basis:
        offer["your_price_basis"] = basis
    unit = offer.get("unit_price_value") or {}
    ref = price if price is not None else (app or {}).get("price")
    if unit.get("eur") is not None and ref and your != ref:
        offer["your_unit_price"] = {"per": unit["per"], "eur": round(unit["eur"] * your / ref, 2)}
    return offer


def product_app_note(retailer: str | None, *texts: str | None) -> str | None:
    """App coupons and PAYBACK actions that kaufDA only mentions in the product text."""
    t = " ".join(x for x in texts if x)
    if re.search(r"app[- ]?coupon", t, re.I):
        return f"App Coupon: Rabatt nur mit {loyalty_program(retailer)}"
    if re.search(r"payback|°\s?p\b", t, re.I):
        return "PAYBACK Punkte Aktion (nur mit PAYBACK)"
    return None


# ---------------------------------------------------------------- matching

def norm(text: str | None) -> str:
    t = (text or "").casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("é", "e")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def store_matches(wanted: str, name: str | None, publisher_id: str | None = None) -> bool:
    if publisher_id and wanted.strip().upper() == publisher_id.upper():
        return True
    w = norm(wanted).replace(" ", "")
    if not w or not name:
        return False
    full = norm(name)
    compact = full.replace(" ", "")
    if compact == w or compact.startswith(w):
        return True
    if any(token.startswith(w) for token in full.split()):
        return True
    return len(w) >= 4 and w in compact


def any_store_matches(wanted: list[str], name: str | None, publisher_id: str | None = None) -> bool:
    return any(store_matches(w, name, publisher_id) for w in wanted)


def is_ignored_publisher(name: str | None) -> bool:
    return norm(name) in IGNORED_PUBLISHERS


def text_matches(query: str, *fields: str | None) -> bool:
    haystack = norm(" ".join(f for f in fields if f))
    return all(word in haystack for word in norm(query).split())


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def viewer_url(brochure_id: str, page: int | None = None) -> str:
    return VIEWER_URL.format(bid=brochure_id, page=page or 1)


# ---------------------------------------------------------------- brochures

def brochure_summary(content: dict[str, Any], *, distance_km: float | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    publisher = content.get("publisher") or {}
    bid = content.get("contentId") or content.get("id")
    validity = Validity.from_strings(content.get("validFrom"), content.get("validUntil"))
    store = content.get("closestStore") or {}
    store_text = None
    if store:
        street = " ".join(x for x in (store.get("street"), store.get("streetNumber")) if x)
        town = " ".join(x for x in (store.get("zip"), store.get("city")) if x)
        store_text = ", ".join(x for x in (store.get("name"), street, town) if x)
    if distance_km is None and content.get("distance") is not None:
        distance_km = float(content["distance"])
    return compact({
        "brochure_id": bid,
        "retailer": publisher.get("name"),
        "retailer_id": publisher.get("id"),
        "title": content.get("title"),
        **validity.describe(now),
        "pages": content.get("pageCount"),
        "nearest_store": store_text,
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
        "viewer_url": viewer_url(bid) if bid else None,
    })


# ---------------------------------------------------------------- offers

def pick_categories(query: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the recognized categories that really describe the query.

    kaufDA lists related categories first sometimes ('Milch-Eis-Zauber' for 'Milch')."""
    cats = [e for e in entities or [] if e.get("type") == "CATEGORY" and e.get("id")]
    if not cats:
        return []
    q = norm(query)
    exact = [e for e in cats if norm(e.get("value")) == q]
    if exact:
        return exact
    words = set(q.split())
    inside = [e for e in cats if norm(e.get("value")) and set(norm(e.get("value")).split()) <= words]
    return inside or cats[:1]


def offer_from_search(item: dict[str, Any], brochure: dict[str, Any] | None,
                      category_ids: set[str] | None, now: datetime | None = None) -> dict[str, Any]:
    """Offer from /api/search. Validity comes from the brochure it belongs to."""
    parent = item.get("parentContent") or {}
    bid = parent.get("id")
    page = (parent.get("page") or {}).get("number")
    prices = item.get("prices") or {}
    price = money(prices.get("mainPrice"))
    secondary = money(prices.get("secondaryPrice"))
    unit_text = (prices.get("priceByBaseUnit") or "").strip("() ") or None
    unit = parse_unit_price(unit_text)
    brochure = brochure or {}
    validity = Validity.from_strings(
        item.get("validFrom") or brochure.get("validFrom"),
        item.get("validUntil") or brochure.get("validUntil"),
    )
    paths = item.get("categoryPaths") or []
    retailer = item.get("publisherName")
    conds = [c for c in _split_conditions(prices.get("conditions")) if classify_condition(c) not in ("noise", "no_app")]
    app_price = None
    if price and any(classify_condition(c) in ("loyalty", "points") for c in conds):
        # Search index only: the advertised price needs an app/card (full deal data comes from the viewer).
        app_price = compact({"price": price, "program": loyalty_program(retailer, conds), "conditions": conds,
                             "unit_price": unit_text})
        price = secondary if secondary and not prices.get("secondaryPriceIsUVP") else None
        conds = []
        if price is not None:  # the Grundpreis belonged to the app price
            unit_text, unit = None, None
    out = {
        "offer_id": item.get("id"),
        "product": item.get("title"),
        "brand": item.get("brand"),
        "description": item.get("description") or prices.get("description") or None,
        "retailer": retailer,
        "retailer_id": item.get("publisherId"),
        "price": price,
        "price_text": ("ab " if prices.get("priceRange") else "") + (fmt_eur(price) or "") if price else None,
        ("uvp" if prices.get("secondaryPriceIsUVP") else "regular_price"): secondary,
        "unit_price": unit_text,
        "unit_price_value": {"per": unit[0], "eur": unit[1]} if unit else None,
        "conditions": conds,
        "app_price": app_price,
        "app_only": True if app_price else None,
        "app_note": product_app_note(retailer, item.get("title"), item.get("description")),
        **validity.describe(now),
        "brochure_id": bid,
        "brochure_title": brochure.get("title"),
        "page": page,
        "viewer_url": viewer_url(bid, page) if bid else None,
        "cutout_url": CUTOUT_URL.format(oid=item["id"]) if item.get("id") else None,
        "distance_km": round(float(brochure["distance"]), 1) if brochure.get("distance") is not None else None,
        "category_match": _path_has(paths, category_ids) if category_ids else None,
    }
    out = compact(out)
    estimate_unit_price(out)
    return out


def _path_has(paths: list[Any], category_ids: set[str]) -> bool:
    for path in paths:
        nodes = path if isinstance(path, list) else [path]
        if any(isinstance(n, dict) and n.get("id") in category_ids for n in nodes):
            return True
    return False


PRICE_KEYS = ("price", "price_text", "unit_price", "unit_price_value", "conditions", "regular_price", "uvp",
              "price_note", "app_price", "app_only", "points_price", "multi_buy", "online_only", "app_bonus")


def deals_summary(deals: list[dict[str, Any]], retailer: str | None = None) -> dict[str, Any]:
    """Split kaufDA 'deals' into the store price for everyone, app/card prices, loyalty point prices,
    multi-buy prices and reference prices (Normalpreis/UVP)."""
    buckets: dict[str, list[dict[str, Any]]] = {"plain": [], "loyalty": [], "points": [], "multi_buy": []}
    reference: dict[str, float] = {}
    notes: list[str] = []
    online: list[bool] = []
    bonus = None
    for deal in deals or []:
        typ = deal.get("type") or "OTHER"
        low, high = money(deal.get("min")), money(deal.get("max"))
        kinds: set[str] = set()
        texts: list[str] = []
        for part in _split_conditions(deal.get("conditions")):
            kind = classify_condition(part)
            if kind == "noise":
                continue
            kinds.add(kind)
            if kind not in ("no_app", "online_only"):  # online_only becomes a flag
                texts.append(part if len(part) <= 100 else part[:97] + "...")
        desc = (deal.get("description") or "").strip()
        if desc:
            if re.search(r"\d+[.,]\d{2}\s*€\s*bonus", desc, re.I):
                bonus = desc
            else:
                kind = classify_condition(desc)
                if kind in ("loyalty", "online_only", "points", "no_app"):
                    kinds.add(kind)
                    if kind not in ("no_app", "online_only"):
                        texts.append(desc)
                elif kind != "noise":
                    notes.append(desc)
        if not low:
            notes.extend(texts)  # deals without a price: "2 für 1", "-30 %"
            continue
        if typ in ("REGULAR_PRICE", "RECOMMENDED_RETAIL_PRICE"):
            reference["uvp" if typ == "RECOMMENDED_RETAIL_PRICE" else "regular_price"] = low
            continue
        online.append("online_only" in kinds)
        entry = {"price": low, "price_max": high if high and high != low else None,
                 "unit_price": (deal.get("priceByBaseUnit") or "").strip("() ") or None,
                 "conditions": list(dict.fromkeys(texts))}
        bucket = ("points" if "points" in kinds else "loyalty" if "loyalty" in kinds
                  else "multi_buy" if "multi_buy" in kinds else "plain")
        buckets[bucket].append(entry)

    def cheapest(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        return min(entries, key=lambda e: e["price"]) if entries else None

    out: dict[str, Any] = {}
    head = cheapest(buckets["plain"])
    if head is None and buckets["multi_buy"]:
        head = cheapest(buckets["multi_buy"])
        buckets["multi_buy"].remove(head)
    loyalty_texts = [c for e in buckets["loyalty"] + buckets["points"] for c in e["conditions"]]
    program = loyalty_program(retailer, loyalty_texts) if loyalty_texts or bonus else None
    if head:
        out["price"] = head["price"]
        out["price_text"] = ("ab " if head["price_max"] else "") + (fmt_eur(head["price"]) or "")
        out["unit_price"] = head["unit_price"]
        out["conditions"] = head["conditions"]
    elif buckets["loyalty"] or buckets["points"]:
        out["app_only"] = True
        if reference.get("regular_price"):
            out["price"] = reference["regular_price"]
            out["price_text"] = f"{fmt_eur(reference['regular_price'])} (ohne App, Normalpreis)"
        else:
            source = cheapest(buckets["loyalty"]) or cheapest(buckets["points"])
            out["unit_price"] = source["unit_price"] if source else None
    app = cheapest(buckets["loyalty"])
    if app:
        out["app_price"] = compact({"price": app["price"], "program": program, "conditions": app["conditions"],
                                    "unit_price": app["unit_price"]})
    points = cheapest(buckets["points"])
    if points:
        out["points_price"] = compact({"price": points["price"], "program": program,
                                       "conditions": points["conditions"]})
    if buckets["multi_buy"]:
        out["multi_buy"] = [compact({"price": e["price"], "conditions": e["conditions"], "unit_price": e["unit_price"]})
                            for e in buckets["multi_buy"]]
    if online and all(online):
        out["online_only"] = True
    if bonus:
        out["app_bonus"] = f"{bonus} (Guthaben mit {program}, kein Sofortrabatt)"
    if notes:
        out["price_note"] = "; ".join(dict.fromkeys(notes))
    out.update(reference)
    # unit_price_value belongs to `price`, or to the app price when only that is known.
    unit = parse_unit_price(out.get("unit_price"))
    if unit:
        out["unit_price_value"] = {"per": unit[0], "eur": unit[1]}
    return compact(out)


def offer_from_viewer(content: dict[str, Any], *, retailer: str | None = None,
                      category_ids: set[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Offer from the brochure viewer API (carries its own validity and all deals)."""
    products = content.get("products") or [{}]
    product = products[0] or {}
    description = "; ".join(
        p.get("paragraph", "").strip() for p in product.get("description") or [] if p.get("paragraph")
    )
    parent = content.get("parentContent") or {}
    bid = parent.get("id")
    page_index = (parent.get("page") or {}).get("number")
    page = page_index + 1 if isinstance(page_index, int) else None
    profiles = content.get("publicationProfiles") or []
    validity = Validity(None, None)
    if profiles:
        v = profiles[0].get("validity") or {}
        validity = Validity.from_strings(v.get("startDate"), v.get("endDate"))
    publisher = content.get("publisher") or {}
    retailer = publisher.get("name") or retailer
    paths = [p.get("categoryPaths") or [] for p in products]
    out = {
        "offer_id": content.get("id"),
        "product": product.get("name"),
        "brand": product.get("brandName"),
        "description": description or None,
        "retailer": retailer,
        **deals_summary(content.get("deals") or [], retailer),
        "app_note": product_app_note(retailer, product.get("name"), description),
        **validity.describe(now),
        "brochure_id": bid,
        "page": page,
        "viewer_url": viewer_url(bid, page) if bid else None,
        "cutout_url": content.get("image") or (CUTOUT_URL.format(oid=content["id"]) if content.get("id") else None),
    }
    out = compact(out)
    estimate_unit_price(out)
    if category_ids:
        out["category_match"] = _path_has(paths, category_ids)
    return out


def viewer_validity(content: dict[str, Any]) -> Validity | None:
    profiles = content.get("publicationProfiles") or []
    if not profiles:
        return None
    v = profiles[0].get("validity") or {}
    validity = Validity.from_strings(v.get("startDate"), v.get("endDate"))
    return validity if validity.known else None


RELEVANCE_LABELS = {
    0: "sicher (Kategorie und Suchwort im Namen)",
    1: "sehr wahrscheinlich (Kategorie, Name enthält Suchwort)",
    2: "wahrscheinlich (Kategorie, Name teilweise)",
    3: "Kategorie, Name passt nicht (prüfen!)",
    4: "nur Name (prüfen!)",
    5: "nur teilweise Name (prüfen!)",
    6: "lose (prüfen!)",
}


def relevance_tier(offer: dict[str, Any], query: str) -> int:
    """0 = right category and the query word stands alone in the name ('H-Milch') ... 6 = loosely related.

    kaufDA's categories are broad ('Kondensmilch', 'Kakao zum Backen' sit below 'Milch'); the name check
    separates the real thing from compounds and accessories."""
    words = norm(query).split()
    hay = norm(" ".join(x for x in (offer.get("product"), offer.get("brand")) if x))
    tokens = set(norm(offer.get("product")).split())
    found = sum(1 for w in words if w in hay)
    full, partial = bool(words) and found == len(words), found > 0
    exact = full and any(w in tokens for w in words)
    if offer.get("category_match"):
        return 0 if exact else 1 if full else 2 if partial else 3
    return 4 if full else 5 if partial else 6


def effective_price(offer: dict[str, Any]) -> float | None:
    """Price used for ranking: your_price if personalized, else the store price for everyone."""
    return offer.get("your_price", offer.get("price"))


def effective_unit(offer: dict[str, Any]) -> float | None:
    unit = offer.get("your_unit_price") or offer.get("unit_price_value") or {}
    return unit.get("eur")


def sort_key_price(offer: dict[str, Any]) -> tuple[float, float]:
    return (effective_price(offer) or 1e9, effective_unit(offer) or 1e9)


def sort_key_unit(offer: dict[str, Any]) -> tuple[float, float]:
    unit = effective_unit(offer)
    return (unit if unit is not None else 1e9, effective_price(offer) or 1e9)
