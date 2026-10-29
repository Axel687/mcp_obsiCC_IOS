# obsidian-mcp

An MCP (Model Context Protocol) server that turns an Obsidian vault into a "second brain"
Claude can read and write — from Claude Code on your computer, and from claude.ai and
the Claude iPhone/iPad/Android apps through a self-hosted server.

## Features

- **Notes** — list, read, create, overwrite, append, insert under a specific heading,
  search (substring or regex), delete.
- **Graph of the vault** — overview, tags (frontmatter and inline, nested tags),
  backlinks, outgoing links with resolution, unresolved links, orphan notes.
- **Git sync** — the vault is a Git repo shared with Obsidian Git on your devices; the
  server pulls before working and commits + pushes after every write.
- **PDFs** — extract text from any PDF inside the vault, with optional page ranges.
- **Graphs** — run matplotlib code and save the figure into the vault (local mode only by
  default).

Every path is resolved relative to the vault root and checked so it cannot escape it.

## Two ways to run it

| | `stdio` (default) | `http` |
| --- | --- | --- |
| Where it runs | Launched by Claude Code on your machine | On its own, e.g. Docker on a VPS |
| Who can use it | Claude Code on that machine | claude.ai, desktop & **mobile apps**, Claude Code anywhere |
| Authentication | None needed (nothing is exposed) | **Mandatory**: OAuth with a password page, plus an optional static token |
| `generate_graph` | On | Off unless `OBSIDIAN_ENABLE_GRAPHS=true` |

The mobile apps can only use remote servers, and connectors are added from claude.ai on
the web — they then show up automatically in the apps.

```
 iPhone / iPad / PC                        GitHub (private)              VPS
┌──────────────────┐   push / pull   ┌──────────────────┐  pull/push  ┌────────────────────┐
│ Obsidian + Git   │ ◀─────────────▶ │   vault repo     │ ◀─────────▶ │ obsidian-mcp (http)│
└──────────────────┘                 └──────────────────┘             │ behind Caddy/HTTPS │
                                                                      └─────────▲──────────┘
 Claude app (iOS) / claude.ai / Claude Code  ── MCP over HTTPS + OAuth ───────────┘
```

## Remote deployment (VPS)

Prerequisites: a VPS with Docker, a domain or subdomain whose DNS points at it, ports
80 and 443 open.

1. **Vault repo.** Create a private GitHub repo for the vault and push your vault to it
   (see "Obsidian side" below).
2. **Git token.** Create a fine-grained GitHub token restricted to that repo, permission
   *Contents: read and write*.
3. **Configure.**
   ```bash
   git clone https://github.com/axel687/mcp_obsiCC_IOS.git && cd mcp_obsiCC_IOS
   cp .env.example .env
   nano .env   # domain, password, vault repo URL, git token
   ```
4. **Start.**
   ```bash
   docker compose up -d --build
   docker compose logs -f obsidian-mcp   # should show "cloned ..." then "serving MCP on ..."
   ```
   Caddy fetches the HTTPS certificate automatically. `https://<domain>/healthz` answers `ok`.
5. **Connect Claude.** On claude.ai (Safari works): *Settings → Connectors → Add custom
   connector*, URL `https://<domain>/mcp`. A page from your server asks for the password;
   after that the connector is available on every device, including the mobile apps.
6. **Claude Code (optional).** Either the same OAuth flow, or with the static token:
   ```bash
   claude mcp add --transport http obsidian https://<domain>/mcp \
     --header "Authorization: Bearer <OBSIDIAN_MCP_TOKEN>"
   ```

Update later with `git pull && docker compose up -d --build`. OAuth tokens are kept in
the `state` volume, so Claude doesn't need to reconnect after a redeploy.

### How authentication works

- claude.ai discovers the server's OAuth endpoints, registers itself, and opens
  `/authorize`. The server shows a password page; on success it hands Claude a one-time
  code exchanged for an access token (1 hour) and a refresh token (30 days, rotated on
  every use).
- Only redirects to claude.ai/claude.com and to loopback addresses (Claude Code) are
  accepted (`OBSIDIAN_MCP_ALLOWED_REDIRECTS` to change).
- After 5 wrong passwords in 15 minutes, the login is locked for 15 minutes.
- Tokens are stored hashed (SHA-256) in `/data/state/oauth.json`. Deleting that file
  logs every client out.
- The Git token reaches git through a credential helper reading the environment: it is
  never written into `.git/config` or the remote URL, and is not visible to
  `generate_graph` code.

## Obsidian side (iPhone, iPad, PC)

1. Install the community plugin **Git** (by Vinzent) in Obsidian on each device.
2. Authenticate with your GitHub username and a token for the vault repo (on mobile the
   plugin uses its own HTTPS implementation, no SSH).
3. Suggested settings: *Pull on startup* on, *Auto pull interval* 2 min,
   *Commit-and-sync interval* 5 min.
4. First device: push the existing vault. Other devices: *Git: Clone an existing remote
   repo*.

### Keeping conflicts rare

- The server pulls before tool calls (at most every `OBSIDIAN_PULL_INTERVAL` seconds)
  and pushes after each write; a rejected push is rebased and retried.
- If a rebase hits a real conflict (same lines edited on both sides), the server aborts
  the rebase so it stays usable, keeps its commit locally and reports `sync_error`.
  Resolve on a device, push, and the next write or `sync_vault` goes through.
