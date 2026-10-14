"""Offline tests for the Bring! integration against a simulated Bring! API.
Run: uv run python tests/test_bring.py"""
import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import httpx

from kaufda_mcp import bring as B

CALLS: list[tuple[str, str, object]] = []
STATE = {"token": "tok-1", "refresh": "ref-1", "n": 1, "items": [{"uuid": "u-butter", "itemId": "Butter", "specification": ""},
                                     {"uuid": "u-rahm", "itemId": "Rahm", "specification": "200 g"}]}


def handler(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    body = request.content.decode() if request.content else ""
    CALLS.append((method, path, body))
    if request.url.host == "web.getbring.com":
        return httpx.Response(200, json={"Rahm": "Sahne", "Butter": "Butter", "Brot": "Brot"})
    assert request.headers["X-BRING-API-KEY"] == B.BRING_HEADERS["X-BRING-API-KEY"]
    if path == "/rest/v2/bringauth":
        form = urllib.parse.parse_qs(body)
        if form.get("password") != ["richtig"]:
            return httpx.Response(401, json={"message": "invalid"})
        return httpx.Response(200, json={"uuid": "U1", "publicUuid": "P1", "bringListUUID": "L1", "email": "a@b.de",
                                         "access_token": "tok-1", "refresh_token": "ref-1", "token_type": "Bearer",
                                         "expires_in": 604800})
    if path == "/rest/v2/bringauth/token":
        if urllib.parse.parse_qs(body).get("refresh_token") != [STATE["refresh"]]:
            return httpx.Response(401, json={"message": "invalid refresh token"})
        STATE["n"] += 1
        STATE["token"], STATE["refresh"] = f"tok-{STATE['n']}", f"ref-{STATE['n']}"
        return httpx.Response(200, json={"access_token": STATE["token"], "refresh_token": STATE["refresh"],
                                         "token_type": "Bearer", "expires_in": 604800})
    if request.headers.get("Authorization") != f"Bearer {STATE['token']}":
        return httpx.Response(401)
    if path == "/rest/bringusers/U1/lists":
        return httpx.Response(200, json={"lists": [{"listUuid": "L1", "name": "Zuhause", "theme": "x"},
                                                   {"listUuid": "L2", "name": "Büro", "theme": "x"}]})
    if path == "/rest/bringusersettings/U1":
        return httpx.Response(200, json={"usersettings": [], "userlistsettings": [
            {"listUuid": "L1", "usersettings": [{"key": "listArticleLanguage", "value": "de-DE"}]}]})
    if path == "/rest/v2/bringlists/L1" and method == "GET":
        return httpx.Response(200, json={"uuid": "L1", "status": "SHARED",
                                         "items": {"purchase": STATE["items"], "recently": []}})
    if path == "/rest/v2/bringlists/L1/items" and method == "PUT":
        return httpx.Response(200)
    return httpx.Response(404)


def test_login_sync():
    session = B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(handler))
    assert session["uuid"] == "U1" and session["refresh_token"] == "ref-1" and "password" not in json.dumps(session)
    try:
        B.login_sync("a@b.de", "falsch", transport=httpx.MockTransport(handler))
        raise AssertionError("wrong password accepted")
    except B.BringAuthError:
        pass


async def _client_flow():
    store = B.MemoryStore()
    store.save(B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(handler)))
    store.data["expires_at"] = time.time() - 10  # expired -> refresh
    client = B.BringClient(store, transport=httpx.MockTransport(handler))
    lists = await client.lists()
    assert [entry["name"] for entry in lists] == ["Zuhause", "Büro"]
    assert store.data["access_token"] == "tok-2" and store.data["refresh_token"] == "ref-2"
    items = await client.items("L1")
    assert {i["name"] for i in items["purchase"]} == {"Butter", "Sahne"}  # catalog id "Rahm" shown as "Sahne"
    CALLS.clear()
    await client.change("L1", [{"name": "Sahne", "specification": "Lidl 0,79 €, bis Sa 17.10."},
                               {"name": "Butter", "specification": "Netto 0,99 €"},
                               {"name": "Grillkohle"}], "TO_PURCHASE")
    put = [json.loads(body) for method, path, body in CALLS if method == "PUT"][0]
    changes = {c["itemId"]: c for c in put["changes"]}
    assert changes["Rahm"]["spec"] == "Lidl 0,79 €, bis Sa 17.10." and changes["Rahm"]["uuid"] == "u-rahm"
    assert changes["Butter"]["uuid"] == "u-butter" and changes["Grillkohle"]["operation"] == "TO_PURCHASE"
    STATE["token"] = "revoked"  # server side invalidation -> 401 -> refresh -> retry
    assert len(await client.lists()) == 2 and store.data["refresh_token"] == "ref-3"
    STATE["token"], STATE["refresh"] = "revoked", "revoked"  # refresh token dead too -> clean auth error
    try:
        await client.lists()
        raise AssertionError("dead session accepted")
    except B.BringAuthError as exc:
        assert "neu verbinden" in str(exc) and store.data is None
    client.forget()
    assert store.data is None


def test_client_flow():
    asyncio.run(_client_flow())


def _post(url, data, host=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode()


def test_login_page():
    received = {}
    page = B.LoginPage(on_success=received.update,
                       login=lambda e, p: B.login_sync(e, p, transport=httpx.MockTransport(handler)), ttl_seconds=30)
    url = page.start()
    assert url.startswith("http://127.0.0.1:") and len(url.rsplit("/", 1)[1]) >= 30
    with urllib.request.urlopen(url, timeout=5) as resp:
        html = resp.read().decode()
        assert "Bring! mit kaufDA verbinden" in html and resp.headers["Cache-Control"] == "no-store"
    base = url.rsplit("/", 1)[0]
    try:
        urllib.request.urlopen(base + "/falsch", timeout=5)
        raise AssertionError("wrong token accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    try:
        _post(url, {"email": "a@b.de", "password": "richtig"}, host="evil.example")
        raise AssertionError("foreign Host header accepted")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    status, body = _post(url, {"email": "a@b.de", "password": "falsch"})
    assert status == 200 and "fehlgeschlagen" in body and not received
    status, body = _post(url, {"email": "a@b.de", "password": "richtig"})
    assert "Verbunden" in body and received["uuid"] == "U1" and "password" not in received
    time.sleep(0.5)
    try:
        urllib.request.urlopen(url, timeout=2)
        raise AssertionError("page still open after login")
    except (urllib.error.URLError, ConnectionError):
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
