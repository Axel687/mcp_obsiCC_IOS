"""Git-backed sync for the vault.

Enabled whenever `OBSIDIAN_VAULT_REMOTE` is set. On MCP startup the vault is cloned
(or pulled). After each write the server can call :func:`commit_and_push` to mirror the
change back to the remote. Push conflicts trigger an automatic pull-rebase and retry.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .vault import VaultError, vault_root


class SyncError(VaultError):
    pass


def remote_url() -> str | None:
    value = os.environ.get("OBSIDIAN_VAULT_REMOTE", "").strip()
    return value or None


def is_enabled() -> bool:
    return remote_url() is not None


def auto_push_enabled() -> bool:
    return os.environ.get("OBSIDIAN_AUTO_PUSH", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise SyncError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _configure_identity(repo: Path) -> None:
    name = os.environ.get("OBSIDIAN_GIT_NAME", "Obsidian MCP")
    email = os.environ.get("OBSIDIAN_GIT_EMAIL", "obsidian-mcp@local")
    _git("config", "user.name", name, cwd=repo, check=False)
    _git("config", "user.email", email, cwd=repo, check=False)


def ensure_clone() -> str:
    """Clone the remote vault on first use, or ensure the existing clone is wired.

    Returns a short status string. Safe to call on every server start.
    """
    remote = remote_url()
    if not remote:
        return "sync disabled"

    root_str = os.environ.get("OBSIDIAN_VAULT")
    if not root_str:
        raise SyncError("OBSIDIAN_VAULT is not set")
    root = Path(root_str).expanduser()

    if (root / ".git").is_dir():
        _configure_identity(root)
        return f"existing clone at {root}"

    if root.exists() and any(root.iterdir()):
        raise SyncError(
            f"{root} exists and is not a git repo; refusing to clone into a non-empty directory"
        )

    root.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", remote, str(root), cwd=root.parent)
    _configure_identity(root)
    return f"cloned {remote} -> {root}"


def pull() -> str:
    """Fetch and rebase from the remote. No-op if sync is disabled."""
    if not is_enabled():
        return "sync disabled"
    root = vault_root()
    result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
    message = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise SyncError(f"git pull failed: {message}")
    return message or "up to date"


def _has_changes(repo: Path) -> bool:
    result = _git("status", "--porcelain", cwd=repo)
    return bool(result.stdout.strip())


def commit_and_push(message: str) -> str:
    """Stage everything, commit (if there are changes), then push. Idempotent on clean tree."""
    if not is_enabled():
        return "sync disabled"
    root = vault_root()

    _git("add", "-A", cwd=root)
    if not _has_changes(root):
        return "nothing to commit"

    _git("commit", "-m", message, cwd=root)

    push = _git("push", cwd=root, check=False)
    if push.returncode == 0:
        return "pushed"

    pull_result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
    if pull_result.returncode != 0:
        raise SyncError(
            "push rejected and automatic rebase failed: "
            f"{(pull_result.stderr or pull_result.stdout).strip()}"
        )
    push = _git("push", cwd=root, check=False)
    if push.returncode != 0:
        raise SyncError(f"git push failed after rebase: {(push.stderr or push.stdout).strip()}")
    return "pushed (after rebase)"


def auto_commit(action: str, path: str) -> str:
    """Convenience wrapper used by the server after successful writes."""
    if not is_enabled() or not auto_push_enabled():
        return "auto-push off"
    return commit_and_push(f"obsidian-mcp: {action} {path}")
