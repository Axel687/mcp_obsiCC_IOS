"""Skills over MCP (extension ``io.modelcontextprotocol/skills``), served per agent.

The skills live in a vault, so the owner edits them like any note (from the iPad)
while agents, which only read that vault, cannot change them:

    OBSIDIAN_SKILLS_VAULT=global      # the vault holding the skills
    OBSIDIAN_SKILLS_DIR=Skills        # folder inside it (default "Skills")

    Skills/commun/<skill>/SKILL.md    # served to every agent
    Skills/<agent>/<skill>/SKILL.md   # served to that agent only

A skill is a folder whose ``SKILL.md`` starts with YAML frontmatter holding ``name``
(equal to the folder name) and ``description``; every other file in the folder is
part of it. Files are exposed as ``skill://<group>/<skill>/<file>`` resources, with a
sha256 digest and a size, as the extension asks.

The owner gets no skills: the role skills are for the agents, and the owner's own
skills stay in their claude.ai account. A client that does not speak the extension
can still read the same files as notes of the skills vault.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import PaginatedRequestParams, RequestParams
from mcp_types.jsonrpc import INVALID_PARAMS

from . import access, sync
from .vault import VaultError, configured_vaults, owner_use_vault

IDENTIFIER = "io.modelcontextprotocol/skills"
SCHEME = "skill://"
COMMON_GROUP = "commun"
TTL_MS = 60_000
MAX_FILES = 512
MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SkillFile:
    rel: str  # path inside the skill folder, with "/"
    path: Path
    size: int
    digest: str


@dataclass(frozen=True)
class Skill:
    group: str
    name: str
    frontmatter: dict[str, Any]
    files: tuple[SkillFile, ...]

    @property
    def uri(self) -> str:
        return f"{SCHEME}{self.group}/{self.name}/SKILL.md"

    def file_uri(self, f: SkillFile) -> str:
        return f"{SCHEME}{self.group}/{self.name}/{f.rel}"

    def as_wire(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": self.frontmatter,
            "resources": [
                {"uri": self.file_uri(f), "digest": f.digest, "size": f.size} for f in self.files
            ],
        }


def skills_vault_name() -> str | None:
    value = (os.environ.get("OBSIDIAN_SKILLS_VAULT") or "").strip().lower()
    return value or None


def _skills_root() -> Path | None:
    name = skills_vault_name()
    if name is None:
        return None
    vault = configured_vaults().get(name)
    if vault is None:
        return None
    folder = (os.environ.get("OBSIDIAN_SKILLS_DIR") or "Skills").strip().strip("/") or "Skills"
    return vault.path / folder


def _refresh() -> None:
    """Pull the skills vault (at most every OBSIDIAN_PULL_INTERVAL seconds), so a skill
    edited on another device is served without a restart. Failures are ignored: the
    local copy is served."""
    name = skills_vault_name()
    if name is None:
        return
    try:
        with owner_use_vault(name):
            sync.maybe_pull()
    except VaultError:
        pass


def _frontmatter(text: str) -> dict[str, Any] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("name"), str) or not isinstance(data.get("description"), str):
        return None
    return {str(k): v for k, v in data.items()}


def _load_skill(group: str, folder: Path) -> Skill | None:
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return None
    front = _frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    if front is None or front["name"] != folder.name:
        return None
    files: list[SkillFile] = []
    total = 0
    root = folder.resolve()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or any(p.startswith(".") for p in path.relative_to(folder).parts):
            continue
        if not path.resolve().is_relative_to(root):  # symlink out of the skill
            continue
        data = path.read_bytes()
        total += len(data)
        files.append(
            SkillFile(
                rel=path.relative_to(folder).as_posix(),
                path=path,
                size=len(data),
                digest="sha256:" + hashlib.sha256(data).hexdigest(),
            )
        )
        if len(files) > MAX_FILES or total > MAX_BYTES:
            return None
    return Skill(group, folder.name, front, tuple(files))


def skills_for(agent: access.Agent | None) -> list[Skill]:
    """The skills the caller may see: the common ones, then the agent's own."""
    if agent is None or not agent.rights:
        return []
    root = _skills_root()
    if root is None or not root.is_dir():
        return []
    out: list[Skill] = []
    for group in (COMMON_GROUP, agent.name):
        base = root / group
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
            skill = _load_skill(group, folder)
            if skill is not None:
                out.append(skill)
    return out


def _visible() -> list[Skill]:
    _refresh()
    return skills_for(access.current_agent())


def _not_found(uri: str) -> MCPError:
    return MCPError(INVALID_PARAMS, f"unknown skill or file: {uri}")


def find_skill(uri: str) -> Skill:
    for skill in _visible():
        if skill.uri == uri:
            return skill
    raise _not_found(uri)


def read_file(uri: str) -> tuple[str | bytes, str]:
    """Content and MIME type of one skill file the caller may see."""
    for skill in _visible():
        for f in skill.files:
            if skill.file_uri(f) == uri:
                data = f.path.read_bytes()
                if f.rel.lower().endswith(".md"):
                    return data.decode("utf-8", errors="replace"), "text/markdown"
                try:
                    return data.decode("utf-8"), "text/plain"
                except UnicodeDecodeError:
                    return data, "application/octet-stream"
    raise _not_found(uri)


# -- extension -------------------------------------------------------------------------


class GetSkillParams(RequestParams):
    uri: str


async def _list(ctx: Any, params: PaginatedRequestParams) -> dict[str, Any]:
    # Every skill fits in one page: a handful of roles with a few skills each.
    return {
        "resultType": "complete",
        "skills": [s.as_wire() for s in _visible()],
        "ttlMs": TTL_MS,
        "cacheScope": "private",
    }


async def _get(ctx: Any, params: GetSkillParams) -> dict[str, Any]:
    return {
        "resultType": "complete",
        "skill": find_skill(params.uri).as_wire(),
        "ttlMs": TTL_MS,
        "cacheScope": "private",
    }


class SkillsExtension(Extension):
    identifier = IDENTIFIER

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding("skills/list", PaginatedRequestParams, _list),
            MethodBinding("skills/get", GetSkillParams, _get),
        )


def install(server: Any) -> None:
    """Serve skill files through resources/read (`skill://...`)."""
    from mcp.server.mcpserver.resources.templates import ResourceSecurity

    @server.resource(
        SCHEME + "{+path}",
        name="skill-file",
        description="A file of a skill served to the connected agent (Skills over MCP).",
        security=ResourceSecurity(),
    )
    def skill_file(path: str) -> str | bytes:
        content, _ = read_file(SCHEME + path)
        return content
