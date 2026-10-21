"""Every checks test runs with capcom's config rooted in its own tmp_path, so no
test reads the real ~/.claude or ~/.openclaw (agent definitions, OpenClaw root)."""

from __future__ import annotations

from pathlib import Path

import pytest

import capcom.config
from capcom.config import Config


@pytest.fixture(autouse=True)
def _tmp_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = tmp_path / "roots"
    config = Config(roots / ".claude", roots / ".openclaw", roots / "cache")
    monkeypatch.setattr(capcom.config, "load_config", lambda env=None: config)
