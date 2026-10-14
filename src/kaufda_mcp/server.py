"""kaufDA MCP server: search grocery offers, read brochures, build shopping lists."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from collections import Counter
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import normalize as N
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
searches are then limited to those shops by default (only_favorites=true).
2. search_offers for single products, shopping_list for several items at once.
3. Validity is evaluated per offer against the current date ("today" in every answer): many offers \
start mid-week (e.g. Thursday) and next week's brochures are often already online. Every offer has \
valid_from/valid_until, valid_today and a status like "aktiv", "aktiv, endet heute", \
"demnächst, ab morgen Do 15.10.". Never present an offer with valid_today=false as available today, \
always say from which day it applies. If the user names a shopping day, pass valid_on (e.g. "Donnerstag"); \
for shopping today use when="now". shopping_list returns a shopping_window per store (the days on which \
all picks of that store are valid together). Offers with conditions ("Nur mit App", "Mit Lidl Plus", \
"ab 2 Stück") or conditional_prices only apply under that condition.
4. To show or verify an offer use its cutout_url (kaufDA's clipping from the brochure incl. price) or call \
view_offer, which returns that clipping as an image (mode="context" adds the surroundings when the price or \
a date banner is cut off). Never present any other picture as the offer. view_brochure_page shows the whole \
page. Give the user the viewer_url so they can open the brochure page on kaufda.de.
5. Product search is keyword based: use simple German product words ("Butter", "Hackfleisch", \
"Kaffee Bohnen"). suggest_search_terms helps to find good terms. Results with category_match=false are \
often only loosely related (e.g. "Butterkekse" for "Butter").
"""

mcp = FastMCP("kaufda", instructions=INSTRUCTIONS, log_level=os.environ.get("KAUFDA_MCP_LOGLEVEL", "WARNING").upper())
client = KaufdaClient()
prefs = PreferenceStore()

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
                  radius: float, sort: str, exact_validity: bool) -> dict[str, Any]:
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
            deals = N.deals_summary(detail.get("deals") or [])
            if deals.get("price"):
                # The search index flattens deals and may show an app-only price as the main price.
                for key in ("price", "price_text", "unit_price", "unit_price_value", "conditions",
                            "conditional_prices", "regular_price", "uvp", "price_note"):
                    offer.pop(key, None)
                offer.update(deals)
                N.estimate_unit_price(offer)
        if not validity.matches(when, valid_on, now):
            hidden_time += 1
            continue
        offer.update(validity.describe(now))
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
    """Show the saved home location, favourite stores and search radius. Call this first."""
    data = prefs.load()
    out = {
        "location": data.get("location"),
        "stores": data.get("stores") or [],
        "max_distance_km": prefs.radius_km(),
        "file": str(prefs.path),
    }
    if not data.get("location"):
        out["hint"] = "Kein Standort gespeichert: Nutzer nach PLZ/Ort fragen und set_preferences(location=...) aufrufen."
    if not data.get("stores"):
        out["hint_stores"] = "Keine Lieblingsmärkte gespeichert: es wird bei allen Händlern gesucht."
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
) -> str:
    """Save where the user lives and where they like to shop. Persists across sessions."""
    data = prefs.load()
    if location is not None:
        try:
            data["location"] = await client.geocode(location)
        except KaufdaError as exc:
            raise ToolError(str(exc)) from exc
    current = list(data.get("stores") or [])
    if stores is not None:
        current = [s.strip() for s in stores if s and s.strip()]
    for store in add_stores or []:
        if store.strip() and not any(N.norm(store) == N.norm(c) for c in current):
            current.append(store.strip())
    for store in remove_stores or []:
        current = [c for c in current if not (N.norm(store) == N.norm(c) or N.store_matches(store, c))]
    data["stores"] = current
    if max_distance_km is not None:
        data["max_distance_km"] = max(0.0, float(max_distance_km))
    prefs.save(data)
    return _dump({"saved": True, "location": data.get("location"), "stores": current,
                  "max_distance_km": prefs.radius_km()})


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
        description="Load the brochures to get per-offer validity (e.g. offers starting Thursday) and app-only "
                    "prices. Slightly slower.")] = True,
) -> str:
    """Search current and upcoming offers for a product near the user, with price, unit price,
    validity period (from when / until when), retailer, brochure page and a kaufda.de link."""
    loc = await _location(location)
    wanted, scope = _store_filter(stores, only_favorites)
    try:
        result = await _search(query, loc, wanted=wanted, when=when, valid_on=_valid_on(valid_on),
                               radius=_radius(max_distance_km), sort=sort, exact_validity=exact_validity)
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


_SLIM_KEYS = ("offer_id", "product", "brand", "description", "retailer", "price", "price_text", "unit_price",
              "unit_price_value",
              "conditions", "conditional_prices", "regular_price", "uvp", "validity", "status", "valid_today",
              "starts_in_days", "valid_from", "valid_until", "brochure_id", "page", "viewer_url", "cutout_url",
              "distance_km")


