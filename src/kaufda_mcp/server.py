"""kaufDA MCP server: search grocery offers, read brochures, build shopping lists."""

from __future__ import annotations

import argparse
import asyncio
import io
import itertools
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from . import normalize as N
from .bring import BringClient, BringError, KeyringStore, LoginPage, login_sync
from .client import KaufdaClient, KaufdaError, check_uuid
from .prefs import PreferenceStore

log = logging.getLogger("kaufda_mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
kaufDA (kaufda.de) MCP server: current and upcoming offers from German retail brochures (Prospekte) \
near a location. All prices in EUR, dates in Europe/Berlin.

Workflow:
1. Call get_preferences first. If no location is stored, ask the user for PLZ/Ort and call \
set_preferences(location=...). Also store the shops the user likes (set_preferences(stores=[...])); \
searches are then limited to those shops by default (only_favorites=true). Ask once which retailer apps / \
loyalty cards the user uses (Lidl Plus, PENNY App, Kaufland Card, REWE App, EDEKA App, Netto App, PAYBACK, \
...) and save them with set_preferences(apps=[...]).
2. search_offers for single products. shopping_list makes a shopping PLAN for a whole list: it compares the \
best mix over all stores with the best 1, 2 and 3 store plans and recommends one (recommended.why). Present \
the recommendation, the other_plans (e.g. "nur Lidl: 2 € teurer, aber nur ein Laden") and alternatives, let the \
user decide items under `choose` (umbrella terms like "Trinken" or "Snacks" were searched broadly: suggest the \
best real deals by discount_pct; unsure matches), then ask before changing anything. If the user wants a \
different plan, call shopping_list again with stores=<that plan's stores>.
3. Validity is evaluated per offer against the current date ("today" in every answer): many offers \
start mid-week (e.g. Thursday) and next week's brochures are often already online. Every offer has \
valid_from/valid_until, valid_today and a status like "aktiv", "aktiv, endet heute", \
"demnächst, ab morgen Do 15.10.". Never present an offer with valid_today=false as available today, \
always say from which day it applies. If the user names a shopping day, pass valid_on (e.g. "Donnerstag"); \
for shopping today use when="now". shopping_list returns a shopping_window per store (the days on which \
all picks of that store are valid together).
3b. Prices: `price` is what everybody pays in the store. `app_price` (with `program`, e.g. "PENNY App", \
"Lidl Plus", "Kaufland Card") only applies with that app/card; `app_only`=true means the advertised price \
needs the app and `price` is then the normal price (if known). `your_price` (+ `your_price_basis`) is the \
price for THIS user based on the saved apps; rankings and totals use it. Mention app prices the user could \
get with an app they do not have. `points_price` needs collected loyalty points, `multi_buy` needs a minimum \
quantity, `online_only` offers are not sold in stores, `app_bonus` (REWE Bonus) is credit for later, not a \
discount, `app_note` marks app coupons / PAYBACK actions. Other `conditions` (e.g. "An unserer Theke") apply \
as stated.
4. To show or verify an offer use its cutout_url (kaufDA's clipping from the brochure incl. price) or call \
view_offer, which returns that clipping as an image (mode="context" adds the surroundings when the price or \
a date banner is cut off). Never present any other picture as the offer. view_brochure_page shows the whole \
page. Give the user the viewer_url so they can open the brochure page on kaufda.de.
5. Product search is keyword based: use simple German product words ("Butter", "Hackfleisch", \
"Kaffee Bohnen"). suggest_search_terms helps to find good terms. Results with category_match=false are \
often only loosely related (e.g. "Butterkekse" for "Butter").
6. Shopping list apps: ask once whether the user keeps a shopping list in an app (supported: Bring!) and save \
it with set_preferences(shopping_list_app="bring"). Then call bring_connect and give the user the login_url: \
they log in there themselves. NEVER ask for the Bring! password in chat. Afterwards bring_status lists their \
lists; if there are several, ask which one and call bring_select_list. "Schau dir meine Einkaufsliste an und \
mach einen Plan" = shopping_list(from_bring_list=true): the user's notes (e.g. "2 l") count as quantity, \
items marked urgent only get offers valid today. shopping_list returns bring_updates for the recommended plan: \
short title ("Milch"), Beschreibung = ONLY the store ("Lidl"; with the user's own quantity note: "2 l · \
Lidl"), flag discounted=true. Never write prices, products, app hints or alternatives into Bring!: the user \
knows their apps and cards, the list must stay readable. ASK FIRST: settle choices and alternatives with \
direct questions in the chat ("Trinken: Pepsi bei Lidl oder Sinalco bei Penny?"), then write exactly the \
chosen store. Only after the user's OK call bring_add_items. Its result reads the list back (on_list, \
spec_saved, flags_saved), so report truthfully what really arrived in Bring!. Flags: urgent (Dringend), \
convenient (Wenn's passt), discounted (Im Angebot).
"""

mcp = FastMCP("kaufda", instructions=INSTRUCTIONS, log_level=os.environ.get("KAUFDA_MCP_LOGLEVEL", "WARNING").upper())
client = KaufdaClient()
prefs = PreferenceStore()
bring = BringClient()
bring.attribute_type = prefs.load().get("bring_attribute_type") or bring.attribute_type


def _bring_logged_in(session: dict[str, Any]) -> None:
    """Login page callback (runs in the page's thread): keep the session tokens, never the password."""
    try:
        KeyringStore().save(session)
    except BringError as exc:
        log.warning("Bring! session only kept in memory: %s", exc)
        session["source"] = "memory"
    bring.adopt(session)


login_page = LoginPage(on_success=_bring_logged_in)

When = Literal["all", "now", "upcoming"]
GERMANY_CENTER = {"label": "Deutschland", "lat": 51.1657, "lng": 10.4515}

LocationArg = Annotated[str | None, Field(
    description="PLZ, Ort or address, e.g. '24534' or 'Kiel, Holstenstraße'. Omit to use the saved location.")]
StoresArg = Annotated[list[str] | None, Field(
    description="Only these retailers, e.g. ['Lidl', 'Edeka', 'Aldi']. Overrides the saved favourites.")]
FavArg = Annotated[bool, Field(
    description="If no stores are given: limit results to the saved favourite stores (if any are saved).")]
WhenArg = Annotated[When, Field(
    description="'now' = valid today, 'upcoming' = starts later (e.g. next week's brochure), 'all' = both.")]
ValidOnArg = Annotated[str | None, Field(
    description="Only offers valid on this day: ISO date, '19.10.', 'heute', 'morgen' or a weekday like 'Samstag'. "
                "Overrides 'when'.")]
RadiusArg = Annotated[float | None, Field(
    description="Hide shops farther away than this (km). Omit for the saved radius (default 25), 0 = no limit.")]


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _location(location: str | None, *, required: bool = True) -> dict[str, Any]:
    try:
        if location and location.strip():
            return await client.geocode(location)
        saved = prefs.load().get("location")
        if saved:
            return saved
        env = os.environ.get("KAUFDA_LOCATION")
        if env:
            return await client.geocode(env)
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    if not required:
        return GERMANY_CENTER
    raise ToolError(
        "Kein Standort bekannt. Frag den Nutzer nach PLZ oder Ort und rufe set_preferences(location=...) auf "
        "oder gib location direkt mit."
    )


def _store_filter(stores: list[str] | None, only_favorites: bool) -> tuple[list[str], str]:
    explicit = [s.strip() for s in stores or [] if s and s.strip()]
    if explicit:
        return explicit, ", ".join(explicit)
    if only_favorites:
        favourites = prefs.stores()
        if favourites:
            return favourites, "Lieblingsmärkte: " + ", ".join(favourites)
    return [], "alle Händler"


def _radius(max_distance_km: float | None) -> float:
    return prefs.radius_km() if max_distance_km is None else max_distance_km


def _valid_on(value: str | None):
    try:
        return N.parse_day(value)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _wrap(exc: Exception) -> ToolError:
    return exc if isinstance(exc, ToolError) else ToolError(str(exc))


async def _viewer_index(brochure_ids: set[str], loc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """offer id -> viewer content for all offers in the given brochures."""
    ids = sorted(brochure_ids)
    results = await asyncio.gather(
        *(client.brochure_pages(bid, loc["lat"], loc["lng"]) for bid in ids), return_exceptions=True
    )
    index: dict[str, dict[str, Any]] = {}
    for bid, pages in zip(ids, results):
        if isinstance(pages, BaseException):
            log.warning("brochure pages %s failed: %s", bid, pages)
            continue
        for page in pages:
            for entry in page.get("offers") or []:
                content = entry.get("content") or {}
                if content.get("id"):
                    index[content["id"]] = content
    return index


async def _search(query: str, loc: dict[str, Any], *, wanted: list[str], when: str, valid_on,
                  radius: float, sort: str, exact_validity: bool, apps: list[str]) -> dict[str, Any]:
    raw = await client.search(query, loc["lat"], loc["lng"], limit=100,
                              sort="price" if sort == "price" else None)
    contents = raw.get("contents") or {}
    meta = raw.get("metadata") or {}
    categories = N.pick_categories(query, meta.get("recognizedEntities") or [])
    category_ids = {c["id"] for c in categories}
    now = N.now_berlin()

    brochures: dict[str, dict[str, Any]] = {}
    page_hits: list[dict[str, Any]] = []
    for entry in contents.get("brochures") or []:
        content = entry.get("content") or {}
        bid = content.get("contentId") or content.get("id")
        if bid:
            brochures.setdefault(bid, content)
            page_hits.append(content)

    def shop_ok(name: str | None, pid: str | None) -> bool:
        if N.is_ignored_publisher(name):
            return False
        return not wanted or N.any_store_matches(wanted, name, pid)

    raw_offers = [o for o in contents.get("offers") or [] if not N.is_ignored_publisher(o.get("publisherName"))]
    candidates = [o for o in raw_offers if shop_ok(o.get("publisherName"), o.get("publisherId"))]

    missing = {(o.get("parentContent") or {}).get("id") for o in candidates} - set(brochures) - {None}
    if missing:
        metas = await asyncio.gather(
            *(client.brochure_meta(bid, loc["lat"], loc["lng"]) for bid in missing), return_exceptions=True
        )
        for bid, m in zip(missing, metas):
            if isinstance(m, dict) and m:
                brochures[bid] = m

    viewer: dict[str, dict[str, Any]] = {}
    if exact_validity and candidates:
        viewer = await _viewer_index({(o.get("parentContent") or {}).get("id") for o in candidates} - {None}, loc)

    offers: list[dict[str, Any]] = []
    hidden_far = hidden_time = 0
    seen: set[tuple] = set()
    for item in candidates:
        bid = (item.get("parentContent") or {}).get("id")
        brochure = brochures.get(bid) or {}
        distance = brochure.get("distance")
        if radius and radius > 0 and distance is not None and float(distance) > radius:
            hidden_far += 1
            continue
        offer = N.offer_from_search(item, brochure, category_ids, now)
        validity = N.Validity.from_strings(brochure.get("validFrom"), brochure.get("validUntil"))
        detail = viewer.get(item.get("id"))
        if detail:
            validity = N.viewer_validity(detail) or validity
            deals = N.deals_summary(detail.get("deals") or [], offer.get("retailer"))
            if deals.get("price") or deals.get("app_price") or deals.get("points_price"):
                # The search index flattens deals and may show an app-only price as the main price.
                for key in N.PRICE_KEYS:
                    offer.pop(key, None)
                offer.update(deals)
            if not offer.get("description"):
                # The brochure text carries the pack size ("je 1-l-Packung") the search index leaves out.
                text = "; ".join(p.get("paragraph", "").strip() for prod in (detail.get("products") or [])[:1]
                                 for p in prod.get("description") or [] if p.get("paragraph"))
                if text:
                    offer["description"] = text[:200]
            N.estimate_unit_price(offer)
        if not validity.matches(when, valid_on, now):
            hidden_time += 1
            continue
        offer.update(validity.describe(now))
        N.personalize(offer, apps)
        key = (offer.get("retailer"), offer.get("product"), offer.get("brand"), offer.get("price"),
               offer.get("valid_from"))
        if key in seen:
            continue
        seen.add(key)
        offers.append(offer)

    if sort == "price":
        offers.sort(key=N.sort_key_price)
    elif sort == "unit_price":
        offers.sort(key=N.sort_key_unit)
    elif category_ids:
        offers.sort(key=lambda o: N.relevance_tier(o, query))

    covered = {(o.get("brochure_id"), o.get("page")) for o in offers}
    extra_pages = []
    for content in page_hits:
        bid = content.get("contentId") or content.get("id")
        publisher = content.get("publisher") or {}
        page = content.get("pageNumber") or ((content.get("page") or 0) + 1)
        if (bid, page) in covered or not shop_ok(publisher.get("name"), publisher.get("id")):
            continue
        distance = content.get("distance")
        if radius and radius > 0 and distance is not None and float(distance) > radius:
            continue
        validity = N.Validity.from_strings(content.get("validFrom"), content.get("validUntil"))
        if not validity.matches(when, valid_on, now):
            continue
        covered.add((bid, page))
        extra_pages.append(N.compact({
            "retailer": publisher.get("name"),
            "brochure_id": bid,
            "brochure_title": content.get("title"),
            "page": page,
            **validity.describe(now),
            "viewer_url": N.viewer_url(bid, page),
        }))

    notes = []
    if hidden_far:
        notes.append(f"{hidden_far} Treffer bei Märkten weiter als {radius:g} km ausgeblendet (max_distance_km).")
    if hidden_time:
        notes.append(f"{hidden_time} Treffer außerhalb des gewünschten Zeitraums ausgeblendet.")
    if wanted:
        others = Counter(
            o.get("publisherName") for o in raw_offers if not shop_ok(o.get("publisherName"), o.get("publisherId"))
        )
        if others:
            notes.append("Weitere Treffer bei anderen Händlern: "
                         + ", ".join(f"{name} ({n})" for name, n in others.most_common(8)))
    if not offers and not extra_pages:
        notes.append("Nichts gefunden. Anderen Suchbegriff probieren (suggest_search_terms) oder Filter lockern.")

    return {
        "query": query,
        "recognized_category": ", ".join(c.get("value", "") for c in categories) or None,
        "offers": offers,
        "pages_without_extracted_offer": extra_pages[:10],
        "notes": notes,
    }


# ====================================================================== tools

@mcp.tool()
async def get_preferences() -> str:
    """Show the saved home location, favourite stores, the apps/loyalty cards the user uses and the search
    radius. Call this first."""
    data = prefs.load()
    out = {
        "location": data.get("location"),
        "stores": data.get("stores") or [],
        "apps": data.get("apps") or [],
        "shopping_list_app": data.get("shopping_list_app"),
        "bring_list": (data.get("bring_list") or {}).get("name"),
        "max_distance_km": prefs.radius_km(),
        "file": str(prefs.path),
    }
    if not data.get("shopping_list_app"):
        out["hint_shopping_list"] = ("Unbekannt, ob der Nutzer eine Einkaufslisten App nutzt. Einmal fragen; "
                                     "unterstützt wird Bring! (set_preferences(shopping_list_app='bring'), dann "
                                     "bring_connect).")
    if not data.get("location"):
        out["hint"] = "Kein Standort gespeichert: Nutzer nach PLZ/Ort fragen und set_preferences(location=...) aufrufen."
    if not data.get("stores"):
        out["hint_stores"] = "Keine Lieblingsmärkte gespeichert: es wird bei allen Händlern gesucht."
    if "apps" not in data:
        out["hint_apps"] = ("Unbekannt, welche Händler Apps/Kundenkarten der Nutzer nutzt (z.B. Lidl Plus, PENNY App, "
                            "Kaufland Card, REWE App, PAYBACK). Einmal fragen und set_preferences(apps=[...]) "
                            "speichern; bis dahin gilt your_price ohne App.")
    return _dump(out)


@mcp.tool()
async def set_preferences(
    location: Annotated[str | None, Field(description="New home location: PLZ, Ort or address.")] = None,
    stores: Annotated[list[str] | None, Field(
        description="Replace the favourite stores, e.g. ['Lidl', 'Edeka', 'Penny']. [] clears them.")] = None,
    add_stores: Annotated[list[str] | None, Field(description="Add stores to the favourites.")] = None,
    remove_stores: Annotated[list[str] | None, Field(description="Remove stores from the favourites.")] = None,
    max_distance_km: Annotated[float | None, Field(
        description="Default search radius in km (0 = unlimited).")] = None,
    apps: Annotated[list[str] | None, Field(
        description="Replace the apps/loyalty cards the user uses, e.g. ['Lidl Plus', 'PENNY App', 'Kaufland Card', "
                    "'PAYBACK'] or ['alle']. [] = none. App prices count into your_price only for these.")] = None,
    add_apps: Annotated[list[str] | None, Field(description="Add apps/cards.")] = None,
    remove_apps: Annotated[list[str] | None, Field(description="Remove apps/cards.")] = None,
    shopping_list_app: Annotated[Literal["bring", "keine"] | None, Field(
        description="Shopping list app the user keeps their list in: 'bring' (Bring!) or 'keine'.")] = None,
) -> str:
    """Save where the user lives, where they like to shop and which retailer apps / loyalty cards they use.
    Persists across sessions."""
    data = prefs.load()
    if location is not None:
        try:
            data["location"] = await client.geocode(location)
        except KaufdaError as exc:
            raise ToolError(str(exc)) from exc

    def merge(current: list[str], replace: list[str] | None, add: list[str] | None,
              remove: list[str] | None) -> list[str]:
        if replace is not None:
            current = [s.strip() for s in replace if s and s.strip()]
        for entry in add or []:
            if entry.strip() and not any(N.norm(entry) == N.norm(c) for c in current):
                current.append(entry.strip())
        for entry in remove or []:
            current = [c for c in current if not (N.norm(entry) == N.norm(c) or N.store_matches(entry, c))]
        return current

    data["stores"] = merge(list(data.get("stores") or []), stores, add_stores, remove_stores)
    if apps is not None or add_apps or remove_apps:
        data["apps"] = merge(list(data.get("apps") or []), apps, add_apps, remove_apps)
    if max_distance_km is not None:
        data["max_distance_km"] = max(0.0, float(max_distance_km))
    if shopping_list_app is not None:
        data["shopping_list_app"] = shopping_list_app
    prefs.save(data)
    out = N.compact({"saved": True, "location": data.get("location"), "stores": data["stores"],
                     "apps": data.get("apps"), "shopping_list_app": data.get("shopping_list_app"),
                     "max_distance_km": prefs.radius_km()})
    if shopping_list_app == "bring" and not await bring.connected():
        out["next_step"] = "bring_connect aufrufen und dem Nutzer den Login Link geben."
    return _dump(out)


@mcp.tool()
async def search_offers(
    query: Annotated[str, Field(description="Product in German, e.g. 'Butter', 'Rinderhack', 'Kaffee Bohnen'.")],
    location: LocationArg = None,
    stores: StoresArg = None,
    only_favorites: FavArg = True,
    when: WhenArg = "all",
    valid_on: ValidOnArg = None,
    sort: Annotated[Literal["relevance", "price", "unit_price"], Field(
        description="relevance (category matches first), price, or unit_price (EUR per kg/l).")] = "relevance",
    max_distance_km: RadiusArg = None,
    max_results: Annotated[int, Field(ge=1, le=100)] = 25,
    exact_validity: Annotated[bool, Field(
        description="Load the brochures for per-offer validity (e.g. offers starting Thursday) and the full price "
                    "data (app/card prices, multi-buy, online only). Only disable for speed; app prices may then "
                    "be incomplete.")] = True,
) -> str:
    """Search current and upcoming offers for a product near the user: store price, app/card price,
    your_price (based on the user's apps), unit price, validity (from when / until when), retailer,
    brochure page, cutout image and a kaufda.de link."""
    loc = await _location(location)
    wanted, scope = _store_filter(stores, only_favorites)
    try:
        result = await _search(query, loc, wanted=wanted, when=when, valid_on=_valid_on(valid_on),
                               radius=_radius(max_distance_km), sort=sort, exact_validity=exact_validity,
                               apps=prefs.apps())
    except (KaufdaError, ToolError) as exc:
        raise _wrap(exc) from exc
    offers = result.pop("offers")
    return _dump(N.compact({
        "location": loc.get("label"),
        "store_filter": scope,
        "today": N.fmt_day(N.now_berlin().date()),
        **result,
        "total_offers": len(offers),
        "offers": offers[:max_results],
    }))


def _price_line(offer: dict[str, Any]) -> dict[str, Any]:
    """Short price info for the per-store overview: what the user pays plus the app hint if they lack it."""
    your = N.effective_price(offer)
    basis = offer.get("your_price_basis")
    if your is None:
        text = offer.get("your_price_note")
    elif your == offer.get("price") and offer.get("price_text"):
        text = offer["price_text"] + (f" ({basis})" if basis and "Normalpreis" not in offer["price_text"] else "")
    else:
        text = (N.fmt_eur(your) or "") + (f" ({basis})" if basis else "")
    app = offer.get("app_price") or {}
    hint = None
    if app and not (basis or "").startswith("mit "):
        hint = f"mit {app.get('program', 'App')}: {N.fmt_eur(app.get('price'))}"
    return N.compact({"price_text": text, "app_hint": hint, "conditions": offer.get("conditions"),
                      "app_note": offer.get("app_note"), "online_only": offer.get("online_only")})


_SHORT_RETAILER = {"Netto Marken-Discount": "Netto", "Netto mit dem Scottie": "Netto Scottie",
                   "dm-drogerie markt": "dm", "famila Nordost": "famila", "MediaMarkt Saturn": "MediaMarkt"}


def _short(retailer: str | None) -> str:
    return _SHORT_RETAILER.get(retailer or "", retailer or "?")


def _when_text(offer: dict[str, Any]) -> str:
    if offer.get("valid_from") and not offer.get("valid_today"):
        return "ab " + N.fmt_day(date.fromisoformat(offer["valid_from"]))
    if offer.get("valid_until"):
        return "bis " + N.fmt_day(date.fromisoformat(offer["valid_until"]))
    return ""


def _bring_spec(offer: dict[str, Any], user_note: str = "") -> str:
    """Only the store, so it stays readable in the app: 'Lidl', or '2 l · Lidl' when the user noted a quantity.
    Prices, products and alternatives are discussed in the chat, not written to the list."""
    store = _short(offer.get("retailer"))
    return f"{user_note} · {store}" if user_note else store


def _compact_offer(offer: dict[str, Any]) -> dict[str, Any]:
    return N.compact({
        "product": " ".join(x for x in (offer.get("brand"), offer.get("product")) if x),
        "retailer": _short(offer.get("retailer")),
        **_price_line(offer),
        "discount_pct": offer.get("discount_pct"),
        "status": offer.get("status"),
        "offer_id": offer.get("offer_id"),
        "brochure_id": offer.get("brochure_id"),
    })


def _store_plan(picks: list[dict[str, Any]]) -> dict[str, Any]:
    """Days on which all picks of one store are valid at the same time (offers may start Thursday)."""
    today = N.now_berlin().date()
    starts = [date.fromisoformat(p["_from"]) for p in picks if p.get("_from")]
    ends = [date.fromisoformat(p["_until"]) for p in picks if p.get("_until")]
    start = max([today, *starts])
    end = min(ends) if ends else None
    plan: dict[str, Any] = {"items": picks}
    if end is None or start <= end:
        window = N.fmt_day(start) + (f" bis {N.fmt_day(end)}" if end and end != start else "")
        plan["shopping_window"] = ("heute" if start == today else "ab " + N.fmt_day(start)) + \
            (f" (alles gültig {window})" if end else "")
    else:
        late = [p["item"] for p in picks if p.get("_from") == start.isoformat()]
        early = [p["item"] for p in picks if p.get("_until") == end.isoformat()]
        plan["shopping_window"] = "kein gemeinsamer Tag"
        plan["warning"] = (f"Nicht alles an einem Tag kaufbar: {', '.join(late)} erst ab {N.fmt_day(start)}, "
                           f"{', '.join(early)} nur bis {N.fmt_day(end)} Zwei Einkäufe planen oder per "
                           "valid_on einen Tag festlegen.")
    for p in picks:
        p.pop("_from", None)
        p.pop("_until", None)
    return plan


def _rank(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units = Counter((o.get("unit_price_value") or {}).get("per") for o in offers)
    units.pop(None, None)
    if units:
        unit, count = units.most_common(1)[0]
        if count >= max(2, len(offers) // 2):
            same = [o for o in offers if (o.get("unit_price_value") or {}).get("per") == unit]
            rest = [o for o in offers if o not in same]
            return sorted(same, key=N.sort_key_unit) + sorted(rest, key=N.sort_key_price)
    return sorted(offers, key=N.sort_key_price)


async def _item_candidates(entry: dict[str, Any], loc: dict[str, Any], search_kw: dict[str, Any],
                           horizon: str | None = None) -> None:
    """Fill entry['offers'] with the acceptable offers for one shopping list line, best first.
    horizon (ISO date): ignore offers that only start after it (e.g. next week's brochure)."""

    def in_horizon(o: dict[str, Any]) -> bool:
        return not horizon or not o.get("valid_from") or o["valid_from"] <= horizon

    item = entry["item"]
    queries = N.generic_queries(item)
    if queries:
        # Umbrella term ("Trinken"): look broadly and rank by how good the deal is.
        results = await asyncio.gather(*(_search(q, loc, **search_kw) for q in queries), return_exceptions=True)
        offers, seen = [], set()
        for query, res in zip(queries, results):
            if isinstance(res, BaseException):
                continue
            for o in res["offers"]:
                key = (o.get("retailer"), o.get("product"), o.get("price"))
                if o.get("online_only") or key in seen or N.relevance_tier(o, query) > 2 or not in_horizon(o):
                    continue
                seen.add(key)
                offers.append(o)
        offers.sort(key=lambda o: (-(o.get("discount_pct") or 0), N.effective_price(o) or 1e9))
        entry.update(offers=offers, flexible=True, queries=queries, tier=0)
        return
    res = await _search(item, loc, **search_kw)
    offers = [o for o in res["offers"] if in_horizon(o)]
    offers = [o for o in offers if not o.get("online_only")] or offers
    tiers: dict[int, list[dict[str, Any]]] = {}
    for o in offers:
        tiers.setdefault(N.relevance_tier(o, item), []).append(o)
    if not tiers:
        entry.update(offers=[])
        return
    best_tier = min(tiers)
    candidates = tiers[best_tier]
    usual = N.typical_need(candidates)
    user_need = N.parse_need(entry.get("note"))
    need = user_need or usual
    entry.update(need=need, usual_pack=usual if isinstance(usual, tuple) else None, need_exact=user_need is not None)
    # Cheapest for the amount actually needed; Grundpreis only breaks ties (no 12er Kiste for 2 l).
    order = {id(o): i for i, o in enumerate(_rank(candidates))}
    ranked = sorted(candidates, key=lambda o: (
        round((N.effective_price(o) or 1e9) * N.packs_needed(need, o, entry["usual_pack"], entry["need_exact"]), 2), order[id(o)]))
    if entry.get("urgent"):
        today = [o for o in ranked if o.get("valid_today")]
        if today and len(today) < len(ranked):
            ranked = today
            entry["remark"] = "dringend: nur Angebote, die heute schon gelten"
    entry.update(offers=ranked, tier=best_tier, flexible=best_tier >= 3)


def _evaluate(entries: list[dict[str, Any]], stores: frozenset[str] | None) -> dict[str, Any]:
    """Pick per item the best offer within `stores` (cheapest for the needed amount). 'extra' = how much more
    than the overall best offers (only firm items; umbrella terms just get the best deal in these stores)."""
    picks: dict[str, tuple[dict[str, Any], int, float]] = {}
    extra, covered, savings = 0.0, 0, 0.0

    def take(entry: dict[str, Any], o: dict[str, Any]) -> None:
        nonlocal extra, covered, savings
        need, usual = entry.get("need"), entry.get("usual_pack")
        exact = entry.get("need_exact", True)
        count = N.packs_needed(need, o, usual, exact)
        picks[entry["item"]] = (o, count, (N.effective_price(o) or 0.0) * count)
        if not entry.get("flexible"):
            covered += 1
            ref = entry["offers"][0]
            extra += max(0.0, picks[entry["item"]][2]
                         - (N.effective_price(ref) or 0.0) * N.packs_needed(need, ref, usual, exact))
        regular, eff = o.get("regular_price") or o.get("uvp"), N.effective_price(o)
        if regular and eff and regular > eff:
            savings += (regular - eff) * count

    firm = [e for e in entries if not e.get("flexible")]
    for entry in firm:
        hit = next((o for o in entry["offers"] if stores is None or o.get("retailer") in stores), None)
        if hit:
            take(entry, hit)
    # Umbrella terms ("Trinken") take the best deal in the stores the plan visits anyway.
    visiting = stores if stores is not None else frozenset(o.get("retailer") for o, _, _ in picks.values())
    for entry in entries:
        if entry.get("flexible"):
            hit = next((o for o in entry["offers"] if o.get("retailer") in visiting), None)
            if hit is None and (stores is None or not firm):
                hit = entry["offers"][0]
            if hit:
                take(entry, hit)
    return {"stores": frozenset(o.get("retailer") for o, _, _ in picks.values()), "picks": picks,
            "total": sum(cost for _, _, cost in picks.values()), "savings": savings, "extra": extra,
            "covered": covered}


def _plans(entries: list[dict[str, Any]], max_stores: int) -> list[tuple[str, dict[str, Any]]]:
    """Best mix over all stores plus the best plan with 1, 2, ... stores (most firm items covered, then the
    smallest extra cost for the same amounts)."""
    usable = [e for e in entries if e["offers"]]
    firm = [e for e in usable if not e.get("flexible")] or usable
    ranking = Counter(o.get("retailer") for e in firm for o in e["offers"])
    stores = [s for s, _ in ranking.most_common(12)]
    plans = [("Bester Mix", _evaluate(usable, None))]
    for k in range(1, min(max_stores, len(stores)) + 1):
        best = None
        for combo in itertools.combinations(stores, k):
            ev = _evaluate(usable, frozenset(combo))
            key = (-ev["covered"], round(ev["extra"], 2), round(ev["total"], 2))
            if best is None or key < best[0]:
                best = (key, ev)
        if best:
            plans.append((f"{k} {'Markt' if k == 1 else 'Märkte'}", best[1]))
    unique, seen = [], set()
    for label, ev in plans:
        signature = tuple(sorted((item, id(o)) for item, (o, _, _) in ev["picks"].items()))
        if signature not in seen:
            seen.add(signature)
            unique.append((label, ev))
    return unique


def _recommend(plans: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any], str]:
    """Fewest stores that cover as much as the best mix and cost at most ~2 € (or 5 %) more for the same
    amounts."""
    mix = plans[0][1]
    tolerance = max(2.0, 0.05 * mix["total"])
    fitting = [(label, ev) for label, ev in plans if ev["covered"] == mix["covered"] and ev["extra"] <= tolerance]
    label, ev = min(fitting, key=lambda p: (len(p[1]["stores"]), p[1]["extra"]))
    if ev is mix:
        return label, ev, "Günstigste Kombination für alle Artikel mit Angebot."
    if ev["extra"] < 0.01:
        cost = "genauso günstig wie der beste Mix"
    else:
        cost = f"bei gleichen Mengen nur ca. {N.fmt_eur(ev['extra'])} teurer als der beste Mix"
    return label, ev, f"Nur {len(ev['stores'])} statt {len(mix['stores'])} Märkte, {cost}."


def _plan_out(label: str, ev: dict[str, Any], entries: list[dict[str, Any]], detailed: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": f"{label}: " + " + ".join(sorted(_short(s) for s in ev["stores"])),
        "covered": f"{len(ev['picks'])}/{len(entries)}",
        "total_eur": round(ev["total"], 2),
        "extra_vs_best_mix_eur": round(ev["extra"], 2) or None,
        "savings_eur": round(ev["savings"], 2) or None,
        "not_on_offer_here": [e["item"] for e in entries if e["item"] not in ev["picks"]],
    }
    if not detailed:
        out["items"] = {item: f"{_short(o.get('retailer'))} {_price_line(o).get('price_text') or ''}".strip()
                        for item, (o, _, _) in ev["picks"].items()}
        return N.compact(out)
    by_store: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if entry["item"] not in ev["picks"]:
            continue
        o, count, cost = ev["picks"][entry["item"]]
        by_store.setdefault(o.get("retailer") or "?", []).append(N.compact({
            "item": entry["item"],
            "note": entry.get("note") or None,
            "count": count if count > 1 else None,
            "product": " ".join(x for x in (o.get("brand"), o.get("product")) if x),
            **_price_line(o),
            "cost_eur": round(cost, 2) if count > 1 else None,
            "discount_pct": o.get("discount_pct"),
            "validity": o.get("validity"), "status": o.get("status"), "page": o.get("page"),
            "viewer_url": o.get("viewer_url"), "cutout_url": o.get("cutout_url"),
            "remark": entry.get("remark"),
            "_from": o.get("valid_from"), "_until": o.get("valid_until"),
        }))
    out["by_store"] = {store: _store_plan(picks) for store, picks in by_store.items()}
    return N.compact(out)


@mcp.tool()
async def shopping_list(
    items: Annotated[list[str] | None, Field(
        max_length=40, description="Short item names like on a shopping list: ['Milch', 'Butter', 'Trinken']. "
                                   "Umbrella terms (Trinken, Snacks, Obst, Grillzeug ...) are searched broadly.")] = None,
    from_bring_list: Annotated[bool, Field(
        description="Use the items on the user's Bring! list (their notes like '2 l' count as quantity).")] = False,
    bring_list: Annotated[str | None, Field(description="Other Bring! list than the selected one.")] = None,
    location: LocationArg = None,
    stores: StoresArg = None,
    only_favorites: FavArg = True,
    when: WhenArg = "all",
    valid_on: ValidOnArg = None,
    max_distance_km: RadiusArg = None,
    max_stores: Annotated[int, Field(ge=1, le=4, description="Largest store count to compare plans for.")] = 3,
    alternatives: Annotated[int, Field(ge=0, le=5, description="Extra options per item.")] = 2,
) -> str:
    """Make a shopping plan: finds the offers for every item (from `items` and/or the user's Bring! list),
    then works out where the whole list is cheapest: best mix over all stores vs. the best plan with 1, 2, 3
    stores, with a recommendation, alternatives, items the user should choose (umbrella terms, unsure matches)
    and ready bring_updates. Prices are the user's prices (apps they use); quantities from notes count."""
    loc = await _location(location)
    wanted, scope = _store_filter(stores, only_favorites)
    entries: list[dict[str, Any]] = []
    sources: list[str] = []
    if from_bring_list:
        chosen = await _bring_list(bring_list)
        try:
            on_list = (await bring.items(chosen["uuid"]))["purchase"]
        except BringError as exc:
            raise ToolError(str(exc)) from exc
        for b in on_list:
            entries.append({"item": b["name"], "note": N.user_spec_part(b.get("specification")),
                            "spec": b.get("specification") or "", "urgent": b.get("urgent"), "from_bring": True})
        sources.append(f"Bring! Liste '{chosen['name']}' ({len(on_list)} Artikel)")
    for name in items or []:
        if name.strip() and not any(N.norm(name) == N.norm(e["item"]) for e in entries):
            entries.append({"item": name.strip(), "note": "", "from_bring": False})
    if not entries:
        raise ToolError("Keine Artikel: items angeben oder from_bring_list=true nutzen (ist die Bring! Liste leer?).")
    entries = entries[:40]
    if items:
        sources.append("Eingabe")
    search_kw = dict(wanted=wanted, when=when, valid_on=_valid_on(valid_on), radius=_radius(max_distance_km),
                     sort="relevance", exact_validity=True, apps=prefs.apps())
    today = N.now_berlin().date()
    horizon = None
    if valid_on is None and when == "all":
        # One shopping trip: this week up to Saturday (incl. offers starting Thursday), not next week's brochures.
        horizon = today + timedelta(days=(5 - today.weekday()) % 7)
        period = (f"diese Woche bis {N.fmt_day(horizon)} (Prospekte der nächsten Woche: when='upcoming' "
                  "oder valid_on)")
    elif valid_on:
        period = f"Einkauf am {N.fmt_day(search_kw['valid_on'])}"
    else:
        period = {"now": "nur heute gültige Angebote", "upcoming": "nur kommende Angebote"}.get(when, when)
    results = await asyncio.gather(
        *(_item_candidates(e, loc, search_kw, horizon.isoformat() if horizon else None) for e in entries),
        return_exceptions=True,
    )
    for entry, res in zip(entries, results):
        if isinstance(res, BaseException):
            entry.update(offers=[], error=str(res))

    plans = _plans(entries, max_stores) if any(e["offers"] for e in entries) else []
    out: dict[str, Any] = {
        "location": loc.get("label"),
        "store_filter": scope,
        "today": N.fmt_day(N.now_berlin().date()),
        "apps": prefs.apps() or "keine gespeichert (Preise ohne App)",
        "source": ", ".join(sources),
        "period": period,
    }
    rec_picks: dict[str, Any] = {}
    if plans:
        label, rec, why = _recommend(plans)
        rec_picks = rec["picks"]
        out["recommended"] = {"why": why, **_plan_out(label, rec, entries, detailed=True)}
        out["other_plans"] = [_plan_out(lbl, ev, entries, detailed=False) for lbl, ev in plans if ev is not rec]
    out["choose"] = [N.compact({
        "item": e["item"],
        "why": ("Sammelbegriff, breit gesucht: " + ", ".join(e["queries"])) if e.get("queries")
               else "unsichere Treffer: " + N.RELEVANCE_LABELS.get(e.get("tier", 6), ""),
        "options": [_compact_offer(o) for o in e["offers"][:4]],
    }) for e in entries if e.get("flexible") and e["offers"]]
    out["alternatives"] = {
        e["item"]: [_compact_offer(o) for o in e["offers"]
                    if o is not (rec_picks.get(e["item"]) or (None,))[0]][:alternatives]
        for e in entries if e["offers"] and not e.get("flexible") and alternatives
    }
    out["not_found"] = [e["item"] for e in entries if not e["offers"]]
    errors = {e["item"]: e["error"] for e in entries if e.get("error")}
    if errors:
        out["errors"] = errors
    if from_bring_list or prefs.load().get("shopping_list_app") == "bring":
        updates = []
        for e in entries:
            pick = rec_picks.get(e["item"])
            if pick:
                updates.append({"name": e["item"], "specification": _bring_spec(pick[0], e.get("note", "")),
                                "discounted": True})
            elif e.get("from_bring") and e.get("spec") and e["spec"] != e.get("note"):
                updates.append({"name": e["item"], "specification": e.get("note", ""), "discounted": False})
            elif not e.get("from_bring"):
                updates.append({"name": e["item"]})
        out["bring_updates"] = updates
        out["next_step"] = ("ERST FRAGEN, dann schreiben: Empfehlung und Alternativen kurz im Chat zeigen, bei "
                            "'choose' den Nutzer direkt entscheiden lassen (z.B. 'Pepsi bei Lidl oder Sinalco bei "
                            "Penny?'). Erst nach seinem OK bring_add_items(items=bring_updates). In Bring! steht als "
                            "Beschreibung nur der Markt (plus seine eigene Mengenangabe); passe bring_updates an seine "
                            "Wahl an. Anderen Plan gewählt: shopping_list mit stores=<Märkte des Plans>.")
    return _dump(N.compact(out))


@mcp.tool()
async def list_brochures(
    location: LocationArg = None,
    stores: StoresArg = None,
    only_favorites: FavArg = True,
    when: WhenArg = "all",
    valid_on: ValidOnArg = None,
    max_distance_km: RadiusArg = None,
) -> str:
    """List current and upcoming brochures (Prospekte) near the user with validity, page count,
    nearest store address and distance. Use the brochure_id with get_brochure_offers / view_brochure_page."""
    loc = await _location(location)
    wanted, scope = _store_filter(stores, only_favorites)
    day = _valid_on(valid_on)
    radius = _radius(max_distance_km)
    now = N.now_berlin()
    candidates: dict[str, tuple[dict[str, Any], float | None]] = {}
    try:
        shelf = await client.shelf(loc["lat"], loc["lng"], size=100)
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    for entry in shelf.get("contents") or []:
        if entry.get("contentType") not in ("brochure", "brochurePremium"):
            continue
        content = entry.get("content") or {}
        bid = content.get("contentId") or content.get("id")
        store = content.get("closestStore") or {}
        distance = None
        if store.get("latitude") is not None and store.get("longitude") is not None:
            distance = N.haversine_km(loc["lat"], loc["lng"], store["latitude"], store["longitude"])
        if bid:
            candidates.setdefault(bid, (content, distance))

    def publisher_of(content: dict[str, Any]) -> dict[str, Any]:
        return content.get("publisher") or {}

    # Retailers that are not on the shelf (e.g. ALDI) are found through a retailer search.
    lookups = [w for w in wanted
               if not any(N.store_matches(w, publisher_of(c).get("name"), publisher_of(c).get("id"))
                          for c, _ in candidates.values())]
    if lookups:
        found = await asyncio.gather(*(client.search(w, loc["lat"], loc["lng"], limit=24) for w in lookups),
                                     return_exceptions=True)
        for w, res in zip(lookups, found):
            if isinstance(res, BaseException):
                continue
            for entry in (res.get("contents") or {}).get("brochures") or []:
                content = entry.get("content") or {}
                pub = publisher_of(content)
                bid = content.get("contentId") or content.get("id")
                if bid and N.store_matches(w, pub.get("name"), pub.get("id")):
                    candidates.setdefault(bid, (content, content.get("distance")))

    out, hidden_far = [], 0
    for bid, (content, distance) in candidates.items():
        pub = publisher_of(content)
        if N.is_ignored_publisher(pub.get("name")):
            continue
        if wanted and not N.any_store_matches(wanted, pub.get("name"), pub.get("id")):
            continue
        if radius and radius > 0 and distance is not None and distance > radius:
            hidden_far += 1
            continue
        validity = N.Validity.from_strings(content.get("validFrom"), content.get("validUntil"))
        if not validity.matches(when, day, now):
            continue
        out.append(N.brochure_summary(content, distance_km=distance, now=now))
    out.sort(key=lambda b: (N.norm(b.get("retailer")), b.get("valid_from") or ""))
    notes = []
    if hidden_far:
        notes.append(f"{hidden_far} Prospekte von Märkten weiter als {radius:g} km ausgeblendet.")
    for w in wanted:
        if not any(N.store_matches(w, b.get("retailer"), b.get("retailer_id")) for b in out):
            notes.append(f"Kein passender Prospekt für '{w}' gefunden.")
    return _dump(N.compact({
        "location": loc.get("label"),
        "store_filter": scope,
        "today": N.fmt_day(now.date()),
        "count": len(out),
        "brochures": out,
        "notes": notes,
    }))


async def _load_brochure(brochure_id: str, location: str | None):
    try:
        bid = check_uuid(brochure_id, "Prospekt ID")
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    loc = await _location(location, required=False)
    meta, pages = await asyncio.gather(
        client.brochure_meta(bid, loc["lat"], loc["lng"]),
        client.brochure_pages(bid, loc["lat"], loc["lng"]),
        return_exceptions=True,
    )
    if isinstance(pages, BaseException):
        raise ToolError(f"Prospekt {bid} konnte nicht geladen werden: {pages}")
    if isinstance(meta, BaseException):
        meta = {}
    return bid, meta, pages


def _viewer_offers(pages: list[dict[str, Any]], retailer: str | None) -> list[dict[str, Any]]:
    now = N.now_berlin()
    apps = prefs.apps()
    out = []
    for page in pages:
        for entry in page.get("offers") or []:
            content = entry.get("content") or {}
            offer = N.personalize(N.offer_from_viewer(content, retailer=retailer, now=now), apps)
            offer.pop("brochure_id", None)
            offer["_categories"] = " ".join(
                n.get("name", "") for p in content.get("products") or [] for n in p.get("categoryPaths") or []
                if isinstance(n, dict)
            )
            out.append(offer)
    return out


@mcp.tool()
async def get_brochure_offers(
    brochure_id: Annotated[str, Field(description="brochure_id from list_brochures or search_offers.")],
    query: Annotated[str | None, Field(description="Only offers whose name/brand/category contain these words.")] = None,
    page: Annotated[int | None, Field(ge=1, description="Only offers on this page (1 = cover).")] = None,
    location: LocationArg = None,
    max_results: Annotated[int, Field(ge=1, le=400)] = 80,
) -> str:
    """All offers inside one brochure with prices, conditions (e.g. 'Nur mit App') and per-offer validity.
    Use query/page to narrow down large brochures."""
    bid, meta, pages = await _load_brochure(brochure_id, location)
    retailer = (meta.get("publisher") or {}).get("name")
    offers = _viewer_offers(pages, retailer)
    selected = [
        o for o in offers
        if (page is None or o.get("page") == page)
        and (not query or N.text_matches(query, o.get("product"), o.get("brand"), o.get("description"),
                                         o.get("_categories")))
    ]
    for o in selected:
        o.pop("_categories", None)
        o.pop("retailer", None)
    info = N.brochure_summary(meta) if meta else {"brochure_id": bid, "viewer_url": N.viewer_url(bid)}
    notes = []
    if len(selected) > max_results:
        notes.append(f"{len(selected) - max_results} weitere Angebote nicht angezeigt: query oder page nutzen.")
    if not selected:
        notes.append("Keine passenden Angebote. Manche Prospektseiten haben keine erfassten Angebote: "
                     "view_brochure_page zeigt die Seite als Bild.")
    return _dump(N.compact({
        "brochure": info,
        "page_count": len(pages),
        "offers_in_brochure": len(offers),
        "matching": len(selected),
        "offers": selected[:max_results],
        "notes": notes,
    }))


@mcp.tool(structured_output=False)
async def view_brochure_page(
    brochure_id: Annotated[str, Field(description="brochure_id from list_brochures or search_offers.")],
    page: Annotated[int, Field(ge=1, description="Page number, 1 = cover (same numbering as 'page' in offers).")],
    high_resolution: Annotated[bool, Field(description="1600px instead of 1024px, for small print.")] = False,
    location: LocationArg = None,
) -> list:
    """Show a brochure page as an image (to read or verify prices/dates yourself) plus the offers
    kaufDA extracted from that page."""
    bid, meta, pages = await _load_brochure(brochure_id, location)
    if not 1 <= page <= len(pages):
        raise ToolError(f"Seite {page} gibt es nicht, der Prospekt hat {len(pages)} Seiten.")
    target = next((p for p in pages if p.get("number") == page - 1), pages[page - 1])
    images = {img.get("size"): img.get("url") for img in target.get("images") or []}
    url = images.get("1600x1600" if high_resolution else "768x1024") or next(iter(images.values()), None)
    if not url:
        raise ToolError("Für diese Seite gibt es kein Bild.")
    try:
        data = await client.image(url)
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    retailer = (meta.get("publisher") or {}).get("name")
    offers = [o for o in _viewer_offers([target], retailer)]
    for o in offers:
        o.pop("_categories", None)
        o.pop("retailer", None)
        o.pop("viewer_url", None)
        o.pop("cutout_url", None)
    info = N.compact({
        "brochure_id": bid,
        "retailer": retailer,
        "title": meta.get("title"),
        **(N.Validity.from_strings(meta.get("validFrom"), meta.get("validUntil")).describe() if meta else {}),
        "page": page,
        "page_count": len(pages),
        "viewer_url": N.viewer_url(bid, page),
        "offers_on_page": offers,
    })
    return [Image(data=data, format="jpeg"), _dump(info)]


def _crop_area(page_jpeg: bytes, area: dict[str, Any], margin: float) -> bytes:
    """Cut the offer's area (normalized coordinates) plus a margin out of a brochure page."""
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(page_jpeg)) as im:
        width, height = im.size
        top_left, bottom_right = area.get("topLeft") or {}, area.get("bottomRight") or {}
        x0, y0 = float(top_left.get("x", 0)) * width, float(top_left.get("y", 0)) * height
        x1, y1 = float(bottom_right.get("x", 1)) * width, float(bottom_right.get("y", 1)) * height
        pad_x, pad_y = (x1 - x0) * margin, (y1 - y0) * margin
        box = (max(0, int(x0 - pad_x)), max(0, int(y0 - pad_y)),
               min(width, int(x1 + pad_x)), min(height, int(y1 + pad_y)))
        crop = im.crop(box).convert("RGB")
        crop.thumbnail((1200, 1200))
        out = io.BytesIO()
        crop.save(out, format="JPEG", quality=85)
        return out.getvalue()


@mcp.tool(structured_output=False)
async def view_offer(
    offer_id: Annotated[str, Field(description="offer_id from search_offers / get_brochure_offers.")],
    brochure_id: Annotated[str, Field(description="brochure_id the offer belongs to.")],
    mode: Annotated[Literal["cutout", "context"], Field(
        description="'cutout' = kaufDA's clipping of the offer (usually incl. price). 'context' = the offer's area "
                    "cut from the brochure page with a margin around it: use it when the price, a date banner or "
                    "a condition is not visible in the cutout.")] = "cutout",
    location: LocationArg = None,
) -> list:
    """Show the offer as it appears in the brochure (image incl. price) plus all its details, to verify price,
    conditions and validity. This image (or cutout_url) is the proof for an offer; product photos are not."""
    try:
        oid = check_uuid(offer_id, "Angebots ID")
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    bid, meta, pages = await _load_brochure(brochure_id, location)
    retailer = (meta.get("publisher") or {}).get("name")
    for page in pages:
        for entry in page.get("offers") or []:
            content = entry.get("content") or {}
            if (content.get("id") or "").lower() != oid:
                continue
            offer = N.personalize(N.offer_from_viewer(content, retailer=retailer), prefs.apps())
            area =((content.get("parentContent") or {}).get("page") or {}).get("area")
            images = {img.get("size"): img.get("url") for img in page.get("images") or []}
            blocks: list[Any] = []
            try:
                tl, br = (area or {}).get("topLeft") or {}, (area or {}).get("bottomRight") or {}
                share = (br.get("x", 1) - tl.get("x", 0)) * (br.get("y", 1) - tl.get("y", 0)) if area else 1.0
                if mode == "context" and share < 0.03 and images.get("768x1024"):
                    # Grouped offers ("Besen, Kehrset, Mopp ... ab 2.99") only mark the product photo,
                    # the price sits in a shared text block somewhere on the page.
                    blocks.append(Image(data=await client.image(images["768x1024"]), format="jpeg"))
                    offer["image"] = ("ganze Prospektseite: Sammelangebot, der Preis steht vermutlich in einem "
                                      "gemeinsamen Textblock")
                elif mode == "context" and area and (images.get("1600x1600") or images.get("2800x2800")):
                    page_jpeg = await client.image(images.get("1600x1600") or images["2800x2800"])
                    blocks.append(Image(data=await asyncio.to_thread(_crop_area, page_jpeg, area, 0.35),
                                        format="jpeg"))
                    offer["image"] = "Ausschnitt der Prospektseite mit Rand um das Angebot"
                elif offer.get("cutout_url"):
                    blocks.append(Image(data=await client.image(offer["cutout_url"]), format="jpeg"))
                    offer["image"] = ("kaufDA Ausschnitt aus dem Prospekt. Fehlt der Preis, mit mode='context' "
                                      "erneut aufrufen.")
                elif images.get("768x1024"):
                    blocks.append(Image(data=await client.image(images["768x1024"]), format="jpeg"))
                    offer["image"] = "ganze Prospektseite (kein Ausschnitt verfügbar)"
            except (KaufdaError, OSError) as exc:
                offer["image_error"] = str(exc)
            blocks.append(_dump(offer))
            return blocks
    raise ToolError(f"Angebot {oid} nicht in Prospekt {bid} gefunden.")


@mcp.tool()
async def suggest_search_terms(
    query: Annotated[str, Field(description="Start of a product word, e.g. 'Hafer'.")],
) -> str:
    """Autocomplete product terms the kaufDA search understands (helps to pick a good query)."""
    try:
        suggestions = await client.suggest(query)
    except KaufdaError as exc:
        raise ToolError(str(exc)) from exc
    return _dump([s.get("name") for s in suggestions if s.get("name")])


# ====================================================================== Bring! shopping list

class BringItemIn(BaseModel):
    name: str = Field(description="Short title as on a shopping list, e.g. 'Milch', 'Butter' (not the product name).")
    specification: str | None = Field(
        default=None,
        description="Beschreibung under the title: ONLY the store, e.g. 'Lidl', or the user's own quantity plus "
                    "store, e.g. '2 l · Lidl'. No prices or product details. None keeps the existing text, "
                    "'' clears it.")
    urgent: bool | None = Field(default=None, description="Flag 'Dringend'. None keeps the current value.")
    convenient: bool | None = Field(default=None, description="Flag 'Wenn's passt' / bei Gelegenheit.")
    discounted: bool | None = Field(default=None, description="Flag 'Im Angebot'. True for items bought on offer.")


def _remember_attribute_type() -> None:
    if bring.attribute_type_verified and prefs.load().get("bring_attribute_type") != bring.attribute_type:
        data = prefs.load()
        data["bring_attribute_type"] = bring.attribute_type
        prefs.save(data)


async def _bring_list(list_name: str | None) -> dict[str, str]:
    try:
        lists = await bring.lists()
    except BringError as exc:
        raise ToolError(str(exc)) from exc
    if not lists:
        raise ToolError("In diesem Bring! Konto gibt es keine Liste.")
    names = ", ".join(entry["name"] for entry in lists)
    if list_name:
        wanted = N.norm(list_name)
        hits = ([e for e in lists if e["uuid"] == list_name or N.norm(e["name"]) == wanted]
                or [e for e in lists if wanted and wanted in N.norm(e["name"])])
        if not hits:
            raise ToolError(f"Bring! Liste '{list_name}' nicht gefunden. Vorhanden: {names}")
        return hits[0]
    saved = (prefs.load().get("bring_list") or {}).get("uuid")
    for entry in lists:
        if entry["uuid"] == saved:
            return entry
    if len(lists) == 1:
        return lists[0]
    raise ToolError(f"Mehrere Bring! Listen vorhanden ({names}). Den Nutzer fragen und bring_select_list aufrufen.")


@mcp.tool()
async def bring_status() -> str:
    """Is the user's Bring! account connected? Shows the account, the selected list and all lists."""
    available, backend = KeyringStore.available()
    try:
        session = await bring.session()
    except BringError:
        return _dump(N.compact({
            "connected": False,
            "next_step": "bring_connect aufrufen und dem Nutzer den Login Link geben. Nie nach dem Passwort fragen.",
            "secure_storage": backend if available else f"nicht verfügbar ({backend})",
        }))
    try:
        lists = await bring.lists()
    except BringError as exc:
        raise ToolError(str(exc)) from exc
    selected = prefs.load().get("bring_list") or {}
    if not selected and len(lists) == 1:
        selected = {"uuid": lists[0]["uuid"], "name": lists[0]["name"]}
    out = {
        "connected": True,
        "account": session.get("email"),
        "selected_list": selected.get("name"),
        "lists": [entry["name"] for entry in lists],
        "stored": {"env": "Umgebungsvariablen", "memory": "nur bis zum Neustart"}.get(
            session.get("source") or "", "sicher im Schlüsselspeicher des Systems"),
    }
    if not selected.get("name"):
        out["next_step"] = "Nutzer fragen, welche Liste genutzt werden soll, dann bring_select_list."
    return _dump(N.compact(out))


@mcp.tool()
async def bring_connect() -> str:
    """Start the Bring! login: returns a one-time link to a local login page (on this computer, valid 10 minutes).
    Give the link to the user; they enter their Bring! mail and password there, directly. Their password never
    passes through the chat and is not stored, only the session token (in the system keychain).
    Never ask the user for the password yourself."""
    if await bring.connected():
        return _dump({"connected": True, "next_step": "Bereits verbunden, bring_status zeigt die Listen."})
    available, backend = KeyringStore.available()
    url = await asyncio.to_thread(login_page.start)
    return _dump(N.compact({
        "login_url": url,
        "valid_minutes": 10,
        "tell_user": ("Öffne den Link im Browser auf diesem Rechner, melde dich mit deinen Bring! Zugangsdaten an "
                      "und sag mir danach kurz Bescheid."),
        "secure_storage": backend if available else f"nicht verfügbar ({backend}): Sitzung gilt nur bis zum Neustart",
        "alternatives": {
            "terminal": "kaufda-mcp bring-login (fragt Mail und Passwort im Terminal ab)",
            "server_or_bot": "Umgebungsvariablen BRING_EMAIL und BRING_PASSWORD für den MCP Server setzen",
        },
        "note": "Der Link funktioniert nur auf dem Rechner, auf dem der MCP Server läuft.",
    }))


@mcp.tool()
async def bring_select_list(
    list_name: Annotated[str, Field(description="Name (or uuid) of the Bring! list, e.g. 'Zuhause'.")],
) -> str:
    """Choose which Bring! list the shopping items go to (saved permanently)."""
    entry = await _bring_list(list_name)
    data = prefs.load()
    data["bring_list"] = {"uuid": entry["uuid"], "name": entry["name"]}
    data.setdefault("shopping_list_app", "bring")
    prefs.save(data)
    return _dump({"saved": True, "bring_list": entry["name"]})


@mcp.tool()
async def bring_get_list(
    list_name: Annotated[str | None, Field(description="Other list than the selected one.")] = None,
) -> str:
    """Show what is on the user's Bring! list: title, Beschreibung/Menge and the flags dringend, wenn's passt,
    im Angebot, plus recently bought items. To turn the list into a shopping plan use
    shopping_list(from_bring_list=true)."""
    entry = await _bring_list(list_name)
    try:
        items = await bring.items(entry["uuid"])
    except BringError as exc:
        raise ToolError(str(exc)) from exc
    _remember_attribute_type()
    return _dump({
        "list": entry["name"],
        "to_buy": [N.compact({key: i.get(key) for key in ("name", "specification", "urgent", "convenient",
                                                          "discounted")})
                   for i in items["purchase"]],
        "recently_bought": [i.get("name") for i in items["recently"][:20]],
    })


@mcp.tool()
async def bring_add_items(
    items: Annotated[list[BringItemIn], Field(min_length=1, max_length=60,
                                              description="Items to put on the list (existing ones get updated).")],
    list_name: Annotated[str | None, Field(description="Other list than the selected one.")] = None,
) -> str:
    """Add or update items on the user's Bring! list: short title, Beschreibung = just the store (plus the
    user's own quantity) and the flags dringend / wenn's passt / im Angebot. Only call this after the user
    explicitly agreed in the chat; shopping_list returns ready-made bring_updates. The list is read back
    afterwards: on_list, spec_saved and flags_saved tell what Bring! really stored."""
    entry = await _bring_list(list_name)
    try:
        payload = [i.model_dump() if isinstance(i, BaseModel) else BringItemIn(**i).model_dump() for i in items]
        results = await bring.change(entry["uuid"], payload, "TO_PURCHASE")
    except BringError as exc:
        raise ToolError(str(exc)) from exc
    _remember_attribute_type()
    out: dict[str, Any] = {"list": entry["name"], "items": results}
    failed = [r["name"] for r in results if r.get("on_list") is False or r.get("spec_saved") is False]
    if failed:
        out["warning"] = (f"Nicht wie gewünscht in Bring! angekommen: {', '.join(failed)}. Dem Nutzer ehrlich so "
                          "sagen, nicht als erledigt melden.")
    else:
        out["verified"] = "Alles zurückgelesen: steht so in Bring!."
    if any(r.get("flags_saved") is False for r in results):
        out["note"] = ("Bring! hat die Markierungen (dringend/Angebot) nicht übernommen. Tipp: einmal in der Bring! "
                       "App einen Artikel als dringend markieren, dann bring_get_list aufrufen; danach kennt der "
                       "Server das Format und es klappt.")
    return _dump(out)


@mcp.tool()
async def bring_remove_items(
    names: Annotated[list[str], Field(min_length=1, description="Item names to take off the list.")],
    mark_as_bought: Annotated[bool, Field(
        description="true = move to 'zuletzt gekauft' like ticking it off in the app, false = delete.")] = False,
    list_name: Annotated[str | None, Field(description="Other list than the selected one.")] = None,
) -> str:
    """Remove items from the user's Bring! list or tick them off as bought."""
    entry = await _bring_list(list_name)
    try:
        done = await bring.change(entry["uuid"], [{"name": n} for n in names],
                                  "TO_RECENTLY" if mark_as_bought else "REMOVE")
    except BringError as exc:
        raise ToolError(str(exc)) from exc
    return _dump({"list": entry["name"], "items": done})


@mcp.tool()
async def bring_disconnect() -> str:
    """Disconnect Bring!: deletes the stored session token from this computer."""
    bring.forget()
    data = prefs.load()
    data.pop("bring_list", None)
    prefs.save(data)
    return _dump({"disconnected": True})


@mcp.prompt(name="wocheneinkauf", description="Wocheneinkauf anhand der aktuellen Prospekte planen")
def wocheneinkauf(einkaufsliste: str = "meine Bring! Liste", woche: str = "aktuelle Woche") -> str:
    return (
        f"Plane meinen Einkauf für die {woche} mit kaufDA.\n"
        f"Einkaufsliste: {einkaufsliste}\n\n"
        "1. Prüfe mit get_preferences Standort, Lieblingsmärkte, meine Apps/Kundenkarten und ob ich Bring! nutze "
        "(frag nach, falls etwas fehlt).\n"
        "2. Nutze shopping_list (bei meiner Bring! Liste mit from_bring_list=true; when='upcoming' für nächste "
        "Woche).\n"
        "3. Stell mir den empfohlenen Plan vor und die anderen Pläne (z.B. weniger Märkte, etwas teurer), mit "
        "meinem Preis, Gültigkeit (ab wann / bis wann), Bedingungen wie App Preise oder Mengenrabatte und Links.\n"
        "4. Bei Sammelbegriffen wie 'Trinken' schlag mir die besten echten Deals vor; unsichere Treffer prüfst du "
        "mit view_offer. Sag mir, wo ich mit einer App, die ich nicht habe, noch sparen könnte, und was nirgends "
        "im Angebot ist.\n"
        "5. Frag mich direkt, welchen Plan und welche Alternativen ich will, und trag es erst nach meinem OK mit "
        "bring_add_items in Bring! ein: kurzer Titel, als Beschreibung nur der Markt (bzw. meine Menge plus Markt), "
        "Im Angebot markiert. Keine Preise oder Produkte in die Beschreibung."
    )


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("kaufda-mcp")
    except Exception:
        return "unbekannt"


def _bring_cli(command: str) -> int:
    """Terminal helpers for Bring!, e.g. for servers without a browser."""
    store = KeyringStore()
    if command == "bring-logout":
        store.delete()
        print("Bring! Sitzung gelöscht.")
        return 0
    if command == "bring-login":
        import getpass

        available, backend = KeyringStore.available()
        if not available:
            print(f"Kein Schlüsselspeicher verfügbar ({backend}). Nutze BRING_EMAIL und BRING_PASSWORD.")
            return 1
        email = input("Bring! Mailadresse: ").strip()
        password = getpass.getpass("Bring! Passwort (wird nicht gespeichert): ")
        try:
            session = login_sync(email, password)
            store.save(session)
        except BringError as exc:
            print(f"Fehler: {exc}")
            return 1
        print(f"Verbunden als {session.get('email')}. Gespeichert im Schlüsselspeicher ({backend}).")
    try:
        lists = asyncio.run(BringClient(store).lists())
    except BringError as exc:
        print(f"Nicht verbunden: {exc}")
        return 1
    print("Bring! Listen: " + ", ".join(entry["name"] for entry in lists))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="kaufda-mcp", description="kaufDA MCP server")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "bring-login", "bring-logout", "bring-status"],
                        help="serve (Standard) startet den MCP Server; bring-login/-logout/-status verwalten Bring!")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("KAUFDA_MCP_LOGLEVEL", "WARNING"))
    if args.command != "serve":
        sys.exit(_bring_cli(args.command))
    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