- Best habit: let Claude write to an inbox note or append to sections
  (`append_note`, `insert_under_heading`) rather than rewriting notes you're editing.

## Local mode (Claude Code, stdio)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Claude Code MCP config (see `config.example.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["-m", "obsidian_mcp"],
      "env": { "OBSIDIAN_VAULT": "/absolute/path/to/your/Vault" }
    }
  }
}
```

Add `OBSIDIAN_VAULT_REMOTE` (and `OBSIDIAN_GIT_TOKEN` if needed) to use the same Git sync
locally, e.g. from a cloud sandbox.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OBSIDIAN_VAULT` | *(required)* | Absolute path to the vault (cloned there if Git sync is on) |
| `OBSIDIAN_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `OBSIDIAN_MCP_PUBLIC_URL` | — | HTTP mode: public base URL, e.g. `https://mcp.example.com` |
| `OBSIDIAN_MCP_PASSWORD` | — | HTTP mode: login password (12+ characters) |
| `OBSIDIAN_MCP_TOKEN` | *(unset)* | HTTP mode: optional static bearer token (32+ characters) |
| `OBSIDIAN_MCP_HOST` / `OBSIDIAN_MCP_PORT` | `0.0.0.0` / `8000` | HTTP listen address |
| `OBSIDIAN_MCP_STATE_DIR` | `~/.local/state/obsidian-mcp` | Where OAuth clients and tokens are stored |
| `OBSIDIAN_MCP_ALLOWED_REDIRECTS` | claude.ai, claude.com, loopback | Comma-separated OAuth redirect allowlist |
| `OBSIDIAN_ENABLE_GRAPHS` | on in stdio, off in http | Expose `generate_graph` |
| `OBSIDIAN_VAULT_REMOTE` | *(unset)* | Git URL of the vault repo — enables sync |
| `OBSIDIAN_GIT_TOKEN` | *(unset)* | Token for HTTPS git auth (e.g. GitHub fine-grained PAT) |
| `OBSIDIAN_PULL_INTERVAL` | `30` | Min seconds between automatic pulls; `0` = every call, negative = never |
| `OBSIDIAN_AUTO_PUSH` | `true` | Commit + push after every write |
| `OBSIDIAN_GIT_NAME` / `OBSIDIAN_GIT_EMAIL` | `Obsidian MCP` / `obsidian-mcp@local` | Commit identity |
| `OBSIDIAN_ATTACHMENTS_DIR` | `attachments` | Vault-relative folder for attachments |
| `OBSIDIAN_GRAPHS_DIR` | `attachments/graphs` | Where generated graphs are written |
| `OBSIDIAN_GRAPH_TIMEOUT` | `30` | Seconds before graph subprocesses are killed |

## Tools

| Tool | What it does |
| --- | --- |
| `vault_overview()` | Note count, folders, top tags, most linked notes, orphans, unresolved links. |
| `list_notes(subdir?)` | List every `.md` file in the vault (or under a subdir). |
| `read_note(path)` | Return a note's full markdown content. |
| `search_notes(query, regex=false, max_results=200)` | Search note bodies. |
| `list_tags(prefix?)` | Tags with their note counts; `prefix` narrows to a tag and its nested tags. |
| `notes_with_tag(tag, include_nested=true)` | Notes carrying a tag. |
| `get_backlinks(path)` | Notes linking to this one, with the line of each link. |
| `get_links(path)` | Outgoing links, resolved to paths; lists links to missing notes. |
| `find_orphans(include_without_outgoing=false)` | Notes nothing links to. |
| `write_note(path, content, overwrite=false)` | Create (or overwrite) a note. |
| `append_note(path, content)` | Append text to a note, creating it if needed. |
| `insert_under_heading(path, heading, content, create_if_missing=true)` | Insert text at the end of a section. |
| `delete_note(path)` | Delete a note (committed, so recoverable from Git history). |
| `read_pdf(path, page_start?, page_end?, joined=true)` | Extract PDF text. |
| `sync_vault(action="both")` | Manually pull/push/both. No-op if Git sync is off. |
| `generate_graph(code, filename?, dpi=150, timeout?, embed_in_note?, alt_text?)` | Run matplotlib code and save a figure (see below). |

Links follow Obsidian's rules: `[[Note]]`, `[[Note#Heading]]`, `[[Note|alias]]`, embeds
`![[...]]` and markdown links to `.md` files; a bare name resolves to the note in the same
folder first, then the shortest path. Tags and links inside code are ignored.

### Graph generation contract

The code passed to `generate_graph` runs in a fresh Python subprocess (isolated mode,
timeout, no secrets in its environment) with `matplotlib.pyplot` imported as `plt`. The
server saves whichever figure you leave open, or the variable `fig` if you assign one.

```python
import numpy as np
x = np.linspace(0, 2 * np.pi, 200)
plt.plot(x, np.sin(x), label="sin")
plt.legend()
```

With `embed_in_note`, a `![alt](attachments/graphs/...)` line is appended to that note.

This is still arbitrary code execution: only enable it on a server you're comfortable
letting Claude run code on.

## Development

```bash
pip install -e . pytest
pytest
```

`tests/test_http.py` starts a real server and walks through the OAuth flow claude.ai
uses, against a local Git remote standing in for GitHub.
