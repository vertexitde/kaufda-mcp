"""Start the server over stdio like an MCP client would and exercise the tools.
Run: uv run python tests/smoke_stdio.py"""
import asyncio, json, os, sys, tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ, KAUFDA_MCP_PREFS=str(Path(tempfile.mkdtemp()) / "prefs.json"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "kaufda_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.serverInfo.name, "| instructions:", len(init.instructions or ""), "chars")
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            prompts = await session.list_prompts()
            print("prompts:", [p.name for p in prompts.prompts])

            async def call(name, **args):
                res = await session.call_tool(name, args)
                kinds = [c.type for c in res.content]
                text = next((c.text for c in res.content if c.type == "text"), "")
                return res.isError, kinds, text

            err, kinds, text = await call("search_offers", query="Butter")
            print("search_offers ohne Standort -> isError", err, "|", text[:160])
            print("set_preferences:", (await call("set_preferences", location="24534", stores=["Lidl", "Netto", "Penny"]))[2][:200])
            err, kinds, text = await call("search_offers", query="Butter", max_results=3)
            data = json.loads(text)
            print("search_offers:", err, data["recognized_category"], data["total_offers"])
            for o in data["offers"]:
                print("   ", o["retailer"], "|", o.get("brand"), o["product"], "|", o.get("price_text"), o.get("conditions"), o.get("conditional_prices"), "|", o["validity"], "|", o["status"])
            netto = json.loads((await call("search_offers", query="Butter", stores=["Netto mit dem Scottie"]))[2])
            for o in netto["offers"][:3]:
                print("   Scottie:", o.get("brand"), o["product"], o.get("price_text"), "| app:", o.get("conditional_prices"))
            milch = json.loads((await call("search_offers", query="Milch", max_results=2))[2])
            print("Milch category:", milch["recognized_category"])
            o = data["offers"][0]
            err, kinds, text = await call("view_offer", offer_id=o["offer_id"], brochure_id=o["brochure_id"])
            print("view_offer:", err, kinds, text[:120])
            err, kinds, text = await call("view_brochure_page", brochure_id=o["brochure_id"], page=o["page"])
            print("view_brochure_page:", err, kinds, text[:120])
            err, kinds, text = await call("view_brochure_page", brochure_id=o["brochure_id"], page=999)
            print("bad page -> isError", err, text[:120])
            err, kinds, text = await call("get_brochure_offers", brochure_id="nope")
            print("bad id -> isError", err, text[:120])
            p = await session.get_prompt("wocheneinkauf", {"einkaufsliste": "Butter, Eier"})
            print("prompt ok:", p.messages[0].content.text[:80].replace("\n", " "))

asyncio.run(main())
