"""Note operations: list, read, write, append, insert-under-heading, search, delete."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .vault import (
    VaultError,
    ensure_md_extension,
    relative_to_vault,
    resolve_inside_vault,
    vault_root,
)


@dataclass
class SearchHit:
    path: str
    line: int
    preview: str


def list_notes(subdir: str | None = None) -> list[str]:
    base = resolve_inside_vault(subdir) if subdir else vault_root()
    if not base.is_dir():
        raise VaultError(f"Not a directory: {base}")
    out: list[str] = []
    for p in base.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(vault_root()).parts):
            continue
        out.append(relative_to_vault(p))
    out.sort()
    return out


def read_note(path: str) -> str:
    target = resolve_inside_vault(ensure_md_extension(path))
    if not target.is_file():
        raise VaultError(f"Note not found: {path}")
    return target.read_text(encoding="utf-8")


def write_note(path: str, content: str, overwrite: bool = False) -> str:
    target = resolve_inside_vault(ensure_md_extension(path))
    if target.exists() and not overwrite:
        raise VaultError(
            f"Note already exists: {path}. Pass overwrite=true to replace it."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return relative_to_vault(target)


def append_note(path: str, content: str, ensure_newline: bool = True) -> str:
    target = resolve_inside_vault(ensure_md_extension(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if ensure_newline and existing and not existing.endswith("\n"):
            prefix = "\n"
    with target.open("a", encoding="utf-8") as fh:
        if prefix:
            fh.write(prefix)
        fh.write(content)
        if ensure_newline and not content.endswith("\n"):
            fh.write("\n")
    return relative_to_vault(target)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def insert_under_heading(
    path: str, heading: str, content: str, create_if_missing: bool = True
) -> str:
    """Insert `content` at the end of the section matching `heading` (exact text match).

    If the heading doesn't exist, append it (and the content) at the end of the note when
    `create_if_missing` is true; otherwise raise.
    """
    target = resolve_inside_vault(ensure_md_extension(path))
    if not target.exists():
        if not create_if_missing:
            raise VaultError(f"Note not found: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    lines = target.read_text(encoding="utf-8").splitlines(keepends=False)
    heading_norm = heading.strip()

    start_idx: int | None = None
    start_level: int | None = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(2).strip() == heading_norm:
            start_idx = i
            start_level = len(m.group(1))
            break

    block = content.splitlines(keepends=False)
    if not block or block[-1] != "":
        block.append("")

    if start_idx is None:
        if not create_if_missing:
            raise VaultError(f"Heading not found: {heading!r}")
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"## {heading_norm}")
        lines.append("")
        lines.extend(block)
    else:
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            m = _HEADING_RE.match(lines[j])
            if m and len(m.group(1)) <= (start_level or 6):
                end_idx = j
                break
        insert_at = end_idx
        while insert_at > start_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines[insert_at:insert_at] = [""] + block

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return relative_to_vault(target)


def search_notes(query: str, regex: bool = False, max_results: int = 200) -> list[SearchHit]:
    if not query:
        raise VaultError("query is empty")
    pattern = re.compile(query) if regex else re.compile(re.escape(query), re.IGNORECASE)
    hits: list[SearchHit] = []
    for note_rel in list_notes():
        note_path = resolve_inside_vault(note_rel)
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(SearchHit(path=note_rel, line=lineno, preview=line.strip()[:240]))
                if len(hits) >= max_results:
                    return hits
    return hits


def delete_note(path: str) -> str:
    target = resolve_inside_vault(ensure_md_extension(path))
    if not target.is_file():
        raise VaultError(f"Note not found: {path}")
    rel = relative_to_vault(target)
    target.unlink()
    return rel


def embed_attachment(note_path: str, attachment_rel: str, alt_text: str | None = None) -> str:
    """Append an Obsidian-style embed link for an attachment to a note."""
    alt = alt_text or Path(attachment_rel).stem
    link = f"![{alt}]({attachment_rel.replace(' ', '%20')})"
    return append_note(note_path, link)
