from __future__ import annotations

import asyncio
import json

import pytest
import requests

from obsidian_mcp import status, x_post, x_search, youtube, youtube_search


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("OBSIDIAN_MCP_STATE_DIR", str(tmp_path))
    for env in (youtube.PROXY_ENV, youtube_search.API_KEY_ENV, x_post.INBOX_ENV,
                x_search.COOKIES_ENV, x_search.ACCOUNT_ENV, "OBSIDIAN_VAULT_REMOTE"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(status, "_usage", None)
    return tmp_path


def test_tracked_counts_calls_without_content(state) -> None:
    def read_note(path: str) -> dict:
        return {"ok": True, "content": "secret text"}

    async def search_x(query: str) -> dict:
        return {"ok": False, "error": "nope"}

    wrapped = status.tracked(read_note)
    assert wrapped.__name__ == "read_note"
    assert list(__import__("inspect").signature(wrapped).parameters) == ["path"]
    wrapped(path="Private note")
    wrapped(path="Private note")
    asyncio.run(status.tracked(search_x)(query="my query"))

    saved = (state / status.USAGE_FILE).read_text()
    assert "Private note" not in saved and "secret" not in saved and "my query" not in saved
    data = json.loads(saved)
    assert data["read_note"]["calls"] == 2 and data["read_note"]["last_ok"] is True
    assert data["search_x"]["errors"] == 1 and data["search_x"]["last_ok"] is False


def test_tracked_counts_exceptions(state) -> None:
    def boom() -> dict:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        status.tracked(boom)()
    assert status.usage()["boom"]["errors"] == 1


def test_usage_survives_restart(state, monkeypatch) -> None:
    status.record("read_note", True)
    monkeypatch.setattr(status, "_usage", None)
    assert status.usage()["read_note"]["calls"] == 1


def test_report_not_configured(monkeypatch) -> None:
    status.record("youtube_transcript", True)
    # Network checks are replaced: only configuration is looked at here.
    monkeypatch.setattr(status, "check_fxtwitter", lambda: status._result("ok", "fake"))
    monkeypatch.setitem(status._SYNC_CHECKS, "fxtwitter", status.check_fxtwitter)
    out = asyncio.run(status.report(["youtube_transcript", "read_note", "read_x_post"]))
    checks = out["checks"]
    assert checks["youtube_proxy"]["state"] == "not_configured"
    assert checks["youtube_api_key"]["state"] == "not_configured"
    assert checks["x_search_account"]["state"] == "not_configured"
    assert checks["vault_git"]["state"] == "not_configured"
    tools = {t["tool"]: t for t in out["tools"]}
    assert tools["youtube_transcript"]["calls"] == 1
    assert tools["youtube_transcript"]["state"] == "not_configured"
    assert tools["read_x_post"]["state"] == "ok"
    assert "Revolut" in out["not_covered"]


def test_report_without_checks() -> None:
    out = asyncio.run(status.report(["read_note"], check=False))
    assert out["checks"] == {} and out["tools"][0]["state"] == "unchecked"


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code


def test_proxy_check_hides_credentials(monkeypatch) -> None:
    monkeypatch.setenv(youtube.PROXY_ENV, "http://user:hunter2@geo.example.com:12321")
    seen = {}

    def fake_get(url, proxies=None, timeout=None):
        seen["url"] = url
        return _Resp(204)

    monkeypatch.setattr(status.requests, "get", fake_get)
    out = status.check_youtube_proxy()
    assert out["state"] == "ok" and out["proxy"] == "geo.example.com:12321"
    assert "hunter2" not in json.dumps(out) and "user" not in json.dumps(out)

    def refused(url, proxies=None, timeout=None):
        raise requests.exceptions.ProxyError("407 Proxy Authentication Required hunter2")

    monkeypatch.setattr(status.requests, "get", refused)
    out = status.check_youtube_proxy()
    assert out["state"] == "error" and "password" in out["detail"]
    assert "hunter2" not in json.dumps(out)


def test_api_key_check(monkeypatch) -> None:
    monkeypatch.setenv(youtube_search.API_KEY_ENV, "AIza-secret")

    def bad(endpoint, params):
        raise youtube_search.SearchError("YouTube API error 400: API key not valid")

    monkeypatch.setattr(youtube_search, "_get", bad)
    out = status.check_youtube_api_key()
    assert out["state"] == "error" and "AIza" not in json.dumps(out)
    monkeypatch.setattr(youtube_search, "_get", lambda e, p: {"items": [{"id": "x"}]})
    assert status.check_youtube_api_key()["state"] == "ok"


def test_x_search_account_from_twscrape_state(monkeypatch, state) -> None:
    monkeypatch.setenv(x_search.COOKIES_ENV, "auth_token=abc; ct0=def")
    monkeypatch.setenv(x_search.ACCOUNT_ENV, "burner")
    assert asyncio.run(status.check_x_search_account())["detail"].endswith("never used yet")

    async def seed(active: bool) -> None:
        from twscrape import AccountsPool

        pool = AccountsPool(str(state / "twscrape.db"))
        await pool.add_account_cookies("burner", "auth_token=abc; ct0=def")
        if not active:
            await pool.mark_inactive("burner", "account suspended")

    asyncio.run(seed(True))
    assert asyncio.run(status.check_x_search_account())["state"] == "ok"

    async def lock() -> None:
        import time as t

        from twscrape import AccountsPool

        await AccountsPool(str(state / "twscrape.db")).lock_until(
            "burner", "SearchTimeline", int(t.time()) + 600
        )

    asyncio.run(lock())
    out = asyncio.run(status.check_x_search_account())
    assert out["state"] == "warning" and "rate limit" in out["detail"]
    asyncio.run(seed(False))
    out = asyncio.run(status.check_x_search_account())
    assert out["state"] == "error" and "suspended" in out["detail"]
    assert "abc" not in json.dumps(out)
