"""The vault as a graph: tags, links, backlinks, orphans.

Follows Obsidian's conventions closely enough for navigation:

- Tags come from the frontmatter (``tags:`` / ``tag:``, as a list or a string) and from
  inline ``#tags`` in the body. Nested tags (``#philo/nietzsche``) are kept whole; a
  search for ``philo`` includes them by default.
- Links are wikilinks (``[[Note]]``, ``[[Note#Heading]]``, ``[[Note|alias]]``, embeds
  ``![[...]]``) and markdown links to local ``.md`` files.
- A link without a folder resolves by file name, preferring a note in the same folder,
  then the shortest path, like Obsidian's "shortest path" setting.
- Anything inside fenced code blocks or inline code is ignored.

Parsed notes are cached by modification time, so repeated calls on a big vault stay fast.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

import yaml

from .notes import list_notes
from .vault import VaultError, ensure_md_extension, resolve_inside_vault, vault_root

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(\n|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_INLINE_TAG_RE = re.compile(r"(?<![^\s(\[,])#([\w/-]+)")
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]|#^]*)([#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
_MDLINK_RE = re.compile(r"(!?)\[[^\]]*\]\(<?([^)<>\s]+)>?(?:\s+\"[^\"]*\")?\)")


@dataclass
class Link:
    target: str  # as written, without heading/alias
    line: int
    embed: bool
    resolved: str | None = None  # vault-relative path of the note, if it exists


@dataclass
class ParsedNote:
    path: str
    tags: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


_cache: dict[str, tuple[float, ParsedNote]] = {}
_cache_lock = threading.Lock()


def _blank(match: re.Match[str]) -> str:
    """Replace matched text with spaces, keeping newlines so line numbers survive."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _normalise_tag(raw: str) -> str | None:
    tag = raw.strip().lstrip("#").strip().strip("/")
    # Obsidian: a tag needs at least one non-numeric character.
    if not tag or tag.replace("/", "").isdigit():
        return None
    return tag


def _frontmatter_tags(block: str) -> list[str]:
    try:
        data = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("tags", data.get("tag"))
    if raw is None:
        return []
    items: list[Any] = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    out = []
    for item in items:
        if item is None:
            continue
        tag = _normalise_tag(str(item))
        if tag:
            out.append(tag)
    return out


def _parse(path: str, text: str) -> ParsedNote:
    note = ParsedNote(path=path, lines=text.splitlines())
    tags: list[str] = []

    body = text
    fm = _FRONTMATTER_RE.match(text)
    if fm:
        tags.extend(_frontmatter_tags(fm.group(1) or ""))
        body = _blank(fm) + text[fm.end():]

    body = _FENCE_RE.sub(_blank, body)
    body = _INLINE_CODE_RE.sub(_blank, body)

    for lineno, line in enumerate(body.splitlines(), start=1):
        for m in _INLINE_TAG_RE.finditer(line):
            tag = _normalise_tag(m.group(1))
            if tag:
                tags.append(tag)
        for m in _WIKILINK_RE.finditer(line):
            target = m.group(2).strip()
            if target:  # [[#Heading]] points inside the same note
                note.links.append(Link(target=target, line=lineno, embed=bool(m.group(1))))
        for m in _MDLINK_RE.finditer(line):
            target = unquote(m.group(2).split("#", 1)[0])
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
                continue  # external URL, mailto:, obsidian://...
            note.links.append(Link(target=target, line=lineno, embed=bool(m.group(1))))

    # Keep first-seen order, drop duplicates (case-insensitive like Obsidian).
    seen: set[str] = set()
    for tag in tags:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            note.tags.append(tag)
    return note


