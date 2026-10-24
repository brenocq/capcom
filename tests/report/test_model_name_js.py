"""static/agent.js's modelName, run by node: a Claude model id as its family and
version, another provider's id as it is, cut past 24 characters."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def names(ids: list[str | None]) -> list[str]:
    src = (STATIC / "agent.js").read_text()
    start = src.index("const MODEL_CHARS")
    end = src.index("\n}\n", src.index("export function modelName")) + 3
    code = src[start:end].replace("export function", "function")
    code += f"\nconsole.log(JSON.stringify({json.dumps(ids)}.map(modelName)));\n"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    return list(json.loads(out.stdout))


def test_model_names() -> None:
    long_id = "accounts/fireworks/models/qwen3-coder-480b"
    assert names(
        ["claude-opus-5-5", "claude-3-5-haiku-20241022", "gpt-5", long_id, None, "claude-x[1m]"]
    ) == ["Opus 5.5", "Haiku 3.5", "gpt-5", long_id[:23] + "…", "unknown model", "X"]
