"""Shopping plan end to end: real kaufDA offers, simulated Bring! list.
Run: uv run python tests/smoke_plan.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

os.environ["KAUFDA_MCP_PREFS"] = str(Path(tempfile.mkdtemp()) / "prefs.json")
sys.path.insert(0, str(Path(__file__).parent))
import test_bring as TB  # noqa: E402  (simulated Bring! backend)
from kaufda_mcp import bring as B  # noqa: E402
from kaufda_mcp import server as S  # noqa: E402


async def main():
    TB.reset()
    TB.STATE["items"] = [
        {"uuid": "u1", "itemId": "Milch", "specification": "2 l"},
        {"uuid": "u2", "itemId": "Butter", "specification": "bei Edeka: alte Notiz 1,99 €"},
        {"uuid": "u3", "itemId": "Eier", "specification": "",
         "attributes": [{"type": "PURCH_CONDITIONS", "content": {"urgent": True}}]},
        {"uuid": "u4", "itemId": "Hackfleisch", "specification": "500 g"},
        {"uuid": "u5", "itemId": "Trinken", "specification": ""},
        {"uuid": "u6", "itemId": "Kaffee", "specification": ""},
        {"uuid": "u7", "itemId": "Glühbirnen", "specification": "E27"},
    ]
    store = B.MemoryStore()
    store.save(B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(TB.handler)))
    S.bring = B.BringClient(store, transport=httpx.MockTransport(TB.handler))
    await S.set_preferences(location="24534", stores=["Lidl", "Penny", "Netto", "Aldi", "Edeka"],
                            apps=["Lidl Plus"], shopping_list_app="bring")
    await S.bring_select_list("Zuhause")
    print("Bring Liste:", json.loads(await S.bring_get_list())["to_buy"])

    plan = json.loads(await S.shopping_list(from_bring_list=True))
    rec = plan["recommended"]
    print("\nQuelle:", plan["source"])
    print("EMPFEHLUNG:", rec["label"], "|", rec["covered"], "|", rec["total_eur"], "€ | gespart", rec.get("savings_eur"),
          "\n  warum:", rec["why"])
    for store_name, block in rec["by_store"].items():
        print(f"  {store_name}: {block['shopping_window']} {block.get('warning', '')}")
        for it in block["items"]:
            print(f"     {it['item']:<12} {it.get('note', ''):<6} x{it.get('count', 1)} {it['product'][:34]:<34} "
                  f"{it.get('price_text', '')}  {it.get('cost_eur', '')} {it.get('remark', '')}")
    print("ANDERE PLÄNE:")
    for p in plan.get("other_plans", []):
        print(f"  {p['label']:<40} {p['covered']} {p['total_eur']:>6} €  fehlt: {p.get('not_on_offer_here', [])}")
    print("AUSWÄHLEN:")
    for c in plan.get("choose", []):
        print(f"  {c['item']}: {c['why']}")
        for o in c["options"]:
            print(f"     {o['retailer']:<10} {o['product'][:36]:<36} {o.get('price_text', '')} -{o.get('discount_pct', 0)}%")
    print("NICHT GEFUNDEN:", plan.get("not_found"))
    print("BRING UPDATES:")
    for u in plan["bring_updates"]:
        print("  ", u)

    res = json.loads(await S.bring_add_items(items=plan["bring_updates"]))
    print("\nbring_add_items:", [(r["name"], r["result"], r.get("flags_saved")) for r in res["items"]])
    print("Liste danach:")
    for i in json.loads(await S.bring_get_list())["to_buy"]:
        print("  ", i)


asyncio.run(main())
