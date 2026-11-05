"""Client for the official Volvo Cars APIs (Connected Vehicle v2, Energy v2, Location v1).

Docs: https://developer.volvocars.com/apis/
"""

import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .clock import Clock
from .db import DB

# Volvo ID is moving to a new sign-in service; clients made there have IDs
# starting "vid-dc-". Old "dc-" clients stop working after 31 January 2027.
OLD_ID = {
    "authorize": "https://volvoid.eu.volvocars.com/as/authorization.oauth2",
    "token": "https://volvoid.eu.volvocars.com/as/token.oauth2",
}
NEW_ID = {
    "authorize": "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/auth",
    "token": "https://auth.volvocars.com/realms/volvocars/protocol/openid-connect/token",
}


def volvo_id(client_id: str) -> dict[str, str]:
    """Which Volvo ID service a client belongs to."""
    return NEW_ID if client_id.startswith("vid-") else OLD_ID

API_URL = "https://api.volvocars.com"
CONNECTED = "/connected-vehicle/v2/vehicles"
ENERGY = "/energy/v2/vehicles"
LOCATION = "/location/v1/vehicles"

# Volvo allows 10,000 calls per day per API key.
DAILY_LIMIT = 10_000

# Every scope must also be ticked on the application in the developer portal.
SCOPES = [
    "openid",
    "conve:battery_charge_level",
    "conve:brake_status",
    "conve:climatization_start_stop",
    "conve:command_accessibility",
    "conve:commands",
    "conve:diagnostics_engine_status",
    "conve:diagnostics_workshop",
    "conve:doors_status",
    "conve:engine_status",
    "conve:fuel_status",
    "conve:lock",
    "conve:lock_status",
    "conve:odometer_status",
    "conve:trip_statistics",
    "conve:tyre_status",
    "conve:vehicle_relation",
    "conve:warnings",
    "conve:windows_status",
    "energy:capability:read",
    "energy:state:read",
    "location:read",
]

# Added after launch. If the application on the developer portal was published
# without them, Volvo refuses the whole sign-in, so they are dropped on retry.
OPTIONAL_SCOPES = {"conve:brake_status", "conve:diagnostics_engine_status", "conve:fuel_status"}

COMMANDS = {
    "lock": "LOCK",
    "climatization-start": "CLIMATIZATION_START",
    "climatization-stop": "CLIMATIZATION_STOP",
}


class VolvoError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class VolvoAuthError(VolvoError):
    """Volvo no longer accepts our tokens; the owner has to sign in again."""


class VolvoRateLimited(VolvoError):
    pass


