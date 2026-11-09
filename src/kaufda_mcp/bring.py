"""Bring! shopping list integration.

Bring! has no public API. These are the endpoints the Bring! apps use, as documented by the community
(https://github.com/miaucl/bring-api, used by Home Assistant). Session tokens and, if the user agrees, mail and
password are kept in the operating system's keychain; where none works (headless Linux) in a file only the user
can read. Credentials never pass through the chat.
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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

from . import normalize as N
from .prefs import default_path

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
SESSION_KEY = "bring-session"
LOGIN_KEY = "bring-login"
KEYRING_CHUNK = 1000  # Windows Credential Manager takes at most 1280 characters per entry, tokens are longer
KEYRING_TIMEOUT = 5.0  # a locked Linux keyring can wait forever for an unlock prompt nobody sees


class BringError(RuntimeError):
    pass


class BringAuthError(BringError):
    pass


# ---------------------------------------------------------------- credential storage

class KeyringBackend:
    """OS keychain (Windows Credential Manager, macOS Keychain, Secret Service). Values are split into chunks."""

    _NAMES = {"Windows": "Windows Anmeldeinformationsverwaltung", "macOS": "macOS Schlüsselbund",
              "SecretService": "Secret Service Schlüsselbund", "kwallet": "KWallet"}

    def __init__(self) -> None:
        self._hung = False

    def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        if self._hung:
            raise TimeoutError("Schlüsselspeicher antwortet nicht")
        result: list[tuple[bool, Any]] = []

        def run() -> None:
            try:
                result.append((True, func(*args)))
            except Exception as exc:
                result.append((False, exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(KEYRING_TIMEOUT)
        if not result:
            self._hung = True
            raise TimeoutError("Schlüsselspeicher antwortet nicht")
        ok, value = result[0]
        if not ok:
            raise value
        return value

    def _keyring(self) -> Any:
        import keyring
        from keyring.backends import fail, null

        if isinstance(self._call(keyring.get_keyring), (fail.Keyring, null.Keyring)):
            raise RuntimeError("kein Schlüsselspeicher vorhanden")
        return keyring

    def usable(self) -> bool:
        try:
            self._keyring()
            return True
        except Exception:
            return False

    def describe(self) -> str:
        import keyring

        name = type(keyring.get_keyring()).__module__.split(".")[-1]
        return self._NAMES.get(name, f"{name} Schlüsselspeicher")

    def _chunk_count(self, keyring: Any, name: str) -> int:
        header = self._call(keyring.get_password, KEYRING_SERVICE, name)
        try:
            meta = json.loads(header) if header else None
        except ValueError:
            return 0
        return int(meta["chunks"]) if isinstance(meta, dict) and "chunks" in meta else 0

    def read(self, name: str) -> str | None:
        keyring = self._keyring()
        header = self._call(keyring.get_password, KEYRING_SERVICE, name)
        count = self._chunk_count(keyring, name)
        if not count:
            return header  # single entry written by older versions
        parts = [self._call(keyring.get_password, KEYRING_SERVICE, f"{name}.{i}") for i in range(count)]
        return "".join(parts) if all(p is not None for p in parts) else None

    def write(self, name: str, text: str) -> None:
        keyring = self._keyring()
        old = self._chunk_count(keyring, name)
        chunks = [text[i:i + KEYRING_CHUNK] for i in range(0, len(text), KEYRING_CHUNK)] or [""]
        for i, chunk in enumerate(chunks):
            self._call(keyring.set_password, KEYRING_SERVICE, f"{name}.{i}", chunk)
        self._call(keyring.set_password, KEYRING_SERVICE, name, json.dumps({"chunks": len(chunks)}))
        for i in range(len(chunks), old):
            self._remove(keyring, f"{name}.{i}")

    def _remove(self, keyring: Any, account: str) -> None:
        try:
            self._call(keyring.delete_password, KEYRING_SERVICE, account)
        except Exception:
            pass  # already gone

    def delete(self, name: str) -> None:
        keyring = self._keyring()
        for i in range(self._chunk_count(keyring, name)):
            self._remove(keyring, f"{name}.{i}")
        self._remove(keyring, name)


class FileBackend:
    """Fallback without a keychain (headless Debian/Ubuntu): a file only the user can read, like ~/.netrc.
    Not encrypted. It sits next to preferences.json, so KAUFDA_MCP_PREFS moves it too (e.g. onto a Docker volume)."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_path().parent / "bring-credentials.json"

    def usable(self) -> bool:
        return True

    def describe(self) -> str:
        return f"Datei {self.path} (nur dein Benutzer kann sie lesen)"

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _dump(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never readable by others, not even briefly
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)

    def read(self, name: str) -> str | None:
        return self._load().get(name)

    def write(self, name: str, text: str) -> None:
        self._dump({**self._load(), name: text})

    def delete(self, name: str) -> None:
        data = self._load()
        if name not in data:
            return
        del data[name]
        if data:
            self._dump(data)
        else:
            self.path.unlink(missing_ok=True)