class VaultIndex:
    """A snapshot of every note's tags and links, with link resolution."""

    def __init__(self) -> None:
        root = vault_root()
        self.notes: dict[str, ParsedNote] = {}
        live: set[str] = set()
        for rel in list_notes():
            live.add(rel)
            abs_path = root / rel
            try:
                mtime = abs_path.stat().st_mtime
            except OSError:
                continue
            with _cache_lock:
                cached = _cache.get(rel)
            if cached and cached[0] == mtime:
                parsed = cached[1]
            else:
                try:
                    parsed = _parse(rel, abs_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue
                with _cache_lock:
                    _cache[rel] = (mtime, parsed)
            self.notes[rel] = parsed
        with _cache_lock:
            for stale in set(_cache) - live:
                del _cache[stale]

        self._by_path = {p[:-3].lower(): p for p in self.notes}
        self._by_name: dict[str, list[str]] = {}
        for p in self.notes:
            self._by_name.setdefault(PurePosixPath(p).stem.lower(), []).append(p)

        for note in self.notes.values():
            for link in note.links:
                link.resolved = self.resolve(link.target, from_note=note.path)

    def resolve(self, target: str, from_note: str | None = None) -> str | None:
        t = target.strip().replace("\\", "/")
        if t.lower().endswith(".md"):
            t = t[:-3]
        # Attachments (image.png, doc.pdf) simply won't match a note and resolve to None.
        key = t.lstrip("/").lower()

        if "/" in key:
            if from_note and not target.startswith("/"):
                rel = PurePosixPath(from_note).parent / key
                # Normalise "../" without touching the filesystem.
                parts: list[str] = []
                for part in rel.parts:
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part != ".":
                        parts.append(part)
                candidate = "/".join(parts).lower()
                if candidate in self._by_path:
                    return self._by_path[candidate]
            return self._by_path.get(key) or self._by_path.get(key.lstrip("./"))

        candidates = self._by_name.get(key, [])
        if not candidates:
            return None
        if len(candidates) == 1 or not from_note:
            return min(candidates, key=lambda p: (p.count("/"), p))
        folder = str(PurePosixPath(from_note).parent)
        same = [c for c in candidates if str(PurePosixPath(c).parent) == folder]
        return same[0] if same else min(candidates, key=lambda p: (p.count("/"), p))

    def note(self, path: str) -> ParsedNote:
        target = resolve_inside_vault(ensure_md_extension(path))
        rel = str(target.relative_to(vault_root()))
        if rel in self.notes:
            return self.notes[rel]
        # Accept a bare note name too, like a wikilink.
        resolved = self.resolve(path)
        if resolved:
            return self.notes[resolved]
        raise VaultError(f"Note not found: {path}")


# -- public API used by the server ------------------------------------------------------


def list_tags(prefix: str | None = None) -> list[dict[str, Any]]:
    index = VaultIndex()
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for note in index.notes.values():
        for tag in note.tags:
            counts[tag.lower()] += 1
            display.setdefault(tag.lower(), tag)
    p = prefix.lstrip("#").lower() if prefix else None
    rows = [
        {"tag": display[k], "count": n}
        for k, n in counts.items()
        if p is None or k == p or k.startswith(p + "/")
    ]
    rows.sort(key=lambda r: (-r["count"], r["tag"].lower()))
    return rows


def notes_with_tag(tag: str, include_nested: bool = True) -> list[str]:
    wanted = tag.lstrip("#").lower().strip("/")
    if not wanted:
        raise VaultError("tag is empty")
    index = VaultIndex()
    out = []
    for path, note in index.notes.items():
        for t in note.tags:
            lt = t.lower()
            if lt == wanted or (include_nested and lt.startswith(wanted + "/")):
                out.append(path)
                break
    return sorted(out)


def backlinks(path: str) -> dict[str, Any]:
    index = VaultIndex()
    target = index.note(path).path
    hits = []
    for source, note in index.notes.items():
        if source == target:
            continue
        for link in note.links:
            if link.resolved == target:
                context = note.lines[link.line - 1].strip() if link.line <= len(note.lines) else ""
                hits.append({"source": source, "line": link.line, "context": context[:240]})
    return {"path": target, "count": len(hits), "backlinks": hits}


def outgoing_links(path: str) -> dict[str, Any]:
    index = VaultIndex()
    note = index.note(path)
    links = [asdict(link) for link in note.links]
    return {
        "path": note.path,
        "links": links,
        "unresolved": sorted({l["target"] for l in links if l["resolved"] is None}),
    }


def find_orphans(include_without_outgoing: bool = False) -> list[str]:
    """Notes nothing links to. With `include_without_outgoing`, only those that also
    link to nothing (completely isolated)."""
    index = VaultIndex()
    linked: set[str] = set()
    for note in index.notes.values():
        for link in note.links:
            if link.resolved and link.resolved != note.path:
                linked.add(link.resolved)
    orphans = []
    for path, note in index.notes.items():
        if path in linked:
            continue
        if include_without_outgoing and any(l.resolved for l in note.links):
            continue
        orphans.append(path)
    return sorted(orphans)


def overview(top_tags: int = 25) -> dict[str, Any]:
    """A map of the vault, meant as Claude's first call in a conversation."""
    index = VaultIndex()
    folders: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    incoming: Counter[str] = Counter()
    for path, note in index.notes.items():
        parent = str(PurePosixPath(path).parent)
        folders["/" if parent == "." else parent] += 1
        for link in note.links:
            if link.resolved is None:
                if not PurePosixPath(link.target).suffix or link.target.lower().endswith(".md"):
                    unresolved[link.target] += 1
            elif link.resolved != path:
                incoming[link.resolved] += 1

    tags = list_tags()
    return {
        "note_count": len(index.notes),
        "folders": [{"folder": f, "notes": n} for f, n in sorted(folders.items())],
        "top_tags": tags[:top_tags],
        "tag_count": len(tags),
        "most_linked": [{"path": p, "backlinks": n} for p, n in incoming.most_common(15)],
        "orphan_count": sum(1 for p in index.notes if p not in incoming),
        "unresolved_links": [{"target": t, "count": n} for t, n in unresolved.most_common(15)],
    }
