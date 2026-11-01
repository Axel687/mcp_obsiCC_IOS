"""Fetch the subtitles of a YouTube video, with youtube-transcript-api.

YouTube often blocks requests coming from cloud servers (VPS, data centres). If that
happens, set ``OBSIDIAN_YOUTUBE_PROXY`` to a proxy URL (``http://user:pass@host:port``)
and every request goes through it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
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
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}


class YouTubeError(Exception):
    pass


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


def _api() -> YouTubeTranscriptApi:
    proxy = os.environ.get(PROXY_ENV, "").strip()
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
    try:
        tracks = _api().list(vid)
    except YouTubeTranscriptApiException as exc:
        raise YouTubeError(_explain(exc)) from exc
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
    try:
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
        fetched = track.fetch()
    except YouTubeTranscriptApiException as exc:
        raise YouTubeError(_explain(exc)) from exc

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


def _explain(exc: Exception) -> str:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if isinstance(exc, (IpBlocked, RequestBlocked)):
        return (
            f"YouTube blocked the request from this server ({exc.__class__.__name__}). "
            f"Cloud IPs are often blocked; set {PROXY_ENV} to route requests through a proxy."
        )
    if isinstance(exc, TranscriptsDisabled):
        return "Subtitles are disabled for this video."
    return f"{exc.__class__.__name__}: {first}"
