"""Test bench for search_x, run by hand on the VPS against the real X.

    docker compose exec obsidian-mcp python -m obsidian_mcp.x_search_bench

It measures, with the same account and code as the search_x tool:

- quota: X's limit per 15 minutes, as sent in its rate-limit headers;
- volume: posts per request and time per request;
- depth: how far back in time pages of "latest" results go for a busy query;
- precision: for each search operator, the share of results that really match it
  (from:, lang:, since:/until:, min_faves:, "exact phrase", -word, filter:links,
  -filter:replies), and whether "latest" is really newest first.

Searches run one at a time, with a pause between them, and the bench stops before the
quota falls under a reserve (default 10 requests), so the search_x tool keeps working.
Nothing hides the automation. The report goes to stdout and to a JSON file in the
state directory; it never contains the cookies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import x_search

REPORT_FILE = "x_search_bench.json"


# -- checks: does a post match the operator? -------------------------------------------

Check = Callable[[dict[str, Any]], bool]


def _text(p: dict[str, Any]) -> str:
    return (p.get("text") or "").lower()


def _date(p: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(p["date"])
    except (KeyError, TypeError, ValueError):
        return None


def check_from(user: str) -> Check:
    return lambda p: (p.get("author") or {}).get("username", "").lower() == user.lower()


def check_lang(lang: str) -> Check:
    return lambda p: p.get("lang") == lang


def check_between(start: datetime, end: datetime) -> Check:
    def ok(p: dict[str, Any]) -> bool:
        d = _date(p)
        return d is not None and start <= d < end
    return ok


def check_min_likes(n: int) -> Check:
    # Likes only grow, so a post that matched min_faves when X indexed it still does.
    return lambda p: (p.get("likes") or 0) >= n


def check_phrase(phrase: str) -> Check:
    # X also matches the phrase in links and in the quoted post: count those as right.
    def ok(p: dict[str, Any]) -> bool:
        hay = _text(p) + " " + " ".join(p.get("links") or []).lower()
        hay += " " + _text(p.get("quoted") or {})
        # X ignores punctuation inside a phrase: "open-source" matches "open source".
        norm = lambda t: re.sub(r"[\W_]+", " ", t.lower()).strip()  # noqa: E731
        return norm(phrase) in norm(hay)
    return ok


def check_without(word: str) -> Check:
    return lambda p: re.search(rf"\b{re.escape(word.lower())}\b", _text(p)) is None


def check_has_links() -> Check:
    return lambda p: bool(p.get("links") or p.get("media") or p.get("quoted"))


def check_not_reply() -> Check:
    return lambda p: p.get("kind") != "reply"


def check_mentions(word: str) -> Check:
    """Loose relevance: the keyword is somewhere in the post, its links or quoted post."""
    return check_phrase(word)


@dataclass
class Case:
    name: str
    query: str
    checks: dict[str, Check]
    sort: str = "latest"


def default_cases(now: datetime) -> list[Case]:
    start = (now - timedelta(days=3)).date()
    end = (now - timedelta(days=1)).date()
    s, e = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in (start, end))
    return [
        Case("keyword", "bitcoin", {"keyword in post": check_mentions("bitcoin")}),
        Case("from:", "from:NASA", {"author is @NASA": check_from("NASA")}),
        Case("lang:", "intelligence artificielle lang:fr", {
            "lang is fr": check_lang("fr"),
            "keywords in post": lambda p: all(w in _text(p) for w in ("intelligence", "artificielle")),
        }),
        # Seen on 2026-11-05: X counts until: as inclusive and uses the account's time
        # zone (results up to 21:59 UTC on the until day), so allow day ends +-14 h.
        Case("since:/until:", f"election since:{start} until:{end}",
             {f"date in {start}..{end} (account time zone)":
              check_between(s - timedelta(hours=14), e + timedelta(days=1, hours=14))}),
        Case("min_faves:", "AI min_faves:1000", {"likes >= 1000": check_min_likes(1000)}),
        Case("\"exact phrase\"", '"open source"', {"phrase present": check_phrase("open source")}),
        Case("-word", "python -django", {
            "no 'django'": check_without("django"),
            "'python' present": check_mentions("python"),
        }),
        Case("filter:links", "climate filter:links", {"has a link or media": check_has_links()}),
        Case("-filter:replies", "from:NASA -filter:replies", {
            "not a reply": check_not_reply(), "author is @NASA": check_from("NASA"),
        }),
        Case("top", "openai", {"keyword in post": check_mentions("openai")}, sort="top"),
    ]


# -- running ---------------------------------------------------------------------------


class QuotaReserve(Exception):
    pass


class Paused(Exception):
    """X paused or refused the account: every further request would wait, then fail."""


@dataclass
class Bench:
    api: Any
    account: str
    reserve: int
    delay: float
    log: Callable[[str], None] = print
    requests: list[dict[str, Any]] = field(default_factory=list)
    quota: dict[str, Any] | None = None

    async def pages(self, query: str, sort: str, max_pages: int, limit: int = -1):
        """Yield (posts, seconds) per page, one request at a time."""
        from twscrape.accounts_pool import NoAccountError

        kv = {"product": x_search.SORTS[sort]}
        start = time.monotonic()
        try:
            async with aclosing(self.api.search_raw(query, limit=limit, kv=kv)) as gen:
                async for item in self._pages(gen, query, max_pages, start):
                    yield item
        except NoAccountError:
            raise Paused("X paused the account (rate limit) or refused its session") from None

    async def _pages(self, gen: Any, query: str, max_pages: int, start: float):
        from twscrape.models import parse_tweets

        n = 0
        async for rep in gen:
            spent = time.monotonic() - start
            q = x_search._quota(rep.headers, self.account)
            if q:
                self.quota = q
            posts = [x_search._post(t) for t in x_search.page_tweets(rep.json(), parse_tweets)]
            self.requests.append({"query": query, "posts": len(posts), "seconds": round(spent, 2),
                                  "remaining": (q or {}).get("remaining")})
            n += 1
            yield posts, spent
            if n >= max_pages:
                return
            if q and q["remaining"] <= self.reserve:
                raise QuotaReserve(q["remaining"])
            await asyncio.sleep(self.delay)
            start = time.monotonic()

    def guard(self) -> None:
        if self.quota and self.quota["remaining"] <= self.reserve:
            raise QuotaReserve(self.quota["remaining"])


def score(posts: list[dict[str, Any]], checks: dict[str, Check]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, check in checks.items():
        bad = [p for p in posts if not check(p)]
        out[label] = {
            "match": len(posts) - len(bad),
            "total": len(posts),
            "rate": round((len(posts) - len(bad)) / len(posts), 3) if posts else None,
            "misses": [p.get("url") for p in bad[:3]],
        }
    return out


def ordered_newest_first(posts: list[dict[str, Any]]) -> float | None:
    dates = [d for d in (_date(p) for p in posts) if d]
    if len(dates) < 2:
        return None
    pairs = list(zip(dates, dates[1:]))
    return round(sum(a >= b for a, b in pairs) / len(pairs), 3)


async def run_precision(bench: Bench, cases: list[Case], results: list[dict[str, Any]]) -> None:
    """Fill `results` case by case, so an early stop keeps what was measured."""
    for case in cases:
        bench.guard()
        bench.log(f"[precision] {case.name:<16} {case.query!r} ({case.sort})")
        posts: list[dict[str, Any]] = []
        seconds = None
        try:
            async for page, spent in bench.pages(case.query, case.sort, max_pages=1):
                posts, seconds = page, spent
        except (QuotaReserve, Paused):
            raise
        except Exception as exc:  # one broken case must not stop the bench
            results.append({"case": case.name, "query": case.query, "error": f"{exc.__class__.__name__}: {exc}"})
            bench.log(f"    error: {exc}")
            continue
        res = {"case": case.name, "query": case.query, "sort": case.sort, "posts": len(posts),
               "seconds": round(seconds or 0, 2), "checks": score(posts, case.checks)}
        if case.sort == "latest":
            res["newest_first"] = ordered_newest_first(posts)
        results.append(res)
        for label, r in res["checks"].items():
            rate = "n/a" if r["rate"] is None else f"{r['rate']:.0%}"
            bench.log(f"    {label:<28} {r['match']}/{r['total']}  {rate}")
        await asyncio.sleep(bench.delay)


async def run_depth(bench: Bench, query: str, max_pages: int) -> dict[str, Any]:
    bench.guard()
    bench.log(f"[depth] {query!r}: up to {max_pages} pages of 'latest'")
    per_page: list[int] = []
    times: list[float] = []
    ids: set[str] = set()
    dupes = 0
    newest = oldest = None
    stop = "max_pages"
    try:
        async for page, spent in bench.pages(query, "latest", max_pages=max_pages):
            per_page.append(len(page))
            times.append(spent)
            for p in page:
                if p["id"] in ids:
                    dupes += 1
                ids.add(p["id"])
                d = _date(p)
                if d:
                    newest = max(newest, d) if newest else d
                    oldest = min(oldest, d) if oldest else d
            bench.log(f"    page {len(per_page)}: {len(page)} posts, {spent:.1f}s, oldest so far {oldest}")
        else:
            if len(per_page) < max_pages:
                stop = "no more results"
    except QuotaReserve:
        stop = "quota reserve reached"
    except Paused as exc:
        stop = str(exc)
    span_h = round((newest - oldest).total_seconds() / 3600, 2) if newest and oldest else None
    return {
        "query": query, "pages": len(per_page), "posts": len(ids), "duplicates": dupes,
        "posts_per_page": per_page, "seconds_per_page": [round(t, 2) for t in times],
        "newest": newest.isoformat() if newest else None,
        "oldest": oldest.isoformat() if oldest else None,
        "hours_covered": span_h, "stopped_because": stop,
    }


def summary(report: dict[str, Any]) -> list[str]:
    lines = ["", "=== search_x bench: summary ==="]
    q = report.get("quota") or {}
    lines.append(f"account @{report['account']}  requests used by the bench: {len(report['requests'])}")
    if q:
        lines.append(f"X quota: limit {q.get('limit')} requests / 15 min, {q.get('remaining')} left, "
                     f"reset at {q.get('resets_at')}")
    else:
        lines.append("X quota: X did not send its rate-limit headers")
    secs = [r["seconds"] for r in report["requests"]]
    posts = [r["posts"] for r in report["requests"]]
    if secs:
        lines.append(f"per request: {statistics.median(posts):.0f} posts (median), "
                     f"{statistics.median(secs):.1f}s median, {max(secs):.1f}s max")
    d = report.get("depth")
    if d:
        lines.append(f"depth '{d['query']}': {d['posts']} posts in {d['pages']} pages cover "
                     f"{d['hours_covered']} h (stopped: {d['stopped_because']}, duplicates {d['duplicates']})")
    lines.append("precision (share of results matching the operator):")
    for r in report.get("precision", []):
        if "error" in r:
            lines.append(f"  {r['case']:<16} ERROR {r['error']}")
            continue
        rates = ", ".join(
            f"{k} {'n/a' if v['rate'] is None else format(v['rate'], '.0%')}" for k, v in r["checks"].items()
        )
        order = f", newest first {r['newest_first']:.0%}" if r.get("newest_first") is not None else ""
        lines.append(f"  {r['case']:<16} {r['posts']:>2} posts: {rates}{order}")
    if report.get("stopped"):
        lines.append(f"stopped early: {report['stopped']}")
    return lines


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    cookies, account, db = x_search._settings()
    pool = await x_search._pool(cookies, account, db)
    from twscrape import API

    bench = Bench(API(pool, raise_when_no_account=True), account, args.reserve, args.delay)
    now = datetime.now(tz=timezone.utc)
    report: dict[str, Any] = {"account": account, "started": now.isoformat(timespec="seconds"),
                              "reserve": args.reserve, "delay": args.delay}
    cases = default_cases(now)
    if args.only:
        cases = [c for c in cases if c.name in set(args.only)]
    try:
        if not args.skip_precision:
            report["precision"] = []
            await run_precision(bench, cases, report["precision"])
        if args.depth_pages > 0:
            report["depth"] = await run_depth(bench, args.depth_query, args.depth_pages)
    except QuotaReserve as exc:
        report["stopped"] = f"quota reserve reached ({exc.args[0]} requests left, reserve {args.reserve})"
    except Paused as exc:
        report["stopped"] = str(exc)
    report["requests"] = bench.requests
    report["quota"] = bench.quota and {k: bench.quota[k] for k in ("limit", "remaining", "resets_at")}
    report["finished"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    if bench.quota:
        x_search._save_quota(db, bench.quota)
    path = Path(args.report) if args.report else db.parent / REPORT_FILE
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    report["report_file"] = str(path)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m obsidian_mcp.x_search_bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--reserve", type=int, default=10, help="stop when X's quota falls to this many requests (default 10)")
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests (default 3)")
    ap.add_argument("--depth-query", default="bitcoin", help="busy query for the depth test (default: bitcoin)")
    ap.add_argument("--depth-pages", type=int, default=5, help="pages for the depth test, 0 to skip (default 5)")
    ap.add_argument("--skip-precision", action="store_true", help="skip the operator tests")
    ap.add_argument("--only", nargs="*", help="run only these precision cases (e.g. from: lang:)")
    ap.add_argument("--report", help="JSON report path (default: state dir/x_search_bench.json)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = asyncio.run(main_async(args))
    except x_search.XSearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(summary(report)))
    print(f"\nfull report: {report['report_file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
