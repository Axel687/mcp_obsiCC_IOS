"""Several vaults on one server: configuration, choice of vault per call, Git sync of
each vault, and the rule that changes need an explicit vault when there is a choice."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from obsidian_mcp import server, status, sync
from obsidian_mcp.vault import VaultError, configured_vaults, get_vault, use_vault


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_remote(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    remote = tmp_path / f"{name}.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    work = tmp_path / f"{name}-seed"
    git("clone", str(remote), str(work), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "seed", cwd=work)
    for rel, text in files.items():
        (work / rel).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return remote


@pytest.fixture
def two_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    main_remote = make_remote(tmp_path, "main", {"Accueil.md": "# Accueil\nVoir [[Jung]].\n"})
    cours_remote = make_remote(
        tmp_path, "cours", {"Maths.md": "# Maths\nAlgèbre.\n", "CLAUDE.md": "règles\n"}
    )
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path / "data" / "vault"))
    monkeypatch.setenv("OBSIDIAN_VAULT_REMOTE", str(main_remote))
    monkeypatch.setenv("OBSIDIAN_EXTRA_VAULTS", f"cours={cours_remote}")
    monkeypatch.setenv("OBSIDIAN_PULL_INTERVAL", "0")
    monkeypatch.setenv("OBSIDIAN_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OBSIDIAN_VAULT_NAME", raising=False)
    monkeypatch.delenv("OBSIDIAN_VAULTS_DIR", raising=False)
    sync._last_pull.clear()
    lines = sync.bootstrap_all()
    assert all("failed" not in line for line in lines), lines
    return {"tmp": tmp_path, "main_remote": main_remote, "cours_remote": cours_remote}


def test_configuration(two_vaults) -> None:
    vaults = configured_vaults()
    assert list(vaults) == ["principal", "cours"]
    assert vaults["principal"].main
    assert vaults["cours"].path == two_vaults["tmp"] / "data" / "vaults" / "cours"
    assert get_vault().name == "principal"
    with use_vault("cours"):
        assert get_vault().name == "cours"
    assert get_vault().name == "principal"


@pytest.mark.parametrize(
    "value",
    ["sans-egal", "Mauvais Nom=https://x/y.git", "principal=https://x/y.git", "a=x.git;a=y.git"],
)
def test_bad_configuration(value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
    monkeypatch.setenv("OBSIDIAN_EXTRA_VAULTS", value)
    with pytest.raises(VaultError):
        configured_vaults()


def test_list_vaults(two_vaults) -> None:
    out = server.list_vaults()
    assert out["ok"] and out["choice_needed"] and out["default"] == "principal"
    by_name = {v["name"]: v for v in out["vaults"]}
    assert by_name["principal"]["note_count"] == 1
    assert by_name["cours"]["note_count"] == 2
    assert by_name["cours"]["has_rules_file"] and not by_name["principal"]["has_rules_file"]


def test_reads_default_to_main_vault(two_vaults) -> None:
    out = server.list_notes()
    assert out["ok"] and out["vault"] == "principal" and out["notes"] == ["Accueil.md"]
    out = server.read_note("Maths", vault="cours")
    assert out["ok"] and out["vault"] == "cours" and "Algèbre" in out["content"]
    assert server.read_note("Maths")["ok"] is False  # not in the main vault
    out = server.search_notes("Algèbre", vault="cours")
    assert out["count"] == 1
    assert server.vault_overview(vault="cours")["ok"]


def test_unknown_vault(two_vaults) -> None:
    out = server.list_notes(vault="perso")
    assert out["ok"] is False and "available vaults: principal, cours" in out["error"]


def test_changes_need_explicit_vault(two_vaults) -> None:
    out = server.write_note("Nouvelle.md", "texte")
    assert out["ok"] is False and "pass `vault` explicitly" in out["error"]
    assert not (two_vaults["tmp"] / "data" / "vault" / "Nouvelle.md").exists()


def test_write_goes_to_chosen_vault_and_its_remote(two_vaults) -> None:
    out = server.append_note("Maths.md", "Géométrie.", vault="cours")
    assert out["ok"], out
    assert out["sync"] == "pushed" and out["vault"] == "cours"
    tmp = two_vaults["tmp"]
    check = tmp / "check"
    git("clone", str(two_vaults["cours_remote"]), str(check), cwd=tmp)
    assert "Géométrie." in (check / "Maths.md").read_text(encoding="utf-8")
    # The main vault's repository is untouched.
    log = git("log", "--oneline", cwd=tmp / "data" / "vault")
    assert len(log.splitlines()) == 1


def test_links_cache_is_per_vault(two_vaults) -> None:
    assert server.get_links("Accueil")["ok"]
    assert server.get_links("Accueil", vault="cours")["ok"] is False
    assert server.list_notes()["notes"] == ["Accueil.md"]


def test_sync_all_vaults(two_vaults) -> None:
    out = server.sync_vault("pull")
    assert out["ok"] and set(out["vaults"]) == {"principal", "cours"}
    out = server.sync_vault("pull", vault="cours")
    assert out["ok"] and "vaults" not in out


def test_status_checks_every_vault(two_vaults) -> None:
    out = status.check_vault_git()
    assert out["state"] == "ok", out
    assert set(out["vaults"]) == {"principal", "cours"}


def test_single_vault_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
    monkeypatch.delenv("OBSIDIAN_VAULT_REMOTE", raising=False)
    monkeypatch.delenv("OBSIDIAN_EXTRA_VAULTS", raising=False)
    out = server.write_note("A.md", "x")
    assert out["ok"] and "vault" not in out
    assert server.list_vaults()["choice_needed"] is False


def test_tools_expose_vault_parameter() -> None:
    from obsidian_mcp.config import Settings

    srv = server.build_server(Settings())
    import asyncio

    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "list_vaults" in tools
    for name in ("read_note", "write_note", "search_notes", "vault_overview", "sync_vault"):
        assert "vault" in tools[name].input_schema["properties"], name
