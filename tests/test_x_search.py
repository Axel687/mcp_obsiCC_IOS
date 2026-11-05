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


class FakeRep:
    def __init__(self, tweets, headers=None) -> None:
        self.tweets = tweets
        self.headers = headers or {}

    def json(self):
        return {"tweets": self.tweets}


def _fake_search(monkeypatch, tweets, seen, pages=None, headers=None):
    """Fake X: `pages` lists the tweets of each page (default: one page with `tweets`)."""
    import twscrape
    import twscrape.models

    pages = pages if pages is not None else [tweets]

    async def fake(self, q, limit=-1, kv=None):
        seen.append((q, limit, kv))
        for i, page in enumerate(pages):
            yield FakeRep(page, (headers or [{}] * len(pages))[i])

    def parse(obj, limit=-1):
        items = obj["tweets"]
        return iter(items if limit < 0 else items[:limit])

    monkeypatch.setattr(twscrape.API, "search_raw", fake)
    monkeypatch.setattr(twscrape.models, "parse_tweets", parse)


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


def _headers(limit, remaining, reset):
    return {"x-rate-limit-limit": str(limit), "x-rate-limit-remaining": str(remaining),
            "x-rate-limit-reset": str(reset)}


def test_quota_reported_and_saved(env, monkeypatch) -> None:
    reset = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    seen: list = []
    pages = [[_tweet(id_str=str(i)) for i in range(20)], [_tweet(id_str=str(i)) for i in range(20, 40)],
             [_tweet(id_str=str(i)) for i in range(40, 60)]]
    _fake_search(monkeypatch, [], seen, pages=pages,
                 headers=[_headers(50, 49, reset), _headers(50, 48, reset), _headers(50, 47, reset)])
    out = run(server.search_x("claude", count=50))
    assert out["ok"] and out["count"] == 50 and out["requests"] == 3
    assert [p["id"] for p in out["posts"]][-1] == "49"
    assert out["quota"] == {"limit": 50, "remaining": 47, "resets_at": "2030-01-01T00:00:00+00:00",
                            "searches_left": 15}
    saved = x_search.last_quota(env / "twscrape.db")
    assert saved["remaining"] == 47 and saved["account"] == "burner"


def test_small_count_costs_one_request(env, monkeypatch) -> None:
    reset = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    _fake_search(monkeypatch, [], [], pages=[[_tweet(id_str=str(i)) for i in range(20)], [_tweet()]],
                 headers=[_headers(50, 49, reset), _headers(50, 48, reset)])
    out = run(server.search_x("claude", count=20))
    assert out["requests"] == 1 and out["count"] == 20 and out["quota"]["searches_left"] == 49


def test_missing_headers_no_quota(env, monkeypatch) -> None:
    _fake_search(monkeypatch, [_tweet()], [])
    out = run(server.search_x("claude"))
    assert out["ok"] and "quota" not in out and out["requests"] == 1


@pytest.mark.parametrize("count,expected", [(1, 1), (20, 1), (21, 2), (40, 2), (50, 3), (500, 3), (0, 1)])
def test_requests_needed(count, expected) -> None:
    assert x_search.requests_needed(count) == expected


def test_last_quota_after_reset(env) -> None:
    db = env / "twscrape.db"
    past = {"account": "burner", "limit": 50, "remaining": 0, "resets_at": "2020-01-01T00:00:00+00:00"}
    (env / x_search.QUOTA_FILE).write_text(__import__("json").dumps(past))
    q = x_search.last_quota(db)
    assert q["remaining"] == 50 and "reset" in q["note"]
    (env / x_search.QUOTA_FILE).write_text("garbage")
    assert x_search.last_quota(db) is None


def test_busy_error_mentions_used_up_quota(env, monkeypatch) -> None:
    import json
    import twscrape
    from twscrape.accounts_pool import NoAccountError

    (env / x_search.QUOTA_FILE).write_text(json.dumps(
        {"account": "burner", "limit": 50, "remaining": 0, "resets_at": "2030-01-01T00:00:00+00:00"}))

    async def fake(self, q, limit=-1, kv=None):
        raise NoAccountError("none")
        yield  # pragma: no cover

    monkeypatch.setattr(twscrape.API, "search_raw", fake)
    out = run(server.search_x("claude"))
    assert not out["ok"] and "busy or paused" in out["error"] and "used up until 2030-01-01" in out["error"]


def test_docstring_guides_quota_use() -> None:
    doc = server.search_x.__doc__
    assert "count <= 20 costs 1 request" in doc and "read_x_post" in doc and "never touch this quota" in doc


def test_page_tweets_keeps_timeline_entries_in_order() -> None:
    t = {i: SimpleNamespace(id_str=i) for i in ("1", "2", "3", "9")}
    obj = {"data": {"instructions": [{"entries": [
        {"entryId": "tweet-2"}, {"entryId": "promoted-tweet-3"}, {"entryId": "tweet-1"},
        {"entryId": "cursor-bottom-x"}, {"entryId": "tweet-2"},
    ]}]}}
    # "9" stands for a quoted post that twscrape also returns: it is not a result.
    parse = lambda o, limit=-1: iter([t["9"], t["1"], t["3"], t["2"]])  # noqa: E731
    assert [x.id_str for x in x_search.page_tweets(obj, parse)] == ["2", "1"]
    # Unknown layout: fall back to everything twscrape found.
    assert len(x_search.page_tweets({"data": {}}, parse)) == 4


def test_count_is_respected_within_a_page(env, monkeypatch) -> None:
    _fake_search(monkeypatch, [], [], pages=[[_tweet(id_str=str(i)) for i in range(21)]])
    out = run(server.search_x("claude", count=10))
    assert out["count"] == 10 and out["requests"] == 1
