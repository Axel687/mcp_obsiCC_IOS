"""End-to-end test of the HTTP mode: a real server process, a Git remote, and the OAuth
flow claude.ai goes through (discovery → registration → authorize → password page →
token → MCP calls → refresh)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

PASSWORD = "correct horse battery staple"
STATIC_TOKEN = "s" * 40
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, *, data: Any = None, form: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict[str, str], str]:
    body = None
    hdrs = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with _opener.open(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def setup(tmp_path: Path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    phone = tmp_path / "phone"  # stands in for Obsidian Git on the iPhone
    git("clone", str(remote), str(phone), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=phone)
    git("config", "user.name", "phone", cwd=phone)
    (phone / "Cortex.md").write_text("# Cortex\nVoir [[Jung]]. #index\n", encoding="utf-8")
    (phone / "Jung.md").write_text("---\ntags: [psy]\n---\nArchétypes.\n", encoding="utf-8")
    git("add", "-A", cwd=phone)
    git("commit", "-m", "init", cwd=phone)
    git("push", "-u", "origin", "main", cwd=phone)

    port = free_port()
    base = f"http://localhost:{port}"
    env = {
        **os.environ,
        "OBSIDIAN_MCP_TRANSPORT": "http",
        "OBSIDIAN_MCP_HOST": "127.0.0.1",
        "OBSIDIAN_MCP_PORT": str(port),
        "OBSIDIAN_MCP_PUBLIC_URL": base,
        "OBSIDIAN_MCP_PASSWORD": PASSWORD,
        "OBSIDIAN_MCP_TOKEN": STATIC_TOKEN,
        "OBSIDIAN_MCP_STATE_DIR": str(tmp_path / "state"),
        "OBSIDIAN_VAULT": str(tmp_path / "server-vault"),
        "OBSIDIAN_VAULT_REMOTE": str(remote),
        "OBSIDIAN_PULL_INTERVAL": "0",
    }

    procs: list[subprocess.Popen] = []

    def start() -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "obsidian_mcp"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        procs.append(proc)
        for _ in range(100):
            try:
                if http("GET", base + "/healthz")[0] == 200:
                    return
            except OSError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read())
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop() -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait(10)

    start()
    yield {"base": base, "phone": phone, "remote": remote, "start": start, "stop": stop}
    stop()


def oauth_login(base: str) -> dict[str, Any]:
    status, _, body = http("POST", base + "/register", data={
        "client_name": "Claude", "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert status == 201, body
    client_id = json.loads(body)["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "resource": base + "/mcp",
    })
    status, headers, _ = http("GET", f"{base}/authorize?{query}")
    assert status == 302
    login_url = headers["location"]
    assert login_url.startswith(base + "/login?txn=")
    txn = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)["txn"][0]

    status, _, page = http("GET", login_url)
    assert status == 200 and "Claude" in page and "claude.ai" in page

    status, _, page = http("POST", base + "/login", form={"txn": txn, "password": "wrong"})
    assert status == 401 and "incorrect" in page

    status, headers, _ = http("POST", base + "/login", form={"txn": txn, "password": PASSWORD})
    assert status == 302
    back = urllib.parse.urlparse(headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    params = urllib.parse.parse_qs(back.query)
    assert params["state"] == ["xyz"]
    code = params["code"][0]

    token_form = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                  "redirect_uri": CALLBACK, "code_verifier": verifier}
    status, _, body = http("POST", base + "/token", form=token_form)
    assert status == 200, body
    tokens = json.loads(body)
    # Codes are single-use.
    assert http("POST", base + "/token", form=token_form)[0] == 400
    return {"client_id": client_id, **tokens}


class Mcp:
    def __init__(self, base: str, token: str) -> None:
        self.url = base + "/mcp"
        self.token = token
        self.ids = 0
        self.protocol: str | None = None

    def rpc(self, method: str, params: dict | None = None) -> tuple[int, Any]:
        self.ids += 1
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/json, text/event-stream"}
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        status, hdrs, body = http("POST", self.url, headers=headers,
                                  data={"jsonrpc": "2.0", "id": self.ids, "method": method,
                                        "params": params or {}})
        if status != 200:
            return status, body
        if "text/event-stream" in hdrs.get("content-type", ""):
            msgs = [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:") and l[5:].strip()]
            body_json = [m for m in msgs if m.get("id") == self.ids][-1]
        else:
            body_json = json.loads(body)
        return status, body_json

    def initialize(self) -> dict:
        status, res = self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}})
        assert status == 200, res
        self.protocol = res["result"]["protocolVersion"]
        return res["result"]

    def call(self, tool: str, **args: Any) -> dict:
        status, res = self.rpc("tools/call", {"name": tool, "arguments": args})
        assert status == 200, res
        result = res["result"]
        assert not result.get("isError"), result
        if result.get("structuredContent") is not None:
            sc = result["structuredContent"]
            return sc.get("result", sc) if set(sc) == {"result"} else sc
        return json.loads(result["content"][0]["text"])


def test_unauthenticated_requests_are_rejected(setup) -> None:
    base = setup["base"]
    status, headers, _ = http("POST", base + "/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Accept": "application/json, text/event-stream"})
    assert status == 401
    assert "resource_metadata" in headers.get("www-authenticate", "")
    assert Mcp(base, "not-a-token").rpc("tools/list")[0] == 401

    status, _, body = http("GET", base + "/.well-known/oauth-authorization-server")
    meta = json.loads(body)
    assert status == 200 and meta["registration_endpoint"] == base + "/register"
    assert "S256" in meta["code_challenge_methods_supported"]


def test_foreign_redirects_are_refused(setup) -> None:
    status, _, _ = http("POST", setup["base"] + "/register", data={
        "client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
        "token_endpoint_auth_method": "none"})
    assert status == 400


def test_full_flow_and_sync(setup) -> None:
    base, phone = setup["base"], setup["phone"]
    tokens = oauth_login(base)
    client = Mcp(base, tokens["access_token"])
    info = client.initialize()
    assert "second brain" in (info.get("instructions") or "")

    status, res = client.rpc("tools/list")
    names = {t["name"] for t in res["result"]["tools"]}
    assert {"vault_overview", "get_backlinks", "list_tags", "append_note"} <= names
    assert "generate_graph" not in names  # off by default over HTTP

    ov = client.call("vault_overview")
    assert ov["note_count"] == 2
    assert client.call("get_backlinks", path="Jung")["backlinks"][0]["source"] == "Cortex.md"

    # Claude writes → commit pushed to the remote → visible on the phone.
    client.call("append_note", path="Inbox", content="- idée depuis Claude")
    git("pull", cwd=phone)
    assert "idée depuis Claude" in (phone / "Inbox.md").read_text(encoding="utf-8")

    # The phone writes → the next read on the server sees it without a restart.
    (phone / "Jung.md").write_text("---\ntags: [psy]\n---\nOmbre et persona.\n", encoding="utf-8")
    git("commit", "-am", "edit from phone", cwd=phone)
    git("push", cwd=phone)
    assert "Ombre" in client.call("read_note", path="Jung")["content"]

    # Static token for Claude Code.
    assert Mcp(base, STATIC_TOKEN).call("list_tags")["tags"][0]["tag"] in {"index", "psy"}

    # Refresh rotates both tokens and kills the old access token.
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"]}
    status, _, body = http("POST", base + "/token", form=form)
    assert status == 200, body
    new = json.loads(body)
    assert Mcp(base, tokens["access_token"]).rpc("tools/list")[0] == 401
    assert http("POST", base + "/token", form=form)[0] == 400  # old refresh token is spent

    # Tokens survive a restart (claude.ai shouldn't have to reconnect after a deploy).
    setup["stop"]()
    setup["start"]()
    again = Mcp(base, new["access_token"])
    again.initialize()
    assert again.call("list_notes")["notes"] == ["Cortex.md", "Inbox.md", "Jung.md"]


def test_login_lockout(setup) -> None:
    base = setup["base"]
    status, _, body = http("POST", base + "/register", data={
        "client_name": "Claude", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
    client_id = json.loads(body)["client_id"]
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    _, headers, _ = http("GET", f"{base}/authorize?{query}")
    txn = urllib.parse.parse_qs(urllib.parse.urlparse(headers["location"]).query)["txn"][0]
    for _ in range(5):
        assert http("POST", base + "/login", form={"txn": txn, "password": "nope"})[0] == 401
    # Even the right password is refused while locked.
    assert http("POST", base + "/login", form={"txn": txn, "password": PASSWORD})[0] == 429