def _slim(offer: dict[str, Any]) -> dict[str, Any]:
    return {k: offer[k] for k in _SLIM_KEYS if k in offer}


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


@mcp.tool()
async def shopping_list(
    items: Annotated[list[str], Field(min_length=1, max_length=40,
                                      description="Products to buy, e.g. ['Butter', 'Eier', 'Hackfleisch'].")],
    location: LocationArg = None,
    stores: StoresArg = None,
    only_favorites: FavArg = True,
    when: WhenArg = "all",
    valid_on: ValidOnArg = None,
    max_distance_km: RadiusArg = None,
    alternatives: Annotated[int, Field(ge=0, le=5, description="Extra options per item.")] = 2,
) -> str:
    """Find the best current/upcoming offer for every item of a shopping list and group the picks by store.
    Ranked by unit price where comparable, otherwise by price; category matches are preferred."""
    loc = await _location(location)
    wanted, scope = _store_filter(stores, only_favorites)
    day = _valid_on(valid_on)
    radius = _radius(max_distance_km)
    results = await asyncio.gather(
        *(_search(item, loc, wanted=wanted, when=when, valid_on=day, radius=radius, sort="relevance",
                  exact_validity=True) for item in items),
        return_exceptions=True,
    )
    per_item, by_store, missing = [], {}, []
    total = 0.0
    for item, res in zip(items, results):
        if isinstance(res, BaseException):
            per_item.append({"item": item, "error": str(res)})
            continue
        offers = res["offers"]
        if not offers:
            missing.append(item)
            per_item.append(N.compact({"item": item, "found": False, "notes": res["notes"],
                                       "pages_without_extracted_offer": res["pages_without_extracted_offer"][:3]}))
            continue
        tiers: dict[int, list[dict[str, Any]]] = {}
        for o in offers:
            tiers.setdefault(N.relevance_tier(o, item), []).append(o)
        ranked = [o for tier in sorted(tiers) for o in _rank(tiers[tier])]
        best = ranked[0]
        total += best.get("price") or 0.0
        per_item.append(N.compact({
            "item": item,
            "match": N.RELEVANCE_LABELS[N.relevance_tier(best, item)],
            "best": _slim(best),
            "alternatives": [_slim(o) for o in ranked[1:1 + alternatives]],
        }))
        by_store.setdefault(best.get("retailer") or "?", []).append(N.compact({
            "item": item, "product": " ".join(x for x in (best.get("brand"), best.get("product")) if x),
            "price_text": best.get("price_text"), "conditions": best.get("conditions"),
            "validity": best.get("validity"), "status": best.get("status"), "page": best.get("page"),
            "viewer_url": best.get("viewer_url"), "cutout_url": best.get("cutout_url"),
            "_from": best.get("valid_from"), "_until": best.get("valid_until"),
        }))
    return _dump(N.compact({
        "location": loc.get("label"),
        "store_filter": scope,
        "today": N.fmt_day(N.now_berlin().date()),
        "by_store": {store: _store_plan(picks) for store, picks in by_store.items()},
        "estimated_total_eur": round(total, 2),
        "not_found": missing,
        "items": per_item,
    }))


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
    out = []
    for page in pages:
        for entry in page.get("offers") or []:
            content = entry.get("content") or {}
            offer = N.offer_from_viewer(content, retailer=retailer, now=now)
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
            offer = N.offer_from_viewer(content, retailer=retailer)
            area = ((content.get("parentContent") or {}).get("page") or {}).get("area")
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


@mcp.prompt(name="wocheneinkauf", description="Wocheneinkauf anhand der aktuellen Prospekte planen")
def wocheneinkauf(einkaufsliste: str, woche: str = "aktuelle Woche") -> str:
    return (
        f"Plane meinen Einkauf für die {woche} mit kaufDA.\n"
        f"Einkaufsliste: {einkaufsliste}\n\n"
        "1. Prüfe mit get_preferences Standort und Lieblingsmärkte (frag nach, falls etwas fehlt).\n"
        "2. Nutze shopping_list für die ganze Liste (when='upcoming' für nächste Woche).\n"
        "3. Prüfe unsichere Treffer (match 'Textsuche') mit view_offer und sortiere Unpassendes aus.\n"
        "4. Gib mir eine Liste pro Markt mit Preis, Gültigkeit (ab wann / bis wann), Bedingungen wie "
        "'Nur mit App' und dem kaufDA Link. Nenne am Ende, was nirgends im Angebot ist."
    )


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("kaufda-mcp")
    except Exception:
        return "unbekannt"


def main() -> None:
    parser = argparse.ArgumentParser(prog="kaufda-mcp", description="kaufDA MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("KAUFDA_MCP_LOGLEVEL", "WARNING"))
    if args.transport != "stdio":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
