"""Where capcom reads from and writes to. Everything comes from the environment, so
nothing is tied to one machine's paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    claude_root: Path  # $CLAUDE_CONFIG_DIR or ~/.claude (read-only)
    openclaw_root: Path  # $OPENCLAW_STATE_DIR or ~/.openclaw (read-only)
    cache_dir: Path  # $XDG_CACHE_HOME/capcom or ~/.cache/capcom (the index: rebuilt by a scan)
    state_dir: Path  # $XDG_STATE_HOME/capcom or ~/.local/state/capcom (history no scan rebuilds)

    @property
    def projects_dir(self) -> Path:
        return self.claude_root / "projects"

    @property
    def index_path(self) -> Path:
        return self.cache_dir / "index.sqlite"


def load_config(env: os._Environ[str] | dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    home = Path(env.get("HOME", str(Path.home())))
    claude = env.get("CLAUDE_CONFIG_DIR") or str(home / ".claude")
    openclaw = env.get("OPENCLAW_STATE_DIR") or str(home / ".openclaw")
    xdg_cache = env.get("XDG_CACHE_HOME") or str(home / ".cache")
    xdg_state = env.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Config(
        claude_root=Path(claude).expanduser(),
        openclaw_root=Path(openclaw).expanduser(),
        cache_dir=Path(xdg_cache).expanduser() / "capcom",
        state_dir=Path(xdg_state).expanduser() / "capcom",
    )
