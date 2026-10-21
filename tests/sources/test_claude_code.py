"""ClaudeCodeSource on synthetic logs (tests/fixtures/generate.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from capcom.sources import claude_code
from capcom.sources.claude_code import ClaudeCodeSource, tool_target
from capcom.types import (
    AgentInfo,
    Batch,
    CostState,
    Event,
    EventKind,
    LogFile,
    LogKind,
    Request,
    SessionInfo,
    Source,
    estimate_tokens,
)
from tests.fixtures.generate import Reply, SessionWriter, at, build_sample


def parse(log: LogFile, start: int = 0, source: ClaudeCodeSource | None = None) -> list[Batch]:
    return list((source or ClaudeCodeSource()).parse(log, start))


def of[T](batches: list[Batch], cls: type[T]) -> list[T]:
    return [r for b in batches for r in b.records if isinstance(r, cls)]


def events(batches: list[Batch], kind: EventKind) -> list[Event]:
    return [e for e in of(batches, Event) if e.kind == kind]


def session_log(path: Path, session_id: str = "s1") -> LogFile:
    return LogFile(path=path, kind=LogKind.SESSION, project_dir="-p", session_id=session_id)


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / ".claude"
    return root / "projects", build_sample(root)


def logs_of(projects: Path) -> list[LogFile]:
    return list(ClaudeCodeSource().discover(projects))


# --------------------------------------------------------------------------- discovery


def test_satisfies_protocol() -> None:
    source: Source = ClaudeCodeSource()
    assert source is not None


def test_discover_layout(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    projects = root / "projects"
    b = SessionWriter(root, cwd="/home/user/b", session_id="s-b")
    b.user_prompt(at(0))
    nometa = b.subagent("coder", "write it", agent_id="a2", meta=False)
    nometa.close()
    withmeta = b.subagent("scout", "look", agent_id="a1")
    withmeta.close()
    b.close()
    # A workflow agent, nested one level down.
    wf = projects / "-home-user-b" / "s-b" / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "agent-w1.jsonl").write_text("")
    (wf / "agent-w1.meta.json").write_text("{}")
    (wf / "journal.jsonl").write_text("")
    # Things that are not logs.
    results = projects / "-home-user-b" / "s-b" / "tool-results"
    results.mkdir()
    (results / "out.jsonl").write_text("")
    (projects / "-home-user-b" / "notes.txt").write_text("")
    (projects / "stray.jsonl").write_text("")
    # A session folder without its main log, and a second project.
    orphan = projects / "-home-user-b" / "s-a" / "subagents"
    orphan.mkdir(parents=True)
    (orphan / "agent-z.jsonl").write_text("")
    a = SessionWriter(root, cwd="/home/user/a", session_id="s-z")
    a.close()

    found = logs_of(projects)
    got = [(f.project_dir, f.kind, f.session_id, f.agent_id) for f in found]
    assert got == [
        ("-home-user-a", LogKind.SESSION, "s-z", None),
        ("-home-user-b", LogKind.SUBAGENT, "s-a", "z"),
        ("-home-user-b", LogKind.SESSION, "s-b", None),
        ("-home-user-b", LogKind.SUBAGENT, "s-b", "a1"),
        ("-home-user-b", LogKind.SUBAGENT, "s-b", "a2"),
        ("-home-user-b", LogKind.SUBAGENT, "s-b", "w1"),
    ]
    meta = {f.agent_id: f.meta_path for f in found if f.kind == LogKind.SUBAGENT}
    assert meta["a1"] is not None and meta["a1"].name == "agent-a1.meta.json"
    assert meta["a2"] is None and meta["z"] is None
    assert meta["w1"] == wf / "agent-w1.meta.json"
    assert logs_of(projects) == found  # stable


def test_discover_missing_root(tmp_path: Path) -> None:
    assert logs_of(tmp_path / "nope") == []


# --------------------------------------------------------------------------- the sample


def test_sample_main_log(sample: tuple[Path, dict[str, Any]]) -> None:
    projects, facts = sample
    source = ClaudeCodeSource()
    main = next(f for f in logs_of(projects) if f.kind == LogKind.SESSION)
    batches = parse(main, source=source)
    size = main.path.stat().st_size
    raw = main.path.read_bytes()
    assert batches[-1].end_offset == raw.rindex(b"\n") + 1 < size
    assert sum(b.skipped for b in batches) == 1  # the "{not json" line
    assert source.unknown_types == {"future-record-type": 1}

    reqs = {r.message_id: r for r in of(batches, Request)}
    m1, m2, m3 = facts["main_message_ids"]
    assert set(reqs) == {m1, m2, m3}
    r1 = reqs[m1]
    assert r1.usage.cache_write_1h == 20_000 and r1.usage.cache_write_5m == 0
    assert r1.usage.output == 250 and r1.usage.thinking == 120
    assert not r1.output_estimated and r1.agent_id is None
    assert r1.stop_reason == "tool_use" and r1.effort == "high" and r1.speed == "standard"
    assert r1.service_tier == "standard" and r1.version == "2.1.304"
    assert r1.cwd == "/home/user/proj" and r1.request_id and r1.model == "claude-opus-5-5"
    assert r1.session_id == facts["session_id"] and r1.project_dir == facts["project_dir"]
    assert [(c.name, c.target) for c in r1.tool_calls] == [("Read", "/home/user/proj/big.json")]
    assert r1.timestamp == at(2) and r1.timestamp.tzinfo is not None
    assert [(c.name, c.target) for c in reqs[m2].tool_calls] == [("Agent", "scout")]
    assert reqs[m3].model == "claude-sonnet-5-5"

    (spawn,) = events(batches, EventKind.SPAWN)
    assert spawn.attrs == {
        "tool_use_id": facts["agent_tool_id"],
        "agent_type": "scout",
        "description": "look around",
    }
    results = {e.attrs["tool_use_id"]: e for e in events(batches, EventKind.TOOL_RESULT)}
    read = results[facts["read_tool_id"]].attrs
    assert read["tool"] == "Read" and read["target"] == "/home/user/proj/big.json"
    assert read["chars"] == 40_000 and read["size_tokens"] == 10_000
    assert read["is_image"] is False and read["is_error"] is False
    assert results[facts["agent_tool_id"]].attrs["tool"] == "Agent"

    (away,) = events(batches, EventKind.AWAY)
    assert away.timestamp == at(4000)
    (comp,) = events(batches, EventKind.COMPACTION)
    assert comp.attrs == {
        "trigger": "auto",
        "pre_tokens": 180_000,
        "post_tokens": 9_000,
        "duration_ms": 30_000,
    }
    (model,) = events(batches, EventKind.MODEL)
    assert model.attrs == {"model": "claude-sonnet-5-5"}
    (memory,) = events(batches, EventKind.MEMORY)
    assert memory.attrs == {
        "source": "instructions",
        "paths": ("/home/user/proj/CLAUDE.md",),
        "size_tokens": 1_000,
    }

    sets = {e.attrs["source"]: e.attrs for e in events(batches, EventKind.TOOL_SET)}
    assert sets["skill_listing"]["names"] == ("alpha", "beta")
    assert sets["skill_listing"]["size_tokens"] == estimate_tokens(
        len("- alpha: synthetic skill\n- beta: synthetic skill\n")
    )
    assert sets["deferred_tools_delta"]["added"] == ("mcp__srv__one", "mcp__srv__two")
    assert sets["deferred_tools_delta"]["removed"] == ()
    shape = sets["api-request-shape"]
    assert shape["names"] == ("Read", "Bash", "Agent", "mcp__srv__one")
    assert shape["shape_hash"] == "h1"
    assert isinstance(shape["size_tokens"], int) and shape["size_tokens"] > 0
    (api,) = events(batches, EventKind.API_REQUEST)
    assert api.attrs["query_source"] == "repl_main_thread"
    assert api.attrs["model"] == "claude-opus-5-5" and api.attrs["cache_ttl"] == "1h"
    assert api.attrs["shape_hash"] == "h1" and api.attrs["request_id"]

    (cost,) = of(batches, CostState)
    assert cost.total_cost_usd == 1.5 and cost.session_id == facts["session_id"]
    line_start = raw.index(b'{"type":"cost-state"')
    assert cost.offset == line_start
    opus = cost.models["claude-opus-5-5"]
    assert (opus.input, opus.output, opus.cache_read, opus.cache_write) == (6, 330, 20_000, 30_000)
    assert opus.thinking == 120 and opus.cost_usd == 1.2 and cost.has_unknown_model_cost is False

    (info,) = of(batches, SessionInfo)
    assert info.title == "Synthetic session" and info.cwd == "/home/user/proj"
    assert info.version == "2.1.304" and info.entrypoint == "cli" and info.git_branch == "main"
    assert info.first_seen == at(0) and info.last_seen == at(4102)
    assert of(batches, AgentInfo) == []


def test_sample_subagent_log(sample: tuple[Path, dict[str, Any]]) -> None:
    projects, facts = sample
    sub = next(f for f in logs_of(projects) if f.kind == LogKind.SUBAGENT)
    batches = parse(sub)
    (agent,) = of(batches, AgentInfo)
    assert agent == AgentInfo(
        agent_id=facts["subagent_id"],
        session_id=facts["session_id"],
        agent_type="scout",
        description="look around",
        tool_use_id=facts["agent_tool_id"],
        spawn_depth=1,
        is_built_in=False,
    )
    (req,) = of(batches, Request)
    assert req.agent_id == facts["subagent_id"] and req.usage.cache_write_5m == 12_000
    tool_json = len(json.dumps({"command": "ls -la /home/user/proj"}, separators=(",", ":")))
    assert req.output_estimated and req.usage.output == estimate_tokens(2_000 + tool_json)
    assert [(c.name, c.target) for c in req.tool_calls] == [("Bash", "ls")]
    (info,) = of(batches, SessionInfo)
    assert info.session_id == facts["session_id"] and info.cwd is None
    assert info.first_seen == at(5)


# --------------------------------------------------------------------------- output estimate


def _sub(tmp_path: Path, meta: bool = True) -> tuple[SessionWriter, LogFile]:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    sub = s.subagent("coder", "do it", agent_id="ag", meta=meta)
    s.close()
    log = LogFile(
        path=sub.path,
        kind=LogKind.SUBAGENT,
        project_dir="-w",
        session_id="s1",
        agent_id="ag",
        meta_path=sub.path.with_name("agent-ag.meta.json") if meta else None,
    )
    return sub, log


def test_output_estimate_rule(tmp_path: Path) -> None:
    sub, log = _sub(tmp_path)
    tool = ("Grep", {"pattern": "x", "path": "/w/src"})
    m_tool = sub.assistant(Reply(at(1), output=4, stream_start_output=True, tools=[tool]))
    m_think = sub.assistant(
        Reply(at(2), output=3, stream_start_output=True, thinking_text="k" * 800, text="a" * 400)
    )
    m_text = sub.assistant(Reply(at(3), output=4, text="b" * 4000))
    m_big = sub.assistant(Reply(at(4), output=20, tools=[tool], text="c" * 4000))
    m_small = sub.assistant(Reply(at(5), output=15, tools=[("Bash", {"command": "ls"})]))
    sub.close()
    reqs = {r.message_id: r for r in of(parse(log), Request)}
    tool_chars = len(json.dumps(tool[1], separators=(",", ":")))
    assert reqs[m_tool].output_estimated
    assert reqs[m_tool].usage.output == max(4, estimate_tokens(tool_chars))
    assert reqs[m_think].usage.output == 300 and reqs[m_think].output_estimated
    assert not reqs[m_text].output_estimated and reqs[m_text].usage.output == 4
    assert not reqs[m_big].output_estimated and reqs[m_big].usage.output == 20
    # The rule keeps the logged value when it is larger than the estimate.
    assert reqs[m_small].output_estimated and reqs[m_small].usage.output == 15


def test_no_estimate_on_main_thread(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    mid = s.assistant(
        Reply(at(1), output=4, thinking_text="k" * 4000, tools=[("Bash", {"command": "ls"})])
    )
    s.close()
    (req,) = of(parse(session_log(s.path)), Request)
    assert req.message_id == mid and req.usage.output == 4 and not req.output_estimated


def test_estimate_counts_streamed_blocks_once(tmp_path: Path) -> None:
    sub, log = _sub(tmp_path)
    rec: dict[str, Any] = {
        "type": "assistant",
        "uuid": "u1",
        "timestamp": "2026-10-20T09:00:00.000Z",
        "message": {
            "id": "msg_same",
            "model": "claude-haiku-4-5",
            "content": [{"type": "thinking", "thinking": "z" * 400}],
            "usage": {"output_tokens": 2},
        },
    }
    sub.write(rec)
    sub.write({**rec, "uuid": "u2"})  # the same block written again
    sub.close()
    (req,) = of(parse(log), Request)
    assert req.usage.output == 100 and req.output_estimated


# --------------------------------------------------------------------------- agents


def test_missing_meta(tmp_path: Path) -> None:
    sub, log = _sub(tmp_path, meta=False)
    sub.assistant(Reply(at(1)))
    sub.close()
    (agent,) = of(parse(log), AgentInfo)
    assert agent == AgentInfo(agent_id="ag", session_id="s1")
    # Written after discovery: the next parse call reads it.
    log.path.with_name("agent-ag.meta.json").write_text(
        json.dumps({"agentType": "coder", "finished": True})
    )
    (agent,) = of(parse(log, start=log.path.stat().st_size), AgentInfo)
    assert agent.agent_type == "coder" and agent.finished is True


def test_bad_meta(tmp_path: Path) -> None:
    sub, log = _sub(tmp_path)
    sub.close()
    assert log.meta_path is not None
    log.meta_path.write_text("{broken")
    batches = parse(log)
    assert len(batches) == 1 and of(batches, AgentInfo) == [AgentInfo("ag", "s1")]


# --------------------------------------------------------------------------- streaming


def test_duplicates_within_a_batch(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    mid = s.assistant(
        Reply(
            at(1),
            output=500,
            thinking_text="t",
            text="answer",
            tools=[("Read", {"file_path": "/w/a"}), ("Edit", {"file_path": "/w/b"})],
            records=4,
        )
    )
    s.close()
    batches = parse(session_log(s.path))
    (req,) = of(batches, Request)
    assert req.message_id == mid and req.usage.output == 500
    assert [c.target for c in req.tool_calls] == ["/w/a", "/w/b"]
    assert req.stop_reason == "tool_use"


def test_duplicates_across_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_code, "MAX_BATCH_LINES", 2)
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    tools = [("Read", {"file_path": f"/w/{i}"}) for i in range(3)]
    mid = s.assistant(Reply(at(1), output=77, tools=tools, records=3))
    s.close()
    batches = parse(session_log(s.path))
    assert [b.lines for b in batches] == [2, 1]
    first, last = (of([b], Request) for b in batches)
    assert len(first) == 1 and len(last) == 1
    assert first[0].message_id == last[0].message_id == mid
    assert first[0].usage.output == 4 and last[0].usage.output == 77
    # The later batch's row (which the store keeps) carries every tool call.
    assert [c.target for c in last[0].tool_calls] == ["/w/0", "/w/1", "/w/2"]


def test_batch_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    for i in range(7):
        s.turn_duration(at(i), 10 + i)
    s.close()
    log = session_log(s.path)
    monkeypatch.setattr(claude_code, "MAX_BATCH_LINES", 3)
    batches = parse(log)
    assert [b.lines for b in batches] == [3, 3, 1]
    ends = [b.end_offset for b in batches]
    assert ends == sorted(ends) and ends[-1] == log.path.stat().st_size
    assert len(events(batches, EventKind.TURN)) == 7
    monkeypatch.setattr(claude_code, "MAX_BATCH_LINES", 1000)
    monkeypatch.setattr(claude_code, "MAX_BATCH_BYTES", 1)
    assert [b.lines for b in parse(log)] == [1] * 7


def test_resume_from_each_end_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / ".claude"
    build_sample(root)
    main = next(f for f in logs_of(root / "projects") if f.kind == LogKind.SESSION)
    whole = parse(main)
    monkeypatch.setattr(claude_code, "MAX_BATCH_LINES", 4)
    pieces: list[Batch] = []
    start = 0
    for b in parse(main):
        pieces.extend(parse(main, start=start)[:1])
        start = b.end_offset
    assert start == whole[-1].end_offset
    assert {e.key for e in of(whole, Event)} == {e.key for e in of(pieces, Event)}


# --------------------------------------------------------------------------- tolerance


def test_partial_last_line_completed_later(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.turn_duration(at(0), 5)
    full = {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 99,
        "uuid": "u-late",
        "timestamp": "2026-10-20T09:00:09Z",
    }
    s.partial_line(full)
    log = session_log(s.path)
    batches = parse(log)
    end = batches[-1].end_offset
    assert end < log.path.stat().st_size and batches[-1].lines == 1
    assert [e.attrs["duration_ms"] for e in events(batches, EventKind.TURN)] == [5]
    # The writer finishes the line.
    line = json.dumps(full, separators=(",", ":"))
    s.raw(line[-5:])
    s.close()
    again = parse(log, start=end)
    (turn,) = events(again, EventKind.TURN)
    assert turn.attrs["duration_ms"] == 99 and turn.key == "u-late"
    assert again[-1].end_offset == log.path.stat().st_size


def test_bad_lines_and_unknown_types(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.raw("{not json")
    s.raw('[1, "type", "assistant"]')
    s.raw('"assistant"')
    s.raw("")
    s.unknown_record()
    s.write({"type": "assistant"})  # no message, no id, no timestamp
    s.write({"type": "attachment", "attachment": {"type": "brand_new_kind"}})
    s.write({"type": "system", "subtype": "brand_new_subtype"})
    s.write({"type": "user", "message": {"content": [{"type": "tool_result"}]}})  # no time
    s.write({"type": "cost-state"})
    s.close()
    source = ClaudeCodeSource()
    batches = parse(session_log(s.path), source=source)
    assert sum(b.lines for b in batches) == 10
    # Bad JSON, two non-objects, the empty line, the id-less reply, and the tool
    # result with no time to place it at.
    assert sum(b.skipped for b in batches) == 6
    (cost,) = [r for b in batches for r in b.records]
    assert isinstance(cost, CostState) and cost.total_cost_usd == 0.0 and cost.models == {}
    assert source.unknown_types == {"future-record-type": 1}


def test_start_at_end_and_missing_file(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.turn_duration(at(0), 5)
    s.close()
    log = session_log(s.path)
    size = log.path.stat().st_size
    (b,) = parse(log, start=size)
    assert b.records == [] and b.end_offset == size and b.lines == 0
    assert parse(session_log(tmp_path / "gone.jsonl")) == []


def test_prefilter_keeps_every_needed_record(tmp_path: Path) -> None:
    """Records written with spaces (another JSON style), or with the type key not
    first, are still read; ignored types holding a nested needed type are harmless."""
    path = tmp_path / "s.jsonl"
    recs: list[dict[str, Any]] = [
        {"timestamp": "2026-10-20T09:00:00Z", "uuid": "a", "type": "system",
         "subtype": "away_summary"},
        {"type": "progress", "data": {"message": {"type": "assistant", "message": {}}}},
        {"type": "file-history-snapshot", "snapshot": {"type": "x"}},
    ]  # fmt: skip
    with path.open("w") as f:
        f.write(json.dumps(recs[0]) + "\n")  # ", " and ": " separators
        for r in recs[1:]:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        f.write('{"type" : "ai-title", "aiTitle": "spaced"}\n')
    source = ClaudeCodeSource()
    batches = parse(session_log(path), source=source)
    assert [e.key for e in events(batches, EventKind.AWAY)] == ["a"]
    (info,) = of(batches, SessionInfo)
    assert info.title == "spaced"
    assert sum(b.skipped for b in batches) == 0 and not source.unknown_types


def test_prefilter_matches_decoding_on_sample(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    build_sample(root)
    for log in logs_of(root / "projects"):
        for raw in log.path.read_bytes().splitlines():
            try:
                kind = json.loads(raw).get("type")
            except ValueError:
                continue
            passes = any(m in raw for m in claude_code._MARKERS)
            assert passes == (kind in claude_code._USED_TYPES), kind


# --------------------------------------------------------------------------- mappings


def test_tool_results_images_errors_and_keys(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.assistant(Reply(at(1), tools=[("mcp__shot__take", {}), ("Bash", {"command": "false"})]))
    shot, bash = s.last_tool_ids
    s.tool_result(shot, at(2), chars=500_000, is_image=True)
    s.tool_result(bash, at(3), chars=12, is_error=True)
    s.tool_result("toolu_unknown", at(4), chars=8)
    # Two results in one record, with a text list.
    s.write(
        {
            "type": "user",
            "uuid": "u-two",
            "timestamp": "2026-10-20T09:00:05Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "abcd"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [{"type": "text", "text": "x" * 10}, {"type": "image"}],
                    },
                    {"type": "text", "text": "not a result"},
                ]
            },
        }
    )
    s.close()
    res = {
        e.attrs["tool_use_id"]: e for e in events(parse(session_log(s.path)), EventKind.TOOL_RESULT)
    }
    img = res[shot].attrs
    assert img["tool"] == "mcp__shot__take" and "target" not in img
    assert img["is_image"] is True and img["chars"] == 0 and img["size_tokens"] == 1_600
    assert res[bash].attrs["is_error"] is True and res[bash].attrs["tool"] == "Bash"
    assert "tool" not in res["toolu_unknown"].attrs
    assert res["t1"].attrs["chars"] == 4
    assert res["t2"].attrs["chars"] == 10 and res["t2"].attrs["size_tokens"] == 3 + 1_600
    assert res["t1"].key == "u-two:t1" and res["t2"].key == "u-two:t2"
    assert len({e.key for e in res.values()}) == len(res)


def test_cache_write_split_missing(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    base = {"type": "assistant", "timestamp": "2026-10-20T09:00:00Z"}
    s.write({**base, "message": {"id": "m1", "usage": {"cache_creation_input_tokens": 700}}})
    s.write(
        {
            **base,
            "message": {
                "id": "m2",
                "usage": {
                    "cache_creation_input_tokens": 700,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 700,
                    },
                    "inference_geo": "us",
                    "speed": "fast",
                    "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
                },
            },
        }
    )
    s.close()
    reqs = {r.message_id: r for r in of(parse(session_log(s.path)), Request)}
    assert (reqs["m1"].usage.cache_write_5m, reqs["m1"].usage.cache_write_1h) == (700, 0)
    assert reqs["m1"].model == "unknown"
    m2 = reqs["m2"]
    assert (m2.usage.cache_write_5m, m2.usage.cache_write_1h) == (0, 700)
    assert m2.inference_geo == "us" and m2.speed == "fast"
    assert (m2.usage.web_searches, m2.usage.web_fetches) == (2, 1)


def test_api_errors(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.api_error_system(at(1), code="EAI_AGAIN")
    mid = s.assistant(
        Reply(
            at(2),
            model="<synthetic>",
            input=0,
            output=0,
            api_error=True,
            error="rate_limit",
            error_status=429,
        )
    )
    s.close()
    batches = parse(session_log(s.path))
    errors = events(batches, EventKind.API_ERROR)
    assert [(e.attrs["error"], e.attrs.get("status"), e.attrs["source"]) for e in errors] == [
        ("EAI_AGAIN", None, "system"),
        ("rate_limit", 429, "assistant"),
    ]
    (req,) = of(batches, Request)
    assert req.message_id == mid and req.is_api_error


def test_attachments(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.nested_memory(at(1), "/w/sub/CLAUDE.md", 400)
    s.invoked_skills(at(2), {"pdf": 800, "docx": 40})
    s.mcp_instructions_delta(at(3), {"improved": 4000}, removed=["old"])
    s.agent_listing_delta(at(4), ["scout", "coder"])
    s.deferred_tools_record(at(5), ["mcp__a__x", "mcp__a__y"])
    s.attachment(at(6), {"type": "total_tokens_reminder", "text": "noise"})
    s.close()
    batches = parse(session_log(s.path))
    (mem,) = events(batches, EventKind.MEMORY)
    assert mem.attrs == {
        "source": "nested_memory",
        "paths": ("/w/sub/CLAUDE.md",),
        "size_tokens": 100,
    }
    (skills,) = events(batches, EventKind.SKILLS_INVOKED)
    assert skills.attrs == {"names": ("pdf", "docx"), "size_tokens": 210}
    sets = {e.attrs["source"]: e.attrs for e in events(batches, EventKind.TOOL_SET)}
    assert sets["mcp_instructions_delta"] == {
        "source": "mcp_instructions_delta",
        "added": ("improved",),
        "removed": ("old",),
        "size_tokens": 1_000,
    }
    agents = sets["agent_listing_delta"]
    assert agents["added"] == ("scout", "coder") and agents["removed"] == ()
    assert agents["size_tokens"] == estimate_tokens(
        len("- scout: synthetic agent") + len("- coder: synthetic agent")
    )
    record = sets["deferred_tools_record"]
    assert record["names"] == ("mcp__a__x", "mcp__a__y")
    assert isinstance(record["size_tokens"], int) and record["size_tokens"] > 10
    assert len(of(batches, Event)) == 5


def test_api_request_ttl_default(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.api_request(at(1), "claude-haiku-4-5", ttl="5m")
    s.write(
        {
            "type": "api-request",
            "timestamp": "2026-10-20T09:00:02Z",
            "cacheControl": [{"path": ["system"], "value": {"type": "ephemeral"}}],
        }
    )
    s.write({"type": "api-request", "timestamp": "2026-10-20T09:00:03Z"})
    s.close()
    batches = parse(session_log(s.path))
    reqs = events(batches, EventKind.API_REQUEST)
    assert [e.attrs.get("cache_ttl") for e in reqs] == ["5m", "5m", None]
    raw = s.path.read_bytes()
    second = raw.index(b"\n") + 1
    assert reqs[1].key == str(second)  # no uuid: the line's offset


def test_tool_targets() -> None:
    assert tool_target("Bash", {"command": "  FOO=1 BAR=2 uv run pytest -q"}) == "uv"
    assert tool_target("Bash", {"command": 'API_KEY="abc def" curl x'}) == "curl"
    assert tool_target("Bash", {"command": "GIT_AUTHOR_NAME='Jane Q Doe' git commit"}) == "git"
    assert tool_target("Bash", {"command": "/usr/bin/git status"}) == "git"
    assert tool_target("Bash", {"command": "sudo rm -rf /tmp/x"}) == "sudo"
    assert tool_target("Bash", {"command": "env FOO=1 make"}) == "env"
    assert tool_target("Bash", {"command": 'X="unbalanced git push'}) is None
    assert tool_target("Bash", {"command": "ls # it's a comment"}) == "ls"
    assert tool_target("Skill", {"command": "/deploy prod --token xyz"}) == "deploy"
    assert tool_target("WebFetch", {"url": "https://" + "a" * 300 + ".com/"}) == "a" * 255
    assert tool_target("Bash", {"command": ""}) is None
    assert tool_target("WebFetch", {"url": "https://docs.example.com/a?b=c"}) == "docs.example.com"
    assert tool_target("WebFetch", {"url": 3}) is None
    assert tool_target("Skill", {"skill": "pdf", "args": "secret"}) == "pdf"
    assert tool_target("Grep", {"pattern": "secret", "path": "/w/src"}) == "/w/src"
    assert tool_target("Glob", {"pattern": "*.py"}) is None
    assert tool_target("NotebookEdit", {"notebook_path": "/w/n.ipynb"}) == "/w/n.ipynb"
    assert tool_target("Write", {"file_path": "/w/x", "content": "secret"}) == "/w/x"
    assert tool_target("Task", {"subagent_type": "general-purpose"}) == "general-purpose"
    assert tool_target("mcp__srv__do", {"q": "secret"}) is None
    assert tool_target("Read", "not a dict") is None


def test_timestamps_are_utc(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.write({"type": "system", "subtype": "away_summary", "uuid": "a",
             "timestamp": "2026-10-20T11:00:00+02:00"})  # fmt: skip
    s.write({"type": "system", "subtype": "away_summary", "uuid": "b",
             "timestamp": "2026-10-20T09:30:00"})  # fmt: skip
    s.write({"type": "system", "subtype": "away_summary", "uuid": "c", "timestamp": "garbage"})
    s.close()
    away = events(parse(session_log(s.path)), EventKind.AWAY)
    assert [e.timestamp for e in away] == [
        datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 9, 30, tzinfo=UTC),
        datetime(2026, 10, 20, 9, 30, tzinfo=UTC),  # no usable time: the last one seen
    ]
    assert all(e.timestamp.utcoffset() is not None for e in away)


def test_no_content_in_records(sample: tuple[Path, dict[str, Any]]) -> None:
    projects, _ = sample
    text = repr([parse(log) for log in logs_of(projects)])
    for secret in ("synthetic answer", "synthetic prompt", "synthetic summary", "tttt", "hhhh"):
        assert secret not in text


def test_reply_split_across_parse_calls(tmp_path: Path) -> None:
    """A live file read mid-reply: the second parse call only sees the later records,
    so its Request carries the tool calls and estimate of those records alone. The
    store merges tool_calls by tool_use_id and keeps the larger estimated output."""
    sub, log = _sub(tmp_path)
    base: dict[str, Any] = {
        "type": "assistant",
        "timestamp": "2026-10-20T09:00:00Z",
        "message": {"id": "msg_split", "model": "claude-haiku-4-5", "usage": {"output_tokens": 4}},
    }

    def block(uuid: str, tid: str, path: str) -> dict[str, Any]:
        content = [{"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": path}}]
        return {**base, "uuid": uuid, "message": {**base["message"], "content": content}}

    sub.write(block("u1", "t1", "/w/" + "a" * 400))
    first = parse(log)
    (r1,) = of(first, Request)
    sub.write(block("u2", "t2", "/w/b"))
    sub.close()
    (r2,) = of(parse(log, start=first[-1].end_offset), Request)
    assert r1.message_id == r2.message_id == "msg_split"
    assert [c.tool_use_id for c in r1.tool_calls] == ["t1"]
    assert [c.tool_use_id for c in r2.tool_calls] == ["t2"]
    assert r1.output_estimated and r2.output_estimated
    assert r1.usage.output > r2.usage.output == estimate_tokens(len('{"file_path":"/w/b"}'))


def test_old_sidechain_records_in_main_log(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.agent_id = "old-agent"  # records carry isSidechain and agentId, in the main log
    s.assistant(Reply(at(1), output=4, stream_start_output=True, thinking_text="k" * 400))
    s.agent_id = None
    s.assistant(Reply(at(2), output=4, stream_start_output=True, thinking_text="k" * 400))
    s.close()
    side, main = of(parse(session_log(s.path)), Request)
    assert side.agent_id == "old-agent" and side.output_estimated and side.usage.output == 100
    assert main.agent_id is None and not main.output_estimated and main.usage.output == 4


def test_unreadable_file_yields_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "dir.jsonl"
    folder.mkdir()  # opening a directory raises IsADirectoryError, an OSError
    assert parse(session_log(folder)) == []


def test_unknown_type_labels(tmp_path: Path) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.write({"type": ["assistant"]})
    s.write({"type": 7, "x": '"type":"assistant"'})
    s.write({"type": "q" * 300, "y": {"type": "assistant"}})
    s.close()
    source = ClaudeCodeSource()
    parse(session_log(s.path), source=source)
    assert source.unknown_types == {"non-string": 2, "q" * 64: 1}


def test_discovery_does_not_follow_symlinked_folders(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    subagents = projects / "-p" / "s1" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a.jsonl").write_text("")
    (subagents / "loop").symlink_to(subagents, target_is_directory=True)
    (projects / "-link").symlink_to(projects / "-p", target_is_directory=True)
    found = logs_of(projects)
    assert [(f.project_dir, f.agent_id) for f in found] == [("-p", "a")]


def test_workflow_agent_gets_agent_info(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    wf = projects / "-p" / "s1" / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "agent-w1.jsonl").write_text("")
    (wf / "agent-w1.meta.json").write_text(
        json.dumps({"agentType": "reviewer", "workflowPhase": "review", "spawnDepth": 2})
    )
    (log,) = logs_of(projects)
    (agent,) = of(parse(log), AgentInfo)
    assert agent == AgentInfo("w1", "s1", agent_type="reviewer", spawn_depth=2)


def test_cost_state_timestamp(sample: tuple[Path, dict[str, Any]]) -> None:
    projects, _ = sample
    main = next(f for f in logs_of(projects) if f.kind == LogKind.SESSION)
    (cost,) = of(parse(main), CostState)
    assert cost.timestamp == at(4102)  # the last reply before it
    # A parse that starts at the cost-state line has seen no time yet.
    (late,) = of(parse(main, start=cost.offset), CostState)
    assert late.offset == cost.offset and late.timestamp is None


def test_cost_state_next_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = SessionWriter(tmp_path, cwd="/w", session_id="s1")
    s.assistant(Reply(at(10), output=5))
    s.cost_state(0.5, {})
    s.unknown_record()  # never decoded: does not end the window
    s.ai_title("Synthetic")  # decoded, but has no time
    s.cost_state(0.6, {})
    s.turn_duration(at(9), 100)  # out of order: still the first one after them
    s.assistant(Reply(at(20), output=5))
    s.cost_state(0.7, {})
    s.close()
    log = session_log(s.path)
    batches = parse(log)
    costs = of(batches, CostState)
    assert [c.total_cost_usd for c in costs] == [0.5, 0.6, 0.7]
    assert [c.timestamp for c in costs] == [at(10), at(10), at(20)]
    assert [c.next_timestamp for c in costs] == [at(9), at(9), None]
    # Records keep file order: the held cost-states come before the turn event.
    kinds = [type(r).__name__ for r in batches[0].records]
    assert kinds.index("CostState") < kinds.index("Event")
    # A cost-state at the end of a batch leaves next_timestamp to the store.
    monkeypatch.setattr(claude_code, "MAX_BATCH_LINES", 2)
    split = of(parse(log), CostState)
    assert split[0].next_timestamp is None and split[1].next_timestamp == at(9)
    # A batch's first decoded time, in file order: what the store fills a
    # batch-ending cost-state from.
    assert [b.first_timestamp for b in batches] == [at(10)]
