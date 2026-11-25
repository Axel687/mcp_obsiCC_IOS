"""Agents: logins other than the owner's, each limited to some vaults.

The owner (the password of the login page, or `OBSIDIAN_MCP_TOKEN`) sees every vault,
as before. An agent is an AI working for the owner (for example the roles of an
agentic start-up) that must only see its own vaults:

    OBSIDIAN_AGENTS=analyste:analyste=rw,global=r;ceo:ceo=rw,analyste=r,global=r

Each entry is ``agent:vault=right,vault=right`` with right ``rw`` (read and write) or
``r`` (read only); entries are separated by ``;``. The first ``rw`` vault is the
agent's default vault. Each agent has one secret, in its own variable:

    OBSIDIAN_AGENT_TOKEN_ANALYSTE=<openssl rand -hex 32>

(name in capitals, ``-`` becomes ``_``). That secret is both the password the agent's
login page accepts and a bearer token (``Authorization: Bearer ...``) for clients that
send headers, such as Claude Code or the Agent SDK. Tokens issued to an agent carry
its name (`subject` = ``agent:<name>``), so every call is checked:

- vaults it was not granted do not exist for it (not listed, refused by name);
- changes are refused in a vault it may only read;
- tools that reveal the owner's whole setup (``ecosystem_status``) are refused.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass

from .vault import VaultError

AGENT_PREFIX = "agent:"
OWNER = "owner"
MIN_AGENT_TOKEN_LENGTH = 32
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class Agent:
    name: str
    rights: tuple[tuple[str, str], ...]  # (vault, "rw" | "r"), in the order given

    @property
    def subject(self) -> str:
        return AGENT_PREFIX + self.name

    @property
    def vaults(self) -> list[str]:
        return [v for v, _ in self.rights]

    def can_write(self, vault: str) -> bool:
        return (vault, "rw") in self.rights

    @property
    def default_vault(self) -> str | None:
        for vault, right in self.rights:
            if right == "rw":
                return vault
        return self.rights[0][0] if self.rights else None


def token_variable(name: str) -> str:
    return "OBSIDIAN_AGENT_TOKEN_" + name.upper().replace("-", "_")


def configured_agents() -> dict[str, Agent]:
    """Agents from `OBSIDIAN_AGENTS`. Read from the environment on each call."""
    raw = (os.environ.get("OBSIDIAN_AGENTS") or "").strip()
    agents: dict[str, Agent] = {}
    if not raw:
        return agents
    for item in re.split(r"[;\n]", raw):
        item = item.strip()
        if not item:
            continue
        name, sep, grants = item.partition(":")
        name = name.strip().lower()
        if not sep or not _NAME_RE.match(name):
            raise VaultError(f"OBSIDIAN_AGENTS: expected agent:vault=rw,..., got {item!r}")
        if name in agents:
            raise VaultError(f"OBSIDIAN_AGENTS: agent {name!r} is defined twice")
        rights: list[tuple[str, str]] = []
        for grant in grants.split(","):
            grant = grant.strip()
            if not grant:
                continue
            vault, eq, right = grant.partition("=")
            vault, right = vault.strip().lower(), right.strip().lower()
            if not eq or right not in {"r", "rw"} or not _NAME_RE.match(vault):
                raise VaultError(
                    f"OBSIDIAN_AGENTS: agent {name!r}: expected vault=r or vault=rw, got {grant!r}"
                )
            if any(v == vault for v, _ in rights):
                raise VaultError(f"OBSIDIAN_AGENTS: agent {name!r}: vault {vault!r} given twice")
            rights.append((vault, right))
        if not rights:
            raise VaultError(f"OBSIDIAN_AGENTS: agent {name!r} has no vault")
        agents[name] = Agent(name, tuple(rights))
    return agents


def agent_token(name: str) -> str | None:
    value = (os.environ.get(token_variable(name)) or "").strip()
    return value or None


def agent_for_secret(secret: str) -> Agent | None:
    """The agent whose secret this is (login password or bearer token), if any."""
    if not secret:
        return None
    try:
        agents = configured_agents()
    except VaultError:
        return None
    found = None
    for agent in agents.values():
        token = agent_token(agent.name)
        # Compare against every agent (no early exit) to keep timing uniform.
        if token and len(token) >= MIN_AGENT_TOKEN_LENGTH and hmac.compare_digest(
            secret.encode(), token.encode()
        ):
            found = agent
    return found


def agent_for_subject(subject: str | None) -> Agent | None:
    """Map a token's subject to its agent. Unknown agents (removed from the
    configuration) get no vault at all rather than the owner's rights."""
    if not subject or not subject.startswith(AGENT_PREFIX):
        return None
    name = subject[len(AGENT_PREFIX):]
    try:
        agent = configured_agents().get(name)
    except VaultError:
        agent = None
    return agent or Agent(name, ())


def current_agent() -> Agent | None:
    """The agent making the current call, or None for the owner (and in stdio mode)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover
        return None
    token = get_access_token()
    return agent_for_subject(token.subject) if token is not None else None


def check(agent: Agent, name: str | None, *, write: bool) -> str:
    """The vault `agent` may use for this call, or VaultError. Without `name` the
    agent's default vault (its first `rw` vault) is used."""
    wanted = (name or "").strip().lower() or agent.default_vault
    if wanted is None or wanted not in agent.vaults:
        shown = ", ".join(agent.vaults) or "none"
        raise VaultError(f"unknown vault {wanted!r}; available vaults: {shown} (call list_vaults)")
    if write and not agent.can_write(wanted):
        raise VaultError(f"vault {wanted!r} is read-only for agent {agent.name!r}")
    return wanted


def startup_report() -> list[str]:
    """One line per agent for the server log; flags missing or weak secrets and
    vaults that do not exist."""
    from .vault import configured_vaults

    try:
        agents = configured_agents()
        vaults = configured_vaults()
    except VaultError as exc:
        return [f"agents configuration error: {exc}"]
    lines = []
    for agent in agents.values():
        token = agent_token(agent.name)
        problems = []
        if not token:
            problems.append(f"{token_variable(agent.name)} is not set, the agent cannot log in")
        elif len(token) < MIN_AGENT_TOKEN_LENGTH:
            problems.append(
                f"{token_variable(agent.name)} is shorter than {MIN_AGENT_TOKEN_LENGTH} "
                "characters, the agent cannot log in"
            )
        owner_secrets = {os.environ.get("OBSIDIAN_MCP_PASSWORD"), os.environ.get("OBSIDIAN_MCP_TOKEN")}
        if token and token in owner_secrets:
            problems.append("its secret is the owner's password or token: it would log in as the owner")
        missing = [v for v in agent.vaults if v not in vaults]
        if missing:
            problems.append(f"unknown vaults: {', '.join(missing)}")
        rights = ", ".join(f"{v}={r}" for v, r in agent.rights)
        lines.append(f"agent {agent.name}: {rights}" + (f" ({'; '.join(problems)})" if problems else ""))
    return lines
