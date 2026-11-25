"""Agents: their own login, only the vaults they are granted, read-only where told.

Also covers vaults stored in a sub-folder of a repository (`url#folder`), which is how
the start-up's vaults share one repository."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from obsidian_mcp import access
from obsidian_mcp.vault import VaultError, configured_vaults

from test_http import PASSWORD, STATIC_TOKEN, Mcp, free_port, git, http, oauth_login

ANALYSTE = "a" * 40
CEO = "c" * 40


def seed(tmp_path: Path, name: str, files: dict[str, str]) -> tuple[Path, Path]:
    remote = tmp_path / f"{name}.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    work = tmp_path / f"{name}-work"
    git("clone", str(remote), str(work), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "seed", cwd=work)
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return remote, work


# -- configuration ---------------------------------------------------------------------


def test_parse_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_AGENTS", "analyste:analyste=rw,global=r; ceo:ceo=rw,analyste=r,global=r")
    agents = access.configured_agents()
    assert list(agents) == ["analyste", "ceo"]
    ceo = agents["ceo"]
    assert ceo.vaults == ["ceo", "analyste", "global"]
    assert ceo.default_vault == "ceo" and ceo.can_write("ceo") and not ceo.can_write("analyste")
    assert access.check(ceo, None, write=True) == "ceo"
    assert access.check(ceo, "analyste", write=False) == "analyste"
    with pytest.raises(VaultError, match="read-only"):
        access.check(ceo, "analyste", write=True)
    with pytest.raises(VaultError, match="unknown vault"):
        access.check(ceo, "principal", write=False)


@pytest.mark.parametrize("raw", ["analyste", "analyste:", "analyste:x=rwx", "a:x=r;a:y=r", "Bad name:x=r"])
def test_bad_agents(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("OBSIDIAN_AGENTS", raw)
    with pytest.raises(VaultError):
        access.configured_agents()


def test_secret_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_AGENTS", "analyste:analyste=rw;court:x=r")
    monkeypatch.setenv("OBSIDIAN_AGENT_TOKEN_ANALYSTE", ANALYSTE)
    monkeypatch.setenv("OBSIDIAN_AGENT_TOKEN_COURT", "trop-court")
    assert access.agent_for_secret(ANALYSTE).name == "analyste"
    assert access.agent_for_secret("trop-court") is None  # weak secrets never log in
    assert access.agent_for_secret("") is None
    # A token whose agent was removed from the configuration gets no vault at all.
    assert access.agent_for_subject("agent:parti").vaults == []
    assert access.agent_for_subject("owner") is None


def test_subfolder_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv(
        "OBSIDIAN_EXTRA_VAULTS",
        "analyste=https://github.com/me/startup.git#analyste;ceo=https://github.com/me/startup.git#/ceo/",
    )
    vaults = configured_vaults()
    assert vaults["analyste"].path == tmp_path / "vaults" / "analyste" / "analyste"
    assert vaults["analyste"].repo == tmp_path / "vaults" / "analyste"
    assert vaults["analyste"].remote == "https://github.com/me/startup.git"
    assert vaults["ceo"].path == tmp_path / "vaults" / "ceo" / "ceo"
    monkeypatch.setenv("OBSIDIAN_EXTRA_VAULTS", "x=https://github.com/me/startup.git#../etc")
    with pytest.raises(VaultError, match="invalid folder"):
        configured_vaults()


# -- end to end ------------------------------------------------------------------------


@pytest.fixture
def startup(tmp_path: Path):
    main_remote, _ = seed(tmp_path, "main", {"Prive.md": "# Journal intime\n"})
    startup_remote, work = seed(tmp_path, "startup", {
        "README.md": "# Entrée\n",
        "analyste/Etat actuel.md": "# Etat analyste\n",
        "ceo/Etat actuel.md": "# Etat CEO\n",
        "global/Roles/CEO.md": "# Consignes CEO\n",
        "global/Skills/commun/journaliser/SKILL.md":
            "---\nname: journaliser\ndescription: Tout noter.\n---\n\n# Journaliser\n",
        "global/Skills/analyste/questions/SKILL.md":
            "---\nname: questions\ndescription: Lister les questions.\n---\n\nVoir modele.md\n",
        "global/Skills/analyste/questions/modele.md": "Q1 ...\n",
        "global/Skills/ceo/boucler/SKILL.md":
            "---\nname: boucler\ndescription: Boucler.\n---\n\nAu plus 3 tours.\n",
        "global/Skills/ceo/mauvais/SKILL.md": "pas de frontmatter\n",
    })
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
        "OBSIDIAN_VAULT": str(tmp_path / "server" / "vault"),
        "OBSIDIAN_VAULT_REMOTE": str(main_remote),
        "OBSIDIAN_EXTRA_VAULTS": ";".join(
            f"{n}={startup_remote}#{n}" for n in ("analyste", "ceo", "global")
        ),
        "OBSIDIAN_AGENTS": "analyste:analyste=rw,global=r;ceo:ceo=rw,analyste=r,global=r",
        "OBSIDIAN_AGENT_TOKEN_ANALYSTE": ANALYSTE,
        "OBSIDIAN_AGENT_TOKEN_CEO": CEO,
        "OBSIDIAN_SKILLS_VAULT": "global",
        "OBSIDIAN_PULL_INTERVAL": "0",
    }
    proc = subprocess.Popen([sys.executable, "-m", "obsidian_mcp"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            if http("GET", base + "/healthz")[0] == 200:
                break
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(proc.stdout.read())
        time.sleep(0.1)
    yield {"base": base, "work": work}
    proc.terminate()
    proc.wait(10)


def test_agents_end_to_end(startup) -> None:
    base, work = startup["base"], startup["work"]

    # The analyst logs in on the password page with its own secret.
    tokens = oauth_login(base, password=ANALYSTE)
    analyste = Mcp(base, tokens["access_token"])
    analyste.initialize()
    listed = analyste.call("list_vaults")
    assert listed["agent"] == "analyste" and listed["default"] == "analyste"
    assert [(v["name"], v["access"]) for v in listed["vaults"]] == [
        ("analyste", "read-write"), ("global", "read-only")]
    assert "repository" not in json.dumps(listed)

    # Without `vault` it works in its own vault, which is only its folder of the repo.
    res = analyste.call("list_notes")
    assert res["notes"] == ["Etat actuel.md"]
    assert analyste.call("read_note", path="Etat actuel")["content"] == "# Etat analyste\n"
    assert analyste.call("append_note", path="Journal", content="- 1re note")["ok"]
    assert analyste.call("read_note", path="Roles/CEO", vault="global")["ok"]

    # Everything else is invisible or read-only.
    for vault in ("principal", "ceo"):
        res = analyste.call("read_note", path="Prive", vault=vault)
        assert not res["ok"] and "unknown vault" in res["error"]
    res = analyste.call("write_note", path="X", content="x", vault="global")
    assert not res["ok"] and "read-only" in res["error"]
    res = analyste.call("read_note", path="../ceo/Etat actuel")
    assert not res["ok"] and "escapes" in res["error"]
    assert not analyste.call("ecosystem_status")["ok"]
    assert set(analyste.call("sync_vault")["vaults"]) == {"analyste", "global"}

    # The write was pushed into the analyst's folder of the shared repository.
    git("pull", cwd=work)
    assert (work / "analyste" / "Journal.md").read_text(encoding="utf-8").strip() == "- 1re note"

    # The refreshed token is still the analyst's.
    form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"]}
    status, _, body = http("POST", base + "/token", form=form)
    assert status == 200, body
    assert Mcp(base, json.loads(body)["access_token"]).call("list_vaults")["agent"] == "analyste"

    # The CEO, with its secret as a bearer token: writes its vault, reads the analyst's.
    ceo = Mcp(base, CEO)
    ceo.initialize()
    assert ceo.call("read_note", path="Journal", vault="analyste")["ok"]
    assert not ceo.call("append_note", path="Journal", content="x", vault="analyste")["ok"]
    assert ceo.call("append_note", path="Decisions", content="- go")["ok"]
    assert not ceo.call("read_note", path="Prive", vault="principal")["ok"]
    git("pull", cwd=work)
    assert (work / "ceo" / "Decisions.md").is_file()

    # The owner still sees everything.
    owner = Mcp(base, STATIC_TOKEN)
    owner.initialize()
    names = [v["name"] for v in owner.call("list_vaults")["vaults"]]
    assert names == ["principal", "analyste", "ceo", "global"]
    assert owner.call("read_note", path="Prive")["ok"]


META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def modern(client: Mcp, method: str, params: dict | None = None) -> tuple[int, dict]:
    """A request on the 2026-07-28 revision (the one Skills over MCP needs): no
    handshake, the version travels in headers and in `_meta`."""
    client.ids += 1
    headers = {"Authorization": f"Bearer {client.token}",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if params and "uri" in params:
        headers["Mcp-Name"] = params["uri"]
    status, hdrs, body = http("POST", client.url, headers=headers, data={
        "jsonrpc": "2.0", "id": client.ids, "method": method,
        "params": {**(params or {}), "_meta": META}})
    if "text/event-stream" in hdrs.get("content-type", hdrs.get("Content-Type", "")):
        msgs = [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:") and l[5:].strip()]
        return status, [m for m in msgs if m.get("id") == client.ids][-1]
    return status, json.loads(body)


def skills_list(client: Mcp) -> list[dict]:
    status, res = modern(client, "skills/list")
    assert status == 200 and "result" in res, res
    assert res["result"]["resultType"] == "complete"
    return res["result"]["skills"]


def test_skills_per_agent(startup) -> None:
    base = startup["base"]
    analyste = Mcp(base, ANALYSTE)
    status, res = modern(analyste, "server/discover")
    assert "io.modelcontextprotocol/skills" in res["result"]["capabilities"]["extensions"], res

    listed = {s["frontmatter"]["name"]: s for s in skills_list(analyste)}
    assert set(listed) == {"journaliser", "questions"}  # common + its own, not the CEO's
    q = listed["questions"]
    assert q["uri"] == "skill://analyste/questions/SKILL.md"
    files = {r["uri"]: r for r in q["resources"]}
    assert set(files) == {"skill://analyste/questions/SKILL.md", "skill://analyste/questions/modele.md"}
    body = "---\nname: questions\ndescription: Lister les questions.\n---\n\nVoir modele.md\n"
    import hashlib
    assert files["skill://analyste/questions/SKILL.md"]["digest"] == "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    status, res = modern(analyste, "skills/get", {"uri": "skill://analyste/questions/SKILL.md"})
    assert res["result"]["skill"]["frontmatter"]["description"] == "Lister les questions."
    status, res = modern(analyste, "resources/read", {"uri": "skill://analyste/questions/modele.md"})
    assert res["result"]["contents"][0]["text"] == "Q1 ...\n"

    # Another role's skill is invisible, by get and by read.
    status, res = modern(analyste, "skills/get", {"uri": "skill://ceo/boucler/SKILL.md"})
    assert res["error"]["code"] == -32602, res
    status, res = modern(analyste, "resources/read", {"uri": "skill://ceo/boucler/SKILL.md"})
    assert "error" in res, res
    status, res = modern(analyste, "resources/read", {"uri": "skill://analyste/../ceo/boucler/SKILL.md"})
    assert "error" in res, res

    # The CEO sees its own (a malformed skill is skipped); the owner sees none.
    ceo = Mcp(base, CEO)
    assert {s["frontmatter"]["name"] for s in skills_list(ceo)} == {"journaliser", "boucler"}
    owner = Mcp(base, STATIC_TOKEN)
    assert skills_list(owner) == []
