from __future__ import annotations

import pytest
import requests

from obsidian_mcp import server, x_post


class FakeResp:
    def __init__(self, status: int, payload: dict | None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


LONG = "Dès la fin des années 1990, Jérémy Rifkin décrivait " + "x" * 400 + " fin."
FX = {
    "code": 200,
    "message": "OK",
    "tweet": {
        "id": "2117195728044929458",
        "url": "https://x.com/gchampeau/status/2117195728044929458",
        "text": LONG,
        "author": {"name": "Guillaume Champeau", "screen_name": "gchampeau"},
        "created_timestamp": 1793628000,
        "lang": "fr",
        "likes": 12, "retweets": 3, "replies": 1, "views": 900,
        "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "altText": "chart"}]},
        "quote": {
            "id": "1", "url": "https://x.com/sama/status/1", "text": "quoted",
            "author": {"name": "Sam Altman", "screen_name": "sama"},
            "quote": {"id": "0", "text": "too deep"},
        },
    },
}
OEMBED_HTML = (
    '<blockquote class="twitter-tweet" data-dnt="true"><p lang="fr" dir="ltr">'
    "Dès la fin des années 1990, Jérémy Rifkin décrivait dans &quot;L&#39;âge de "
    "l&#39;accès&quot; comment… "
    '<a href="https://t.co/47EUltyeNM">https://t.co/47EUltyeNM</a></p>'
    "&mdash; Guillaume Champeau (@gchampeau) "
    '<a href="https://x.com/gchampeau/status/2117195728044929458?ref_src=twsrc%5Etfw">'
    "November 2, 2026</a></blockquote>\n\n"
)
OEMBED = {
    "url": "https://x.com/gchampeau/status/2117195728044929458",
    "author_name": "Guillaume Champeau",
    "author_url": "https://x.com/gchampeau",
    "html": OEMBED_HTML,
}
URL = "https://x.com/gchampeau/status/2117195728044929458?a=fJP-XObEEnnuHAmWyoY&s=46"


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, dict | None]] = []

    def install(fx, oembed=None):
        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params))
            assert headers and "User-Agent" in headers
            resp = fx if url.startswith(x_post.FXTWITTER) else oembed
            if isinstance(resp, Exception):
                raise resp
            return resp
        monkeypatch.setattr(x_post.requests, "get", fake_get)
        return calls
    return install


@pytest.mark.parametrize(
    "url,expected",
    [
        (URL, ("2117195728044929458", "gchampeau")),
        ("twitter.com/jack/status/20", ("20", "jack")),
        ("https://mobile.twitter.com/jack/statuses/20/photo/1", ("20", "jack")),
        ("https://www.fxtwitter.com/jack/status/20", ("20", "jack")),
        ("https://x.com/i/web/status/20", ("20", None)),
        ("20", ("20", None)),
    ],
)
def test_parse_url(url, expected) -> None:
    assert x_post.parse_url(url) == expected


@pytest.mark.parametrize("url", ["https://example.com/jack/status/20", "https://x.com/jack", "", "https://x.com/jack/status/abc"])
def test_parse_url_rejects(url) -> None:
    with pytest.raises(x_post.XPostError):
        x_post.parse_url(url)


def test_fxtwitter_full_text(web) -> None:
    calls = web(FakeResp(200, FX))
    out = server.read_x_post(URL)
    assert out["ok"] and out["source"] == "fxtwitter"
    assert out["text"] == LONG and out["truncated"] is False
    assert out["username"] == "gchampeau" and out["reposts"] == 3
    assert out["date"].startswith("2026-")
    assert out["media"] == [{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "alt_text": "chart"}]
    assert out["quote"]["username"] == "sama" and "quote" not in out["quote"]
    assert calls == [(f"{x_post.FXTWITTER}/gchampeau/status/2117195728044929458", None)]


def test_fallback_to_oembed_marks_truncation(web) -> None:
    calls = web(requests.ConnectionError("down"), FakeResp(200, OEMBED))
    out = server.read_x_post(URL)
    assert out["ok"] and out["source"] == "oembed"
    assert out["text"].startswith("Dès la fin des années 1990, Jérémy Rifkin décrivait dans \"L'âge de l'accès\"")
    assert out["text"].endswith("… https://t.co/47EUltyeNM")
    assert out["truncated"] is True and "note" in out
    assert out["username"] == "gchampeau" and "FxTwitter" in out["fallback_reason"]
    assert calls[1][1]["url"] == "https://x.com/gchampeau/status/2117195728044929458"


