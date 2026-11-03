"""Read one public post on X (Twitter) from its link, without an X account or API key.

Two sources, tried in order:

1. FxTwitter (``api.fxtwitter.com``), the open-source service behind the fixed X
   previews on Discord. It returns the full text, long posts included, plus author,
   date, counters, media links and the quoted post. X articles come back in full,
   converted to Markdown (headings, lists, links, images, code blocks). It is a volunteer-run third party:
   it can be down or cut off by X.
2. X's official oEmbed endpoint (``publish.x.com/oembed``), used to embed posts on web
   pages. Always available but only gives the text, and long posts are cut after
   about 280 characters: the result then says ``truncated: true``.

Only the post id travels to these services, never any user account or credential.
Protected and deleted posts cannot be read.
"""

from __future__ import annotations

import html
import os
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


_BLOCK_PREFIX = {
    "header-one": "# ", "header-two": "## ", "header-three": "### ",
    "unordered-list-item": "- ", "ordered-list-item": "1. ", "blockquote": "> ",
}


def _entity_map(raw: Any) -> dict[str, dict[str, Any]]:
    """Draft.js entityMap, either {key: entity} or [{"key": k, "value": entity}]."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    out: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        if isinstance(item, dict) and isinstance(item.get("value"), dict):
            out[str(item.get("key"))] = item["value"]
    return out


def _media_urls(article: dict[str, Any]) -> dict[str, str]:
    """media id -> image URL, from whatever media lists the article carries."""
    urls: dict[str, str] = {}
    candidates = list(article.get("media_entities") or [])
    if isinstance(article.get("cover_media"), dict):
        candidates.append(article["cover_media"])
    for m in candidates:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("media_id") or m.get("id") or "")
        info = m.get("media_info") or {}
        url = info.get("original_img_url") or m.get("url") or m.get("media_url_https")
        if mid and url:
            urls[mid] = url
    return urls


def _split(text: str, offset: int, length: int) -> tuple[str, str, str]:
    """Split text around an entity range.

    X counts article offsets in code points (Python string indices), not UTF-16 units:
    with UTF-16, a link after an emoji came out one character too early.
    """
    return text[:offset], text[offset:offset + length], text[offset + length:]


def _with_links(text: str, ranges: list[dict[str, Any]], entities: dict[str, dict[str, Any]]) -> str:
    links = []
    for r in ranges or []:
        ent = entities.get(str(r.get("key")), {})
        url = (ent.get("data") or {}).get("url")
        if ent.get("type") == "LINK" and url:
            links.append((int(r.get("offset", 0)), int(r.get("length", 0)), url))
    for offset, length, url in sorted(links, reverse=True):  # right to left keeps offsets valid
        before, label, after = _split(text, offset, length)
        text = f"{before}[{label}]({url}){after}"
    return text


def _media_line(block: dict[str, Any], entities: dict[str, dict[str, Any]], urls: dict[str, str]) -> str:
    lines = []
    for r in block.get("entityRanges") or []:
        ent = entities.get(str(r.get("key")), {})
        data = ent.get("data") or {}
        if ent.get("type") == "MEDIA":
            caption = data.get("caption") or ""
            for item in data.get("mediaItems") or [{}]:
                url = urls.get(str(item.get("mediaId", "")), "")
                lines.append(f"![{caption}]({url})" if url else f"[image{': ' + caption if caption else ''}]")
        elif ent.get("type") == "MARKDOWN" and data.get("markdown"):
            lines.append(data["markdown"].strip("\n"))
        elif ent.get("type") in {"TWEET", "POST"} and data.get("tweetId"):
            lines.append(f"[post cité : https://x.com/i/status/{data['tweetId']}]")
        elif data.get("url"):
            lines.append(f"[{ent.get('type', 'lien').lower()} : {data['url']}]")
    return "\n".join(lines)


def _article(article: dict[str, Any]) -> dict[str, Any]:
    """Turn an X article (Draft.js blocks) into Markdown text."""
    out: dict[str, Any] = {
        "title": article.get("title", ""),
        "preview_text": article.get("preview_text", ""),
    }
    cover = (article.get("cover_media") or {}).get("media_info", {}).get("original_img_url")
    if cover:
        out["cover_image"] = cover
    content = article.get("content") or {}
    blocks = content.get("blocks") or []
    if not blocks:
        return out
    entities = _entity_map(content.get("entityMap"))
    urls = _media_urls(article)
    parts: list[str] = []
    images = 0
    for block in blocks:
        kind = block.get("type", "unstyled")
        if kind == "atomic":
            line = _media_line(block, entities, urls)
            images += line.count("![") + line.count("[image")
        else:
            text = _with_links(block.get("text", ""), block.get("entityRanges") or [], entities)
            if kind == "code-block":
                line = f"```\n{text}\n```"
            else:
                line = _BLOCK_PREFIX.get(kind, "") + text
        if line.strip():
            parts.append(line)
    out["text"] = "\n\n".join(parts)
    out["blocks"] = len(blocks)
    out["images"] = images
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
        "reposts": tweet.get("retweets", tweet.get("reposts")),  # v1 / v2 names
        "replies": tweet.get("replies"),
        "views": tweet.get("views"),
        "media": _media(tweet),
    }
    for key in ("quotes", "bookmarks"):  # API v2 only
        if tweet.get(key) is not None:
            post[key] = tweet[key]
    replying = tweet.get("replying_to")
    if isinstance(replying, dict):  # API v2: {"screen_name", "status"}
        post["replying_to"] = replying.get("screen_name", "")
        if replying.get("status"):
            post["replying_to_status"] = str(replying["status"])
    elif replying:
        post["replying_to"] = replying
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
        post["article"] = _article(article)
    note = tweet.get("community_note")
    if note:
        post["community_note"] = note.get("text", "") if isinstance(note, dict) else str(note)
    if tweet.get("quote") and depth == 0 and tweet["quote"].get("type") != "tombstone":
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


# -- conversations (FxTwitter API v2) -------------------------------------------------

SORTS = {"likes", "recency"}
MAX_PAGE = 100


def _v2(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        resp = _get(f"{FXTWITTER}/2/{path}", params)
    except requests.RequestException as exc:
        raise XPostError(f"could not reach FxTwitter ({exc.__class__.__name__})") from None
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200:
        return data
    if resp.status_code == 204:  # e.g. nothing newer than `since`
        return {}
    if resp.status_code in {401, 403, 404}:
        raise XPostError(f"not found: deleted, protected or suspended ({data.get('message') or resp.status_code})")
    raise XPostError(f"FxTwitter error {resp.status_code}: {data.get('message') or 'unknown'}")


def _compact(tweet: dict[str, Any]) -> dict[str, Any]:
    """A reply or a quoting post: the post fields without the bulky article."""
    if tweet.get("type") == "tombstone":
        return {"id": str(tweet.get("id", "")), "unavailable": tweet.get("message") or "unavailable"}
    post = _from_fx(tweet, depth=1)
    post.pop("article", None)
    return post


def _user(user: dict[str, Any]) -> dict[str, Any]:
    out = {
        "name": user.get("name", ""),
        "username": user.get("screen_name", ""),
        "followers": user.get("followers"),
        "description": user.get("description", ""),
    }
    if (user.get("verification") or {}).get("verified"):
        out["verified"] = True
    return out


def _cursor(data: dict[str, Any]) -> str | None:
    return (data.get("cursor") or {}).get("bottom") or None


def _count(n: int) -> int:
    return max(1, min(int(n), MAX_PAGE))


def conversation(url: str, sort: str = "likes", cursor: str | None = None) -> dict[str, Any]:
    """A post, the posts above it in its thread, and one page of replies."""
    post_id, _ = parse_url(url)
    if sort not in SORTS:
        raise XPostError(f"sort must be one of {sorted(SORTS)}")
    params: dict[str, Any] = {"ranking_mode": sort}
    if cursor:
        params["cursor"] = cursor
    data = _v2(f"conversation/{post_id}", params)
    out: dict[str, Any] = {"id": post_id}
    status = data.get("status")
    # FxTwitter repeats the post on every page: only page 1 gets it, and an article is
    # reduced to its title (read_x_post gives the full text).
    if status and not cursor:
        if status.get("type") == "tombstone":
            out["post"] = _compact(status)
        else:
            out["post"] = _from_fx(status)
            article = out["post"].get("article")
            if article:
                out["post"]["article"] = {
                    "title": article.get("title", ""),
                    "note": "full text with read_x_post",
                }
        thread = [_compact(t) for t in data.get("thread") or [] if str(t.get("id")) != post_id]
        if thread:
            out["thread"] = thread
    out["replies"] = [_compact(t) for t in data.get("replies") or []]
    # X hands out a fresh cursor even when there is nothing left: an empty page ends it,
    # so a caller following next_cursor cannot loop forever.
    out["next_cursor"] = _cursor(data) if out["replies"] else None
    return out


def reposters(url: str, count: int = 40, cursor: str | None = None) -> dict[str, Any]:
    """Accounts that reposted a post (newest first, as X lists them)."""
    post_id, _ = parse_url(url)
    params: dict[str, Any] = {"count": _count(count)}
    if cursor:
        params["cursor"] = cursor
    data = _v2(f"status/{post_id}/reposts", params)
    users = [_user(u) for u in data.get("results") or []]
    out: dict[str, Any] = {"id": post_id, "users": users[: params["count"]]}
    # X ignores the requested page size (5 asked, 33 sent in the real test): cut here.
    # The cursor still points after X's whole page, so say how many were left out.
    if len(users) > params["count"]:
        out["not_shown"] = len(users) - params["count"]
    out["next_cursor"] = _cursor(data)
    return out


# -- inbox: the posts of a dedicated account (FxTwitter API v2) --------------------------

INBOX_ENV = "OBSIDIAN_X_INBOX_ACCOUNT"
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$|^(id:\d{1,25})$", re.I)
_REPLIES_CURSOR = "r:"  # marks a cursor from the with_replies timeline
_SINCE_RE = re.compile(r"^(\d+)\s*([hd])$")


def _handle(account: str | None) -> str:
    value = (account or os.environ.get(INBOX_ENV, "")).strip()
    if not value:
        raise XPostError(f"no inbox account: pass `account` or set {INBOX_ENV} (X username, without @)")
    m = _HANDLE_RE.match(value.rstrip("/").rsplit("/", 1)[-1])
    if not m:
        raise XPostError(f"not an X username: {value!r}")
    return m.group(1) or m.group(2).lower()


def _since(value: str) -> int:
    """'2026-11-02', '2026-11-02T08:00', '24h' or '7d' -> Unix seconds (UTC)."""
    v = value.strip()
    m = _SINCE_RE.match(v)
    if m:
        hours = int(m.group(1)) * (24 if m.group(2) == "d" else 1)
        return int(datetime.now(tz=timezone.utc).timestamp()) - hours * 3600
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise XPostError("since must be a date (2026-11-02), a date-time or a duration like 24h / 7d") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _timeline(handle: str, params: dict[str, Any], replies: bool) -> dict[str, Any]:
    if replies:
        params = {**params, "with_replies": "1"}
    return _v2(f"profile/{handle}/statuses", params)


def _mine(status: dict[str, Any], handle: str) -> bool:
    """With replies, X also sends other people's replies in the threads: keep the account's own."""
    if handle.startswith("id:"):
        ids = {str((status.get("author") or {}).get("id", "")), str((status.get("reposted_by") or {}).get("id", ""))}
        return handle[3:] in ids
    names = {(status.get("author") or {}).get("screen_name", ""), (status.get("reposted_by") or {}).get("screen_name", "")}
    return handle.lower() in {n.lower() for n in names if n}


def inbox(
    account: str | None = None,
    since: str | None = None,
    count: int = 20,
    cursor: str | None = None,
    only_reposts: bool = False,
) -> dict[str, Any]:
    """Latest posts of the inbox account, newest first: what the user reposted for review.

    FxTwitter's plain profile timeline answers 404 for some accounts whose profile it
    cannot look up (seen on 2026-11-03 for @jack, while @NASA works); its with_replies
    timeline still works for them, so it is the fallback. Its cursors are marked "r:".
    """
    handle = _handle(account)
    params: dict[str, Any] = {"count": _count(count)}
    replies = bool(cursor and cursor.startswith(_REPLIES_CURSOR))
    if cursor:
        params["cursor"] = cursor.removeprefix(_REPLIES_CURSOR)
    elif since:
        params["since"] = _since(since)
    try:
        data = _timeline(handle, params, replies)
    except XPostError as exc:
        if replies or "not found" not in str(exc):
            raise
        replies = True
        try:
            data = _timeline(handle, params, replies)
        except XPostError as exc2:
            if "not found" in str(exc2):
                raise XPostError(
                    f"no posts found for @{handle}: the account does not exist, is protected "
                    "(it must be public), or has not posted or reposted anything yet"
                ) from None
            raise
    items: list[dict[str, Any]] = []
    for status in data.get("results") or []:
        if status.get("type") not in (None, "status"):
            continue
        if replies and not _mine(status, handle):
            continue
        reposted = bool(status.get("reposted_by"))
        if only_reposts and not reposted:
            continue
        post = _from_fx(status)
        article = post.get("article")
        if article:  # keep the inbox light; read_x_post has the full article
            post["article"] = {"title": article.get("title", ""), "note": "full text with read_x_post"}
        if reposted:
            kind = "repost"
        elif post.get("quote"):
            kind = "quote"
        elif post.get("replying_to"):
            kind = "reply"
        else:
            kind = "post"
        items.append({"kind": kind, **post})
    posts = items[: params["count"]]
    out: dict[str, Any] = {"account": handle, "count": len(posts), "posts": posts}
    if len(items) > params["count"]:
        out["not_shown"] = len(items) - params["count"]
    # Same guard as conversation(): an empty page ends the pagination.
    nxt = _cursor(data) if data.get("results") else None
    out["next_cursor"] = (_REPLIES_CURSOR + nxt if replies else nxt) if nxt else None
    return out
