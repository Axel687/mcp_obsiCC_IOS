"""Runtime settings, read once from environment variables.

Two modes:

- ``stdio`` (default): the historical mode. Claude Code launches the server as a
  subprocess on the same machine. Nothing is exposed on the network.
- ``http``: the server runs on its own (e.g. in Docker on a VPS) and speaks MCP over
  Streamable HTTP, so that claude.ai, the iOS/Android apps and Claude Code can all
  reach it. In this mode authentication is mandatory: the server refuses to start
  without a password.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_REDIRECTS = (
    # claude.ai web, desktop and mobile apps (connectors are brokered by Anthropic).
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # Claude Code and other local MCP clients use a loopback callback on a random port.
    "http://localhost",
    "http://127.0.0.1",
)

MIN_PASSWORD_LENGTH = 12
MIN_STATIC_TOKEN_LENGTH = 32


class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    transport: str = "stdio"
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str | None = None
    password: str | None = None
    static_token: str | None = None
    state_dir: Path = field(default_factory=lambda: Path("~/.local/state/obsidian-mcp").expanduser())
    allowed_redirects: tuple[str, ...] = DEFAULT_ALLOWED_REDIRECTS
    enable_graphs: bool = True
    pull_interval: float = 30.0

    @property
    def is_http(self) -> bool:
        return self.transport == "http"

    @property
    def mcp_url(self) -> str:
        assert self.public_url
        return self.public_url + "/mcp"


def load_settings() -> Settings:
    transport = (_env("OBSIDIAN_MCP_TRANSPORT", "stdio") or "stdio").lower()
    if transport in {"streamable-http", "streamable_http"}:
        transport = "http"
    if transport not in {"stdio", "http"}:
        raise ConfigError(f"OBSIDIAN_MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}")

    is_http = transport == "http"

    redirects_raw = _env("OBSIDIAN_MCP_ALLOWED_REDIRECTS")
    allowed_redirects = (
        tuple(r.strip() for r in redirects_raw.split(",") if r.strip())
        if redirects_raw
        else DEFAULT_ALLOWED_REDIRECTS
    )

    public_url = _env("OBSIDIAN_MCP_PUBLIC_URL")
    if public_url:
        public_url = public_url.rstrip("/")

    settings = Settings(
        transport=transport,
        host=_env("OBSIDIAN_MCP_HOST", "0.0.0.0") or "0.0.0.0",
        port=int(_env("OBSIDIAN_MCP_PORT", "8000") or 8000),
        public_url=public_url,
        password=_env("OBSIDIAN_MCP_PASSWORD"),
        static_token=_env("OBSIDIAN_MCP_TOKEN"),
        state_dir=Path(
            _env("OBSIDIAN_MCP_STATE_DIR", "~/.local/state/obsidian-mcp") or ""
        ).expanduser(),
        allowed_redirects=allowed_redirects,
        # Running arbitrary Python is acceptable on your own machine, much less on a
        # server reachable from the Internet: off by default in HTTP mode.
        enable_graphs=_env_bool("OBSIDIAN_ENABLE_GRAPHS", default=not is_http),
        pull_interval=float(_env("OBSIDIAN_PULL_INTERVAL", "30") or 30),
    )

    if is_http:
        _validate_http(settings)
    return settings


def _validate_http(settings: Settings) -> None:
    problems: list[str] = []
    if not settings.public_url:
        problems.append(
            "OBSIDIAN_MCP_PUBLIC_URL is required (the public HTTPS base URL, e.g. https://mcp.example.com)"
        )
    elif not settings.public_url.startswith("https://") and not settings.public_url.startswith(
        ("http://localhost", "http://127.0.0.1")
    ):
        problems.append("OBSIDIAN_MCP_PUBLIC_URL must use https:// (OAuth requires it)")
    if not settings.password:
        problems.append("OBSIDIAN_MCP_PASSWORD is required in HTTP mode")
    elif len(settings.password) < MIN_PASSWORD_LENGTH:
        problems.append(f"OBSIDIAN_MCP_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters")
    if settings.static_token is not None and len(settings.static_token) < MIN_STATIC_TOKEN_LENGTH:
        problems.append(
            f"OBSIDIAN_MCP_TOKEN must be at least {MIN_STATIC_TOKEN_LENGTH} characters "
            "(generate one with: openssl rand -hex 32)"
        )
    if problems:
        raise ConfigError("Invalid HTTP configuration:\n  - " + "\n  - ".join(problems))
