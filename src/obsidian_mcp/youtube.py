"""Fetch the subtitles of a YouTube video, with youtube-transcript-api.

YouTube often blocks requests coming from cloud servers (VPS, data centres). If that
happens, set ``OBSIDIAN_YOUTUBE_PROXY`` to a proxy URL (``http://user:pass@host:port``)
and every request goes through it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)
from youtube_transcript_api.proxies import GenericProxyConfig

PROXY_ENV = "OBSIDIAN_YOUTUBE_PROXY"
# With a rotating proxy, each new connection gets a new IP: retry a block a few times.
PROXY_ATTEMPTS = 3
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}


T = TypeVar("T")


class YouTubeError(Exception):
    def __init__(self, message: str, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


def video_id(url_or_id: str) -> str:
    """Extract the 11-character video id from a YouTube URL, or accept a bare id."""
    value = url_or_id.strip()
    if _ID_RE.match(value):
        return value
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    if _ID_RE.match(candidate):
        return candidate
    raise YouTubeError(f"not a YouTube video URL or id: {url_or_id!r}")


def _proxy() -> str:
    return os.environ.get(PROXY_ENV, "").strip()


def _api() -> YouTubeTranscriptApi:
    proxy = _proxy()
    if proxy:
        return YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy))
    return YouTubeTranscriptApi()


def _timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class Transcript:
    video_id: str
    language: str
    language_code: str
    is_generated: bool
    translated_from: str | None
    text: str
    snippet_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "url": f"https://www.youtube.com/watch?v={self.video_id}",
            "language": self.language,
            "language_code": self.language_code,
            "auto_generated": self.is_generated,
            "translated_from": self.translated_from,
            "snippet_count": self.snippet_count,
            "text": self.text,
        }


def available(url_or_id: str) -> list[dict[str, Any]]:
    """Subtitle tracks a video offers."""
    vid = video_id(url_or_id)
    tracks = _with_retries(lambda: _api().list(vid))
    return [
        {
            "language": t.language,
            "language_code": t.language_code,
            "auto_generated": t.is_generated,
            "translatable": t.is_translatable,
        }
        for t in tracks
    ]


def fetch(
    url_or_id: str,
    languages: list[str] | None = None,
    translate_to: str | None = None,
    timestamps: bool = False,
) -> Transcript:
    """Fetch the subtitles in the first available language of `languages`.

    If none of them exists, fall back to any track (manual ones first). With
    `translate_to`, YouTube's machine translation is applied to that track.
    """
    vid = video_id(url_or_id)
    wanted = languages or ["fr", "en"]
    fetched, translated_from = _with_retries(lambda: _fetch_once(vid, wanted, translate_to))

    lines: list[str] = []
    for snip in fetched.snippets:
        text = " ".join(snip.text.split())
        if not text:
            continue
        lines.append(f"[{_timestamp(snip.start)}] {text}" if timestamps else text)
    body = "\n".join(lines) if timestamps else " ".join(lines)
    return Transcript(
        video_id=vid,
        language=fetched.language,
        language_code=fetched.language_code,
        is_generated=fetched.is_generated,
        translated_from=translated_from,
        text=body,
        snippet_count=len(lines),
    )


def _fetch_once(vid: str, wanted: list[str], translate_to: str | None) -> tuple[Any, str | None]:
    tracks = _api().list(vid)
    try:
        track = tracks.find_transcript(wanted)
    except NoTranscriptFound:
        everything = sorted(tracks, key=lambda t: t.is_generated)
        if not everything:
            raise
        track = everything[0]
    source_code = track.language_code
    translated_from = None
    if translate_to and translate_to != source_code:
        track = track.translate(translate_to)
        translated_from = source_code
    return track.fetch(), translated_from


def _with_retries(call: Callable[[], T]) -> T:
    """Run `call`; behind a proxy, retry a YouTube block (a rotating proxy changes IP)."""
    attempts = PROXY_ATTEMPTS if _proxy() else 1
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except (IpBlocked, RequestBlocked) as exc:
            if attempt == attempts:
                raise YouTubeError(_explain(exc, attempts), blocked=True) from exc
        except YouTubeTranscriptApiException as exc:
            raise YouTubeError(_explain(exc)) from exc
    raise AssertionError("unreachable")


def _explain(exc: Exception, attempts: int = 1) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if isinstance(exc, (IpBlocked, RequestBlocked)):
        name = exc.__class__.__name__
        if _proxy():
            return (
                f"YouTube blocked the request ({name}) even through the proxy set in {PROXY_ENV} "
                f"({attempts} attempts). Check that the proxy works and rotates its IP, "
                "or try again later."
            )
        return (
            f"YouTube blocked the request from this server ({name}); no proxy is configured. "
            f"Cloud IPs are often blocked; set {PROXY_ENV} to route requests through a proxy."
        )
    if isinstance(exc, TranscriptsDisabled):
        return "Subtitles are disabled for this video."
    return f"{exc.__class__.__name__}: {first}"
