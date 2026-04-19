"""Vault path helpers — every user-provided path is resolved through here."""

from __future__ import annotations

import os
from pathlib import Path


class VaultError(Exception):
    pass


def vault_root() -> Path:
    raw = os.environ.get("OBSIDIAN_VAULT")
    if not raw:
        raise VaultError(
            "OBSIDIAN_VAULT is not set. Point it at the absolute path of your Obsidian vault."
        )
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise VaultError(f"OBSIDIAN_VAULT does not exist or is not a directory: {root}")
    return root


def attachments_dir() -> Path:
    rel = os.environ.get("OBSIDIAN_ATTACHMENTS_DIR", "attachments")
    return (vault_root() / rel).resolve()


def graphs_dir() -> Path:
    rel = os.environ.get("OBSIDIAN_GRAPHS_DIR", "attachments/graphs")
    return (vault_root() / rel).resolve()


def resolve_inside_vault(relative: str) -> Path:
    """Resolve a vault-relative path and refuse anything that escapes the vault."""
    if not relative or relative.strip() == "":
        raise VaultError("Path is empty.")
    root = vault_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise VaultError(
            f"Path escapes the vault: {relative!r} resolves to {candidate}"
        ) from exc
    return candidate


def ensure_md_extension(relative: str) -> str:
    return relative if relative.lower().endswith(".md") else relative + ".md"


def relative_to_vault(path: Path) -> str:
    return str(path.resolve().relative_to(vault_root()))
