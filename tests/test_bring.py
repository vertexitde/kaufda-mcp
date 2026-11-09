"""Offline tests for the Bring! integration against a simulated, stateful Bring! API.
Run: uv run python tests/test_bring.py"""
import asyncio
import contextlib
import json
import os
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import httpx
import keyring
import keyring.backend
import keyring.errors
from keyring.backends import fail, null

from kaufda_mcp import bring as B

CALLS: list[tuple[str, str, str]] = []
STATE: dict = {}


def reset(accepted_attribute_type="PURCH_CONDITIONS"):
    STATE.clear()
    STATE.update(token="tok-1", refresh="ref-1", n=1, attr_type=accepted_attribute_type, recently=[], items=[
        {"uuid": "u-butter", "itemId": "Butter", "specification": "2 Stück"},
        {"uuid": "u-rahm", "itemId": "Rahm", "specification": "200 g · bei Lidl: Milbona Sahne 0,79 € bis Sa 17.10."},
    ])


def _find(item_id):
    return next((i for i in STATE["items"] if i["itemId"] == item_id), None)


def handler(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    body = request.content.decode() if request.content else ""
    CALLS.append((method, path, body))
    if request.url.host == "web.getbring.com":
        return httpx.Response(200, json={"Rahm": "Sahne", "Butter": "Butter", "Brot": "Brot"})
    assert request.headers["X-BRING-API-KEY"] == B.BRING_HEADERS["X-BRING-API-KEY"]
    if path == "/rest/v2/bringauth":
        if urllib.parse.parse_qs(body).get("password") != ["richtig"]:
            return httpx.Response(401, json={"message": "invalid"})
        return httpx.Response(200, json={"uuid": "U1", "publicUuid": "P1", "bringListUUID": "L1", "email": "a@b.de",
                                         "access_token": "tok-1", "refresh_token": "ref-1", "token_type": "Bearer",
                                         "expires_in": 604800})
    if path == "/rest/v2/bringauth/token":
        if STATE.get("token_status"):
            return httpx.Response(STATE["token_status"])
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
                                         "items": {"purchase": STATE["items"], "recently": STATE["recently"]}})
    if path == "/rest/v2/bringlists/L1" and method == "PUT":  # legacy, name addressed
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()}
        if form.get("purchase"):
            item = _find(form["purchase"])
            if item:
                item["specification"] = form.get("specification", "")
            else:
                STATE["items"].append({"uuid": str(uuid.uuid4()), "itemId": form["purchase"],
                                       "specification": form.get("specification", "")})
        if form.get("remove"):
            STATE["items"] = [i for i in STATE["items"] if i["itemId"] != form["remove"]]
        if form.get("recently"):
            item = _find(form["recently"])
            STATE["items"] = [i for i in STATE["items"] if i["itemId"] != form["recently"]]
            if item:
                STATE["recently"].append(item)
        return httpx.Response(204)
    if path == "/rest/v2/bringlists/L1/items" and method == "PUT":
        for change in json.loads(body)["changes"]:
            item = next((i for i in STATE["items"] if i["uuid"] == change["uuid"]), None)
            attr = change.get("attribute") or {}
            if change["operation"] == "ATTRIBUTE_UPDATE" and item and attr.get("type") == STATE["attr_type"]:
                item["attributes"] = [{"type": attr["type"], "content": attr["content"]}]
        return httpx.Response(200)
    return httpx.Response(404)


def _client():
    store = B.MemoryStore()
    store.save(B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(handler)))
    return store, B.BringClient(store, transport=httpx.MockTransport(handler))


def test_login_sync():
    reset()
    session = B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(handler))
    assert session["uuid"] == "U1" and session["refresh_token"] == "ref-1" and "password" not in json.dumps(session)
    try:
        B.login_sync("a@b.de", "falsch", transport=httpx.MockTransport(handler))
        raise AssertionError("wrong password accepted")
    except B.BringAuthError:
        pass


async def _session_flow():
    reset()
    store, client = _client()
    store.data["expires_at"] = time.time() - 10  # expired -> refresh
    assert [entry["name"] for entry in await client.lists()] == ["Zuhause", "Büro"]
    assert store.data["access_token"] == "tok-2" and store.data["refresh_token"] == "ref-2"
    STATE["token"] = "revoked"  # server side invalidation -> 401 -> refresh -> retry
    assert len(await client.lists()) == 2 and store.data["refresh_token"] == "ref-3"
    STATE["token"], STATE["refresh"] = "revoked", "revoked"  # refresh token dead too
    try:
        await client.lists()
        raise AssertionError("dead session accepted")
    except B.BringAuthError as exc:
        assert "neu verbinden" in str(exc) and store.data is None


