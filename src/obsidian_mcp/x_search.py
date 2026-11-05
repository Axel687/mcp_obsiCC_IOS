"""Search posts on X by keywords, with twscrape and a dedicated X account.

X search needs a logged-in session. This module uses the browser cookies of one
account set aside for it (never the user's main account): ``auth_token`` and ``ct0``
in ``OBSIDIAN_X_SEARCH_COOKIES``, the account's username in
``OBSIDIAN_X_SEARCH_ACCOUNT``. twscrape keeps its state (rate-limit locks, errors) in
a small SQLite file next to the OAuth state.

This is scraping X's private web API: it is against X's terms of service, and X may
lock or suspend the account. Nothing here tries to hide the automation (no proxy, no
fingerprint tricks): the account is used as is, so that its fate shows whether X
tolerates this use. The cookies are never returned, logged or written to the vault.
"""

from __future__ import annotations

import json
import math
import os
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COOKIES_ENV = "OBSIDIAN_X_SEARCH_COOKIES"
ACCOUNT_ENV = "OBSIDIAN_X_SEARCH_ACCOUNT"
FALLBACK_ACCOUNT_ENV = "OBSIDIAN_X_INBOX_ACCOUNT"
SORTS = {"latest": "Latest", "top": "Top"}
MAX_COUNT = 50
PAGE_SIZE = 20  # posts per request: X's search pages hold at most 20
WAIT_SECONDS = 45.0
QUOTA_FILE = "x_search_quota.json"


class XSearchError(Exception):
    pass


def _settings() -> tuple[str, str, Path]:
    cookies = os.environ.get(COOKIES_ENV, "").strip()
    account = (
        os.environ.get(ACCOUNT_ENV, "").strip() or os.environ.get(FALLBACK_ACCOUNT_ENV, "").strip()
    ).lstrip("@")
    if not cookies or not account:
        raise XSearchError(
            f"X search is not configured: set {COOKIES_ENV} to the account's browser cookies "
            f'("auth_token=...; ct0=...") and {ACCOUNT_ENV} to its username.'
        )
    state = Path(
        os.environ.get("OBSIDIAN_MCP_STATE_DIR", "").strip() or "~/.local/state/obsidian-mcp"
    ).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    return cookies, account, state / "twscrape.db"


async def _pool(cookies: str, account: str, db: Path) -> Any:
    from twscrape import AccountsPool, set_log_level
    from twscrape.utils import parse_cookies

    set_log_level("WARNING")
    # twscrape locks the account while a search runs (up to 15 min if a call dies), so a
    # second call made at the same time waits for the first instead of failing at once.
    pool = AccountsPool(
        str(db), raise_when_no_account=True, wait_timeout=WAIT_SECONDS, wait_interval=1.0
    )
    try:
        wanted = parse_cookies(cookies)
    except Exception:
        raise XSearchError(f"{COOKIES_ENV} could not be parsed") from None
    if not wanted.get("auth_token") or not wanted.get("ct0"):
        raise XSearchError(f"{COOKIES_ENV} must contain auth_token and ct0")

    acc = await pool.get_account(account)
    stored = acc.cookies if acc else {}
    if acc is None or any(stored.get(k) != wanted[k] for k in ("auth_token", "ct0")):
        # New account or new cookies (the user refreshed them): start clean.
        await pool.add_account_cookies(account, cookies)
        await pool.reset_locks()
    elif not acc.active:
        raise XSearchError(
            f"X refused the session of @{account}: {acc.error_msg or 'unknown reason'}. "
            "The account may be locked or suspended, or its cookies expired: check it in a "
            f"browser, then put fresh cookies in {COOKIES_ENV} and restart."
        )
    return pool


def _user(u: Any) -> dict[str, Any]:
    return {
        "username": u.username,
        "name": u.displayname,
        "followers": u.followersCount,
        "verified": bool(u.blue or u.verified),
    }


def _post(t: Any, nested: bool = True) -> dict[str, Any]:
    kind = "post"
    if t.inReplyToTweetId:
        kind = "reply"
    elif t.quotedTweet is not None:
        kind = "quote"
    out: dict[str, Any] = {
        "url": t.url,
        "id": t.id_str,
        "kind": kind,
        "date": t.date.isoformat() if t.date else None,
        "author": _user(t.user),
        "text": t.rawContent,
        "lang": t.lang,
        "likes": t.likeCount,
        "reposts": t.retweetCount,
        "replies": t.replyCount,
        "quotes": t.quoteCount,
        "views": t.viewCount,
    }
    if t.inReplyToScreenName:
        out["replying_to"] = t.inReplyToScreenName
    links = [link.url for link in (t.links or []) if link.url]
    if links:
        out["links"] = links
    media = t.media
    if media is not None:
        urls = [p.url for p in media.photos] + [v.thumbnailUrl for v in media.videos]
        urls += [a.thumbnailUrl for a in media.animated]
        if urls:
            out["media"] = urls
    if nested and t.quotedTweet is not None:
        out["quoted"] = _post(t.quotedTweet, nested=False)
    return out


