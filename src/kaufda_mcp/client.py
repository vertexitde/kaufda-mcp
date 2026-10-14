"""HTTP access to kaufDA (search, shelf, brochure viewer) and geocoding."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger("kaufda_mcp")

KAUFDA = "https://www.kaufda.de"
VIEWER_API = "https://content-viewer-be.kaufda.de/v1"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MEDIA_HOSTS = ("content-media.bonial.biz", "publisher-media.bonial.biz")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
GEOCODER_UA = "kaufda-mcp/0.1 (private MCP server for personal grocery planning)"

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class KaufdaError(RuntimeError):
    pass


def check_uuid(value: str, what: str = "ID") -> str:
    value = (value or "").strip()
    if not _UUID.match(value):
        raise KaufdaError(f"Ungültige {what}: {value!r} (erwartet UUID wie 6a40c781-41fb-45eb-a279-398e8d00acd9)")
    return value.lower()


class _TTLCache:
    def __init__(self, max_items: int = 300):
        self._data: dict[str, tuple[float, Any]] = {}
        self._max = max_items

    def get(self, key: str) -> Any:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max:
            for old in sorted(self._data, key=lambda k: self._data[k][0])[: self._max // 4]:
                self._data.pop(old, None)
        self._data[key] = (time.monotonic() + ttl, value)


class KaufdaClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0),
            follow_redirects=True,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "de-DE,de;q=0.9"},
        )
        self._cache = _TTLCache()
        self._sem = asyncio.Semaphore(6)
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._geo_lock = asyncio.Lock()
        self._geo_last = 0.0

    # ------------------------------------------------------------ plumbing

    async def _fetch(self, key: str, url: str, params: dict[str, str] | None, ttl: float,
                     binary: bool, headers: dict[str, str] | None) -> Any:
        async with self._sem:
            try:
                resp = await self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise KaufdaError(f"Netzwerkfehler bei {url}: {exc}") from exc
        if resp.status_code != 200:
            raise KaufdaError(f"HTTP {resp.status_code} bei {resp.url}")
        if binary:
            value: Any = resp.content
        else:
            try:
                value = resp.json()
            except ValueError as exc:
                raise KaufdaError(f"Keine JSON Antwort von {resp.url}") from exc
        if ttl > 0:
            self._cache.set(key, value, ttl)
        return value

    async def _get(self, url: str, params: dict[str, str] | None = None, *, ttl: float = 900,
                   binary: bool = False, headers: dict[str, str] | None = None) -> Any:
        key = f"{'bin' if binary else 'json'}:{url}?{json.dumps(params or {}, sort_keys=True)}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key, url, params, ttl, binary, headers))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    @staticmethod
    def _loc(lat: float, lng: float) -> dict[str, str]:
        return {"lat": f"{lat:.6f}", "lng": f"{lng:.6f}"}

    # ------------------------------------------------------------ kaufDA

    async def search(self, query: str, lat: float, lng: float, *, limit: int = 100,
                     sort: str | None = None) -> dict[str, Any]:
        params = {"query": query, **self._loc(lat, lng), "offset": "0", "limit": str(limit)}
        if sort:
            params["sort"] = sort
        data = await self._get(f"{KAUFDA}/api/search", params, ttl=900)
        return (data or {}).get("searchResults") or {}

    async def suggest(self, query: str) -> list[dict[str, Any]]:
        data = await self._get(f"{KAUFDA}/api/suggest", {"query": query}, ttl=3600)
        return (data or {}).get("suggestions") or []

    async def shelf(self, lat: float, lng: float, *, size: int = 100, page: int = 0,
                    sector_ids: str = "") -> dict[str, Any]:
        params = {**self._loc(lat, lng), "size": str(size), "page": str(page), "sectorIds": sector_ids}
        return await self._get(f"{KAUFDA}/api/shelf", params, ttl=900) or {}

    async def brochure_meta(self, brochure_id: str, lat: float, lng: float) -> dict[str, Any]:
        bid = check_uuid(brochure_id, "Prospekt ID")
        params = {"partner": "kaufda_web", **self._loc(lat, lng)}
        data = await self._get(f"{VIEWER_API}/brochures/{bid}", params, ttl=3600)
        return (data or {}).get("content") or {}

    async def brochure_pages(self, brochure_id: str, lat: float, lng: float) -> list[dict[str, Any]]:
        bid = check_uuid(brochure_id, "Prospekt ID")
        params = {"partner": "kaufda_web", "brochureKey": "", **self._loc(lat, lng)}
        data = await self._get(f"{VIEWER_API}/brochures/{bid}/pages", params, ttl=3600)
        pages = (data or {}).get("contents") or []
        return sorted(pages, key=lambda p: p.get("number", 0))

    async def image(self, url: str) -> bytes:
        host = httpx.URL(url).host
        if host not in MEDIA_HOSTS:
            raise KaufdaError(f"Bild Host nicht erlaubt: {host}")
        return await self._get(url, None, ttl=600, binary=True)

    # ------------------------------------------------------------ geocoding

    async def geocode(self, query: str) -> dict[str, Any]:
        """Resolve a German PLZ, town or address to coordinates (OpenStreetMap Nominatim)."""
        text = query.strip()
        if not text:
            raise KaufdaError("Leerer Standort")
        params = {"format": "jsonv2", "limit": "1", "addressdetails": "1",
                  "countrycodes": "de", "accept-language": "de"}
        if re.fullmatch(r"\d{5}", text):
            params["postalcode"] = text
        else:
            params["q"] = text
        key = f"geo:{json.dumps(params, sort_keys=True)}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        async with self._geo_lock:  # Nominatim policy: max 1 request per second
            wait = 1.1 - (time.monotonic() - self._geo_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._http.get(NOMINATIM, params=params, headers={"User-Agent": GEOCODER_UA})
            except httpx.HTTPError as exc:
                raise KaufdaError(f"Geocoding fehlgeschlagen: {exc}") from exc
            finally:
                self._geo_last = time.monotonic()
        if resp.status_code != 200:
            raise KaufdaError(f"Geocoding HTTP {resp.status_code}")
        hits = resp.json()
        if not hits:
            raise KaufdaError(f"Ort nicht gefunden: {query!r}. Bitte PLZ oder 'Ort, Straße' angeben.")
        hit = hits[0]
        addr = hit.get("address") or {}
        town = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("municipality") or addr.get("county"))
        street = " ".join(x for x in (addr.get("road"), addr.get("house_number")) if x)
        label = ", ".join(x for x in (street, " ".join(y for y in (addr.get("postcode"), town) if y)) if x)
        result = {
            "query": query,
            "label": label or hit.get("display_name"),
            "lat": round(float(hit["lat"]), 6),
            "lng": round(float(hit["lon"]), 6),
            "zip": addr.get("postcode"),
            "city": town,
        }
        self._cache.set(key, result, 86400)
        return result