def test_session_flow():
    asyncio.run(_session_flow())


async def _items_flow():
    reset()
    _, client = _client()
    items = await client.items("L1")
    assert {i["name"] for i in items["purchase"]} == {"Butter", "Sahne"}  # catalog id "Rahm" shown as "Sahne"
    res = await client.change("L1", [
        {"name": "Sahne", "specification": "200 g · bei Penny: Sahne 0,69 € bis Sa 17.10.", "discounted": True},
        {"name": "Butter"},  # no specification -> keep "2 Stück"
        {"name": "Grillkohle", "specification": "", "urgent": True},
    ], "TO_PURCHASE")
    by = {r["name"]: r for r in res}
    assert _find("Rahm")["specification"].startswith("200 g · bei Penny")
    assert _find("Butter")["specification"] == "2 Stück" and by["Butter"]["result"] == "aktualisiert"
    assert by["Grillkohle"]["result"] == "hinzugefügt" and by["Grillkohle"]["flags_saved"] is True
    assert by["Sahne"]["flags_saved"] is True and by["Sahne"]["flags"] == {"discounted": True}
    assert all(r["on_list"] for r in res) and by["Sahne"]["spec_saved"] and by["Grillkohle"]["spec_saved"]
    after = {i["name"]: i for i in (await client.items("L1"))["purchase"]}
    assert after["Sahne"].get("discounted") and after["Grillkohle"].get("urgent")
    res = await client.change("L1", [{"name": "Butter"}, {"name": "Gibtsnicht"}], "TO_RECENTLY")
    assert [r["result"] for r in res] == ["als gekauft abgehakt", "stand nicht auf der Liste"]
    assert res[0]["verified"] is True and "verified" not in res[1]
    await client.change("L1", [{"name": "Grillkohle"}], "REMOVE")
    assert {i["itemId"] for i in STATE["items"]} == {"Rahm"} and STATE["recently"][0]["itemId"] == "Butter"


def test_items_flow():
    asyncio.run(_items_flow())


async def _attribute_type_is_learned():
    reset(accepted_attribute_type="ITEM_FLAGS")  # Bring! uses another name than our default guess
    _, client = _client()
    res = await client.change("L1", [{"name": "Butter", "urgent": True}], "TO_PURCHASE")
    assert res[0]["flags_saved"] is False and not client.attribute_type_verified
    _find("Rahm")["attributes"] = [{"type": "ITEM_FLAGS", "content": {"urgent": True}}]  # user marks it in the app
    await client.items("L1")
    assert client.attribute_type == "ITEM_FLAGS" and client.attribute_type_verified
    res = await client.change("L1", [{"name": "Butter", "urgent": True}], "TO_PURCHASE")
    assert res[0]["flags_saved"] is True


def test_attribute_type_is_learned():
    asyncio.run(_attribute_type_is_learned())


