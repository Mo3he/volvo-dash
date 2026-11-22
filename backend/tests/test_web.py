"""The static web app and notification routing."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from volvodash import app as webapp
from volvodash import enrich


def test_web_app_only_serves_files_inside_its_folder(tmp_path, monkeypatch):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("index")
    (static / "assets" / "app.js").write_text("js")
    (tmp_path / "secret.txt").write_text("secret")
    (tmp_path / "dist-other").mkdir()
    (tmp_path / "dist-other" / "x.txt").write_text("sibling")
    monkeypatch.setattr(webapp, "STATIC", static)

    def served(path):
        return str(asyncio.run(webapp.spa(path)).path)

    assert served("assets/app.js").endswith("app.js")
    assert served("../secret.txt").endswith("index.html")
    assert served(str(tmp_path / "secret.txt")).endswith("index.html")
    assert served("../dist-other/x.txt").endswith("index.html")
    assert served("trips").endswith("index.html")


def test_status_includes_when_lock_state_was_fetched(rig, monkeypatch):
    r = rig("live")
    fetched_at = r.clock.now() - 90
    r.db.x(
        "INSERT INTO state (key, value, unit, ts, fetched_at) VALUES (?, ?, ?, ?, ?)",
        "centralLock", json.dumps("LOCKED"), None, fetched_at - 10, fetched_at,
    )
    monkeypatch.setattr(webapp.ctx, "db", r.db, raising=False)
    monkeypatch.setattr(webapp.ctx, "clock", r.clock, raising=False)
    monkeypatch.setattr(webapp.ctx, "client", r.client, raising=False)
    monkeypatch.setattr(webapp.ctx, "collector", r.collector, raising=False)
    monkeypatch.setattr(webapp.ctx, "mqtt", SimpleNamespace(status={}), raising=False)
    monkeypatch.setattr(webapp.ctx, "sim_mode", None, raising=False)

    status = asyncio.run(webapp.status())

    assert status["state"]["centralLock"]["ts"] == fetched_at - 10
    assert status["state"]["centralLock"]["fetched_at"] == fetched_at


@pytest.mark.parametrize("url, body", [
    ("https://discord.com/api/webhooks/1/abc", {"content": "T. m"}),
    ("https://canary.discord.com/api/webhooks/1/abc", {"content": "T. m"}),
    ("https://hooks.slack.com/services/x", {"text": "T. m"}),
    ("https://evil.example/discord.com/api/webhooks/1", None),
    ("https://hooks.slack.com.evil.example/x", None),
    ("https://ntfy.example/topic?u=hooks.slack.com", None),
])
def test_webhook_format_follows_the_host(monkeypatch, url, body):
    sent = {}

    def handler(request):
        sent["json"] = request.headers.get("content-type") == "application/json"
        sent["content"] = request.content
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(enrich.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(enrich.send_webhook(url, "T", "m"))
    if body is None:
        assert not sent["json"] and sent["content"] == b"m"
    else:
        import json
        assert sent["json"] and json.loads(sent["content"]) == body