def _quota(headers: Any, account: str) -> dict[str, Any] | None:
    """X's rate-limit headers for the search endpoint, as sent with each answer."""
    try:
        limit = int(headers.get("x-rate-limit-limit", -1))
        remaining = int(headers.get("x-rate-limit-remaining", -1))
        reset = int(headers.get("x-rate-limit-reset", -1))
    except (TypeError, ValueError):
        return None
    if remaining < 0 or reset <= 0:
        return None
    return {
        "account": account,
        "limit": limit if limit >= 0 else None,
        "remaining": remaining,
        "resets_at": datetime.fromtimestamp(reset, tz=timezone.utc).isoformat(timespec="seconds"),
        "seen_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }


def _save_quota(db: Path, quota: dict[str, Any]) -> None:
    try:
        (db.parent / QUOTA_FILE).write_text(json.dumps(quota))
    except OSError:
        pass  # the quota is informative: never fail a search for it


def last_quota(db: Path) -> dict[str, Any] | None:
    """Quota as X reported it at the last search (None if unknown or already reset)."""
    try:
        quota = json.loads((db.parent / QUOTA_FILE).read_text())
        reset = datetime.fromisoformat(quota["resets_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if reset <= datetime.now(tz=timezone.utc):
        quota = {**quota, "remaining": quota.get("limit"), "note": "window reset since the last search"}
    return quota


def requests_needed(count: int) -> int:
    return math.ceil(max(1, min(int(count), MAX_COUNT)) / PAGE_SIZE)


async def search(query: str, count: int = 20, sort: str = "latest") -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise XSearchError("query is empty")
    product = SORTS.get((sort or "latest").lower())
    if product is None:
        raise XSearchError(f"sort must be one of {sorted(SORTS)}")
    count = max(1, min(int(count), MAX_COUNT))

    cookies, account, db = _settings()
    pool = await _pool(cookies, account, db)

    from twscrape import API
    from twscrape.accounts_pool import NoAccountError
    from twscrape.models import parse_tweets

    api = API(pool, raise_when_no_account=True)
    posts: list[dict[str, Any]] = []
    pages = 0
    quota: dict[str, Any] | None = None
    try:
        # search_raw yields X's raw answers: the posts plus the rate-limit headers.
        async with aclosing(api.search_raw(query, limit=count, kv={"product": product})) as gen:
            async for rep in gen:
                pages += 1
                quota = _quota(rep.headers, account) or quota
                for tweet in parse_tweets(rep.json(), count - len(posts)):
                    posts.append(_post(tweet))
                if len(posts) >= count:
                    break
    except NoAccountError:
        acc = await pool.get_account(account)
        if acc is not None and not acc.active:
            raise XSearchError(
                f"X refused the session of @{account}: {acc.error_msg or 'unknown reason'}. "
                "The account may be locked or suspended, or its cookies expired."
            ) from None
        nat = await pool.next_available_at("SearchTimeline")
        known = last_quota(db)
        hint = ""
        if known and known.get("remaining") == 0:
            hint = f" X's quota is used up until {known['resets_at']} (UTC)."
        raise XSearchError(
            f"X search is busy or paused for @{account} (another search still running, "
            f"rate limit or connection error); retry in a minute, at the latest after "
            f"{nat or 'a few minutes'} (UTC).{hint}"
        ) from None
    except XSearchError:
        raise
    except Exception as exc:  # twscrape breaks when X changes its private API
        raise XSearchError(f"X search failed ({exc.__class__.__name__}: {exc})") from None
    finally:
        if quota:
            _save_quota(db, quota)

    out: dict[str, Any] = {
        "query": query, "sort": sort.lower(), "account": account,
        "count": len(posts), "posts": posts, "requests": pages,
    }
    if quota:
        out["quota"] = {k: quota[k] for k in ("limit", "remaining", "resets_at")}
        out["quota"]["searches_left"] = quota["remaining"] // requests_needed(count)
    return out