class VolvoForbidden(VolvoError):
    """This one resource is off limits: scope not granted, or (for location) the
    car is in a region Volvo does not serve it in. Not a reason to sign in again."""


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class VolvoClient:
    def __init__(self, db: DB, clock: Clock, transport: httpx.AsyncBaseTransport | None = None):
        self.db = db
        self.clock = clock
        self.http = httpx.AsyncClient(transport=transport, timeout=30)
        # Volvo's refresh tokens are single-use. Polls fire several requests at
        # once, and without this they would all refresh with the same token; the
        # first wins and Volvo rejects the rest as reuse.
        self._refresh_lock = asyncio.Lock()
        # Called with (method, endpoint, status, body) for every API answer; the
        # collector uses it to keep the raw log. Tokens never pass through here.
        self.recorder: Callable[[str, str, int, str], None] | None = None

    # -- configuration -----------------------------------------------------

    @property
    def creds(self) -> dict[str, str]:
        return self.db.get("volvo_creds", {})

    @property
    def tokens(self) -> dict[str, Any]:
        return self.db.get("volvo_tokens", {})

    @property
    def vin(self) -> str:
        return self.db.get("volvo_vin", "")

    def configured(self) -> bool:
        c = self.creds
        return all(c.get(k) for k in ("api_key", "client_id", "client_secret", "redirect_uri"))

    def connected(self) -> bool:
        return self.configured() and bool(self.tokens.get("refresh_token")) and bool(self.vin)

    # -- OAuth (authorization code + PKCE) ---------------------------------

    def authorize_url(self) -> str:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(16)
        self.db.set("volvo_oauth_pending", {"verifier": verifier, "state": state})
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode().rstrip("=")
        )
        return volvo_id(self.creds["client_id"])["authorize"] + "?" + urlencode({
            "response_type": "code",
            "client_id": self.creds["client_id"],
            "redirect_uri": self.creds["redirect_uri"],
            "scope": " ".join(self.scopes()),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })

    def scopes(self) -> list[str]:
        if self.db.get("volvo_without_optional_scopes"):
            return [s for s in SCOPES if s not in OPTIONAL_SCOPES]
        return SCOPES

    def refused_scopes(self, error: str) -> str:
        """Note a scope refusal so the next attempt asks for less; returns a message."""
        if error == "invalid_scope" and not self.db.get("volvo_without_optional_scopes"):
            self.db.set("volvo_without_optional_scopes", True)
            return ("Your Volvo application does not include some optional scopes. "
                    "Sign in again and it will work without them.")
        return ""

    async def exchange(self, code_or_url: str, state: str | None = None) -> None:
        """Finish sign-in. Accepts the bare code or the whole URL Volvo redirected to."""
        code = code_or_url.strip()
        if "code=" in code or "error=" in code:
            query = parse_qs(urlparse(code).query)
            if "error" in query:
                retry = self.refused_scopes(query["error"][0])
                raise VolvoAuthError(retry or query.get("error_description", query["error"])[0])
            state = query.get("state", [state])[0]
            code = query["code"][0]
        pending = self.db.get("volvo_oauth_pending")
        if not pending:
            raise VolvoAuthError("No sign-in in progress. Start again from Settings.")
        if state is not None and state != pending["state"]:
            raise VolvoAuthError("Sign-in state mismatch. Start again from Settings.")
        await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.creds["redirect_uri"],
            "code_verifier": pending["verifier"],
        })
        self.db.delete("volvo_oauth_pending")
        self.db.set("volvo_consent_at", self.clock.now())
        self.db.delete("volvo_auth_error")

    async def _token_request(self, data: dict[str, str]) -> None:
        c = self.creds
        basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
        try:
            r = await self.http.post(
                volvo_id(c["client_id"])["token"], data=data, headers={"Authorization": f"Basic {basic}"}
            )
        except httpx.HTTPError as e:
            raise VolvoError(f"Could not reach Volvo ID: {type(e).__name__}") from e
        if r.status_code in (400, 401, 403):
            # Volvo answers 400 for an expired or revoked refresh token.
            raise VolvoAuthError(_error_text(r), r.status_code)
        if r.status_code >= 400:
            raise VolvoError(_error_text(r), r.status_code)
        body = r.json()
        self.db.set("volvo_tokens", {
            "access_token": body["access_token"],
            # Volvo rotates refresh tokens; keep the old one if none came back.
            "refresh_token": body.get("refresh_token") or self.tokens.get("refresh_token"),
            "expires_at": self.clock.now() + int(body.get("expires_in", 300)),
        })

    async def _access_token(self) -> str:
        if not self.tokens.get("refresh_token"):
            raise VolvoAuthError("Not signed in to Volvo.")
        async with self._refresh_lock:
            # Read again under the lock: another request may have just refreshed.
            t = self.tokens
            if t.get("expires_at", 0) - 30 < self.clock.now():
                await self._token_request({
                    "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                })
                t = self.tokens
        return t["access_token"]

    def disconnect(self) -> None:
        for key in ("volvo_tokens", "volvo_vin", "volvo_vehicle", "volvo_oauth_pending",
                    "volvo_consent_at", "volvo_auth_error", "volvo_capabilities"):
            self.db.delete(key)

    # -- requests ----------------------------------------------------------

    def calls_today(self) -> int:
        row = self.db.one("SELECT calls FROM api_usage WHERE day = ?", self._day())
        return row["calls"] if row else 0

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock.now(), timezone.utc).strftime("%Y-%m-%d")

    async def _request(self, method: str, path: str) -> dict[str, Any]:
        token = await self._access_token()
        self.db.x(
            "INSERT INTO api_usage (day, calls) VALUES (?, 1) "
            "ON CONFLICT (day) DO UPDATE SET calls = calls + 1",
            self._day(),
        )
        headers = {"Authorization": f"Bearer {token}", "vcc-api-key": self.creds["api_key"]}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        endpoint = path.replace(self.vin, "{vin}") if self.vin else path
        try:
            r = await self.http.request(method, API_URL + path, headers=headers)
        except httpx.HTTPError as e:
            if self.recorder:
                self.recorder(method, endpoint, 0, type(e).__name__)
            raise VolvoError(f"Could not reach Volvo: {type(e).__name__}") from e
        if self.recorder:
            self.recorder(method, endpoint, r.status_code, r.text)
        if r.status_code == 404:
            # Volvo answers 404 for data this car does not have.
            return {}
        if r.status_code == 401:
            raise VolvoAuthError(_error_text(r), 401)
        if r.status_code == 403:
            raise VolvoForbidden(_error_text(r), 403)
        if r.status_code == 429:
            raise VolvoRateLimited(_error_text(r), 429)
        if r.status_code >= 400:
            raise VolvoError(_error_text(r), r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise VolvoError("Volvo returned something that is not JSON") from e

    async def _data(self, path: str) -> dict[str, Any]:
        return (await self._request("GET", path)).get("data") or {}

    async def vehicles(self) -> list[str]:
        body = await self._request("GET", CONNECTED)
        return [v["vin"] for v in body.get("data") or []]

    async def vehicle(self, vin: str = "") -> dict[str, Any]:
        return await self._data(f"{CONNECTED}/{vin or self.vin}")

    async def connected_fields(self, resource: str) -> dict[str, Any]:
        """engine-status, command-accessibility, odometer, statistics, doors, windows,
        tyres, warnings, diagnostics: all return {field: {value, unit?, timestamp}}."""
        return await self._data(f"{CONNECTED}/{self.vin}/{resource}")

    async def energy_state(self) -> dict[str, Any]:
        """{field: {status, value, unit?, updatedAt}}; unsupported fields have status ERROR."""
        body = await self._request("GET", f"{ENERGY}/{self.vin}/state")
        return {
            k: v for k, v in body.items()
            if isinstance(v, dict) and v.get("status") == "OK"
        }

    async def energy_capabilities(self) -> dict[str, Any]:
        return await self._request("GET", f"{ENERGY}/{self.vin}/capabilities")

    async def location(self) -> dict[str, Any]:
        return await self._data(f"{LOCATION}/{self.vin}/location")

    async def available_commands(self) -> list[str]:
        body = await self._request("GET", f"{CONNECTED}/{self.vin}/commands")
        return [c["command"] for c in body.get("data") or []]

    async def command(self, name: str) -> dict[str, Any]:
        if name not in COMMANDS:
            raise VolvoError(f"Unknown command {name}")
        body = await self._request("POST", f"{CONNECTED}/{self.vin}/commands/{name}")
        return body.get("data") or {}


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = body.get("error")
    if isinstance(err, dict):
        return f"{err.get('message', '')} {err.get('description', '')}".strip() or f"HTTP {r.status_code}"
    return str(body.get("error_description") or err or f"HTTP {r.status_code}")
