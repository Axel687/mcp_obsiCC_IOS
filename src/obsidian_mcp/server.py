"""MCP server entrypoint — registers tools over stdio using the FastMCP helper."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import graphs, notes, pdf
from .notes import embed_attachment
from .vault import VaultError

mcp = FastMCP("obsidian-mcp")


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "type": exc.__class__.__name__}


@mcp.tool()
def list_notes(subdir: str | None = None) -> dict[str, Any]:
    """List all markdown notes in the vault (optionally scoped to a subdirectory)."""
    try:
        return {"ok": True, "notes": notes.list_notes(subdir)}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def read_note(path: str) -> dict[str, Any]:
    """Read a note's full markdown content. `path` is vault-relative; the `.md` suffix is optional."""
    try:
        return {"ok": True, "path": path, "content": notes.read_note(path)}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def write_note(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Create a note (or overwrite if `overwrite=true`). Creates parent directories as needed."""
    try:
        written = notes.write_note(path, content, overwrite=overwrite)
        return {"ok": True, "path": written}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def append_note(path: str, content: str) -> dict[str, Any]:
    """Append text to the end of a note (creates the note if it doesn't exist)."""
    try:
        written = notes.append_note(path, content)
        return {"ok": True, "path": written}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def insert_under_heading(
    path: str,
    heading: str,
    content: str,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    """Insert text at the end of the section whose heading matches `heading` exactly.

    If the heading isn't present and `create_if_missing` is true, a new `## heading`
    section is appended with the content underneath.
    """
    try:
        written = notes.insert_under_heading(
            path, heading, content, create_if_missing=create_if_missing
        )
        return {"ok": True, "path": written}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def search_notes(query: str, regex: bool = False, max_results: int = 200) -> dict[str, Any]:
    """Search note contents. Default is case-insensitive substring; pass `regex=true` for regex."""
    try:
        hits = notes.search_notes(query, regex=regex, max_results=max_results)
        return {"ok": True, "count": len(hits), "hits": [asdict(h) for h in hits]}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def delete_note(path: str) -> dict[str, Any]:
    """Delete a note from the vault."""
    try:
        return {"ok": True, "deleted": notes.delete_note(path)}
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def read_pdf(
    path: str,
    page_start: int | None = None,
    page_end: int | None = None,
    joined: bool = True,
) -> dict[str, Any]:
    """Extract text from a PDF stored in the vault. Pages are 1-indexed, inclusive.

    Set `joined=false` to receive per-page strings instead of a single concatenated text.
    """
    try:
        extract = pdf.read_pdf(path, page_start=page_start, page_end=page_end)
        payload: dict[str, Any] = {
            "ok": True,
            "path": extract.path,
            "page_count": extract.page_count,
        }
        if joined:
            payload["text"] = extract.joined()
        else:
            payload["pages"] = extract.pages
        return payload
    except VaultError as exc:
        return _err(exc)


@mcp.tool()
def generate_graph(
    code: str,
    filename: str | None = None,
    dpi: int = 150,
    timeout: float | None = None,
    embed_in_note: str | None = None,
    alt_text: str | None = None,
) -> dict[str, Any]:
    """Run Python (matplotlib) code and save the resulting figure inside the vault.

    The user code has `plt` (matplotlib.pyplot) pre-imported and can optionally assign a
    `fig` variable. If `embed_in_note` is provided, an Obsidian-style image link is
    appended to that note.
    """
    try:
        result = graphs.generate_graph(code, filename=filename, dpi=dpi, timeout=timeout)
        payload: dict[str, Any] = {
            "ok": True,
            "path": result.path,
            "absolute_path": result.absolute_path,
        }
        if embed_in_note:
            embed_attachment(embed_in_note, result.path, alt_text=alt_text)
            payload["embedded_in"] = embed_in_note
        return payload
    except VaultError as exc:
        return _err(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
