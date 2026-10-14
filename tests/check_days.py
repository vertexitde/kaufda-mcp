"""Shows how validity and shopping windows depend on today's weekday.
Run: uv run python tests/check_days.py"""
import asyncio, json, os, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ["KAUFDA_MCP_PREFS"] = str(Path(tempfile.mkdtemp()) / "prefs.json")
from kaufda_mcp import normalize as N  # noqa: E402
from kaufda_mcp import server as S  # noqa: E402

ITEMS = ["Butter", "Eier", "Hackfleisch", "Milch"]


async def run(label, fake_now=None, **kw):
    patches = [mock.patch.object(N, "now_berlin", lambda: fake_now)] if fake_now else []
    for p in patches:
        p.start()
    try:
        out = json.loads(await S.shopping_list(ITEMS, alternatives=0, **kw))
    finally:
        for p in patches:
            p.stop()
    print(f"\n=== {label} | today {out['today']} ===")
    for store, plan in out["by_store"].items():
        print(f"  {store}: {plan.get('shopping_window')} {plan.get('warning', '')}")
        for it in plan["items"]:
            print(f"     {it['item']:<12} {it.get('product','')[:34]:<34} {it.get('price_text','')!s:<9} {it.get('status')}")


async def main():
    await S.set_preferences(location="24534", stores=["Lidl", "Netto", "Penny", "Aldi", "Edeka"])
    real = N.now_berlin()
    await run("jetzt")
    await run("nur heute gültig (when=now)", when="now")
    await run("Einkauf am Donnerstag (valid_on)", valid_on="Donnerstag")
    thu = (real + timedelta(days=(3 - real.weekday()) % 7)).replace(hour=10, minute=0)
    await run("simuliert: heute ist Donnerstag", fake_now=thu)

asyncio.run(main())
