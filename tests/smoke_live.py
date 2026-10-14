"""Live smoke test against kaufda.de. Run: uv run python tests/smoke_live.py"""
import asyncio, json, os, sys, tempfile, time
from pathlib import Path

os.environ["KAUFDA_MCP_PREFS"] = str(Path(tempfile.mkdtemp()) / "prefs.json")
from kaufda_mcp import server as S  # noqa: E402


def show(title, text, n=1800):
    print(f"\n===== {title} =====")
    print(text if len(text) <= n else text[:n] + f" ... [{len(text)} chars]")


async def main():
    t = time.perf_counter()
    show("get_preferences (leer)", await S.get_preferences())
    show("set_preferences", await S.set_preferences(location="24534", stores=["Lidl", "Edeka", "Penny", "Aldi", "Netto"]))
    show("search_offers Butter", await S.search_offers("Butter", max_results=6))
    show("search_offers Butter upcoming", await S.search_offers("Butter", when="upcoming", max_results=4))
    show("search_offers Kaffee unit_price alle Händler", await S.search_offers("Kaffee Bohnen", only_favorites=False, sort="unit_price", max_results=4))
    show("shopping_list", await S.shopping_list(["Butter", "Eier", "Hackfleisch", "Bananen", "Zahnpasta"]), 3500)
    lb = await S.list_brochures()
    show("list_brochures (Favoriten)", lb, 2500)
    bid = json.loads(lb)["brochures"][0]["brochure_id"]
    show("get_brochure_offers query=Käse", await S.get_brochure_offers(bid, query="Käse", max_results=3))
    page = await S.view_brochure_page(bid, page=1)
    print("\n===== view_brochure_page =====", type(page[0]).__name__, len(page[0].data), "bytes;", page[1][:600])
    offers = json.loads(await S.get_brochure_offers(bid, max_results=1))["offers"]
    vo = await S.view_offer(offers[0]["offer_id"], bid)
    print("\n===== view_offer =====", [type(b).__name__ for b in vo], vo[-1][:500])
    show("suggest", await S.suggest_search_terms("Hafer"))
    show("valid_on Samstag, Kiel", await S.search_offers("Milch", location="Kiel", valid_on="Samstag", max_results=3))
    print(f"\nTotal {time.perf_counter() - t:.1f}s")

asyncio.run(main())