def test_short_post_from_oembed_is_not_truncated(web) -> None:
    short = dict(OEMBED, html='<blockquote><p lang="en">just setting up my twttr</p>&mdash; jack</blockquote>')
    web(FakeResp(500, {"message": "oops"}), FakeResp(200, short))
    out = server.read_x_post("https://x.com/jack/status/20")
    assert out["text"] == "just setting up my twttr" and out["truncated"] is False


def test_deleted_post(web) -> None:
    web(FakeResp(404, {"code": 404, "message": "NOT_FOUND"}), FakeResp(404, None))
    out = server.read_x_post("https://x.com/jack/status/20")
    assert out["ok"] is False and "not found" in out["error"]


def test_both_sources_down(web) -> None:
    web(requests.Timeout("slow"), requests.ConnectionError("down"))
    out = server.read_x_post("20")
    assert out["ok"] is False and "FxTwitter" in out["error"] and "oEmbed" in out["error"]


def test_bad_link() -> None:
    out = server.read_x_post("https://example.com/whatever")
    assert out["ok"] is False and "not an X post link" in out["error"]


ARTICLE = {
    "id": "2117189870661554176",
    "title": "Claude SEO Second Brain: The Complete Guide",
    "preview_text": "I RUN ALL OF OUR SEO OUT OF THIS SETUP. ",
    "cover_media": {
        "id": "QXBp", "media_id": "2117190345209372672",
        "media_info": {"__typename": "ApiImage", "original_img_url": "https://pbs.twimg.com/media/cover.jpg"},
    },
    "media_entities": [
        {"media_id": "2117191116013363200", "media_info": {"original_img_url": "https://pbs.twimg.com/media/fig.jpg"}},
    ],
    "content": {
        "blocks": [
            {"key": "a", "type": "unstyled", "text": "I RUN ALL OF OUR SEO OUT OF THIS SETUP.", "entityRanges": [], "data": {}},
            {"key": "b", "type": "header-two", "text": "Why 🧠 memory", "entityRanges": [], "data": {}},
            {"key": "c", "type": "unstyled", "text": "😀 Read the docs now.",
             "entityRanges": [{"key": 3, "offset": 7, "length": 3}], "data": {}},
            {"key": "d", "type": "atomic", "text": " ", "entityRanges": [{"key": 8, "offset": 0, "length": 1}], "data": {}},
            {"key": "e", "type": "atomic", "text": " ", "entityRanges": [{"key": 12, "offset": 0, "length": 1}], "data": {}},
            {"key": "f", "type": "unordered-list-item", "text": "first point", "entityRanges": [], "data": {}},
        ],
        "entityMap": [
            {"key": "3", "value": {"type": "LINK", "data": {"url": "https://docs.example.com"}}},
            {"key": "8", "value": {"type": "MEDIA", "data": {
                "caption": "To get similar results for your business",
                "mediaItems": [{"mediaId": "2117191116013363200"}]}}},
            {"key": "12", "value": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "999"}]}}},
        ],
    },
}


def test_article_is_read_in_full(web) -> None:
    tweet = dict(FX["tweet"], text="", article=ARTICLE)
    tweet.pop("quote")
    web(FakeResp(200, {"code": 200, "tweet": tweet}))
    art = server.read_x_post("https://x.com/NicholasDulait/status/2117194945668714566?s=20")["article"]
    assert art["title"].startswith("Claude SEO Second Brain")
    assert art["cover_image"] == "https://pbs.twimg.com/media/cover.jpg"
    assert art["blocks"] == 6 and art["images"] == 2
    assert art["text"] == "\n\n".join([
        "I RUN ALL OF OUR SEO OUT OF THIS SETUP.",
        "## Why 🧠 memory",
        "😀 Read [the](https://docs.example.com) docs now.",
        "![To get similar results for your business](https://pbs.twimg.com/media/fig.jpg)",
        "[image]",
        "- first point",
    ])


def test_article_entity_map_as_dict() -> None:
    art = x_post._article({
        "title": "t",
        "content": {
            "blocks": [{"type": "unstyled", "text": "see here", "entityRanges": [{"key": 0, "offset": 4, "length": 4}]}],
            "entityMap": {"0": {"type": "LINK", "data": {"url": "https://a.b"}}},
        },
    })
    assert art["text"] == "see [here](https://a.b)"


