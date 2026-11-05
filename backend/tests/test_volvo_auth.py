"""Signing in to Volvo, against a fake Volvo ID server."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from volvodash.clock import ManualClock
from volvodash.db import DB
from volvodash.volvo import SCOPES, VolvoAuthError, VolvoClient


def make_client(seen: list[httpx.Request], client_id: str = "dc-id") -> VolvoClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        form = parse_qs(request.content.decode())
        if form.get("code") == ["good"] or form.get("refresh_token") == ["r1"]:
            return httpx.Response(200, json={
                "access_token": "a1", "refresh_token": "r2", "expires_in": 1800})
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "nope"})

    db = DB(":memory:")
    db.set("volvo_creds", {"api_key": "k", "client_id": client_id, "client_secret": "secret",
                           "redirect_uri": "https://dash.example/api/volvo/callback"})
    return VolvoClient(db, ManualClock(1000), httpx.MockTransport(handle))


def test_sign_in_round_trip():
    seen: list[httpx.Request] = []
    client = make_client(seen)
    query = parse_qs(urlparse(client.authorize_url()).query)
    assert query["redirect_uri"] == ["https://dash.example/api/volvo/callback"]
    assert query["scope"] == [" ".join(SCOPES)]
    assert query["code_challenge_method"] == ["S256"]

    # The owner can paste the whole address Volvo redirected them to.
    pasted = f"https://dash.example/api/volvo/callback?code=good&state={query['state'][0]}"
    asyncio.run(client.exchange(pasted))

    assert client.tokens["refresh_token"] == "r2"
    form = parse_qs(seen[0].content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert len(form["code_verifier"][0]) >= 43
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_wrong_state_is_rejected():
    client = make_client([])
    client.authorize_url()
    with pytest.raises(VolvoAuthError):
        asyncio.run(client.exchange("good", state="someone-elses"))
    assert not client.tokens


def test_rejected_refresh_means_sign_in_again():
    client = make_client([])
    client.db.set("volvo_tokens", {"access_token": "old", "refresh_token": "stale", "expires_at": 0})
    client.db.set("volvo_vin", "VIN")
    with pytest.raises(VolvoAuthError):
        asyncio.run(client.vehicle())


def test_refused_optional_scopes_are_dropped_on_retry():
    from volvodash.volvo import OPTIONAL_SCOPES

    client = make_client([])
    client.authorize_url()
    state = client.db.get("volvo_oauth_pending")["state"]
    with pytest.raises(VolvoAuthError, match="optional scopes"):
        asyncio.run(client.exchange(
            f"http://localhost:8000/api/volvo/callback?error=invalid_scope&state={state}"))
    scope = parse_qs(urlparse(client.authorize_url()).query)["scope"][0].split()
    assert "location:read" in scope and not OPTIONAL_SCOPES & set(scope)


@pytest.mark.parametrize("client_id, host", [
    ("dc-abc", "volvoid.eu.volvocars.com"),
    ("vid-dc-abc", "auth.volvocars.com"),
])
def test_old_and_new_volvo_id_clients(client_id, host):
    # Clients made on Volvo ID's new service sign in there; old ones keep the old one.
    seen: list[httpx.Request] = []
    client = make_client(seen, client_id)
    url = urlparse(client.authorize_url())
    assert url.hostname == host
    asyncio.run(client.exchange("good", state=parse_qs(url.query)["state"][0]))
    assert seen[0].url.host == host
    assert client.tokens["refresh_token"] == "r2"


def test_reads_when_the_sign_in_runs_out():
    import base64, json
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 9_000_000}).encode()).decode().rstrip("=")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "a", "refresh_token": f"h.{payload}.s", "expires_in": 300})

    db = DB(":memory:")
    db.set("volvo_creds", {"api_key": "k", "client_id": "vid-dcr-x", "client_secret": "s",
                           "redirect_uri": "https://dash.example/api/volvo/callback"})
    db.set("volvo_tokens", {"refresh_token": "old", "expires_at": 0})
    client = VolvoClient(db, ManualClock(1000), httpx.MockTransport(handle))
    asyncio.run(client._access_token())
    # No lifetime in the answer: read it from the token itself.
    assert client.sign_in_until() == 9_000_000
