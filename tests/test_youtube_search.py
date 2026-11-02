from __future__ import annotations

import pytest

from obsidian_mcp import server, youtube_search


class FakeResp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict:
        return self._payload


SEARCH = {
    "items": [
        {
            "id": {"videoId": "NF1pwjL9-DE"},
            "snippet": {
                "title": "Elliptic Curves - Computerphile",
                "channelTitle": "Computerphile",
                "channelId": "UC9-y-6csu5WGm29I7JiwpnA",
                "publishedAt": "2018-10-11T12:00:00Z",
                "description": "Mike&#39;s video",
            },
        }
    ]
}
VIDEOS = {
    "items": [
        {
            "id": "NF1pwjL9-DE",
            "contentDetails": {"duration": "PT8M42S", "caption": "true"},
            "statistics": {"viewCount": "1000", "likeCount": "50"},
        }
    ]
}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(youtube_search.API_KEY_ENV, "secret-key")
    calls: list[tuple[str, dict, dict]] = []

    def install(responses: dict[str, FakeResp]):
        def fake_get(url, params=None, headers=None, timeout=None):
            endpoint = url.rsplit("/", 1)[-1]
            calls.append((endpoint, params, headers))
            return responses[endpoint]
        monkeypatch.setattr(youtube_search.requests, "get", fake_get)
        return calls
    return install


def test_search_merges_details(api) -> None:
    calls = api({"search": FakeResp(200, SEARCH), "videos": FakeResp(200, VIDEOS)})
    out = server.youtube_search_videos("elliptic curves", language="en", duration="medium")
    assert out["ok"] and out["count"] == 1
    r = out["results"][0]
    assert r["duration"] == "8:42" and r["views"] == 1000 and r["manual_captions"] is True
    assert r["description"] == "Mike's video" and r["published"] == "2018-10-11"
    endpoint, params, headers = calls[0]
    assert params["relevanceLanguage"] == "en" and params["videoDuration"] == "medium"
    assert "publishedAfter" not in params
    # The key travels in a header, never in the URL parameters.
    assert headers == {"X-goog-api-key": "secret-key"}
    assert "secret-key" not in str(params)


def test_channel_handle_is_resolved(api) -> None:
    calls = api({
        "channels": FakeResp(200, {"items": [{"id": "UC9-y-6csu5WGm29I7JiwpnA"}]}),
        "search": FakeResp(200, {"items": []}),
    })
    out = server.youtube_search_videos(channel="https://www.youtube.com/@Computerphile", order="date")
    assert out == {"ok": True, "count": 0, "results": []}
    assert calls[0][1]["forHandle"] == "@Computerphile"
    assert calls[1][1]["channelId"] == "UC9-y-6csu5WGm29I7JiwpnA"


def test_missing_key(monkeypatch) -> None:
    monkeypatch.delenv(youtube_search.API_KEY_ENV, raising=False)
    out = server.youtube_search_videos("x")
    assert out["ok"] is False and youtube_search.API_KEY_ENV in out["error"]


def test_quota_exceeded(api) -> None:
    api({"search": FakeResp(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})})
    out = server.youtube_search_videos("x")
    assert out["ok"] is False and "quota" in out["error"]


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"query": "x", "order": "bad"}, {"query": "x", "duration": "huge"},
     {"query": "x", "published_after": "2025/01/01"}],
)
def test_bad_arguments(api, kwargs) -> None:
    api({})
    assert server.youtube_search_videos(**kwargs)["ok"] is False


@pytest.mark.parametrize("iso,expected", [("PT8M42S", "8:42"), ("PT1H2M5S", "1:02:05"), ("PT45S", "0:45"), ("P1DT1H", "25:00:00"), ("", "")])
def test_duration(iso, expected) -> None:
    assert youtube_search._duration(iso) == expected
