"""Google sign-in, signed session cookies, and the encrypted credential store.

The app is gated because whoever can open the URL would otherwise be able to use
whatever credentials the server holds. Sign-in is restricted to one Google
Workspace domain, and the same consent grants the Drive access the render
pipeline needs — so there is no separate Drive API key in production.

Credentials live encrypted on disk rather than in environment variables so they
can be (re)connected from the UI. On Railway that path must be a mounted volume
or a redeploy wipes it and you reconnect once.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

import requests

# ── config ───────────────────────────────────────────────────────────────
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "gymclassvr.com")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

# Explicit local-dev escape hatch: skips the login gate and falls back to
# GOOGLE_API_KEY for Drive. Must never be set on the deployed app.
DEV_NO_AUTH = os.environ.get("DEV_NO_AUTH") == "1"

SESSION_COOKIE = "randcut_session"
STATE_COOKIE = "randcut_oauth_state"
SESSION_DAYS = 30

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.readonly",
]

STATE_DIR = Path(os.environ.get("RANDCUT_STATE_DIR", "state"))
STORE_PATH = STATE_DIR / "credentials.enc"

_lock = threading.RLock()
_store_cache: dict | None = None


class AuthError(Exception):
    """Sign-in failed in a way worth showing the user."""


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and SECRET_KEY)


def enforcing() -> bool:
    """Whether the login gate is live."""
    return configured() and not DEV_NO_AUTH


def storage_ready() -> bool:
    """Whether credentials can be saved — encryption needs SECRET_KEY."""
    return bool(SECRET_KEY)


# ── encrypted store ──────────────────────────────────────────────────────
def _fernet():
    """Fernet keyed off SECRET_KEY. Imported lazily so the app still boots
    (in dev-bypass mode) without the cryptography package."""
    from cryptography.fernet import Fernet
    if not SECRET_KEY:
        raise AuthError("SECRET_KEY is not set — cannot read or write stored credentials.")
    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())
    return Fernet(key)


def _read_store() -> dict:
    global _store_cache
    with _lock:
        if _store_cache is not None:
            return _store_cache
        if not STORE_PATH.exists():
            _store_cache = {}
            return _store_cache
        try:
            _store_cache = json.loads(_fernet().decrypt(STORE_PATH.read_bytes()))
        except Exception as e:
            # a rotated SECRET_KEY makes the file unreadable; don't take the app down
            print(f"Warning: could not read credential store ({e}). Starting empty.")
            _store_cache = {}
        return _store_cache


def _write_store(store: dict):
    global _store_cache
    with _lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_bytes(_fernet().encrypt(json.dumps(store).encode()))
        tmp.replace(STORE_PATH)          # atomic, so a crash can't truncate it
        os.chmod(STORE_PATH, 0o600)
        _store_cache = store


def _save_service(name: str, data: dict | None):
    store = dict(_read_store())
    if data is None:
        store.pop(name, None)
    else:
        store[name] = data
    _write_store(store)


# ── signed session cookie ────────────────────────────────────────────────
# Signed rather than server-side so sessions survive a restart or redeploy.
def _sign(payload: bytes) -> str:
    sig = hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(payload).decode()}.{base64.urlsafe_b64encode(sig).decode()}"


def _unsign(value: str) -> dict | None:
    try:
        payload_b64, sig_b64 = value.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_b64)
        expected = hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig_b64)):
            return None
        data = json.loads(payload)
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def make_session(email: str, name: str) -> str:
    return _sign(json.dumps({
        "email": email,
        "name": name,
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }).encode())


def current_user(request) -> dict | None:
    """The signed-in user, or None. In dev-bypass mode, a stand-in."""
    if not enforcing():
        return {"email": "dev@localhost", "name": "Local dev", "dev": True}
    cookie = request.cookies.get(SESSION_COOKIE)
    return _unsign(cookie) if cookie else None


# ── oauth flow ───────────────────────────────────────────────────────────
def login_url() -> tuple[str, str]:
    """Google consent URL plus the CSRF state to round-trip in a cookie."""
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",     # we need a refresh token for the render worker
        "prompt": "consent",          # ...and Google only re-issues one when asked
        "include_granted_scopes": "true",
        "state": state,
        "hd": ALLOWED_DOMAIN,         # hint; the real check is server-side below
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state


def _id_token_claims(id_token: str) -> dict:
    """Read the payload of an ID token that came straight from Google's token
    endpoint over TLS in exchange for our client secret. Google documents that
    such tokens need no signature check; anything arriving another way would."""
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def complete_login(code: str) -> dict:
    """Exchange the code, enforce the domain, and store the Drive credential."""
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=20)
    if not resp.ok:
        raise AuthError(f"Google rejected the sign-in: {resp.text[:200]}")
    tokens = resp.json()

    claims = _id_token_claims(tokens["id_token"])
    email = claims.get("email", "")
    if not claims.get("email_verified"):
        raise AuthError("That Google account has no verified email address.")
    # hd is the Workspace domain claim; comparing the email suffix too means a
    # consumer account that happens to spoof a display name still can't in.
    if claims.get("hd") != ALLOWED_DOMAIN or not email.endswith("@" + ALLOWED_DOMAIN):
        raise AuthError(f"Only {ALLOWED_DOMAIN} accounts can use this app.")

    google = {
        "email": email,
        "access_token": tokens.get("access_token"),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)) - 60,
        "connected_at": time.time(),
    }
    # Google omits refresh_token on re-consent sometimes; keep the one we have
    existing = _read_store().get("google") or {}
    google["refresh_token"] = tokens.get("refresh_token") or existing.get("refresh_token")
    if not google["refresh_token"]:
        raise AuthError("Google did not return a refresh token. Revoke the app's access "
                        "at myaccount.google.com/permissions and sign in again.")
    _save_service("google", google)

    return {"email": email, "name": claims.get("name") or email}


def google_connection() -> dict | None:
    g = _read_store().get("google")
    if not g:
        return None
    return {"email": g.get("email"), "connected_at": g.get("connected_at")}


def drive_access_token() -> str | None:
    """A live Drive access token, refreshed if expired. None if Drive was never
    connected. Safe to call from the render worker thread."""
    with _lock:
        g = dict(_read_store().get("google") or {})
        if not g.get("refresh_token"):
            return None
        if g.get("access_token") and g.get("expires_at", 0) > time.time():
            return g["access_token"]

        resp = requests.post(TOKEN_URL, data={
            "refresh_token": g["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
        }, timeout=20)
        if not resp.ok:
            # revoked or expired grant — force a reconnect rather than loop
            print(f"Warning: Drive token refresh failed: {resp.text[:200]}")
            return None
        tokens = resp.json()
        g["access_token"] = tokens.get("access_token")
        g["expires_at"] = time.time() + int(tokens.get("expires_in", 3600)) - 60
        _save_service("google", g)
        return g["access_token"]


def drive_auth() -> tuple[dict, dict]:
    """(extra query params, headers) for a Drive REST call.

    Prefers the connected account; falls back to GOOGLE_API_KEY, which is what
    local dev and the pre-OAuth deployment use.
    """
    token = drive_access_token() if configured() else None
    if token:
        return {}, {"Authorization": f"Bearer {token}"}
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if api_key:
        return {"key": api_key}, {}
    raise AuthError("Drive is not connected. Open Connections and sign in with Google, "
                    "or set GOOGLE_API_KEY for local development.")


# ── buffer ───────────────────────────────────────────────────────────────
# Buffer closed new OAuth app registration in 2019 and its current GraphQL API
# is personal-key only, so there is no sign-in link to offer — the key is pasted.
def buffer_connection() -> dict | None:
    b = _read_store().get("buffer")
    if not b:
        return None
    return {"connected_at": b.get("connected_at"), "hint": b.get("hint")}


def save_buffer_token(token: str):
    token = token.strip()
    if not token:
        raise AuthError("Paste a Buffer access token first.")
    _save_service("buffer", {
        "token": token,
        "connected_at": time.time(),
        # last 4 only, so the UI can show which key is stored without exposing it
        "hint": token[-4:] if len(token) > 8 else None,
    })


def buffer_token() -> str | None:
    b = _read_store().get("buffer")
    return (b or {}).get("token") or os.environ.get("BUFFER_ACCESS_TOKEN") or None


def disconnect(service: str):
    if service not in ("google", "buffer"):
        raise AuthError(f"Unknown service: {service}")
    _save_service(service, None)
