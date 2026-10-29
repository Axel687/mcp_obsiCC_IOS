"""MCP server entrypoint.

Runs over stdio (Claude Code on the same machine) or Streamable HTTP (a VPS reachable
by claude.ai, the mobile apps and Claude Code), depending on
``OBSIDIAN_MCP_TRANSPORT``. See ``config.py`` for every setting.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import graphs, links, notes, pdf, sync
from .config import ConfigError, Settings, load_settings
from .notes import embed_attachment
from .vault import VaultError

INSTRUCTIONS = """\
This server is the user's Obsidian vault, their "second brain". Paths are relative to
the vault root; the `.md` suffix is optional. Good habits:
- Start with `vault_overview` to see folders, main tags and the most-linked notes.
- Follow the graph with `get_backlinks` / `get_links` and `notes_with_tag` rather than
  only full-text search.
- Read a note before rewriting it, and prefer `append_note` or `insert_under_heading`
  over `write_note(overwrite=true)`: the user may be editing the same note on another
  device. Use wikilinks ([[Note]]) when referring to other notes.
"""


def _log(message: str) -> None:
    print(f"[obsidian-mcp] {message}", file=sys.stderr, flush=True)


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "type": exc.__class__.__name__}


def _refresh(payload: dict[str, Any]) -> None:
    """Pull changes from other devices if the local copy may be stale.

    A failed pull must not block reading: the tool still answers from the local copy
    and reports the problem.
    """
    try:
        sync.maybe_pull()
    except VaultError as exc:
        payload["sync_warning"] = f"could not pull latest changes, answering from local copy: {exc}"


def _read(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    _refresh(payload)
    try:
        result = fn()
    except VaultError as exc:
        return {**_err(exc), **payload}
    return {"ok": True, **result, **payload}


@contextmanager
def _writing() -> Iterator[dict[str, Any]]:
    """Hold the git lock for pull → write → commit → push, so a background pull can't
    rebase under a half-written file."""
    payload: dict[str, Any] = {}
    with sync.lock:
        _refresh(payload)
        yield payload


def _with_sync(action: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        status = sync.auto_commit(action, path)
    except VaultError as exc:
        payload["sync_error"] = str(exc)
    else:
        if status not in ("auto-push off", "sync disabled", "nothing to commit"):
            payload["sync"] = status
    return payload


# -- notes ---------------------------------------------------------------------------


def list_notes(subdir: str | None = None) -> dict[str, Any]:
    """List all markdown notes in the vault (optionally scoped to a subdirectory)."""
    return _read(lambda: {"notes": notes.list_notes(subdir)})


def read_note(path: str) -> dict[str, Any]:
    """Read a note's full markdown content. `path` is vault-relative; the `.md` suffix is optional."""
    return _read(lambda: {"path": path, "content": notes.read_note(path)})