class CredentialStore:
    """Bring! session tokens and saved login: keychain where one really works, otherwise the file."""

    def __init__(self, backends: list[Any] | None = None):
        self.backends = backends if backends is not None else [KeyringBackend(), FileBackend()]
        self._used: Any = None

    def _read(self, name: str) -> dict[str, Any] | None:
        for backend in self.backends:
            try:
                text = backend.read(name)
                if text is not None:
                    self._used = backend
                    return json.loads(text)
            except Exception as exc:
                log.info("credential backend %s: %s", type(backend).__name__, exc)
        return None

    def _write(self, name: str, value: dict[str, Any]) -> None:
        text = json.dumps(value)
        errors = []
        for backend in self.backends:
            try:
                backend.write(name, text)
                if backend.read(name) != text:  # some backends accept writes and keep nothing
                    raise RuntimeError("Wert kam beim Zurücklesen nicht an")
            except Exception as exc:
                errors.append(f"{type(backend).__name__}: {exc}")
                continue
            self._used = backend
            self._delete(name, skip=backend)  # no stale copy may shadow this one
            return
        raise BringError("Zugangsdaten konnten nirgends gespeichert werden: " + "; ".join(errors))

    def _delete(self, name: str, skip: Any = None) -> None:
        for backend in self.backends:
            if backend is not skip:
                try:
                    backend.delete(name)
                except Exception as exc:
                    log.info("credential backend %s: %s", type(backend).__name__, exc)

    def load(self) -> dict[str, Any] | None:
        return self._read(SESSION_KEY)

    def save(self, session: dict[str, Any]) -> None:
        self._write(SESSION_KEY, session)

    def delete(self) -> None:
        self._delete(SESSION_KEY)

    def load_login(self) -> dict[str, Any] | None:
        return self._read(LOGIN_KEY)

    def save_login(self, email: str, password: str) -> None:
        self._write(LOGIN_KEY, {"email": email.strip(), "password": password})

    def delete_login(self) -> None:
        self._delete(LOGIN_KEY)

    def describe(self) -> str:
        backend = self._used or next((b for b in self.backends if b.usable()), None)
        return backend.describe() if backend else "nirgends"


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] | None = None
        self.login: dict[str, Any] | None = None

    def load(self) -> dict[str, Any] | None:
        return self.data

    def save(self, session: dict[str, Any]) -> None:
        self.data = dict(session)

    def delete(self) -> None:
        self.data = None

    def load_login(self) -> dict[str, Any] | None:
        return self.login

    def save_login(self, email: str, password: str) -> None:
        self.login = {"email": email.strip(), "password": password}

    def delete_login(self) -> None:
        self.login = None

    def describe(self) -> str:
        return "Arbeitsspeicher"


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
        self.store = store or CredentialStore()
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

    def _drop_session(self) -> None:
        self.store.delete()
        self._session = None
        self._list_locales = None

    def forget(self) -> None:
        """Full disconnect: session and saved login are gone from this computer."""
        self._drop_session()
        self.store.delete_login()

    def _saved_login(self) -> tuple[str, str, str] | None:
        email, password = os.environ.get("BRING_EMAIL"), os.environ.get("BRING_PASSWORD")
        if email and password:
            return email, password, "env"
        stored = self.store.load_login() or {}
        if stored.get("email") and stored.get("password"):
            return stored["email"], stored["password"], "stored"
        return None

    def _persist(self, session: dict[str, Any]) -> None:
        if session.get("source") == "env":
            return
        try:
            self.store.save(session)
            session.pop("source", None)
        except BringError as exc:
            log.warning("Bring! session only kept in memory: %s", exc)
            session["source"] = "memory"

    async def _login(self) -> dict[str, Any]:
        """Log in without the user: BRING_EMAIL/BRING_PASSWORD or the saved login."""
        login = self._saved_login()
        if not login:
            raise BringAuthError("Nicht mit Bring! verbunden. bring_connect aufrufen.")
        email, password, source = login
        try:
            resp = await self._http.post("/v2/bringauth", data={"email": email, "password": password})
        except httpx.HTTPError as exc:
            raise BringError(f"Bring! nicht erreichbar: {exc}") from exc
        if resp.status_code == 401 and source == "stored":
            self.store.delete_login()  # do not keep hammering Bring! with a dead password
            raise BringAuthError("Das gespeicherte Bring! Passwort stimmt nicht mehr. Bitte neu verbinden (bring_connect).")
        session = _session_from_auth(resp, email)
        if source == "env":
            session["source"] = "env"
        self._persist(session)
        return session

    def _auth_headers(self, session: dict[str, Any]) -> dict[str, str]:
        return {
            "Authorization": f"{session['token_type']} {session['access_token']}",
            "X-BRING-USER-UUID": session["uuid"],
            "X-BRING-PUBLIC-USER-UUID": session.get("public_uuid") or "",
        }

    async def _refresh(self, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("refresh_token"):
            try:
                resp = await self._http.post(
                    "/v2/bringauth/token",
                    data={"grant_type": "refresh_token", "refresh_token": session["refresh_token"]},
                    headers=self._auth_headers(session),
                )
            except httpx.HTTPError as exc:
                raise BringError(f"Bring! nicht erreichbar: {exc}") from exc
            if resp.status_code == 200:
                data = resp.json()
                session.update(
                    access_token=data["access_token"],
                    token_type=data.get("token_type") or session["token_type"],
                    refresh_token=data.get("refresh_token") or session["refresh_token"],
                    expires_at=time.time() + int(data.get("expires_in") or 3600) - 60,
                )
                self._persist(session)
                return session
            if resp.status_code not in (400, 401, 403):  # Bring! hiccup: the session is still fine
                raise BringError(f"Bring! Token Erneuerung fehlgeschlagen (HTTP {resp.status_code}).")
        self._drop_session()
        if self._saved_login():
            return await self._login()
        raise BringAuthError("Bring! Sitzung abgelaufen. Bitte neu verbinden (bring_connect).")

    async def session(self, *, force_refresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self._session is None:
                self._session = self.store.load()
            if self._session is None:
                self._session = await self._login()
            if force_refresh or self._session.get("expires_at", 0) < time.time():
                self._session = await self._refresh(self._session)
            return self._session

    async def connected(self) -> bool:
        try:
            await self.session()
            return True
        except BringAuthError:
            return False
        except BringError:
            return True  # Bring! briefly unreachable, the login itself is fine

    def can_auto_login(self) -> bool:
        return self._saved_login() is not None

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
                results.append({"name": name, "result": "aktualisiert" if existing else "hinzugefügt",
                                "specification": spec[:250]})
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
        if results:
            # Read back what Bring! really stored, so the assistant never reports a write that did not arrive.
            fresh = await self.items(list_uuid)
            to_buy = {N.norm(i["name"]): i for i in fresh["purchase"]}
            bought = {N.norm(i["name"]) for i in fresh["recently"]}
            wanted_flags = dict(flag_jobs)
            for r in results:
                key = N.norm(r["name"])
                got = to_buy.get(key)
                if operation == "TO_PURCHASE":
                    r["on_list"] = got is not None
                    if "specification" in r:
                        r["spec_saved"] = got is not None and (got.get("specification") or "") == r["specification"]
                    if r["name"] in wanted_flags:
                        flags = wanted_flags[r["name"]]
                        ok = got is not None and all(bool(got.get(k)) == v for k, v in flags.items())
                        r["flags"] = {k: v for k, v in flags.items() if v} or "keine"
                        r["flags_saved"] = ok
                        if ok:
                            self.attribute_type_verified = True
                elif r["result"] != "stand nicht auf der Liste":
                    r["verified"] = got is None and (operation == "REMOVE" or key in bought)
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
<p>Deine Zugangsdaten gehen direkt an Bring!. Die KI sieht sie nicht. Gemerkt werden sie im Schlüsselspeicher
deines Systems, ohne Schlüsselspeicher (Linux Server) in einer Datei, die nur dein Benutzer lesen kann.</p>{error}
<form method="post" autocomplete="on">
<label for="email">Mailadresse</label><input id="email" name="email" type="email" required autofocus>
<label for="password">Passwort</label><input id="password" name="password" type="password" required>
<label style="display:flex;gap:8px;align-items:center"><input style="width:auto" name="remember" type="checkbox" checked>
Merken, dann muss ich mich nicht nochmal anmelden</label>
<button type="submit">Verbinden</button></form>"""


class LoginPage:
    """One-time login form on 127.0.0.1 with an unguessable path; closes after success or timeout."""

    def __init__(self, on_success: Callable[[dict[str, Any], str, str, bool], None],
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
                    page._on_success(session, email, password, "remember" in form)
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
