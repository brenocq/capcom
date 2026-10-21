"""Every changes test writes marks under its own tmp_path and reads no real
config folder (agent definitions, the marks file)."""

from __future__ import annotations

from pathlib import Path

import pytest

import capcom.config
from capcom.config import Config


@pytest.fixture(autouse=True)
def _tmp_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = tmp_path / "roots"
    config = Config(roots / ".claude", roots / ".openclaw", roots / "cache", roots / "state")
    monkeypatch.setattr(capcom.config, "load_config", lambda env=None: config)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(roots))
