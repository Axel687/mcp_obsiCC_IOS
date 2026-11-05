from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from obsidian_mcp import x_search, x_search_bench as bench

COOKIES = "auth_token=abc123; ct0=def456"
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def post(**kw):
    base = {"url": "https://x.com/a/status/1", "id": "1", "kind": "post", "date": NOW.isoformat(),
            "author": {"username": "NASA"}, "text": "Hello open source world", "lang": "en",
            "likes": 5, "links": [], "media": None}
    base.update(kw)
    return base


# -- checks ------------------------------------------------------------------------------

def test_checks() -> None:
    assert bench.check_from("nasa")(post()) and not bench.check_from("esa")(post())
    assert bench.check_lang("en")(post()) and not bench.check_lang("fr")(post())
    inside = bench.check_between(NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert inside(post()) and not inside(post(date=(NOW - timedelta(days=5)).isoformat()))
    assert not inside(post(date=None))
    assert bench.check_min_likes(5)(post()) and not bench.check_min_likes(6)(post())
    assert bench.check_phrase("open source")(post())
    assert bench.check_phrase("open source")(post(text="x", links=["https://open source.org"])) is True
    assert bench.check_phrase("open source")(post(text="x", quoted={"text": "Open  Source rocks"}))
    assert bench.check_without("django")(post()) and not bench.check_without("world")(post())
    assert not bench.check_without("django")(post(text="I use Django daily"))
    assert bench.check_has_links()(post(links=["https://a.b"])) and not bench.check_has_links()(post())
    assert bench.check_not_reply()(post()) and not bench.check_not_reply()(post(kind="reply"))


def test_score_and_order() -> None:
    posts = [post(id="1"), post(id="2", url="https://x.com/b/status/2", author={"username": "esa"})]
    s = bench.score(posts, {"author": bench.check_from("NASA")})
    assert s["author"] == {"match": 1, "total": 2, "rate": 0.5, "misses": ["https://x.com/b/status/2"]}
    assert bench.score([], {"author": bench.check_from("NASA")})["author"]["rate"] is None
    dated = [post(date=(NOW - timedelta(minutes=m)).isoformat()) for m in (0, 1, 3, 2)]
    assert bench.ordered_newest_first(dated) == pytest.approx(0.667, abs=0.001)
    assert bench.ordered_newest_first([post()]) is None


def test_default_cases_cover_operators() -> None:
    names = [c.name for c in bench.default_cases(NOW)]
    for op in ("from:", "lang:", "since:/until:", "min_faves:", '"exact phrase"', "-word",
               "filter:links", "-filter:replies", "top", "keyword"):
        assert op in names
    since = next(c for c in bench.default_cases(NOW) if c.name == "since:/until:")
    assert "since:2026-11-02 until:2026-11-04" in since.query


# -- full run against a fake X -------------------------------------------------------------

class FakeRep:
    def __init__(self, tweets, remaining):
        self.tweets = tweets
        self.headers = {"x-rate-limit-limit": "50", "x-rate-limit-remaining": str(remaining),
                        "x-rate-limit-reset": str(int((NOW + timedelta(minutes=15)).timestamp()))}

    def json(self):
        return {"tweets": self.tweets}


def _tweet(i, user="NASA", text="bitcoin open source", lang="en", likes=2000, minutes=0, reply=None):
    u = SimpleNamespace(username=user, displayname=user, followersCount=1, blue=False, verified=False)
    return SimpleNamespace(
        url=f"https://x.com/{user}/status/{i}", id_str=str(i), date=NOW - timedelta(minutes=minutes),
        user=u, rawContent=text, lang=lang, likeCount=likes, retweetCount=0, replyCount=0, quoteCount=0,
        viewCount=0, inReplyToTweetId=reply, inReplyToScreenName=None, quotedTweet=None,
        links=[SimpleNamespace(url="https://a.b")], media=None)


@pytest.fixture
def fake_x(monkeypatch, tmp_path):
    import twscrape
    import twscrape.models

    monkeypatch.setenv(x_search.COOKIES_ENV, COOKIES)
    monkeypatch.setenv(x_search.ACCOUNT_ENV, "burner")
    monkeypatch.setenv("OBSIDIAN_MCP_STATE_DIR", str(tmp_path))
    state = {"remaining": 50, "calls": [], "pause_after": None}

    async def search_raw(self, q, limit=-1, kv=None):
        from twscrape.accounts_pool import NoAccountError

        page = 0
        while True:
            if state["pause_after"] is not None and len(state["calls"]) >= state["pause_after"]:
                raise NoAccountError("paused")
            state["remaining"] -= 1
            state["calls"].append((q, kv))
            yield FakeRep([_tweet(page * 20 + i, minutes=page * 60 + i) for i in range(20)], state["remaining"])
            page += 1

    monkeypatch.setattr(twscrape.API, "search_raw", search_raw)
    monkeypatch.setattr(twscrape.models, "parse_tweets", lambda obj, limit=-1: iter(obj["tweets"]))
    return state, tmp_path


def run(argv):
    args = bench.parse_args(["--delay", "0", *argv])
    return asyncio.run(bench.main_async(args))


def test_full_run_writes_report(fake_x) -> None:
    state, tmp = fake_x
    report = run(["--depth-pages", "3"])
    assert len(report["precision"]) == 10 and report["depth"]["pages"] == 3
    assert len(state["calls"]) == 13 and report["quota"]["remaining"] == 37
    nasa = next(r for r in report["precision"] if r["case"] == "from:")
    assert nasa["checks"]["author is @NASA"]["rate"] == 1.0 and nasa["newest_first"] == 1.0
    fr = next(r for r in report["precision"] if r["case"] == "lang:")
    assert fr["checks"]["lang is fr"]["rate"] == 0.0
    top = next(r for r in report["precision"] if r["case"] == "top")
    assert "newest_first" not in top and state["calls"][-4][1] == {"product": "Top"}
    d = report["depth"]
    assert d["posts"] == 60 and d["duplicates"] == 0 and d["hours_covered"] > 2
    saved = json.loads((tmp / bench.REPORT_FILE).read_text())
    assert saved["account"] == "burner" and "abc123" not in json.dumps(saved)
    assert x_search.last_quota(tmp / "twscrape.db")["remaining"] == 37
    text = "\n".join(bench.summary(report))
    assert "limit 50 requests / 15 min" in text and "from:" in text


def test_stops_at_reserve(fake_x) -> None:
    state, _ = fake_x
    state["remaining"] = 15
    report = run(["--reserve", "10", "--depth-pages", "10"])
    # 15 -> precision uses 5 requests down to 10, then the guard stops before the next case.
    assert len(state["calls"]) == 5 and "quota reserve reached" in report["stopped"]
    assert len(report["precision"]) == 5 and "depth" not in report


def test_depth_stops_at_reserve(fake_x) -> None:
    state, _ = fake_x
    state["remaining"] = 14
    report = run(["--skip-precision", "--reserve", "10", "--depth-pages", "10"])
    assert report["depth"]["pages"] == 4 and report["depth"]["stopped_because"] == "quota reserve reached"


def test_paused_account_stops_bench(fake_x) -> None:
    state, _ = fake_x
    state["pause_after"] = 2
    report = run(["--depth-pages", "0"])
    assert len(report["precision"]) == 2 and "paused" in report["stopped"]


def test_only_and_main_output(fake_x, capsys) -> None:
    state, _ = fake_x
    assert bench.main(["--delay", "0", "--only", "from:", "lang:", "--depth-pages", "0"]) == 0
    out = capsys.readouterr().out
    assert len(state["calls"]) == 2 and "summary" in out and "full report" in out


def test_not_configured(monkeypatch, capsys) -> None:
    monkeypatch.delenv(x_search.COOKIES_ENV, raising=False)
    assert bench.main([]) == 1 and "not configured" in capsys.readouterr().err


def test_phrase_ignores_punctuation() -> None:
    assert bench.check_phrase("open source")(post(text="Love open-source tools"))
    assert bench.check_phrase("open source")(post(text="#OpenSource"))  is False
