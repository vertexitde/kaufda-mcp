"""Bring! shopping list integration.

Bring! has no public API. These are the endpoints the Bring! apps use, as documented by the community
(https://github.com/miaucl/bring-api, used by Home Assistant). The password is only used once to log in;
afterwards only the session tokens are kept, in the operating system's keychain.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

from . import normalize as N

log = logging.getLogger("kaufda_mcp")

BRING_API = "https://api.getbring.com/rest"
BRING_LOCALES = "https://web.getbring.com/locale"
BRING_HEADERS = {
    "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Sp",
    "X-BRING-CLIENT": "android",
    "X-BRING-APPLICATION": "bring",
    "X-BRING-COUNTRY": "DE",
}
CATALOG_LOCALE = "de-CH"  # Bring! stores catalog items under their de-CH name
# Per item flags of the Bring! apps: Dringend, Wenn's passt (bei Gelegenheit), Im Angebot.
FLAG_KEYS = ("urgent", "convenient", "discounted")
DEFAULT_ATTRIBUTE_TYPE = "PURCH_CONDITIONS"  # best guess, replaced by the type seen on the user's own items
KEYRING_SERVICE = "kaufda-mcp"
KEYRING_ACCOUNT = "bring-session"


class BringError(RuntimeError):
    pass


class BringAuthError(BringError):
    pass


# ---------------------------------------------------------------- credential storage

class KeyringStore:
    """Session tokens in the OS keychain (Windows Credential Manager, macOS Keychain, Secret Service)."""

    def load(self) -> dict[str, Any] | None:
        try:
            import keyring

            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception as exc:  # no backend, locked keychain ...
            log.warning("keyring not available: %s", exc)
            return None
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def save(self, session: dict[str, Any]) -> None:
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, json.dumps(session))
        except Exception as exc:
            raise BringError(
                f"Kein sicherer Schlüsselspeicher verfügbar ({exc}). Auf Servern stattdessen BRING_EMAIL und "
                "BRING_PASSWORD als Umgebungsvariablen setzen."
            ) from exc

    def delete(self) -> None:
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            pass

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import keyring
            from keyring.backends import fail

            backend = keyring.get_keyring()
            if isinstance(backend, fail.Keyring):
                return False, "kein Schlüsselspeicher gefunden"
            return True, type(backend).__module__.split(".")[-1]
        except Exception as exc:
            return False, str(exc)


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        return self.data

    def save(self, session: dict[str, Any]) -> None:
        self.data = dict(session)

    def delete(self) -> None:
        self.data = None


# ---------------------------------------------------------------- client

def _session_from_auth(resp: httpx.Response, email: str) -> dict[str, Any]:
    if resp.status_code in (400, 401, 404):
        raise BringAuthError("Bring! Login fehlgeschlagen: Mailadresse oder Passwort stimmen nicht.")
    if resp.status_code != 200:
        raise BringError(f"Bring! Login fehlgeschlagen (HTTP {resp.status_code}).")
    data = resp.json()
    return {
        "email": data.get("email") or email,
        "name": data.get("name"),
        "uuid": data["uuid"],
        "public_uuid": data.get("publicUuid") or "",
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "token_type": data.get("token_type") or "Bearer",
        "expires_at": time.time() + int(data.get("expires_in") or 3600) - 60,
        "default_list": data.get("bringListUUID"),
    }


def login_sync(email: str, password: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Log in once with mail and password (used by the login page and the CLI)."""
    with httpx.Client(base_url=BRING_API, timeout=20, headers=BRING_HEADERS, transport=transport) as http:
        try:
            resp = http.post("/v2/bringauth", data={"email": email.strip(), "password": password})
        except httpx.HTTPError as exc:
            raise BringError(f"Bring! nicht erreichbar: {exc}") from exc
    return _session_from_auth(resp, email.strip())


