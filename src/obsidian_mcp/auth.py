"""Minimal single-user OAuth 2.1 authorization server for the HTTP mode.

claude.ai custom connectors (and through them the mobile apps) authenticate with
OAuth: discovery, dynamic client registration, authorization code + PKCE. The MCP SDK
implements the protocol endpoints; this module supplies the storage and the one
interactive step, a password page.

Flow, seen from the phone:

1. You add the connector URL on claude.ai.
2. Claude registers itself (DCR) and opens ``/authorize``.
3. The server redirects to ``/login``, which asks for ``OBSIDIAN_MCP_PASSWORD``.
4. On success the browser goes back to Claude with a one-time code, which Claude
   exchanges for an access token (1 h) and a refresh token (30 days, rotated).

Besides OAuth, an optional static bearer token (``OBSIDIAN_MCP_TOKEN``) lets Claude
Code connect with a plain ``Authorization`` header.

Security choices:

- Tokens are stored as SHA-256 hashes, so a leaked state file does not leak tokens.
- Redirect URIs are restricted to an allowlist (claude.ai/claude.com callbacks and
  loopback by default), checked at registration and again at authorization.
- Failed logins are throttled: after 5 failures in 15 minutes the login is locked for
  15 minutes.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings

ACCESS_TOKEN_TTL = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60  # 30 days
AUTH_CODE_TTL = 5 * 60
PENDING_LOGIN_TTL = 10 * 60
MAX_CLIENTS = 50

LOCKOUT_WINDOW = 15 * 60
LOCKOUT_THRESHOLD = 5

STATIC_CLIENT_ID = "static-token"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def redirect_allowed(uri: str, allowlist: tuple[str, ...]) -> bool:
    """An entry with a path must match exactly; an entry without one (``http://localhost``)
    matches any port and path on that scheme + host (loopback callbacks use random ports)."""
    if "*" in allowlist:
        return True
    target = urlparse(uri)
    for entry in allowlist:
        allowed = urlparse(entry)
        if allowed.path in ("", "/"):
            if target.scheme == allowed.scheme and target.hostname == allowed.hostname:
                return True
        elif uri == entry:
            return True
    return False


class _JsonStore:
    """Tiny thread-safe JSON file. Good enough for a single user."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, Any]] = {"clients": {}, "access": {}, "refresh": {}}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for key in self.data:
                self.data[key] = loaded.get(key, {})

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(self.path)

    def purge_expired(self) -> None:
        now = time.time()
        with self.lock:
            for bucket in ("access", "refresh"):
                expired = [k for k, v in self.data[bucket].items() if v.get("expires_at") and v["expires_at"] < now]
                for key in expired:
                    del self.data[bucket][key]


@dataclass
class _PendingLogin:
    client_id: str
    client_name: str | None
    params: AuthorizationParams
    created_at: float


class VaultOAuthProvider:
    """Implements ``mcp.server.auth.provider.OAuthAuthorizationServerProvider``."""

    def __init__(self, settings: Settings) -> None:
        assert settings.public_url and settings.password
        self.settings = settings
        self.store = _JsonStore(settings.state_dir / "oauth.json")
        self.store.purge_expired()
        self.store.save()
        self._pending: dict[str, _PendingLogin] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._lock = threading.RLock()

    # -- clients ---------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not redirect_allowed(str(uri), self.settings.allowed_redirects):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect_uri not allowed by this server: {uri}",
                )
        with self.store.lock:
            clients = self.store.data["clients"]
            clients[client_info.client_id] = {
                **client_info.model_dump(mode="json", exclude_none=True),
                "_registered_at": time.time(),
            }
            if len(clients) > MAX_CLIENTS:
                oldest = sorted(clients, key=lambda k: clients[k].get("_registered_at", 0))
                for key in oldest[: len(clients) - MAX_CLIENTS]:
                    del clients[key]
            self.store.save()

    # -- authorization ---------------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not redirect_allowed(str(params.redirect_uri), self.settings.allowed_redirects):
            raise AuthorizeError(error="invalid_request", error_description="redirect_uri not allowed")
        txn = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_stale_pending()
            self._pending[txn] = _PendingLogin(
                client_id=client.client_id or "",
                client_name=client.client_name,
                params=params,
                created_at=time.time(),
            )
        return f"{self.settings.public_url}/login?txn={txn}"

    def _drop_stale_pending(self) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if now - v.created_at > PENDING_LOGIN_TTL]:
            del self._pending[key]
        for key in [k for k, v in self._codes.items() if v.expires_at < now]:
            del self._codes[key]

    def locked_out(self) -> bool:
        now = time.time()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < LOCKOUT_WINDOW]
            return len(self._failures) >= LOCKOUT_THRESHOLD

    def pending(self, txn: str) -> _PendingLogin | None:
        with self._lock:
            self._drop_stale_pending()
            return self._pending.get(txn)

    def complete_login(self, txn: str, password: str) -> str | None:
        """Return the redirect URL on success, ``None`` on a wrong password."""
        assert self.settings.password
        with self._lock:
            pending = self._pending.get(txn)
            if pending is None:
                raise KeyError(txn)
            if not hmac.compare_digest(password.encode(), self.settings.password.encode()):
                self._failures.append(time.time())
                return None
            del self._pending[txn]
            code = secrets.token_urlsafe(32)
            params = pending.params
            self._codes[code] = AuthorizationCode(
                code=code,
                scopes=params.scopes or [],
                expires_at=time.time() + AUTH_CODE_TTL,
                client_id=pending.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject="owner",
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._lock:
            code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            # One-time use.
            if self._codes.pop(authorization_code.code, None) is None:
                raise TokenError(error="invalid_grant", error_description="authorization code already used")
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.resource)

    # -- tokens ----------------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.store.lock:
            self.store.purge_expired()
            self.store.data["access"][_hash(access)] = {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + ACCESS_TOKEN_TTL,
                "resource": resource,
                "refresh": _hash(refresh),
            }
            self.store.data["refresh"][_hash(refresh)] = {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": now + REFRESH_TOKEN_TTL,
                "resource": resource,
            }
            self.store.save()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        raw = self.store.data["refresh"].get(_hash(refresh_token))
        if not raw or raw["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=raw["client_id"],
            scopes=raw["scopes"],
            expires_at=raw["expires_at"],
            resource=raw.get("resource"),
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        hashed = _hash(refresh_token.token)
        with self.store.lock:
            if self.store.data["refresh"].pop(hashed, None) is None:
                raise TokenError(error="invalid_grant", error_description="refresh token already used")
            # Revoke the access tokens minted alongside the old refresh token.
            for key in [k for k, v in self.store.data["access"].items() if v.get("refresh") == hashed]:
                del self.store.data["access"][key]
        return self._issue(client.client_id or "", scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        static = self.settings.static_token
        if static and hmac.compare_digest(token.encode(), static.encode()):
            return AccessToken(token=token, client_id=STATIC_CLIENT_ID, scopes=[], subject="owner")
        raw = self.store.data["access"].get(_hash(token))
        if not raw or raw["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=raw["client_id"],
            scopes=raw["scopes"],
            expires_at=raw["expires_at"],
            resource=raw.get("resource"),
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        hashed = _hash(token.token)
        with self.store.lock:
            access = self.store.data["access"].pop(hashed, None)
            if access and access.get("refresh"):
                self.store.data["refresh"].pop(access["refresh"], None)
            if self.store.data["refresh"].pop(hashed, None) is not None:
                for key in [k for k, v in self.store.data["access"].items() if v.get("refresh") == hashed]:
                    del self.store.data["access"][key]
            self.store.save()


# -- login page ----------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Obsidian MCP — connexion</title>
<style>
:root {{ color-scheme: light dark; --bg:#f6f5f2; --card:#fff; --fg:#1c1b19; --muted:#6b6862; --accent:#6f4fd8; --err:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --card:#1f1e1c; --fg:#ecebe7; --muted:#a19e97; --accent:#a48cf0; --err:#f2b8b5; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 -apple-system, system-ui, sans-serif; padding:16px; }}
main {{ width:100%; max-width:380px; background:var(--card); border-radius:14px; padding:28px 24px;
  box-shadow:0 1px 3px rgba(0,0,0,.08); }}
h1 {{ font-size:20px; margin:0 0 6px; }}
p {{ margin:0 0 18px; color:var(--muted); font-size:14px; }}
code {{ font-size:13px; word-break:break-all; }}
input {{ width:100%; font:inherit; padding:12px; border-radius:10px; border:1px solid var(--muted);
  background:transparent; color:inherit; margin-bottom:14px; }}
button {{ width:100%; font:inherit; font-weight:600; padding:12px; border:0; border-radius:10px;
  background:var(--accent); color:#fff; }}
.err {{ color:var(--err); font-size:14px; margin:0 0 14px; }}
</style></head><body><main>
<h1>Accès au vault Obsidian</h1>
{body}
</main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(body=body),
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
        },
    )


def _form(txn: str, pending: _PendingLogin, error: str | None = None) -> HTMLResponse:
    who = html.escape(pending.client_name or "Un client MCP")
    target = html.escape(urlparse(str(pending.params.redirect_uri)).netloc)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = f"""<p><strong>{who}</strong> demande l'accès à ton vault.<br>Retour vers <code>{target}</code></p>
{err}<form method="post" action="login">
<input type="hidden" name="txn" value="{html.escape(txn)}">
<input type="password" name="password" placeholder="Mot de passe" autocomplete="current-password" autofocus required>
<button type="submit">Autoriser</button></form>"""
    return _page(body, status=401 if error else 200)


def install_login_routes(server: Any, provider: VaultOAuthProvider) -> None:
    """Register ``GET/POST /login`` on the MCP server (routes added this way are public)."""

    expired = _page("<p>Cette demande a expiré. Relance la connexion depuis Claude.</p>", status=400)
    locked = _page(
        "<p>Trop de tentatives. Réessaie dans un quart d'heure.</p>", status=429
    )

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            pending = provider.pending(txn)
            if pending is None:
                return expired
            return _form(txn, pending)

        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        pending = provider.pending(txn)
        if pending is None:
            return expired
        if provider.locked_out():
            return locked
        try:
            redirect = provider.complete_login(txn, password)
        except KeyError:
            return expired
        if redirect is None:
            return _form(txn, pending, error="Mot de passe incorrect.")
        return RedirectResponse(redirect, status_code=302)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return Response("ok", media_type="text/plain")

