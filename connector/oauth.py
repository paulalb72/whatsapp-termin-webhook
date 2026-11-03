"""Anmeldung am Connector (OAuth 2.1 mit Login-Seite).

Claude registriert sich automatisch (Dynamic Client Registration) und schickt die Person auf
/login. Dort gibt sie ihren Namen und das Connector-Passwort ein. Danach bekommt Claude
Access-/Refresh-Tokens. Gespeichert werden nur SHA-256-Hashes der Tokens (SQLite im Volume).
Wird CONNECTOR_PASSWORD geändert, sind alle bestehenden Anmeldungen sofort ungültig.
"""

import asyncio
import hashlib
import html
import json
import logging
import secrets
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger("connector.auth")

ACCESS_TTL = 8 * 3600  # Access-Token: 8 Stunden (Claude erneuert ihn selbst per Refresh-Token)
REFRESH_TTL = 90 * 86400  # Refresh-Token: 90 Tage, wird bei jeder Erneuerung ausgetauscht
CODE_TTL = 5 * 60
LOGIN_TTL = 15 * 60
MAX_CLIENTS = 500
FAILURE_WINDOW = 15 * 60
MAX_FAILURES_PER_IP = 5
MAX_FAILURES_TOTAL = 30
DEFAULT_SCOPES = ["bot"]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PasswordOAuth:
    def __init__(self, db_path: str, password: str, public_url: str):
        self.db_path = db_path
        self.password = password
        self.public_url = public_url
        self.generation = digest("generation:" + password)[:16]
        self.failures: dict[str, list[float]] = {}
        self._sql("CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, created REAL NOT NULL, data TEXT NOT NULL)")
        self._sql(
            "CREATE TABLE IF NOT EXISTS grants "
            "(key TEXT PRIMARY KEY, kind TEXT NOT NULL, pair TEXT, expires REAL NOT NULL, data TEXT NOT NULL)"
        )
        self._sql("CREATE INDEX IF NOT EXISTS grants_pair ON grants (pair)")

    # ---------- Speicher ----------

    def _sql(self, query: str, params: tuple = ()) -> list[tuple]:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            return db.execute(query, params).fetchall()

    def _put(self, kind: str, secret: str, data: dict, ttl: int, pair: str | None = None) -> None:
        self._sql(
            "INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?, ?)",
            (digest(secret), kind, pair, time.time() + ttl, json.dumps(data)),
        )

    def _get(self, kind: str, secret: str) -> dict | None:
        if not secret:
            return None
        rows = self._sql("SELECT data, expires FROM grants WHERE key = ? AND kind = ?", (digest(secret), kind))
        if not rows:
            return None
        data, expires = rows[0]
        if expires < time.time():
            self._sql("DELETE FROM grants WHERE key = ?", (digest(secret),))
            return None
        data = json.loads(data)
        if data.get("generation", self.generation) != self.generation:
            return None  # mit einem früheren Passwort angemeldet
        return data

    def _delete(self, secret: str) -> None:
        self._sql("DELETE FROM grants WHERE key = ?", (digest(secret),))

    def _delete_pair_of(self, secret: str) -> None:
        """Access- und Refresh-Token, die zusammen ausgegeben wurden, gemeinsam löschen."""
        rows = self._sql("SELECT pair FROM grants WHERE key = ?", (digest(secret),))
        if rows and rows[0][0]:
            self._sql("DELETE FROM grants WHERE pair = ?", (rows[0][0],))
        self._delete(secret)

    def _issue(self, client_id: str, scopes: list[str], subject: str | None, resource: str | None) -> OAuthToken:
        pair = secrets.token_hex(16)
        access = "bca_" + secrets.token_urlsafe(32)
        refresh = "bcr_" + secrets.token_urlsafe(32)
        now = int(time.time())
        data = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource, "generation": self.generation}
        self._put("access", access, {**data, "expires_at": now + ACCESS_TTL}, ACCESS_TTL, pair)
        self._put("refresh", refresh, {**data, "expires_at": now + REFRESH_TTL}, REFRESH_TTL, pair)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    # ---------- OAuthAuthorizationServerProvider ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        rows = self._sql("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._sql(
            "INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
            (client_info.client_id, time.time(), client_info.model_dump_json()),
        )
        self._sql(
            "DELETE FROM clients WHERE client_id NOT IN (SELECT client_id FROM clients ORDER BY created DESC LIMIT ?)",
            (MAX_CLIENTS,),
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._sql("DELETE FROM grants WHERE expires < ?", (time.time(),))
        request_id = secrets.token_urlsafe(32)
        self._put("login", request_id, {"client_id": client.client_id, "params": params.model_dump(mode="json")}, LOGIN_TTL)
        return f"{self.public_url}/login?{urlencode({'request': request_id})}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        data = self._get("code", authorization_code)
        if not data or data.get("client_id") != client.client_id:
            return None
        data.pop("generation", None)
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self._delete(authorization_code.code)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self._get("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            resource=data.get("resource"),
            subject=data.get("subject"),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self._delete_pair_of(refresh_token.token)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._get("access", token)
        if not data:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            resource=data.get("resource"),
            subject=data.get("subject"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._delete_pair_of(token.token)

    # ---------- Login-Seite ----------

    def _locked(self, ip: str) -> bool:
        cutoff = time.time() - FAILURE_WINDOW
        for key in list(self.failures):
            self.failures[key] = [t for t in self.failures[key] if t > cutoff]
            if not self.failures[key]:
                del self.failures[key]
        total = sum(len(v) for v in self.failures.values())
        return len(self.failures.get(ip, [])) >= MAX_FAILURES_PER_IP or total >= MAX_FAILURES_TOTAL

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = self._get("login", request_id)
            if not pending:
                return expired_page()
            return await self._form(request_id, pending)

        form = await request.form()
        request_id = str(form.get("request", ""))
        name = " ".join(str(form.get("name", "")).split())[:60]
        password = str(form.get("password", ""))
        pending = self._get("login", request_id)
        if not pending:
            return expired_page()

        ip = request.client.host if request.client else "?"
        if self._locked(ip):
            log.warning("Login gesperrt (zu viele Fehlversuche, von %s)", ip)
            return page("Gesperrt", "<p>Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.</p>", 429)
        if not secrets.compare_digest(password.encode(), self.password.encode()):
            self.failures.setdefault(ip, []).append(time.time())
            log.warning("Login fehlgeschlagen (von %s)", ip)
            await asyncio.sleep(1)
            return await self._form(request_id, pending, error="Das Passwort stimmt nicht.", name=name, status=401)
        if len(name) < 2:
            return await self._form(request_id, pending, error="Bitte deinen Namen eintragen.", status=400)

        self._delete(request_id)
        params = pending["params"]
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=params.get("scopes") or DEFAULT_SCOPES,
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params.get("resource"),
            subject=name,
        )
        self._put("code", code, {**auth_code.model_dump(mode="json", exclude={"code"}), "generation": self.generation}, CODE_TTL)
        log.info("Login erfolgreich: %s (von %s)", name, ip)
        return RedirectResponse(
            construct_redirect_uri(params["redirect_uri"], code=code, state=params.get("state")),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    async def _form(self, request_id: str, pending: dict, error: str = "", name: str = "", status: int = 200) -> Response:
        client = await self.get_client(pending["client_id"])
        app_name = (client.client_name if client else None) or "Claude"
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<p><b>{html.escape(app_name)}</b> möchte auf den WhatsApp-Bot zugreifen: Code lesen und ändern,
deployen, Logs lesen und Endpoints testen.</p>
{error_html}
<form method="post" action="/login">
  <input type="hidden" name="request" value="{html.escape(request_id)}">
  <label>Dein Name <small>(steht in jeder Änderung im Verlauf)</small>
    <input name="name" value="{html.escape(name)}" autocomplete="name" required maxlength="60"></label>
  <label>Connector-Passwort
    <input name="password" type="password" autocomplete="current-password" required autofocus></label>
  <button type="submit">Zugriff erlauben</button>
</form>"""
        return page("Anmelden", body, status)


def expired_page() -> Response:
    return page(
        "Link abgelaufen",
        "<p>Dieser Anmeldelink ist abgelaufen oder wurde schon benutzt. "
        "Bitte in Claude noch einmal auf „Verbinden“ klicken.</p>",
        400,
    )


def page(title: str, body: str, status: int = 200) -> Response:
    document = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Bot-Connector</title>
<style>
  :root {{ color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --text:#1d2129; --muted:#5f6673; --line:#d8dce3; --accent:#128c4a; --error:#b3261e; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14161a; --card:#1e2127; --text:#e8eaed; --muted:#a0a6b1; --line:#353a43; --accent:#2fbf6f; --error:#f2867d; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px; background:var(--bg); color:var(--text);
         font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ width:100%; max-width:420px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:28px; }}
  h1 {{ margin:0 0 12px; font-size:1.3rem; }}
  label {{ display:block; margin-top:16px; font-weight:600; }}
  small {{ font-weight:400; color:var(--muted); }}
  input {{ display:block; width:100%; margin-top:6px; padding:10px 12px; font:inherit; color:inherit; background:transparent;
          border:1px solid var(--line); border-radius:8px; }}
  button {{ margin-top:22px; width:100%; padding:11px; font:inherit; font-weight:600; color:#fff; background:var(--accent);
           border:0; border-radius:8px; cursor:pointer; }}
  .error {{ color:var(--error); font-weight:600; }}
</style></head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
    return HTMLResponse(
        document,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        },
    )