class BringClient:
    def __init__(self, store: Any | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store or KeyringStore()
        self._transport = transport
        self._http = httpx.AsyncClient(base_url=BRING_API, timeout=20, headers=BRING_HEADERS, transport=transport)
        self._session: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._catalog: dict[str, dict[str, str]] = {}
        self._list_locales: dict[str, str] | None = None
        self.attribute_type = DEFAULT_ATTRIBUTE_TYPE
        self.attribute_type_verified = False

    # ------------------------------------------------------------ session handling

    def adopt(self, session: dict[str, Any]) -> None:
        """Called after a successful login (login page / CLI)."""
        self._session = dict(session)
        self._list_locales = None

    def forget(self) -> None:
        self.store.delete()
        self._session = None
        self._list_locales = None

    @staticmethod
    def _env_credentials() -> tuple[str, str] | None:
        email, password = os.environ.get("BRING_EMAIL"), os.environ.get("BRING_PASSWORD")
        return (email, password) if email and password else None

    async def _login_env(self) -> dict[str, Any]:
        creds = self._env_credentials()
        if not creds:
            raise BringAuthError("Nicht mit Bring! verbunden. bring_connect aufrufen.")
        resp = await self._http.post("/v2/bringauth", data={"email": creds[0], "password": creds[1]})
        session = _session_from_auth(resp, creds[0])
        session["source"] = "env"
        return session

    def _auth_headers(self, session: dict[str, Any]) -> dict[str, str]:
        return {
            "Authorization": f"{session['token_type']} {session['access_token']}",
            "X-BRING-USER-UUID": session["uuid"],
            "X-BRING-PUBLIC-USER-UUID": session.get("public_uuid") or "",
        }

    async def _refresh(self, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("refresh_token"):
            resp = await self._http.post(
                "/v2/bringauth/token",
                data={"grant_type": "refresh_token", "refresh_token": session["refresh_token"]},
                headers=self._auth_headers(session),
            )
            if resp.status_code == 200:
                data = resp.json()
                session.update(
                    access_token=data["access_token"],
                    token_type=data.get("token_type") or session["token_type"],
                    refresh_token=data.get("refresh_token") or session["refresh_token"],
                    expires_at=time.time() + int(data.get("expires_in") or 3600) - 60,
                )
                if session.get("source") != "env":
                    self.store.save(session)
                return session
        if self._env_credentials():
            return await self._login_env()
        self.forget()
        raise BringAuthError("Bring! Sitzung abgelaufen. Bitte neu verbinden (bring_connect).")

    async def session(self, *, force_refresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self._session is None:
                self._session = self.store.load()
            if self._session is None:
                self._session = await self._login_env()
            if force_refresh or self._session.get("expires_at", 0) < time.time():
                self._session = await self._refresh(self._session)
            return self._session

    async def connected(self) -> bool:
        try:
            await self.session()
            return True
        except BringError:
            return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self.session()
        for attempt in (1, 2):
            try:
                resp = await self._http.request(method, path, headers=self._auth_headers(session), **kwargs)
            except httpx.HTTPError as exc:
                raise BringError(f"Bring! nicht erreichbar: {exc}") from exc
            if resp.status_code == 401 and attempt == 1:
                session = await self.session(force_refresh=True)
                continue
            if resp.status_code >= 400:
                raise BringError(f"Bring! Anfrage fehlgeschlagen (HTTP {resp.status_code}).")
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise BringAuthError("Bring! Anmeldung ungültig. Bitte neu verbinden (bring_connect).")

    # ------------------------------------------------------------ catalog (item names <-> Bring! ids)

    async def _list_locale(self, list_uuid: str) -> str:
        if self._list_locales is None:
            self._list_locales = {}
            try:
                session = await self.session()
                data = await self._request("GET", f"/bringusersettings/{session['uuid']}")
                for entry in (data or {}).get("userlistsettings") or []:
                    for setting in entry.get("usersettings") or []:
                        if setting.get("key") == "listArticleLanguage" and setting.get("value"):
                            self._list_locales[entry.get("listUuid")] = setting["value"]
            except BringError as exc:
                log.warning("bring list settings: %s", exc)
        return self._list_locales.get(list_uuid, "de-DE")

    async def _catalog_for(self, locale: str) -> dict[str, str]:
        """Bring! id -> name in the list's language."""
        if locale == CATALOG_LOCALE:
            return {}
        if locale not in self._catalog:
            try:
                resp = await self._http.get(f"{BRING_LOCALES}/articles.{locale}.json")
                data = resp.json() if resp.status_code == 200 else {}
                self._catalog[locale] = {k: v for k, v in data.items() if isinstance(v, str)}
            except (httpx.HTTPError, ValueError):
                self._catalog[locale] = {}
        return self._catalog[locale]

    async def _to_bring_id(self, name: str, list_uuid: str) -> str:
        catalog = await self._catalog_for(await self._list_locale(list_uuid))
        wanted = N.norm(name)
        for item_id, localized in catalog.items():
            if N.norm(localized) == wanted:
                return item_id
        return name.strip()

    async def _to_name(self, item_id: str, list_uuid: str) -> str:
        catalog = await self._catalog_for(await self._list_locale(list_uuid))
        return catalog.get(item_id, item_id)

    # ------------------------------------------------------------ lists and items

    async def lists(self) -> list[dict[str, str]]:
        session = await self.session()
        data = await self._request("GET", f"/bringusers/{session['uuid']}/lists")
        return [{"uuid": entry["listUuid"], "name": entry.get("name") or "Liste"}
                for entry in (data or {}).get("lists") or [] if entry.get("listUuid")]

    def _learn_attribute_type(self, item: dict[str, Any]) -> dict[str, bool]:
        """Read the per item flags. The attribute's type name is not documented, so it is learned from items
        the user marked in the app (anything carrying urgent/convenient/discounted that is not an offer)."""
        flags = {key: False for key in FLAG_KEYS}
        for attr in item.get("attributes") or []:
            content = attr.get("content") or {}
            if not any(key in content for key in FLAG_KEYS):
                continue
            for key in FLAG_KEYS:
                flags[key] = flags[key] or bool(content.get(key))
            if attr.get("type") and attr["type"] != "RETAIL_PRODUCT" and any(content.get(k) for k in FLAG_KEYS):
                self.attribute_type = attr["type"]
                self.attribute_type_verified = True
        return flags

    async def items(self, list_uuid: str) -> dict[str, list[dict[str, Any]]]:
        data = await self._request("GET", f"/v2/bringlists/{list_uuid}")
        raw = (data or {}).get("items") or {}
        out: dict[str, list[dict[str, Any]]] = {}
        for section in ("purchase", "recently"):
            entries = []
            for item in raw.get(section) or []:
                flags = self._learn_attribute_type(item)
                entries.append(N.compact({
                    "name": await self._to_name(item.get("itemId", ""), list_uuid),
                    "specification": item.get("specification"),
                    "uuid": item.get("uuid"),
                    "item_id": item.get("itemId"),
                    **{key: True for key, on in flags.items() if on},
                }))
            out[section] = entries
        return out

    async def _legacy(self, list_uuid: str, **fields: str) -> None:
        """Name addressed list change. Measured by other Bring! clients: the newer items endpoint silently
        ignores REMOVE/TO_RECENTLY for many items, this form endpoint applies them reliably."""
        form = {"purchase": "", "recently": "", "specification": "", "remove": "", "sender": "null", **fields}
        await self._request("PUT", f"/v2/bringlists/{list_uuid}", data=form)

    async def _set_flags(self, list_uuid: str, item: dict[str, Any], flags: dict[str, bool]) -> None:
        change = {
            "accuracy": "0.0", "altitude": "0.0", "latitude": "0.0", "longitude": "0.0",
            "itemId": item["item_id"], "spec": item.get("specification") or "", "uuid": item["uuid"],
            "operation": "ATTRIBUTE_UPDATE",
            "attribute": {"type": self.attribute_type, "content": flags},
        }
        await self._request("PUT", f"/v2/bringlists/{list_uuid}/items", json={"changes": [change], "sender": ""})

    async def change(self, list_uuid: str, items: list[dict[str, Any]], operation: str) -> list[dict[str, Any]]:
        """operation: TO_PURCHASE (add/update), TO_RECENTLY (bought), REMOVE.

        specification: None keeps the existing text, "" clears it. Flags (urgent, convenient, discounted):
        None keeps them. Returns one result per item, flags are read back to verify they really stuck."""
        before = await self.items(list_uuid)
        current = {N.norm(i["name"]): i for i in before["purchase"]}
        results: list[dict[str, Any]] = []
        flag_jobs: list[tuple[str, dict[str, bool]]] = []
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            key = await self._to_bring_id(name, list_uuid)
            existing = current.get(N.norm(name))
            if operation == "REMOVE":
                if not existing:
                    results.append({"name": name, "result": "stand nicht auf der Liste"})
                    continue
                await self._legacy(list_uuid, remove=existing.get("item_id") or key)
                results.append({"name": name, "result": "entfernt"})
            elif operation == "TO_RECENTLY":
                if not existing:
                    results.append({"name": name, "result": "stand nicht auf der Liste"})
                    continue
                await self._legacy(list_uuid, recently=existing.get("item_id") or key)
                results.append({"name": name, "result": "als gekauft abgehakt"})
            else:
                spec = item.get("specification")
                if spec is None:
                    spec = (existing or {}).get("specification") or ""
                await self._legacy(list_uuid, purchase=(existing or {}).get("item_id") or key, specification=spec[:250])
                results.append(N.compact({"name": name, "result": "aktualisiert" if existing else "hinzugefügt",
                                          "specification": spec}))
                wanted = {k: item[k] for k in FLAG_KEYS if item.get(k) is not None}
                if wanted:
                    merged = {k: bool((existing or {}).get(k)) for k in FLAG_KEYS} | wanted
                    flag_jobs.append((name, merged))
        if flag_jobs:
            after = {N.norm(i["name"]): i for i in (await self.items(list_uuid))["purchase"]}
            for name, flags in flag_jobs:
                target = after.get(N.norm(name))
                if target and target.get("uuid"):
                    try:
                        await self._set_flags(list_uuid, target, flags)
                    except BringError as exc:
                        log.warning("bring flags %s: %s", name, exc)
            check = {N.norm(i["name"]): i for i in (await self.items(list_uuid))["purchase"]}
            by_name = {N.norm(r["name"]): r for r in results}
            for name, flags in flag_jobs:
                got = check.get(N.norm(name)) or {}
                ok = all(bool(got.get(k)) == v for k, v in flags.items())
                by_name[N.norm(name)]["flags"] = {k: v for k, v in flags.items() if v} or "keine"
                by_name[N.norm(name)]["flags_saved"] = ok
                if ok:
                    self.attribute_type_verified = True
        return results


# ---------------------------------------------------------------- local login page

_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Bring! verbinden</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f6f8;color:#1d2733;display:flex;justify-content:center;padding:40px 16px}}
.card{{background:#fff;max-width:380px;width:100%;padding:28px;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0 0 6px}}p{{font-size:14px;line-height:1.5;color:#4a5866}}
label{{display:block;font-size:13px;margin:14px 0 4px}}input{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #c9d2db;border-radius:8px;font-size:15px}}
button{{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:#ee524f;color:#fff;font-size:15px;cursor:pointer}}
.err{{background:#fde8e8;color:#9b1c1c;padding:10px;border-radius:8px;font-size:14px}}.ok{{background:#e6f6ec;color:#17643a;padding:12px;border-radius:8px}}
</style></head><body><div class="card">{body}</div></body></html>"""

_FORM = """<h1>Bring! mit kaufDA verbinden</h1>
<p>Deine Zugangsdaten gehen direkt an Bring!. Die KI sieht sie nicht. Gespeichert wird nur ein Sitzungstoken
im Schlüsselspeicher deines Systems, nicht dein Passwort.</p>{error}
<form method="post" autocomplete="on">
<label for="email">Mailadresse</label><input id="email" name="email" type="email" required autofocus>
<label for="password">Passwort</label><input id="password" name="password" type="password" required>
<button type="submit">Verbinden</button></form>"""


class LoginPage:
    """One-time login form on 127.0.0.1 with an unguessable path; closes after success or timeout."""

    def __init__(self, on_success: Callable[[dict[str, Any]], None],
                 login: Callable[[str, str], dict[str, Any]] = login_sync, ttl_seconds: int = 600):
        self._on_success = on_success
        self._login = login
        self._ttl = ttl_seconds
        self._server: ThreadingHTTPServer | None = None
        self._url: str | None = None
        self._deadline = 0.0
        self.done = threading.Event()

    def start(self) -> str:
        if self._server and time.time() < self._deadline and not self.done.is_set():
            return self._url or ""
        self.stop()
        token = secrets.token_urlsafe(24)
        page = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep stdout clean (stdio transport)
                pass

            def _send(self, status: int, body: str) -> None:
                data = _PAGE.format(body=body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                return self.path == f"/{token}" and host in ("127.0.0.1", "localhost") and not page.done.is_set()

            def do_GET(self) -> None:
                if not self._allowed():
                    return self._send(404, "<h1>Link ungültig oder abgelaufen</h1>")
                self._send(200, _FORM.format(error=""))

            def do_POST(self) -> None:
                if not self._allowed():
                    return self._send(404, "<h1>Link ungültig oder abgelaufen</h1>")
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
                email = (form.get("email") or [""])[0]
                password = (form.get("password") or [""])[0]
                try:
                    session = page._login(email, password)
                    page._on_success(session)
                except BringError as exc:
                    return self._send(200, _FORM.format(error=f'<p class="err">{html.escape(str(exc))}</p>'))
                page.done.set()
                self._send(200, '<h1>Verbunden</h1><p class="ok">Bring! ist verbunden. Du kannst dieses Fenster '
                                "schließen und im Chat weitermachen.</p>")
                threading.Thread(target=page.stop, daemon=True).start()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._url = f"http://127.0.0.1:{self._server.server_address[1]}/{token}"
        self._deadline = time.time() + self._ttl
        self.done.clear()
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        timer = threading.Timer(self._ttl, self.stop)
        timer.daemon = True
        timer.start()
        return self._url

    def stop(self) -> None:
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
