"""Search YouTube with the official YouTube Data API v3.

Needs a free API key in ``OBSIDIAN_YOUTUBE_API_KEY`` (Google Cloud console, "YouTube
Data API v3"). The key is sent in a header, never in the URL, so it cannot leak into
an error message or a log line. The free quota is 10,000 units a day; one search costs
about 100 units (search) + 1 (video details), so roughly 100 searches a day.
"""

from __future__ import annotations

import html
import os
import re
from typing import Any

import requests

API_KEY_ENV = "OBSIDIAN_YOUTUBE_API_KEY"
API = "https://www.googleapis.com/youtube/v3"
ORDERS = {"relevance", "date", "viewCount", "rating"}
DURATIONS = {"short", "medium", "long"}  # <4 min, 4-20 min, >20 min
MAX_RESULTS = 25
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_ISO_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


class SearchError(Exception):
    pass


def _key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise SearchError(
            f"YouTube search is not configured: set {API_KEY_ENV} to a YouTube Data API v3 key."
        )
    return key


def _get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"{API}/{endpoint}",
            params={k: v for k, v in params.items() if v is not None},
            headers={"X-goog-api-key": _key()},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SearchError(f"could not reach the YouTube API ({exc.__class__.__name__})") from None
    if resp.status_code != 200:
        reason = ""
        try:
            err = resp.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("message", "")
        except ValueError:
            pass
        if reason in {"quotaExceeded", "dailyLimitExceeded"}:
            raise SearchError("YouTube API daily quota exceeded; it resets at midnight Pacific time.")
        raise SearchError(f"YouTube API error {resp.status_code}: {reason or 'unknown'}")
    return resp.json()


def _duration(iso: str) -> str:
    m = _ISO_DURATION_RE.match(iso or "")
    if not m:
        return ""
    d, h, mi, s = (int(x or 0) for x in m.groups())
    h += 24 * d
    return f"{h}:{mi:02d}:{s:02d}" if h else f"{mi}:{s:02d}"


def _channel_id(channel: str) -> str:
    value = channel.strip()
    if _CHANNEL_ID_RE.match(value):
        return value
    handle = value.rstrip("/").split("/")[-1]
    handle = handle if handle.startswith("@") else f"@{handle}"
    items = _get("channels", {"part": "id", "forHandle": handle}).get("items") or []
    if not items:
        raise SearchError(f"no YouTube channel found for {channel!r}")
    return items[0]["id"]


def search(
    query: str = "",
    max_results: int = 8,
    language: str | None = None,
    order: str = "relevance",
    published_after: str | None = None,
    duration: str | None = None,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Search videos and return them with their details (duration, views, captions)."""
    if not query.strip() and not channel:
        raise SearchError("give a query, a channel, or both")
    if order not in ORDERS:
        raise SearchError(f"order must be one of {sorted(ORDERS)}")
    if duration is not None and duration not in DURATIONS:
        raise SearchError(f"duration must be one of {sorted(DURATIONS)}")
    if published_after is not None and not _DATE_RE.match(published_after):
        raise SearchError("published_after must be a date like 2025-01-31")

    params: dict[str, Any] = {
        "part": "snippet",
        "type": "video",
        "q": query.strip() or None,
        "maxResults": max(1, min(int(max_results), MAX_RESULTS)),
        "order": order,
        "relevanceLanguage": language,
        "videoDuration": duration,
        "publishedAfter": f"{published_after}T00:00:00Z" if published_after else None,
        "channelId": _channel_id(channel) if channel else None,
    }
    found = _get("search", params).get("items") or []
    ids = [it["id"]["videoId"] for it in found if it.get("id", {}).get("videoId")]
    if not ids:
        return []

    details = {
        v["id"]: v
        for v in _get(
            "videos", {"part": "contentDetails,statistics", "id": ",".join(ids)}
        ).get("items")
        or []
    }
    out: list[dict[str, Any]] = []
    for it in found:
        vid = it.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet", {})
        det = details.get(vid, {})
        stats = det.get("statistics", {})
        out.append(
            {
                "video_id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": html.unescape(sn.get("title", "")),
                "channel": html.unescape(sn.get("channelTitle", "")),
                "channel_id": sn.get("channelId", ""),
                "published": (sn.get("publishedAt") or "")[:10],
                "duration": _duration(det.get("contentDetails", {}).get("duration", "")),
                "views": int(stats["viewCount"]) if "viewCount" in stats else None,
                "likes": int(stats["likeCount"]) if "likeCount" in stats else None,
                "manual_captions": det.get("contentDetails", {}).get("caption") == "true",
                "description": html.unescape(sn.get("description", "")),
            }
        )
    return out
