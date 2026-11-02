from __future__ import annotations

from types import SimpleNamespace

import pytest
from youtube_transcript_api import NoTranscriptFound, RequestBlocked

from obsidian_mcp import server, youtube


@pytest.mark.parametrize(
    "value",
    [
        "NF1pwjL9-DE",
        "https://www.youtube.com/watch?v=NF1pwjL9-DE&t=42s",
        "youtube.com/watch?v=NF1pwjL9-DE",
        "https://m.youtube.com/watch?v=NF1pwjL9-DE",
        "https://youtu.be/NF1pwjL9-DE?si=abc",
        "https://www.youtube.com/shorts/NF1pwjL9-DE",
        "https://www.youtube.com/embed/NF1pwjL9-DE",
        "https://www.youtube.com/live/NF1pwjL9-DE",
    ],
)
def test_video_id(value: str) -> None:
    assert youtube.video_id(value) == "NF1pwjL9-DE"


@pytest.mark.parametrize("value", ["", "hello", "https://vimeo.com/123", "https://evil.com/watch?v=NF1pwjL9-DE"])
def test_video_id_rejects(value: str) -> None:
    with pytest.raises(youtube.YouTubeError):
        youtube.video_id(value)


class FakeTrack:
    def __init__(self, code: str, generated: bool) -> None:
        self.language_code = code
        self.language = code.upper()
        self.is_generated = generated
        self.is_translatable = True

    def translate(self, code: str) -> "FakeTrack":
        return FakeTrack(code, self.is_generated)

    def fetch(self) -> SimpleNamespace:
        snippets = [
            SimpleNamespace(text=f"hello  {self.language_code}", start=1.2, duration=1.0),
            SimpleNamespace(text="  ", start=2.0, duration=1.0),
            SimpleNamespace(text="world\nagain", start=3725.0, duration=1.0),
        ]
        return SimpleNamespace(
            snippets=snippets,
            language=self.language,
            language_code=self.language_code,
            is_generated=self.is_generated,
        )


class FakeList(list):
    def find_transcript(self, codes: list[str]) -> FakeTrack:
        for code in codes:
            for t in self:
                if t.language_code == code:
                    return t
        raise NoTranscriptFound("vid", codes, self)


class FakeApi:
    def __init__(self, tracks: list[FakeTrack] | Exception) -> None:
        self.tracks = tracks

    def list(self, vid: str) -> FakeList:
        if isinstance(self.tracks, Exception):
            raise self.tracks
        return FakeList(self.tracks)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    def install(tracks):
        monkeypatch.setattr(youtube, "_api", lambda: FakeApi(tracks))
    return install


def test_fetch_prefers_requested_language(fake) -> None:
    fake([FakeTrack("en", False), FakeTrack("fr", True)])
    out = server.youtube_transcript("https://youtu.be/NF1pwjL9-DE")
    assert out["ok"] and out["language_code"] == "fr" and out["auto_generated"] is True
    assert out["text"] == "hello fr world again"
    assert out["snippet_count"] == 2


def test_fetch_fallback_translate_and_timestamps(fake) -> None:
    fake([FakeTrack("de", True), FakeTrack("es", False)])
    out = server.youtube_transcript("NF1pwjL9-DE", translate_to="fr", timestamps=True)
    assert out["translated_from"] == "es" and out["language_code"] == "fr"
    assert out["text"] == "[00:01] hello fr\n[1:02:05] world again"


def test_blocked_explains_proxy(fake) -> None:
    fake(RequestBlocked("NF1pwjL9-DE"))
    out = server.youtube_transcript("NF1pwjL9-DE")
    assert out["ok"] is False and youtube.PROXY_ENV in out["error"]


def test_blocked_without_proxy_says_none_configured(fake, monkeypatch) -> None:
    monkeypatch.delenv(youtube.PROXY_ENV, raising=False)
    fake(RequestBlocked("NF1pwjL9-DE"))
    out = server.youtube_transcript("NF1pwjL9-DE")
    assert "no proxy is configured" in out["error"]


def test_proxy_retries_a_block_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv(youtube.PROXY_ENV, "http://u:p@proxy:1")
    answers = [RequestBlocked("NF1pwjL9-DE"), RequestBlocked("NF1pwjL9-DE"), [FakeTrack("en", False)]]
    monkeypatch.setattr(youtube, "_api", lambda: FakeApi(answers.pop(0)))
    out = server.youtube_transcript("NF1pwjL9-DE")
    assert out["ok"] and out["language_code"] == "en" and answers == []


def test_proxy_block_after_all_attempts_says_proxy_used(monkeypatch) -> None:
    monkeypatch.setenv(youtube.PROXY_ENV, "http://u:p@proxy:1")
    calls = []
    def api():
        calls.append(1)
        return FakeApi(RequestBlocked("NF1pwjL9-DE"))
    monkeypatch.setattr(youtube, "_api", api)
    out = server.youtube_transcript("NF1pwjL9-DE")
    assert out["ok"] is False and "even through the proxy" in out["error"]
    assert len(calls) == youtube.PROXY_ATTEMPTS