def test_article_code_block_and_emoji_link_offsets() -> None:
    art = x_post._article({
        "title": "t",
        "content": {
            "blocks": [
                {"type": "unstyled", "text": "\U0001F381 you can click here", "entityRanges": [{"key": 0, "offset": 10, "length": 10}]},
                {"type": "unstyled", "text": ".claude/rules/pages.md:", "entityRanges": []},
                {"type": "atomic", "text": " ", "entityRanges": [{"key": 1, "offset": 0, "length": 1}]},
            ],
            "entityMap": [
                {"key": "0", "value": {"type": "LINK", "data": {"url": "https://a.b"}}},
                {"key": "1", "value": {"type": "MARKDOWN", "data": {"markdown": "```yaml\npaths: pages/**\n```\n"}}},
            ],
        },
    })
    assert art["text"] == "\U0001F381 you can [click here](https://a.b)\n\n.claude/rules/pages.md:\n\n```yaml\npaths: pages/**\n```"
    assert art["images"] == 0


def _v2_status(id_, text, user, likes=0, parent=None, **extra):
    return {
        "type": "status", "id": id_, "url": f"https://x.com/{user}/status/{id_}", "text": text,
        "created_timestamp": 1793628000, "author": {"name": user.title(), "screen_name": user},
        "likes": likes, "reposts": 1, "quotes": 2, "replies": 0, "views": 50, "bookmarks": 4,
        "media": {}, "lang": "fr",
        "replying_to": {"screen_name": "a", "status": parent} if parent else None, **extra,
    }


CONV = {
    "code": 200,
    "status": _v2_status("2", "the post", "a", likes=9, parent="1"),
    "thread": [_v2_status("1", "thread start", "a"), _v2_status("2", "the post", "a")],
    "replies": [
        _v2_status("3", "great", "b", likes=5, parent="2"),
        _v2_status("4", "agree", "c", parent="3"),
        {"type": "tombstone", "id": "5", "message": "This post is unavailable"},
    ],
    "cursor": {"bottom": "CUR"},
}


def test_conversation(web) -> None:
    calls = web(FakeResp(200, CONV))
    out = server.read_x_conversation("https://x.com/a/status/2?s=20", sort="recency")
    assert out["ok"] and out["id"] == "2"
    assert out["post"]["likes"] == 9 and out["post"]["quotes"] == 2 and out["post"]["bookmarks"] == 4
    assert out["post"]["replying_to"] == "a" and out["post"]["replying_to_status"] == "1"
    assert [t["id"] for t in out["thread"]] == ["1"]
    assert [r.get("username") for r in out["replies"]] == ["b", "c", None]
    assert out["replies"][1]["replying_to_status"] == "3"
    assert out["replies"][2]["unavailable"] == "This post is unavailable"
    assert out["next_cursor"] == "CUR"
    assert calls == [(f"{x_post.FXTWITTER}/2/conversation/2", {"ranking_mode": "recency"})]


def test_conversation_next_page_has_only_replies(web) -> None:
    calls = web(FakeResp(200, {"code": 200, "status": None, "thread": None, "replies": [], "cursor": None}))
    out = server.read_x_conversation("2", cursor="CUR")
    assert out["ok"] and "post" not in out and out["replies"] == [] and out["next_cursor"] is None
    assert calls[0][1] == {"ranking_mode": "likes", "cursor": "CUR"}


def test_conversation_errors(web) -> None:
    assert "sort must be" in server.read_x_conversation("2", sort="views")["error"]
    web(FakeResp(404, {"code": 404}))
    assert "not found" in server.read_x_conversation("2")["error"]
    web(requests.ConnectionError("down"))
    assert "could not reach FxTwitter" in server.read_x_conversation("2")["error"]


def test_reposts_and_quotes(web) -> None:
    user = {"name": "Bob", "screen_name": "bob", "followers": 12, "description": "hi", "verification": {"verified": True}}
    calls = web(FakeResp(200, {"code": 200, "results": [user], "cursor": {"top": None, "bottom": "N"}}))
    out = server.read_x_reposts("https://x.com/a/status/2", count=500)
    assert out["users"] == [{"name": "Bob", "username": "bob", "followers": 12, "description": "hi", "verified": True}]
    assert out["next_cursor"] == "N"
    assert calls[-1] == (f"{x_post.FXTWITTER}/2/status/2/reposts", {"count": 100})
    web(FakeResp(200, {"code": 200, "results": [_v2_status("9", "lol", "d")], "cursor": {"bottom": None}}))
    out = server.read_x_quotes("2", count=5, cursor="X")
    assert out["quotes"][0]["text"] == "lol" and out["next_cursor"] is None
    assert calls[-1] == (f"{x_post.FXTWITTER}/2/status/2/quotes", {"count": 5, "cursor": "X"})
