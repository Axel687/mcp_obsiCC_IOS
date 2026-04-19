# obsidian-mcp

An MCP (Model Context Protocol) server that connects Claude Code — or any MCP client — to
an Obsidian vault. It exposes tools to read and modify notes, extract text from PDFs
stored in the vault, and generate matplotlib graphs that are saved straight into the
vault (optionally embedded into a note).

## Features

- **Notes** — list, read, create, overwrite, append, insert under a specific heading,
  search (substring or regex), delete.
- **PDFs** — extract text from any PDF inside the vault, with optional page ranges.
- **Graphs** — run Python (matplotlib) in an isolated subprocess, save the figure to an
  `attachments/graphs/` folder in the vault, and optionally embed it in a note via an
  Obsidian image link.

Every path is resolved relative to the vault root and checked so it cannot escape the
vault.

## Install

Requires Python ≥ 3.10.

```bash
git clone https://github.com/axel687/mcp_obsiCC_IOS.git
cd mcp_obsiCC_IOS
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Configure

The server only needs one environment variable: `OBSIDIAN_VAULT`, pointing at the
absolute path of your vault.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OBSIDIAN_VAULT` | *(required)* | Absolute path to your Obsidian vault (local path, cloned from the remote if Git sync is on) |
| `OBSIDIAN_ATTACHMENTS_DIR` | `attachments` | Vault-relative folder for generic attachments |
| `OBSIDIAN_GRAPHS_DIR` | `attachments/graphs` | Where generated graphs are written |
| `OBSIDIAN_GRAPH_TIMEOUT` | `30` | Seconds before graph subprocesses are killed |
| `OBSIDIAN_VAULT_REMOTE` | *(unset)* | Optional. Git URL of your vault repo — enables the sync feature below |
| `OBSIDIAN_AUTO_PUSH` | `true` | When sync is on, auto-commit+push after every write |
| `OBSIDIAN_GIT_NAME` / `OBSIDIAN_GIT_EMAIL` | `Obsidian MCP` / `obsidian-mcp@local` | Identity used for auto-commits |

### Wire it into Claude Code

