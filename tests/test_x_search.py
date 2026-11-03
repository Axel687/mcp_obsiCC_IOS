from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from obsidian_mcp import server, x_search

COOKIES = "auth_token=abc123; ct0=def456"


def _tweet(**kw):
    user = SimpleNamespace(
        username="alice", displayname="Alice", followersCount=10, blue=True, verified=False
    )
    base = dict(
        url="https://x.com/alice/status/1",
        id_str="1",
        date=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        user=user,
        rawContent="hello world",
        lang="en",
        likeCount=5,
        retweetCount=1,
        replyCount=2,
        quoteCount=0,
        viewCount=100,
        inReplyToTweetId=None,
        inReplyToScreenName=None,
        quotedTweet=None,
        links=[SimpleNamespace(url="https://example.com")],
        media=SimpleNamespace(photos=[SimpleNamespace(url="https://pbs.twimg.com/a.jpg")], videos=[], animated=[]),
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv(x_search.COOKIES_ENV, COOKIES)
    monkeypatch.setenv(x_search.ACCOUNT_ENV, "@burner")
    monkeypatch.setenv("OBSIDIAN_MCP_STATE_DIR", str(tmp_path))
    return tmp_path


def _fake_search(monkeypatch, tweets, seen):
    import twscrape

    async def fake(self, q, limit=-1, kv=None):
        seen.append((q, limit, kv))
        for t in tweets:
            yield t

    monkeypatch.setattr(twscrape.API, "search", fake)


def run(coro):
    return asyncio.run(coro)


def test_not_configured(monkeypatch) -> None:
    monkeypatch.delenv(x_search.COOKIES_ENV, raising=False)
    out = run(server.search_x("claude"))
    assert not out["ok"] and "not configured" in out["error"]


def test_search_formats_posts(env, monkeypatch) -> None:
    seen: list = []
    quoted = _tweet(url="https://x.com/bob/status/2", id_str="2", rawContent="quoted")
    _fake_search(monkeypatch, [_tweet(quotedTweet=quoted), _tweet(id_str="3")], seen)
    out = run(server.search_x("claude mcp", count=1, sort="top"))
    assert out["ok"] and out["count"] == 1 and out["account"] == "burner"
    assert seen == [("claude mcp", 1, {"product": "Top"})]
    post = out["posts"][0]
    assert post["kind"] == "quote" and post["quoted"]["text"] == "quoted"
    assert post["author"] == {"username": "alice", "name": "Alice", "followers": 10, "verified": True}
    assert post["links"] == ["https://example.com"] and post["media"] == ["https://pbs.twimg.com/a.jpg"]
    assert COOKIES not in str(out) and "abc123" not in str(out)
    assert (env / "twscrape.db").exists()


def test_bad_sort_and_empty_query(env) -> None:
    assert "sort" in run(server.search_x("x", sort="old"))["error"]
    assert "empty" in run(server.search_x("  "))["error"]


def test_inactive_account_reported(env, monkeypatch) -> None:
    from twscrape import AccountsPool

    async def setup():
        pool = AccountsPool(str(env / "twscrape.db"))
        await pool.add_account_cookies("burner", COOKIES)
        await pool.mark_inactive("burner", "Account suspended")

    run(setup())
    out = run(server.search_x("claude"))
    assert not out["ok"] and "suspended" in out["error"] and "abc123" not in out["error"]


def test_new_cookies_reactivate(env, monkeypatch) -> None:
    from twscrape import AccountsPool

    async def setup():
        pool = AccountsPool(str(env / "twscrape.db"))
        await pool.add_account_cookies("burner", "auth_token=old; ct0=old")
        await pool.mark_inactive("burner", "expired")

    run(setup())
    _fake_search(monkeypatch, [_tweet()], [])
    out = run(server.search_x("claude"))
    assert out["ok"] and out["count"] == 1
