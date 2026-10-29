"""Run user-supplied Python code to produce a matplotlib figure saved in the vault.

The code runs in an isolated subprocess so that timeouts and crashes don't bring the
MCP server down. A helper module is injected via stdin; the only contract is:

    - A global `plt` (matplotlib.pyplot) is available.
    - If the user's code leaves any figure open, the current figure is saved.
    - Alternatively the user may assign a `fig` variable (matplotlib Figure); it wins.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .vault import VaultError, graphs_dir, relative_to_vault


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class GraphResult:
    path: str
    absolute_path: str
    stdout: str


def _default_filename(prefix: str = "graph") -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_prefix = _SAFE_NAME.sub("_", prefix).strip("_") or "graph"
    return f"{safe_prefix}-{stamp}.png"


_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "MPLCONFIGDIR", "SYSTEMROOT")


def _scrubbed_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}


def _build_runner(user_code: str, out_path: Path, dpi: int) -> str:
    header = textwrap.dedent(
        f"""
        import json, sys, traceback
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        _OUT = r\"\"\"{out_path}\"\"\"
        _DPI = {dpi}

        _user_globals = {{"plt": plt, "__name__": "__obsidian_graph__"}}
        try:
            exec(compile({user_code!r}, "<user-graph>", "exec"), _user_globals)
        except Exception:
            traceback.print_exc()
            sys.exit(2)

        fig = _user_globals.get("fig")
        if fig is None:
            nums = plt.get_fignums()
            if not nums:
                print("ERROR: no figure was produced", file=sys.stderr)
                sys.exit(3)
            fig = plt.figure(nums[-1])
        fig.savefig(_OUT, dpi=_DPI, bbox_inches="tight")
        print(_OUT)
        """
    )
    return header


def generate_graph(
    code: str,
    filename: str | None = None,
    dpi: int = 150,
    timeout: float | None = None,
) -> GraphResult:
    if not code or not code.strip():
        raise VaultError("code is empty")

    out_dir = graphs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    name = filename or _default_filename()
    name = _SAFE_NAME.sub("_", name)
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".pdf")):
        name += ".png"
    out_path = (out_dir / name).resolve()
    try:
        out_path.relative_to(out_dir)
    except ValueError as exc:
        raise VaultError(f"Output filename escapes graphs directory: {filename!r}") from exc

    effective_timeout = timeout
    if effective_timeout is None:
        env_timeout = os.environ.get("OBSIDIAN_GRAPH_TIMEOUT")
        effective_timeout = float(env_timeout) if env_timeout else 30.0

    runner = _build_runner(code, out_path, dpi)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", runner],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            # Don't hand secrets (git token, MCP password...) to user-supplied code.
            env=_scrubbed_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise VaultError(
            f"Graph generation timed out after {effective_timeout}s"
        ) from exc

    if proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise VaultError(f"Graph generation failed (exit {proc.returncode}):\n{msg}")
    if not out_path.is_file():
        raise VaultError(f"Graph runner finished but no file was written: {out_path}")

    return GraphResult(
        path=relative_to_vault(out_path),
        absolute_path=str(out_path),
        stdout=proc.stdout.strip(),
    )
