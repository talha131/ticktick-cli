"""TickTick OAuth 2.0.

OAuth pattern inspired by jacepark12/ticktick-mcp (MIT). Reimplemented to
match our token-store layout and the local-server callback at :8181.

NOTE on refresh tokens: TickTick's Open API token endpoint does NOT issue a
refresh_token alongside the access_token (observed during real /setup).
Instead it issues a long-lived access_token (~180 days). Our code therefore
treats refresh_token as optional everywhere. When the access token does
eventually expire and we have no refresh token to use, the only recovery
is to re-run `setup` and re-authorize in the browser."""

from __future__ import annotations
import json
import time
import secrets
import webbrowser
import http.server
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
import httpx

AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
CALLBACK_PORT = 8181
CALLBACK_PATH = "/callback"
SCOPES = "tasks:read tasks:write"


@dataclass
class Tokens:
    access_token: str
    expires_at: int  # unix epoch seconds
    refresh_token: str | None = None  # TickTick may not issue one


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(
        self,
        access_token: str,
        refresh_token: str | None,
        expires_at: int,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "access_token": access_token,
            "expires_at": expires_at,
        }
        if refresh_token is not None:
            payload["refresh_token"] = refresh_token
        self.path.write_text(json.dumps(payload))
        self.path.chmod(0o600)

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        d = json.loads(self.path.read_text())
        return Tokens(
            access_token=d["access_token"],
            expires_at=int(d["expires_at"]),
            refresh_token=d.get("refresh_token"),
        )


class TickTickAuth:
    def __init__(self, token_store: TokenStore, client_id: str, client_secret: str) -> None:
        self.store = token_store
        self.client_id = client_id
        self.client_secret = client_secret

    def get_access_token_sync(self) -> str:
        tok = self.store.load()
        if tok is None:
            raise RuntimeError("No tokens saved. Run setup first.")
        if tok.expires_at > int(time.time()) + 30:
            return tok.access_token
        # Token expired. Refresh requires a refresh_token, which TickTick
        # often does not issue. Surface a clear recovery message instead of
        # silently failing on a missing field.
        if tok.refresh_token is None:
            raise RuntimeError(
                "Access token expired and TickTick did not issue a refresh "
                "token. Re-authorize by running: "
                "uv run python -m ticktick_cli setup"
            )
        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": tok.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        resp.raise_for_status()
        d = resp.json()
        new = Tokens(
            access_token=d["access_token"],
            expires_at=int(time.time()) + int(d["expires_in"]),
            refresh_token=d.get("refresh_token", tok.refresh_token),
        )
        self.store.save(new.access_token, new.refresh_token, new.expires_at)
        return new.access_token

    def run_initial_auth_flow(self) -> Tokens:
        """Interactive OAuth: open browser, wait for callback, exchange code."""
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.client_id,
            "scope": SCOPES,
            "redirect_uri": f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}",
            "response_type": "code",
            "state": state,
        }
        url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
        captured: dict[str, str] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404); self.end_headers(); return
                qs = dict(urllib.parse.parse_qsl(parsed.query))
                captured.update(qs)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>TickTick auth complete.</h1>"
                                 b"<p>You can close this tab.</p>")

            def log_message(self, *_): pass  # silence

        server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
        webbrowser.open(url)
        server.handle_request()  # one request, then exit
        if captured.get("state") != state:
            raise RuntimeError("OAuth state mismatch — aborting.")
        code = captured["code"]
        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        resp.raise_for_status()
        d = resp.json()
        tok = Tokens(
            access_token=d["access_token"],
            expires_at=int(time.time()) + int(d["expires_in"]),
            refresh_token=d.get("refresh_token"),  # may be absent
        )
        self.store.save(tok.access_token, tok.refresh_token, tok.expires_at)
        return tok
