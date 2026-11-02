"""Read one public post on X (Twitter) from its link, without an X account or API key.

Two sources, tried in order:

1. FxTwitter (``api.fxtwitter.com``), the open-source service behind the fixed X
   previews on Discord. It returns the full text, long posts included, plus author,
   date, counters, media links and the quoted post. It is a volunteer-run third party:
   it can be down or cut off by X.
2. X's official oEmbed endpoint (``publish.x.com/oembed``), used to embed posts on web
   pages. Always available but only gives the text, and long posts are cut after
   about 280 characters: the result then says ``truncated: true``.

Only the post id travels to these services, never any user account or credential.
Protected and deleted posts cannot be read.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

FXTWITTER = "https://api.fxtwitter.com"
OEMBED = "https://publish.x.com/oembed"
USER_AGENT = "obsidian-mcp (+https://github.com/Axel687/mcp_obsiCC_IOS)"
TIMEOUT = 15

_HOSTS = {
    "x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com",
    "fxtwitter.com", "fixupx.com", "vxtwitter.com", "fixvx.com",
}
_ID_RE = re.compile(r"^\d{1,25}$")
_PATH_RE = re.compile(r"^/(?:(?P<user>[A-Za-z0-9_]{1,15})|i(?:/web)?)/status(?:es)?/(?P<id>\d{1,25})")
_P_RE = re.compile(r"<p[^>]*>(?P<text>.*?)</p>", re.S)
_A_RE = re.compile(r'<a href="(?P<href>[^"]*)"[^>]*>.*?</a>', re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class XPostError(Exception):
    pass


def parse_url(url: str) -> tuple[str, str | None]:
    """Return (post id, username or None) from a post URL or a bare id."""
    value = (url or "").strip()
    if _ID_RE.match(value):
        return value, None
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in _HOSTS:
        raise XPostError(f"not an X post link: {url!r}")
    m = _PATH_RE.match(parsed.path)
    if not m:
        raise XPostError(f"not an X post link (expected .../status/<id>): {url!r}")
    return m.group("id"), m.group("user")


def _get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    return requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)


def _date(tweet: dict[str, Any]) -> str:
    ts = tweet.get("created_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(tweet.get("created_at") or "")


def _media(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in (tweet.get("media") or {}).get("all") or []:
        item = {"type": m.get("type", ""), "url": m.get("url", "")}
        if m.get("thumbnail_url"):
            item["thumbnail_url"] = m["thumbnail_url"]
        if m.get("altText"):
            item["alt_text"] = m["altText"]
        out.append(item)
    return out


def _from_fx(tweet: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    author = tweet.get("author") or {}
    post: dict[str, Any] = {
        "id": str(tweet.get("id", "")),
        "url": tweet.get("url", ""),
        "author": author.get("name", ""),
        "username": author.get("screen_name", ""),
        "date": _date(tweet),
        "lang": tweet.get("lang"),
        "text": tweet.get("text", ""),
        "truncated": False,
        "likes": tweet.get("likes"),
        "reposts": tweet.get("retweets"),
        "replies": tweet.get("replies"),
        "views": tweet.get("views"),
        "media": _media(tweet),
    }
    if tweet.get("replying_to"):
        post["replying_to"] = tweet["replying_to"]
        if tweet.get("replying_to_status"):
            post["replying_to_status"] = str(tweet["replying_to_status"])
    poll = tweet.get("poll")
    if poll:
        post["poll"] = [
            {"label": c.get("label", ""), "percentage": c.get("percentage")}
            for c in poll.get("choices") or []
        ]
    article = tweet.get("article")
    if article:
        post["article"] = {
            "title": article.get("title", ""),
            "preview_text": article.get("preview_text", ""),
        }
    note = tweet.get("community_note")
    if note:
        post["community_note"] = note.get("text", "") if isinstance(note, dict) else str(note)
    if tweet.get("quote") and depth == 0:
        post["quote"] = _from_fx(tweet["quote"], depth + 1)
    return post


def _fxtwitter(post_id: str, user: str | None) -> dict[str, Any]:
    resp = _get(f"{FXTWITTER}/{user or 'i'}/status/{post_id}")
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200 and data.get("tweet"):
        return _from_fx(data["tweet"])
    if resp.status_code in {401, 403, 404}:
        raise XPostError(
            f"post {post_id} not found: deleted, protected or suspended "
            f"({data.get('message') or resp.status_code})"
        )
    raise XPostError(f"FxTwitter error {resp.status_code}: {data.get('message') or 'unknown'}")


def _oembed_text(markup: str) -> str:
    m = _P_RE.search(markup)
    if not m:
        return ""
    inner = _BR_RE.sub("\n", m.group("text"))
    inner = _A_RE.sub(lambda a: a.group("href"), inner)
    return html.unescape(_TAG_RE.sub("", inner)).strip()


def _oembed(post_id: str, user: str | None) -> dict[str, Any]:
    url = f"https://x.com/{user or 'i'}/status/{post_id}"
    resp = _get(OEMBED, {"url": url, "omit_script": "true", "dnt": "true"})
    if resp.status_code == 404:
        raise XPostError(f"post {post_id} not found: deleted, protected or suspended")
    if resp.status_code != 200:
        raise XPostError(f"oEmbed error {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise XPostError("oEmbed returned something that is not JSON") from None
    text = _oembed_text(data.get("html", ""))
    # A cut post ends with an ellipsis, often followed by the t.co link to the full post.
    truncated = bool(re.search(r"…(\s+https://t\.co/\S+)?$", text))
    author_url = data.get("author_url", "")
    return {
        "id": post_id,
        "url": data.get("url") or url,
        "author": data.get("author_name", ""),
        "username": author_url.rstrip("/").rsplit("/", 1)[-1] if author_url else (user or ""),
        "text": text,
        "truncated": truncated,
    }


def fetch(url: str) -> dict[str, Any]:
    """Read a post: FxTwitter first, oEmbed as a fallback."""
    post_id, user = parse_url(url)
    try:
        return {"source": "fxtwitter", **_fxtwitter(post_id, user)}
    except XPostError as exc:  # also on "not found": oEmbed gives X's own answer
        first_error = str(exc)
    except requests.RequestException as exc:
        first_error = f"could not reach FxTwitter ({exc.__class__.__name__})"
    try:
        post = _oembed(post_id, user)
    except requests.RequestException as exc:
        raise XPostError(
            f"{first_error}; could not reach X oEmbed either ({exc.__class__.__name__})"
        ) from None
    except XPostError as exc:
        raise XPostError(f"{first_error}; {exc}") from None
    out = {"source": "oembed", **post, "fallback_reason": first_error}
    if post["truncated"]:
        out["note"] = "Only the beginning of this long post is available right now (FxTwitter is unreachable)."
    return out
