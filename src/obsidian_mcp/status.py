"""State of the ecosystem: is each building block working, and when was each tool used.

Two parts:

- **Usage counter.** Every tool call is recorded as (tool name, time, success). Nothing
  else: no argument, no result, no error message, so nothing of what was read or
  searched is kept. The counter lives in ``usage.json`` in the state directory and
  survives restarts.
- **Checks.** Each external dependency (vault Git remote, YouTube proxy, YouTube API
  key, FxTwitter, X search account) is tested with the cheapest request possible. A
  check never returns a secret: a proxy shows its host only, cookies and keys only
  whether they are set.

The Revolut server is a separate service on purpose: this module knows nothing about
it.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from . import sync, x_post, x_search, youtube, youtube_search

USAGE_FILE = "usage.json"
CHECK_TIMEOUT = 15

# Which building blocks each tool needs. Tools absent from this map only need the
# server itself.
DEPENDS_ON: dict[str, list[str]] = {
    "youtube_transcript": ["youtube_proxy"],
    "youtube_search_videos": ["youtube_api_key"],
    "read_x_post": ["fxtwitter"],
    "read_x_conversation": ["fxtwitter"],
    "read_x_reposts": ["fxtwitter"],
    "read_x_inbox": ["fxtwitter", "x_inbox_account"],
    "search_x": ["x_search_account"],
}
_VAULT_TOOLS = {
    "vault_overview", "list_notes", "read_note", "search_notes", "list_tags",
    "notes_with_tag", "get_backlinks", "get_links", "find_orphans", "read_pdf",
    "append_note", "insert_under_heading", "write_note", "delete_note", "sync_vault",
    "generate_graph",
}
for _name in _VAULT_TOOLS:
    DEPENDS_ON[_name] = ["vault_git"]

_started = datetime.now(timezone.utc)
_lock = threading.Lock()
_usage: dict[str, dict[str, Any]] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_dir() -> Path:
    return Path(
        os.environ.get("OBSIDIAN_MCP_STATE_DIR", "").strip() or "~/.local/state/obsidian-mcp"
    ).expanduser()


def _usage_path() -> Path:
    return _state_dir() / USAGE_FILE


def _load() -> dict[str, dict[str, Any]]:
    global _usage
    if _usage is None:
        try:
            data = json.loads(_usage_path().read_text(encoding="utf-8"))
            _usage = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _usage = {}
    return _usage


def record(tool: str, ok: bool) -> None:
    """Count one call of `tool`. Only the name, the time and the outcome are stored."""
    with _lock:
        usage = _load()
        entry = usage.setdefault(tool, {"calls": 0, "errors": 0, "first_call": _now()})
        entry["calls"] = int(entry.get("calls", 0)) + 1
        if not ok:
            entry["errors"] = int(entry.get("errors", 0)) + 1
        entry["last_call"] = _now()
        entry["last_ok"] = ok
        if ok:
            entry["last_success"] = entry["last_call"]
        path = _usage_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(usage, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # counting must never break a tool


def usage() -> dict[str, dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _load().items()}


def _ok(result: Any) -> bool:
    return not (isinstance(result, dict) and result.get("ok") is False)


def tracked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so each call is counted. The signature is kept for the MCP schema."""
    name = fn.__name__
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await fn(*args, **kwargs)
            except BaseException:
                record(name, False)
                raise
            record(name, _ok(result))
            return result

        return run_async

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            record(name, False)
            raise
        record(name, _ok(result))
        return result

    return run


# -- checks --------------------------------------------------------------------------


def _result(state: str, detail: str, **extra: Any) -> dict[str, Any]:
    # state: ok | error | warning | not_configured
    return {"state": state, "detail": detail, **extra}


