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
             "entityRanges": [{"key": 3, "offset": 8, "length": 3}], "data": {}},
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
