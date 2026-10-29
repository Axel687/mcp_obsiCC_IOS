"""Git-backed sync for the vault.

Enabled whenever `OBSIDIAN_VAULT_REMOTE` is set. On MCP startup the vault is cloned
(or pulled). Before tools run, :func:`maybe_pull` fetches changes pushed from your
other devices (at most every `OBSIDIAN_PULL_INTERVAL` seconds), so a long-running
server never works on a stale copy. After each write the server calls
:func:`commit_and_push` to mirror the change back to the remote. Push conflicts trigger
an automatic pull-rebase and retry.

All git operations are serialised by a single lock: tools run in worker threads and
two concurrent `git` processes on one repository would trip over `index.lock`.

Authentication: set `OBSIDIAN_GIT_TOKEN` (e.g. a fine-grained GitHub token limited to
the vault repo). It is handed to git through a credential helper that reads the
environment, so it never ends up in the remote URL or in `.git/config`.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .vault import VaultError, vault_root


class SyncError(VaultError):
    pass


# Re-entrant: commit_and_push() calls pull logic while already holding it.
lock = threading.RLock()
_last_pull = 0.0

_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; echo username=x-access-token; '
    'echo "password=$OBSIDIAN_GIT_TOKEN"; }; f'
)


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
    prefix: list[str] = []
    if os.environ.get("OBSIDIAN_GIT_TOKEN"):
        # The empty value resets any helper inherited from the global config.
        prefix = ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", *prefix, *args], cwd=str(cwd), capture_output=True, text=True, check=False, env=env
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


def _abort_rebase_if_needed(repo: Path) -> None:
    """Leave the working tree usable if a rebase stopped on a conflict.

    Without this, a single conflicting edit would leave the vault mid-rebase and
    every later write and pull would fail until someone fixed it by hand.
    """
    git_dir = repo / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        _git("rebase", "--abort", cwd=repo, check=False)


def _pull_locked(root: Path) -> str:
    global _last_pull
    result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
    message = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        _abort_rebase_if_needed(root)
        raise SyncError(f"git pull failed: {message}")
    _last_pull = time.monotonic()
    return message or "up to date"


def pull() -> str:
    """Fetch and rebase from the remote. No-op if sync is disabled."""
    if not is_enabled():
        return "sync disabled"
    root = vault_root()
    with lock:
        return _pull_locked(root)


def pull_interval() -> float:
    try:
        return float(os.environ.get("OBSIDIAN_PULL_INTERVAL", "30"))
    except ValueError:
        return 30.0


def maybe_pull() -> str | None:
    """Pull if the last successful pull is older than `OBSIDIAN_PULL_INTERVAL` seconds.

    Returns ``None`` when nothing was attempted. A negative interval disables it.
    """
    interval = pull_interval()
    if not is_enabled() or interval < 0:
        return None
    if time.monotonic() - _last_pull < interval:
        return None
    root = vault_root()
    with lock:
        # Another thread may have pulled while we waited for the lock.
        if time.monotonic() - _last_pull < interval:
            return None
        return _pull_locked(root)


def _has_changes(repo: Path) -> bool:
    result = _git("status", "--porcelain", cwd=repo)
    return bool(result.stdout.strip())


def commit_and_push(message: str) -> str:
    """Stage everything, commit (if there are changes), then push. Idempotent on clean tree."""
    global _last_pull
    if not is_enabled():
        return "sync disabled"
    root = vault_root()

    with lock:
        _git("add", "-A", cwd=root)
        if not _has_changes(root):
            return "nothing to commit"

        _git("commit", "-m", message, cwd=root)

        push = _git("push", cwd=root, check=False)
        if push.returncode == 0:
            return "pushed"

        pull_result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
        if pull_result.returncode != 0:
            _abort_rebase_if_needed(root)
            raise SyncError(
                "push rejected and automatic rebase failed (your change is committed locally "
                "and will be pushed once the conflict is resolved on another device): "
                f"{(pull_result.stderr or pull_result.stdout).strip()}"
            )
        push = _git("push", cwd=root, check=False)
        if push.returncode != 0:
            raise SyncError(f"git push failed after rebase: {(push.stderr or push.stdout).strip()}")
        _last_pull = time.monotonic()
        return "pushed (after rebase)"


def auto_commit(action: str, path: str) -> str:
    """Convenience wrapper used by the server after successful writes."""
    if not is_enabled() or not auto_push_enabled():
        return "auto-push off"
    return commit_and_push(f"obsidian-mcp: {action} {path}")