def _timed(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    start = time.monotonic()
    try:
        out = fn()
    except Exception as exc:  # a check must report, never raise
        out = _result("error", f"{exc.__class__.__name__}: {exc}")
    out["ms"] = int((time.monotonic() - start) * 1000)
    return out


def check_vault_git() -> dict[str, Any]:
    if not sync.is_enabled():
        return _result("not_configured", "OBSIDIAN_VAULT_REMOTE is not set (local vault only)")
    root = Path(os.environ.get("OBSIDIAN_VAULT", "")).expanduser()
    if not (root / ".git").is_dir():
        return _result("error", f"no Git clone of the vault at {root}")
    remote = sync._git("ls-remote", "--heads", "origin", cwd=root, check=False)
    if remote.returncode != 0:
        msg = (remote.stderr or remote.stdout).strip().splitlines()
        return _result("error", "cannot reach the vault repository: " + (msg[-1] if msg else "?"))
    extra: dict[str, Any] = {}
    ahead = sync._git("rev-list", "--count", "@{u}..HEAD", cwd=root, check=False)
    if ahead.returncode == 0:
        extra["unpushed_commits"] = int(ahead.stdout.strip() or 0)
    dirty = sync._git("status", "--porcelain", cwd=root, check=False)
    extra["uncommitted_files"] = len([line for line in dirty.stdout.splitlines() if line.strip()])
    last = sync._git("log", "-1", "--format=%cI", cwd=root, check=False)
    if last.returncode == 0 and last.stdout.strip():
        extra["last_commit"] = last.stdout.strip()
    if extra.get("unpushed_commits"):
        return _result("warning", "repository reachable, but local commits are not pushed", **extra)
    return _result("ok", "repository reachable (read and write token)", **extra)


def check_youtube_proxy() -> dict[str, Any]:
    proxy = os.environ.get(youtube.PROXY_ENV, "").strip()
    if not proxy:
        return _result(
            "not_configured",
            f"{youtube.PROXY_ENV} is not set: YouTube blocks the VPS IP for most videos",
        )
    parsed = urlparse(proxy)
    host = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "?")
    try:
        # A few hundred bytes of proxy traffic.
        resp = requests.get(
            "https://www.youtube.com/generate_204",
            proxies={"http": proxy, "https": proxy},
            timeout=CHECK_TIMEOUT,
        )
    except requests.exceptions.ProxyError as exc:
        reason = "authentication refused (password changed?)" if "407" in str(exc) else "unreachable"
        return _result("error", f"proxy {host} {reason}", proxy=host)
    except requests.RequestException as exc:
        return _result("error", f"proxy {host}: {exc.__class__.__name__}", proxy=host)
    if resp.status_code == 204:
        return _result("ok", f"YouTube reachable through {host}", proxy=host)
    return _result("warning", f"YouTube answered {resp.status_code} through {host}", proxy=host)


def check_youtube_api_key() -> dict[str, Any]:
    if not os.environ.get(youtube_search.API_KEY_ENV, "").strip():
        return _result("not_configured", f"{youtube_search.API_KEY_ENV} is not set")
    try:
        # 1 quota unit out of 10,000 a day.
        youtube_search._get("videos", {"part": "id", "id": "jNQXAC9IVRw"})
    except youtube_search.SearchError as exc:
        return _result("error", str(exc))
    return _result("ok", "key accepted by the YouTube Data API")


def check_fxtwitter() -> dict[str, Any]:
    try:
        resp = x_post._get(f"{x_post.FXTWITTER}/jack/status/20")
    except requests.RequestException as exc:
        return _result("error", f"FxTwitter unreachable ({exc.__class__.__name__})")
    if resp.status_code == 200:
        return _result("ok", "FxTwitter answers")
    return _result("error", f"FxTwitter answered {resp.status_code}")


def check_x_inbox_account() -> dict[str, Any]:
    account = os.environ.get(x_post.INBOX_ENV, "").strip().lstrip("@")
    if not account:
        return _result("not_configured", f"{x_post.INBOX_ENV} is not set")
    return _result("ok", f"inbox account @{account} (public posts read through FxTwitter)")


async def check_x_search_account() -> dict[str, Any]:
    """State of the search account as twscrape last saw it. Sends nothing to X."""
    start = time.monotonic()
    try:
        cookies, account, db = x_search._settings()
    except x_search.XSearchError:
        out = _result("not_configured", f"{x_search.COOKIES_ENV} is not set")
    else:
        try:
            out = await _x_account_state(cookies, account, db)
        except Exception as exc:
            out = _result("error", f"{exc.__class__.__name__}: {exc}")
    out["ms"] = int((time.monotonic() - start) * 1000)
    return out


