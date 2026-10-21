"""The synthetic log generator writes the shapes the parsers rely on."""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.generate import build_sample


def test_sample_layout(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    facts = build_sample(root)
    project = root / "projects" / facts["project_dir"]
    main = project / f"{facts['session_id']}.jsonl"
    sub = project / facts["session_id"] / "subagents" / f"agent-{facts['subagent_id']}.jsonl"
    meta = sub.with_name(f"agent-{facts['subagent_id']}.meta.json")
    assert main.is_file() and sub.is_file() and meta.is_file()
    assert json.loads(meta.read_text())["agentType"] == "scout"

    raw = main.read_bytes()
    assert not raw.endswith(b"\n"), "the sample ends with a partial line"
    lines = raw.split(b"\n")[:-1]
    types = []
    bad = 0
    for line in lines:
        try:
            types.append(json.loads(line)["type"])
        except json.JSONDecodeError:
            bad += 1
    assert bad == 1
    assert {"assistant", "user", "attachment", "system", "cost-state", "ai-title"} <= set(types)
    # The first reply is written three times while streaming, with one message.id.
    ids = [json.loads(x)["message"]["id"] for x in lines if b'"type":"assistant"' in x]
    assert ids.count(facts["main_message_ids"][0]) == 3


def test_subagent_stream_start_output(tmp_path: Path) -> None:
    facts = build_sample(tmp_path / ".claude")
    sub = next((tmp_path / ".claude").rglob(f"agent-{facts['subagent_id']}.jsonl"))
    recs = [json.loads(x) for x in sub.read_text().splitlines()]
    assert all(r["isSidechain"] and r["agentId"] == facts["subagent_id"] for r in recs)
    assert {r["message"]["usage"]["output_tokens"] for r in recs} == {4}