Add an entry to your Claude Code MCP config (see `config.example.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["-m", "obsidian_mcp"],
      "env": {
        "OBSIDIAN_VAULT": "/absolute/path/to/your/Vault"
      }
    }
  }
}
```

Then from Claude Code:

```
claude mcp add-from-json obsidian "$(cat config.example.json | jq '.mcpServers.obsidian')"
```

…or configure it through the Claude Code settings UI. Once connected, the tools below
become available.

## Tools

| Tool | What it does |
| --- | --- |
| `list_notes(subdir?)` | List every `.md` file in the vault (or under a subdir). |
| `read_note(path)` | Return a note's full markdown content. |
| `write_note(path, content, overwrite=false)` | Create (or overwrite) a note. |
| `append_note(path, content)` | Append text to a note, creating it if needed. |
| `insert_under_heading(path, heading, content, create_if_missing=true)` | Insert text at the end of a specific section. |
| `search_notes(query, regex=false, max_results=200)` | Search note bodies. |
| `delete_note(path)` | Delete a note. |
| `read_pdf(path, page_start?, page_end?, joined=true)` | Extract PDF text. |
| `generate_graph(code, filename?, dpi=150, timeout?, embed_in_note?, alt_text?)` | Run matplotlib code and save a figure. |
| `sync_vault(action="both")` | Manually pull/push/both. No-op if Git sync is off. |

### Graph generation contract

The code you pass to `generate_graph` runs in a fresh Python subprocess with
`matplotlib.pyplot` already imported as `plt`. The server saves whichever figure you
leave open (or, if you assign a variable named `fig`, that one).

```python
import numpy as np
x = np.linspace(0, 2 * np.pi, 200)
plt.plot(x, np.sin(x), label="sin")
plt.plot(x, np.cos(x), label="cos")
plt.legend()
plt.title("Sin vs. cos")
```

Calling:

```json
{
  "code": "...the snippet above...",
  "filename": "sin-cos",
  "embed_in_note": "journal/2026-04-19",
  "alt_text": "Sin and cos"
}
```

…writes `attachments/graphs/sin-cos-YYYYMMDD-HHMMSS.png` into the vault and appends a
`![Sin and cos](attachments/graphs/...)` line to `journal/2026-04-19.md`.

## iPad-only workflow (Git-synced vault)

Designed for the case where your Obsidian vault lives on an iPad and Claude Code runs
in a cloud sandbox that can't see the iPad's filesystem. The vault becomes a Git repo
and both sides push/pull against the same remote (e.g. a private GitHub repo).

```
┌───────────────┐                ┌─────────────┐               ┌──────────────────┐
│ iPad Obsidian │ ◀── pull ────  │  GitHub     │ ◀── pull ──── │ Claude Code +    │
│ + obsidian-git│ ── push  ────▶ │  (remote)   │ ── push ────▶ │ obsidian-mcp     │
└───────────────┘                └─────────────┘               └──────────────────┘
```

### 1. Create the remote
Create an empty private repo (GitHub, GitLab, whatever). Name it e.g. `obsidian-vault`.

### 2. iPad side — obsidian-git plugin
1. In Obsidian on iPad, *Settings → Community plugins → Browse* → install **Git** by
   Vinzent Zhaoxiang.
2. Generate a GitHub fine-grained Personal Access Token with `contents:read/write`
   scope for that single repo.
3. In the plugin settings:
   - *Authentication → Username*: your GitHub username.
   - *Authentication → Password / Token*: paste the PAT.
   - *Commit-and-sync interval*: `5` minutes (tweak to taste).
   - *Auto pull interval*: `2` minutes.
   - *Pull on startup*: on.
4. Run *Git: Clone an existing remote repo* once to populate the vault, or push your
   existing vault up.

### 3. Claude Code side — obsidian-mcp
Point `OBSIDIAN_VAULT_REMOTE` at the same repo. The server will:

- **Clone** into `OBSIDIAN_VAULT` the first time it starts (directory must not exist
  or must already be the clone).
- **Pull --rebase** at every startup.
- **Commit + push** automatically after each note write, graph generation, or delete.
- **Retry on conflict**: if the push is rejected, pull --rebase --autostash and push
  again. Clean merges get rebased silently; only a truly conflicting edit on the same
  lines will stop the server (you'll see it in the tool's `sync_error` field).

Example config for Claude Code:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["-m", "obsidian_mcp"],
      "env": {
        "OBSIDIAN_VAULT": "/workspace/vault",
        "OBSIDIAN_VAULT_REMOTE": "https://github.com/<you>/obsidian-vault.git",
        "OBSIDIAN_AUTO_PUSH": "true",
        "OBSIDIAN_GIT_NAME": "Obsidian MCP (iPad)",
        "OBSIDIAN_GIT_EMAIL": "mcp@you.dev"
      }
    }
  }
}
```

Authentication in the sandbox: the simplest path is to set `GITHUB_TOKEN` and let
git-over-HTTPS pick it up via the standard credential helper, or embed the PAT
directly in the URL (`https://<user>:<pat>@github.com/...`). Don't commit the token.

### 4. `sync_vault` tool

Use it from Claude if you want to force a sync mid-session:

```
sync_vault(action="pull")   # fetch changes the iPad pushed while you were working
sync_vault(action="push")   # flush pending MCP commits
sync_vault(action="both")   # default
```

### Keeping conflicts rare
- On the iPad: auto-pull every 2 min, auto-commit+push every 5 min.
- On Claude: auto-push after each write (default).
- Don't edit the same note on both sides at the same time. If you do, the second side
  to push has to pull-rebase — usually painless, but will fail on overlapping line
  edits and leave a conflict marker in the working tree. In that case: open the note
  on iPad, resolve, push, and re-run `sync_vault(action="pull")` in Claude.

## Safety notes

- All user-supplied paths are resolved and verified to live inside `OBSIDIAN_VAULT`.
- `generate_graph` executes arbitrary Python — only expose this server to trusted
  clients. Subprocesses run with `-I` (isolated mode) and a configurable timeout.
- The server never touches files with hidden-directory components in their vault path
  (e.g. `.obsidian/`, `.trash/`).

## Development

```bash
pip install -e .
OBSIDIAN_VAULT=/tmp/test-vault python -m obsidian_mcp
```

The process speaks MCP over stdio, so you typically run it through an MCP client rather
than invoking it directly.
