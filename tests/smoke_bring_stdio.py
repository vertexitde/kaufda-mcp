"""Bring! flow over the MCP protocol (without a real Bring! account).
Run: uv run python tests/smoke_bring_stdio.py"""
import asyncio, json, os, sys, tempfile, urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    env = dict(os.environ, KAUFDA_MCP_PREFS=str(Path(tempfile.mkdtemp()) / "prefs.json"))
    env.pop("BRING_EMAIL", None), env.pop("BRING_PASSWORD", None)
    params = StdioServerParameters(command=sys.executable, args=["-m", "kaufda_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            print("bring tools:", [t for t in tools if t.startswith("bring_")])

            async def call(name, **args):
                res = await session.call_tool(name, args)
                return res.isError, next((c.text for c in res.content if c.type == "text"), "")

            print("prefs hint:", json.loads((await call("get_preferences"))[1]).get("hint_shopping_list", "")[:80])
            print("set_preferences:", (await call("set_preferences", location="24534", stores=["Lidl", "Netto", "Penny"],
                                                  apps=["Lidl Plus"], shopping_list_app="bring"))[1])
            print("bring_status:", (await call("bring_status"))[1])
            err, text = await call("bring_connect")
            data = json.loads(text)
            print("bring_connect:", err, data["login_url"][:40] + "...", "|", data["secure_storage"])
            with urllib.request.urlopen(data["login_url"], timeout=5) as resp:
                print("Login Seite:", resp.status, "Formular da:", "Bring! mit kaufDA verbinden" in resp.read().decode())
            err, text = await call("bring_add_items", items=[{"name": "Butter"}])
            print("bring_add_items ohne Login -> isError", err, "|", text[:110])
            sl = json.loads((await call("shopping_list", items=["Butter", "Eier", "Grillkohle"], alternatives=0))[1])
            print("bring_items aus shopping_list:")
            for it in sl["bring_items"]:
                print("   ", it)

asyncio.run(main())
