from __future__ import annotations

import time

import pytest

from obsidian_mcp import server, x_post


class FakeResp:
    def __init__(self, status: int, payload: dict | None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def status(i: str, text: str, author: str = "someone", **extra) -> dict:
    return {"type": "status", "id": i, "url": f"https://x.com/{author}/status/{i}", "text": text,
            "author": {"name": author.title(), "screen_name": author}, "created_timestamp": 1793628000, **extra}


PAGE = {
    "code": 200,
    "results": [
        status("1", "a reposted post", reposted_by={"screen_name": "axel_inbox"}),
        status("2", "my comment", author="axel_inbox", quote=status("9", "quoted")),
        status("3", "a link https://t.co/x", author="axel_inbox"),
        status("4", "art", reposted_by={"screen_name": "axel_inbox"},
               article={"title": "Long read", "content": {"blocks": [{"type": "unstyled", "text": "body"}]}}),
        {"type": "thread", "statuses": []},
    ],
    "cursor": {"top": "t", "bottom": "b1"},
}


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, dict]] = []

    def install(resp: FakeResp):
        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params))
            return resp
        monkeypatch.setattr(x_post.requests, "get", fake_get)
        return calls
    return install


def test_inbox_lists_posts_with_kind(fx, monkeypatch) -> None:
    monkeypatch.setenv(x_post.INBOX_ENV, "@axel_inbox")
    calls = fx(FakeResp(200, PAGE))
    out = server.read_x_inbox()
    assert out["ok"] and out["account"] == "axel_inbox" and out["count"] == 4
    assert [p["kind"] for p in out["posts"]] == ["repost", "quote", "post", "repost"]
    assert out["posts"][1]["quote"]["text"] == "quoted"
    assert out["posts"][3]["article"] == {"title": "Long read", "note": "full text with read_x_post"}
    assert out["next_cursor"] == "b1"
    assert calls == [(f"{x_post.FXTWITTER}/2/profile/axel_inbox/statuses", {"count": 20})]


def test_only_reposts_and_count(fx) -> None:
    fx(FakeResp(200, PAGE))
    out = server.read_x_inbox(account="https://x.com/axel_inbox", only_reposts=True, count=1)
    assert out["count"] == 1 and out["posts"][0]["id"] == "1" and out["not_shown"] == 1


def test_since_duration_and_date(fx) -> None:
    calls = fx(FakeResp(200, PAGE))
    server.read_x_inbox(account="axel_inbox", since="24h")
    assert abs(calls[0][1]["since"] - (time.time() - 86400)) < 5
    server.read_x_inbox(account="axel_inbox", since="2026-11-02")
    assert calls[1][1]["since"] == 1793577600
    server.read_x_inbox(account="axel_inbox", since="2026-11-02", cursor="b1")
    assert calls[2][1] == {"count": 20, "cursor": "b1"}  # since only applies to page 1


def test_nothing_new(fx) -> None:
    fx(FakeResp(204, None))
    out = server.read_x_inbox(account="axel_inbox", since="1h")
    assert out == {"ok": True, "account": "axel_inbox", "count": 0, "posts": [], "next_cursor": None}


def test_errors(fx, monkeypatch) -> None:
    monkeypatch.delenv(x_post.INBOX_ENV, raising=False)
    assert x_post.INBOX_ENV in server.read_x_inbox()["error"]
    assert server.read_x_inbox(account="not a handle!")["ok"] is False
    assert server.read_x_inbox(account="axel_inbox", since="yesterday")["ok"] is False
    fx(FakeResp(404, {"message": "User not found"}))
    out = server.read_x_inbox(account="axel_inbox")
    assert out["ok"] is False and "no posts found" in out["error"]


def test_fallback_to_with_replies_timeline(monkeypatch) -> None:
    calls: list[dict] = []
    page = {
        "code": 200,
        "results": [
            status("1", "reposted", reposted_by={"screen_name": "Axel_Inbox"}),
            status("2", "someone else's reply in a thread", author="stranger"),
            status("3", "my own post", author="axel_inbox"),
        ],
        "cursor": {"bottom": "b2"},
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        if params.get("with_replies"):
            return FakeResp(200, page)
        return FakeResp(404, {"code": 404, "results": [], "cursor": {"top": None, "bottom": None}})

    monkeypatch.setattr(x_post.requests, "get", fake_get)
    out = server.read_x_inbox(account="axel_inbox")
    assert out["ok"] and [p["id"] for p in out["posts"]] == ["1", "3"]
    assert out["next_cursor"] == "r:b2"
    assert calls == [{"count": 20}, {"count": 20, "with_replies": "1"}]
    # The next page goes straight to the with_replies timeline, with the bare cursor.
    server.read_x_inbox(account="axel_inbox", cursor="r:b2")
    assert calls[-1] == {"count": 20, "cursor": "b2", "with_replies": "1"}


def test_both_timelines_empty(fx) -> None:
    fx(FakeResp(404, {"code": 404, "results": [], "cursor": {"top": None, "bottom": None}}))
    out = server.read_x_inbox(account="axel_inbox")
    assert out["ok"] is False and "public" in out["error"]


def test_numeric_account_id(fx) -> None:
    calls = fx(FakeResp(200, PAGE))
    out = server.read_x_inbox(account="ID:12")
    assert out["ok"] and calls[0][0].endswith("/2/profile/id:12/statuses")