def _post(url, data, host=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    if host:
        req.add_header("Host", host)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode()


def test_login_page():
    reset()
    received = {}

    def on_success(session, email, password, remember):
        received.update(session, form=(email, password, remember))

    page = B.LoginPage(on_success=on_success,
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
    status, body = _post(url, {"email": "a@b.de", "password": "richtig", "remember": "on"})
    assert "Verbunden" in body and received["uuid"] == "U1" and received["form"] == ("a@b.de", "richtig", True)
    time.sleep(0.5)
    try:
        urllib.request.urlopen(url, timeout=2)
        raise AssertionError("page still open after login")
    except (urllib.error.URLError, ConnectionError):
        pass
    url = page.start()  # unticked box: not remembered
    _post(url, {"email": "a@b.de", "password": "richtig"})
    assert received["form"] == ("a@b.de", "richtig", False)
    page.stop()


# ---------------------------------------------------------------- storage


class LimitedKeyring(keyring.backend.KeyringBackend):
    """Behaves like Windows Credential Manager: at most 1280 characters per entry."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        if len(password) > 1280:
            raise OSError("1783 CredWrite")
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError(username)
        del self.data[(service, username)]


class LockedKeyring(LimitedKeyring):
    """Secret Service present but locked: reading works, writing fails."""

    def set_password(self, service, username, password):
        raise keyring.errors.KeyringLocked("locked")


@contextlib.contextmanager
def using_keyring(backend):
    before = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(before)


def _token_session(size=3000):
    return {"email": "a@b.de", "uuid": "U1", "access_token": "a" * size, "refresh_token": "r" * size,
            "token_type": "Bearer", "expires_at": time.time() + 3600}


def test_long_tokens_fit_into_a_limited_keychain():
    with using_keyring(LimitedKeyring()) as fake:
        store = B.CredentialStore([B.KeyringBackend()])
        session = _token_session()
        store.save(session)
        assert store.load() == session and max(len(v) for v in fake.data.values()) <= 1280
        many = len(fake.data)
        store.save(_token_session(10))  # shorter value must not leave old chunks behind
        assert len(fake.data) < many and store.load()["access_token"] == "a" * 10
        store.save_login("a@b.de", "pässwörd!")
        assert store.load_login() == {"email": "a@b.de", "password": "pässwörd!"}
        store.delete()
        store.delete_login()
        assert fake.data == {}
        fake.data[(B.KEYRING_SERVICE, B.SESSION_KEY)] = json.dumps({"uuid": "alt"})  # format of older versions
        assert store.load() == {"uuid": "alt"}


def _file_store(directory):
    return B.CredentialStore([B.KeyringBackend(), B.FileBackend(Path(directory) / "bring-credentials.json")])


def test_linux_without_keychain_uses_private_file():
    with tempfile.TemporaryDirectory() as tmp, using_keyring(fail.Keyring()):
        store = _file_store(tmp)
        store.save(_token_session())
        store.save_login("a@b.de", "richtig")
        path = Path(tmp) / "bring-credentials.json"
        assert "Datei" in store.describe() and path.exists() and not list(Path(tmp).glob("*.tmp"))
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        again = _file_store(tmp)  # restart
        assert again.load()["uuid"] == "U1" and again.load_login()["password"] == "richtig"
        again.delete()
        again.delete_login()
        assert not path.exists()


def test_keychains_that_do_not_store_fall_back_to_file():
    for backend in (null.Keyring(), LockedKeyring()):
        with tempfile.TemporaryDirectory() as tmp, using_keyring(backend):
            store = _file_store(tmp)
            store.save_login("a@b.de", "richtig")
            assert _file_store(tmp).load_login()["password"] == "richtig", type(backend).__name__
            assert "Datei" in store.describe()


async def _restart_flow():
    reset()
    with tempfile.TemporaryDirectory() as tmp, using_keyring(fail.Keyring()):
        store = _file_store(tmp)
        store.save(B.login_sync("a@b.de", "richtig", transport=httpx.MockTransport(handler)))
        CALLS.clear()
        assert len(await B.BringClient(_file_store(tmp), transport=httpx.MockTransport(handler)).lists()) == 2
        assert not [c for c in CALLS if c[1] == "/rest/v2/bringauth"]  # restart without a new login


def test_restart_needs_no_login():
    asyncio.run(_restart_flow())


async def _saved_login_flow():
    reset()
    store, client = _client()
    store.save_login("a@b.de", "richtig")
    store.data["expires_at"] = time.time() - 10
    STATE["refresh"] = "ungültig"  # refresh token dead -> logs in again on its own
    assert len(await client.lists()) == 2 and client.can_auto_login()
    assert store.data["access_token"] == "tok-1" and "richtig" not in json.dumps(store.data)

    reset()  # nothing stored at all except the login, e.g. first start after an update
    store, client = _client()
    store.save_login("a@b.de", "richtig")
    store.delete()
    assert len(await client.lists()) == 2 and store.data

    reset()  # password changed at Bring!
    store, client = _client()
    store.save_login("a@b.de", "falsch")
    store.delete()
    try:
        await client.lists()
        raise AssertionError("wrong saved password accepted")
    except B.BringAuthError as exc:
        assert "stimmt nicht mehr" in str(exc) and store.login is None

    reset()  # Bring! hiccup must not throw anything away
    store, client = _client()
    store.save_login("a@b.de", "richtig")
    store.data["expires_at"] = time.time() - 10
    STATE["token_status"] = 503
    assert await client.connected() is True
    try:
        await client.lists()
        raise AssertionError("503 ignored")
    except B.BringAuthError:
        raise AssertionError("503 treated as a bad login")
    except B.BringError:
        assert store.data and store.login

    client.forget()  # bring_disconnect removes everything
    assert store.data is None and store.login is None


def test_saved_login():
    asyncio.run(_saved_login_flow())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