async def _x_account_state(cookies: str, account: str, db: Path) -> dict[str, Any]:
    from twscrape import AccountsPool
    from twscrape.utils import parse_cookies

    if not db.exists():
        return _result("ok", f"@{account} configured, never used yet", account=account)
    pool = AccountsPool(str(db), raise_when_no_account=False)
    acc = await pool.get_account(account)
    if acc is None:
        return _result("ok", f"@{account} configured, never used yet", account=account)
    wanted = parse_cookies(cookies)
    if any(acc.cookies.get(k) != wanted.get(k) for k in ("auth_token", "ct0")):
        return _result("ok", f"@{account}: new cookies, used at the next search", account=account)
    if not acc.active:
        return _result(
            "error",
            f"X refused the session of @{account}: {acc.error_msg or 'unknown reason'}",
            account=account,
        )
    extra: dict[str, Any] = {"account": account}
    if acc.last_used:
        extra["last_used"] = acc.last_used.isoformat(timespec="seconds")
    locks = {
        queue: until for queue, until in (acc.locks or {}).items()
        if until and until > datetime.now(timezone.utc)
    }
    if locks:
        until = max(locks.values())
        return _result(
            "warning",
            f"@{account} paused by X's rate limit until {until.isoformat(timespec='minutes')}",
            **extra,
        )
    quota = x_search.last_quota(db)
    if quota and quota.get("account") == account:
        extra["quota"] = {k: quota.get(k) for k in ("limit", "remaining", "resets_at")}
        if quota.get("remaining") == 0:
            return _result(
                "warning", f"@{account}: X search quota used up until {quota['resets_at']}", **extra
            )
        left = f", {quota['remaining']}/{quota.get('limit')} search requests left"
        return _result("ok", f"@{account} session accepted at the last search{left}", **extra)
    return _result("ok", f"@{account} session accepted at the last search", **extra)


_SYNC_CHECKS: dict[str, Callable[[], dict[str, Any]]] = {
    "vault_git": check_vault_git,
    "youtube_proxy": check_youtube_proxy,
    "youtube_api_key": check_youtube_api_key,
    "fxtwitter": check_fxtwitter,
    "x_inbox_account": check_x_inbox_account,
}


async def run_checks() -> dict[str, dict[str, Any]]:
    import asyncio

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=len(_SYNC_CHECKS)) as pool:
        futures = {
            name: loop.run_in_executor(pool, _timed, fn) for name, fn in _SYNC_CHECKS.items()
        }
        x_state = await check_x_search_account()
        results = {name: await fut for name, fut in futures.items()}
    results["x_search_account"] = x_state
    return results


def app_html() -> str:
    """The MCP App (interactive card) shown with ecosystem_status results."""
    return (Path(__file__).parent / "status_app.html").read_text(encoding="utf-8")


_RANK = {"error": 3, "warning": 2, "not_configured": 1, "ok": 0, "unchecked": 0}


async def report(tool_names: list[str], check: bool = True) -> dict[str, Any]:
    checks = await run_checks() if check else {}
    used = usage()
    tools = []
    for name in tool_names:
        deps = DEPENDS_ON.get(name, [])
        states = [checks[d]["state"] for d in deps if d in checks]
        state = max(states, key=lambda s: _RANK[s]) if states else ("ok" if check else "unchecked")
        entry = used.get(name, {})
        tools.append(
            {
                "tool": name,
                "state": state,
                "depends_on": deps,
                "calls": entry.get("calls", 0),
                "errors": entry.get("errors", 0),
                "last_call": entry.get("last_call"),
                "last_call_ok": entry.get("last_ok"),
                "last_success": entry.get("last_success"),
            }
        )
    worst = max((c["state"] for c in checks.values()), key=lambda s: _RANK[s], default="unchecked")
    return {
        "overall": worst,
        "checked_at": _now(),
        "server": {"name": "obsidian-mcp", "started": _started.isoformat(timespec="seconds")},
        "checks": checks,
        "tools": tools,
        "usage_counted_since": min(
            (e["first_call"] for e in used.values() if e.get("first_call")), default=None
        ),
        "not_covered": (
            "The Revolut server is a separate service and is not checked here: call "
            "revolut_accounts for its state and the expiry date of the bank consent."
        ),
    }