def write_note(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Create a note (or overwrite if `overwrite=true`). Creates parent directories as needed."""
    with _writing() as extra:
        try:
            written = notes.write_note(path, content, overwrite=overwrite)
            return _with_sync("write", written, {"ok": True, "path": written, **extra})
        except VaultError as exc:
            return {**_err(exc), **extra}


def append_note(path: str, content: str) -> dict[str, Any]:
    """Append text to the end of a note (creates the note if it doesn't exist)."""
    with _writing() as extra:
        try:
            written = notes.append_note(path, content)
            return _with_sync("append", written, {"ok": True, "path": written, **extra})
        except VaultError as exc:
            return {**_err(exc), **extra}


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
    with _writing() as extra:
        try:
            written = notes.insert_under_heading(
                path, heading, content, create_if_missing=create_if_missing
            )
            return _with_sync("insert", written, {"ok": True, "path": written, **extra})
        except VaultError as exc:
            return {**_err(exc), **extra}


def search_notes(query: str, regex: bool = False, max_results: int = 200) -> dict[str, Any]:
    """Search note contents. Default is case-insensitive substring; pass `regex=true` for regex."""

    def run() -> dict[str, Any]:
        hits = notes.search_notes(query, regex=regex, max_results=max_results)
        return {"count": len(hits), "hits": [asdict(h) for h in hits]}

    return _read(run)


def delete_note(path: str) -> dict[str, Any]:
    """Delete a note from the vault. The deletion is committed, so it can be undone from Git history."""
    with _writing() as extra:
        try:
            deleted = notes.delete_note(path)
            return _with_sync("delete", deleted, {"ok": True, "deleted": deleted, **extra})
        except VaultError as exc:
            return {**_err(exc), **extra}


def sync_vault(action: str = "both") -> dict[str, Any]:
    """Manually sync the vault with its Git remote.

    `action` can be "pull", "push", or "both" (default). No-op if OBSIDIAN_VAULT_REMOTE
    isn't configured. Reads already pull automatically every few seconds.
    """
    if not sync.is_enabled():
        return {"ok": True, "enabled": False, "note": "OBSIDIAN_VAULT_REMOTE not set"}
    action = action.lower()
    if action not in {"pull", "push", "both"}:
        return _err(VaultError(f"unknown action {action!r} (expected pull|push|both)"))
    result: dict[str, Any] = {"ok": True, "enabled": True}
    try:
        if action in {"pull", "both"}:
            result["pull"] = sync.pull()
        if action in {"push", "both"}:
            result["push"] = sync.commit_and_push("obsidian-mcp: manual sync")
    except VaultError as exc:
        return _err(exc)
    return result


# -- graph of the vault: tags, links ---------------------------------------------------


def vault_overview() -> dict[str, Any]:
    """Map of the vault: note count, folders, most used tags, most linked notes, orphans
    and unresolved links. A good first call to get oriented."""
    return _read(links.overview)


def list_tags(prefix: str | None = None) -> dict[str, Any]:
    """List tags with how many notes use each (frontmatter and inline #tags).

    With `prefix="philo"`, only `#philo` and nested tags like `#philo/nietzsche`.
    """
    return _read(lambda: {"tags": links.list_tags(prefix)})


def notes_with_tag(tag: str, include_nested: bool = True) -> dict[str, Any]:
    """Notes carrying a tag. `#` is optional; nested tags (`tag/sub`) are included by default."""

    def run() -> dict[str, Any]:
        found = links.notes_with_tag(tag, include_nested=include_nested)
        return {"tag": tag.lstrip("#"), "count": len(found), "notes": found}

    return _read(run)


def get_backlinks(path: str) -> dict[str, Any]:
    """Notes that link to this one (wikilinks, embeds and markdown links), with the line
    containing each link. Accepts a vault path or a bare note name."""
    return _read(lambda: links.backlinks(path))


def get_links(path: str) -> dict[str, Any]:
    """Links going out of a note, each resolved to a vault path when the target exists;
    `unresolved` lists links to notes that don't exist yet."""
    return _read(lambda: links.outgoing_links(path))


def find_orphans(include_without_outgoing: bool = False) -> dict[str, Any]:
    """Notes no other note links to. With `include_without_outgoing=true`, only notes
    that are completely isolated (no links in either direction)."""

    def run() -> dict[str, Any]:
        found = links.find_orphans(include_without_outgoing)
        return {"count": len(found), "notes": found}

    return _read(run)


# -- attachments -----------------------------------------------------------------------


def read_pdf(
    path: str,
    page_start: int | None = None,
    page_end: int | None = None,
    joined: bool = True,
) -> dict[str, Any]:
    """Extract text from a PDF stored in the vault. Pages are 1-indexed, inclusive.

    Set `joined=false` to receive per-page strings instead of a single concatenated text.
    """

    def run() -> dict[str, Any]:
        extract = pdf.read_pdf(path, page_start=page_start, page_end=page_end)
        out: dict[str, Any] = {"path": extract.path, "page_count": extract.page_count}
        if joined:
            out["text"] = extract.joined()
        else:
            out["pages"] = extract.pages
        return out

    return _read(run)


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
    with _writing() as extra:
        try:
            result = graphs.generate_graph(code, filename=filename, dpi=dpi, timeout=timeout)
            payload: dict[str, Any] = {"ok": True, "path": result.path, **extra}
            if embed_in_note:
                embed_attachment(embed_in_note, result.path, alt_text=alt_text)
                payload["embedded_in"] = embed_in_note
            return _with_sync("graph", result.path, payload)
        except VaultError as exc:
            return {**_err(exc), **extra}


# -- assembly --------------------------------------------------------------------------

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

_TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = [
    (vault_overview, _READ),
    (list_notes, _READ),
    (read_note, _READ),
    (search_notes, _READ),
    (list_tags, _READ),
    (notes_with_tag, _READ),
    (get_backlinks, _READ),
    (get_links, _READ),
    (find_orphans, _READ),
    (read_pdf, _READ),
    (append_note, _ADD),
    (insert_under_heading, _ADD),
    (write_note, _CHANGE),
    (delete_note, _CHANGE),
    (sync_vault, _ADD),
]


def build_server(settings: Settings) -> MCPServer:
    kwargs: dict[str, Any] = {}
    provider = None
    if settings.is_http:
        from .auth import VaultOAuthProvider

        provider = VaultOAuthProvider(settings)
        kwargs["auth_server_provider"] = provider
        kwargs["auth"] = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # Single-user server: every token we issue is for this server anyway.
            validate_token_resource=False,
        )

    server = MCPServer("obsidian-mcp", instructions=INSTRUCTIONS, **kwargs)
    for fn, annotations in _TOOLS:
        server.add_tool(fn, annotations=annotations)
    if settings.enable_graphs:
        server.add_tool(generate_graph, annotations=_ADD)

    if provider is not None:
        from .auth import install_login_routes

        install_login_routes(server, provider)
    return server


def _bootstrap_sync() -> None:
    """Clone (if needed) and pull before serving any requests."""
    if not sync.is_enabled():
        return
    try:
        _log(sync.ensure_clone())
        _log(f"pull: {sync.pull()}")
    except VaultError as exc:
        _log(f"sync bootstrap failed: {exc}")


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        _log(str(exc))
        sys.exit(2)

    _bootstrap_sync()
    server = build_server(settings)

    if not settings.is_http:
        server.run()
        return

    _log(f"serving MCP on {settings.mcp_url} (listening on {settings.host}:{settings.port})")
    if not settings.enable_graphs:
        _log("generate_graph disabled (set OBSIDIAN_ENABLE_GRAPHS=true to enable)")
    server.run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        # TLS is terminated by the reverse proxy, which also fixes the Host header;
        # access control is the OAuth layer, not the Host check.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


if __name__ == "__main__":
    main()
