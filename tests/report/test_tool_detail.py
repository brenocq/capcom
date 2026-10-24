"""The agent page's step folds: a tool call's input and result read from its log only
when a fold opens (`GET /api/tool`, capcom/report/tooldetail.py), never from a path
the client sends, never cached, and left out of the agent reply and of Ask Roger;
a thinking step's tokens and cost from the index. Synthetic logs only."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.config import Config
from capcom.report import ask, tooldetail
from capcom.report.server import ApiCache, Site, background_scan
from tests.fixtures.generate import Reply, SessionWriter, at
from tests.report import layout
from tests.report.conftest import FAKE, NOW, TABLE, Client, serve

CWD = "/home/user/proj"
SECRET_CMD = "echo zebra-violet-1234 && ls /tmp/hidden-place"
SECRET_OUT = "kumquat-output-line"
SECRET_OLD = "old_text_marmalade"
SECRET_NEW = "new_text_persimmon"


def result(w: SessionWriter, tid: str, t: float, content: Any, is_error: bool = False) -> None:
    rec = w._common(at(t))
    rec.update(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": content,
                        "is_error": is_error,
                    }
                ],
            },
            "toolUseResult": {"stdout": "", "stderr": "", "interrupted": False},
        }
    )
    w.write(rec)


def session(root: Path) -> dict[str, str]:
    """A main thread that runs a failing command with 100 lines of output, edits a
    file and spawns a subagent that reads one."""
    w = SessionWriter(root, CWD, "sess-t")
    w.user_prompt(at(0))
    w.assistant(
        Reply(
            at(10),
            tools=[("Bash", {"command": SECRET_CMD, "description": "List the hidden place"})],
            records=3,  # streamed: the call's record repeats
            cache_read=5000,
            thinking=40,
            output=120,
        )
    )
    (bash,) = w.last_tool_ids
    out = "Exit code 2\n" + "\n".join(f"{SECRET_OUT} {n}" for n in range(100))
    result(w, bash, 12.5, out, is_error=True)
    w.assistant(
        Reply(
            at(20),
            tools=[
                (
                    "Edit",
                    {
                        "file_path": "/home/user/proj/a.py",
                        "old_string": f"{SECRET_OLD}\nsecond",
                        "new_string": f"{SECRET_NEW}\nsecond\nthird",
                    },
                )
            ],
            cache_write_1h=2000,
            cache_write_5m=300,
        )
    )
    (edit,) = w.last_tool_ids
    result(w, edit, 21, "The file was updated.")
    w.assistant(Reply(at(30), tools=[("Agent", {"subagent_type": "scout", "prompt": "x"})]))
    (spawn,) = w.last_tool_ids
    sub = w.subagent("scout", "look", tool_use_id=spawn)
    sub.user_prompt(at(30), "task")
    sub.assistant(Reply(at(35), tools=[("Read", {"file_path": "/p/b.py"})]))
    (read,) = sub.last_tool_ids
    result(sub, read, 36, [{"type": "text", "text": "subagent-file-body"}])
    sub.assistant(Reply(at(40), text="done"))
    result(w, spawn, 41, "ok")
    w.assistant(Reply(at(50), text="all done"))
    return {"bash": bash, "edit": edit, "read": read, "agent": sub.agent_id or ""}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / ".claude", tmp_path / ".openclaw", tmp_path / "cache" / "capcom", tmp_path / "st"
    )


@pytest.fixture
def ids(config: Config) -> dict[str, str]:
    got = session(config.claude_root)
    background_scan(config)
    return got


def site(config: Config, **kw: Any) -> Site:
    kw.setdefault("log_roots", (config.projects_dir,))
    return Site(config.index_path, TABLE, FAKE, clock=lambda: NOW, **kw)


@pytest.fixture
def client(config: Config, ids: dict[str, str]) -> Iterator[tuple[Client, Site]]:
    s = site(config, cache=ApiCache())
    for c in serve(s):
        yield c, s


def tool(c: Client, s: Site, path: str, token: bool = True) -> tuple[int, Any]:
    status, body, _ = c.get(path, headers={"X-Capcom-Token": s.token} if token else {})
    return status, body


# ------------------------------------------------------------------ the endpoint


def test_a_bash_call_and_its_result(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}")
    assert status == 200
    assert body["name"] == "Bash"
    fields = {f["key"]: f["value"] for f in body["input"]["fields"]}
    assert fields == {"command": SECRET_CMD, "description": "List the hidden place"}
    r = body["result"]
    assert r["error"] is True and r["exit"] == 2 and r["interrupted"] is False
    assert r["seconds"] == 2.5 and r["lines"] == 101 and r["cut"] is False
    assert f"{SECRET_OUT} 99" in r["text"]


def test_an_edit_carries_its_change(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['edit']}")
    assert status == 200
    assert [f["key"] for f in body["input"]["fields"]] == ["file_path"]
    e = body["input"]["edit"]
    assert (e["removed"], e["added"]) == (2, 3)
    assert e["hunks"] == [{"old": f"{SECRET_OLD}\nsecond", "new": f"{SECRET_NEW}\nsecond\nthird"}]
    assert body["result"]["exit"] is None and body["result"]["error"] is False


def test_a_subagent_call_is_found_in_its_own_log(
    client: tuple[Client, Site], ids: dict[str, str]
) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/tool/sess-t?agent={ids['agent']}&id={ids['read']}")
    assert status == 200 and body["result"]["text"] == "subagent-file-body"
    # the same call asked of another thread is not known there
    status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['read']}")
    assert status == 404 and body["error"] == "not_found"


def test_unknown_and_malformed_ids(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    assert tool(c, s, "/api/tool/sess-t?agent=main&id=toolu_nothere")[0] == 404
    assert tool(c, s, "/api/tool/nosuch?agent=main&id=" + ids["bash"])[0] == 404
    for bad in (
        "/api/tool/sess-t?agent=main&id=",
        "/api/tool/sess-t?agent=main&id=../../etc/passwd",
        "/api/tool/sess-t?agent=..%2F..%2Fx&id=" + ids["bash"],
        "/api/tool/..%2F..%2F.claude?agent=main&id=" + ids["bash"],
        "/api/tool/sess-t?agent=main&id=%2Fetc%2Fpasswd",
        "/api/tool/sess-t?agent=main&id=a%00b",
    ):
        status, body = tool(c, s, bad)
        assert status in (400, 404), bad
        assert "text" not in json.dumps(body), bad
    # a path segment more is no endpoint at all
    assert tool(c, s, "/api/tool/sess-t/../../x?id=" + ids["bash"])[0] == 404


def test_it_needs_the_launch_token(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}", token=False)
    assert status == 403 and SECRET_OUT not in json.dumps(body)


def test_off_without_log_roots(config: Config, ids: dict[str, str]) -> None:
    s = site(config, log_roots=())
    for c in serve(s):
        status, _ = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}")
        assert status == 404
        _, page, _ = c.get("/session/sess-t/agent/main")
        assert b"capcom-tool-details" not in page
    s = site(config)
    for c in serve(s):
        _, page, _ = c.get("/session/sess-t/agent/main")
        assert b'<meta name="capcom-tool-details" content="on">' in page


def test_only_files_under_the_log_roots_are_read(
    config: Config, ids: dict[str, str], tmp_path: Path
) -> None:
    # the index knows the file, but it is outside the roots this server reads
    s = site(config, log_roots=(tmp_path / "elsewhere",))
    (tmp_path / "elsewhere").mkdir()
    for c in serve(s):
        status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}")
        assert status == 404 and body["error"] == "gone"
    # a link inside a root that leads out of it is not followed
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    link = tmp_path / "elsewhere" / "x.jsonl"
    link.symlink_to(outside)
    assert tooldetail.allowed([link, outside], [tmp_path / "elsewhere"]) == []
    inside = sorted(config.projects_dir.rglob("*.jsonl"))
    assert tooldetail.allowed(inside, [config.projects_dir]) == [p.resolve() for p in inside]


def test_a_deleted_log_is_said_quietly(
    client: tuple[Client, Site], ids: dict[str, str], config: Config
) -> None:
    c, s = client
    main = next(config.projects_dir.rglob("sess-t.jsonl"))
    main.unlink()
    status, body = tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}")
    assert status == 404 and body["error"] == "gone"


def test_the_text_never_reaches_the_cache_or_the_agent_reply(
    client: tuple[Client, Site], ids: dict[str, str]
) -> None:
    c, s = client
    status, d, _ = c.get("/api/agent/sess-t?agent=main")
    assert status == 200
    for _ in range(3):  # the fold's text is asked for, a few times
        assert tool(c, s, f"/api/tool/sess-t?agent=main&id={ids['bash']}")[0] == 200
    reply = json.dumps(d)
    for secret in (SECRET_CMD, "zebra-violet", SECRET_OUT, SECRET_OLD, SECRET_NEW, "hidden place"):
        assert secret not in reply
    assert s.cache is not None
    cached = b"".join(body for _, body in s.cache._entries.values())
    assert cached and b"kumquat" not in cached and b"zebra" not in cached
    # what the folds need is there: each call's id, and each request's tokens and cost
    tools = d["steps"]["tools"]
    assert [t[0][0] for t in tools if t][:2] == [ids["bash"], ids["edit"]]
    R = d["requests"]
    j = d["steps"]["request"][d["steps"]["kind"].index("think")]
    assert R["thinking"][j] == 40 and R["output"][j] == 120 and R["cache_read"][j] == 5000
    k = next(n for n, w in enumerate(R["cache_write_1h"]) if w == 2000)
    parts = R["parts"][k]
    assert parts["cache_write_1h"] > parts["cache_write_5m"] > 0
    assert abs(sum(parts.values()) - R["usd"][k]) < 1e-5


# ------------------------------------------------------------------ the reader


def lines(path: Path, recs: list[dict[str, Any]]) -> None:
    with path.open("a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def call_rec(tid: str, ts: str, command: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": "m1",
            "content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
            ],
        },
    }


def result_rec(tid: str, ts: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
    }


def test_a_partial_last_line_is_skipped(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    lines(p, [call_rec("toolu_a", "2026-10-01T10:00:00.000Z", "ls")])
    with p.open("a") as f:  # the result, still being written
        f.write(json.dumps(result_rec("toolu_a", "2026-10-01T10:00:01.000Z", "x"))[:40])
    got = tooldetail.detail([p], "toolu_a", None)
    assert got is not None and got["result"] is None
    assert got["input"]["fields"] == [{"key": "command", "value": "ls", "cut": False}]


def test_the_last_streamed_record_wins_and_big_output_is_cut(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    big = "y" * 300 + "\n"
    lines(
        p,
        [
            call_rec("toolu_b", "2026-10-01T10:00:00.000Z", "first"),
            call_rec("toolu_b", "2026-10-01T10:00:00.100Z", "final"),
            result_rec("toolu_b", "2026-10-01T10:00:03.000Z", big * 1000),
        ],
    )
    got = tooldetail.detail([p], "toolu_b", None)
    assert got is not None
    assert got["input"]["fields"][0]["value"] == "final"
    r = got["result"]
    assert r["cut"] is True and len(r["text"].encode()) == tooldetail.MAX_OUTPUT
    assert (
        r["bytes"] == len(big) * 1000 and r["lines"] == 1000 and r["seconds"] == 2.9
    )  # from the last streamed record


def test_a_big_log_is_searched_near_the_time_and_read_whole_when_out_of_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tooldetail, "CHUNK", 1 << 14)
    monkeypatch.setattr(tooldetail, "BACK_OFF", 1 << 12)
    p = tmp_path / "big.jsonl"
    t0 = at(0)
    filler = "z" * 200
    recs = []
    for n in range(3000):
        t = (t0 + timedelta(seconds=n * 10)).isoformat().replace("+00:00", "Z")
        recs.append({"type": "user", "timestamp": t, "message": {"content": filler}})
        if n == 2500:
            recs.append(call_rec("toolu_late", t, "late"))
            recs.append(result_rec("toolu_late", t, "late-out"))
        if n == 3:  # a record logged long before its time (out of order)
            late = (t0 + timedelta(seconds=29000)).isoformat().replace("+00:00", "Z")
            recs.append(call_rec("toolu_odd", late, "odd"))
            recs.append(result_rec("toolu_odd", late, "odd-out"))
    lines(p, recs)
    size = p.stat().st_size
    with p.open("rb") as f:
        start = tooldetail.seek_near(f, size, t0 + timedelta(seconds=25000))
    assert size * 0.7 < start < size * 0.85  # near the call, not the start of the file
    got = tooldetail.detail([p], "toolu_late", t0 + timedelta(seconds=25000))
    assert got is not None and got["result"]["text"] == "late-out"
    got = tooldetail.detail([p], "toolu_odd", t0 + timedelta(seconds=29000))
    assert got is not None and got["result"]["text"] == "odd-out"


# ------------------------------------------------------------------ Ask Roger


def test_ask_roger_leaves_the_folds_out() -> None:
    assert "tool details" in ask.LEFT_OUT
    js = (Path(ask.__file__).parent / "static" / "ask.js").read_text()
    drop = js.split("const DROP = [", 1)[1].split("]", 1)[0]
    assert "'.tooltext'" in drop
    assert "['.tooltext', 'tool details']" in js
    private_cell = js.split("const PRIVATE_CELL = [", 1)[1].split("];", 1)[0]
    assert ".tooltext" in private_cell
    # the facts behind a part never read a log's text, so never a tool's
    facts = (Path(ask.__file__).parent / "askfacts.py").read_text()
    assert "tooldetail" not in facts
    agent_js = (Path(ask.__file__).parent / "static" / "agent.js").read_text()
    assert "h('div.agcall.tooltext'" in agent_js


# ------------------------------------------------------------------ the page, in Chrome

chrome_only = pytest.mark.skipif(
    layout.find_chrome() is None or os.environ.get("CAPCOM_LAYOUT") == "0",
    reason="needs Chrome (set CAPCOM_CHROME), or CAPCOM_LAYOUT=0 skips it",
)


@pytest.fixture
def chrome(tmp_path: Path) -> Iterator[layout.Chrome]:
    binary = layout.find_chrome()
    assert binary
    c = layout.Chrome(binary, tmp_path / "profile")
    try:
        yield c
    finally:
        c.close()


def until(chrome: layout.Chrome, expr: str, timeout: float = 10) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if chrome.js(expr):
            return
        time.sleep(0.05)
    raise AssertionError(f"never true: {expr}")


@chrome_only
def test_the_folds_open_close_and_stay_out_of_ask(
    config: Config, ids: dict[str, str], chrome: layout.Chrome
) -> None:
    s = site(config)
    for c in serve(s):
        chrome.viewport(390, 1, "light")
        assert layout.load(chrome, c.base + "/session/sess-t/agent/main")
        until(chrome, "!!document.querySelector('.agprow')")
        chrome.js("document.querySelectorAll('.agprow').forEach((r) => r.click())")
        until(chrome, "document.querySelectorAll('.agchev').length > 0")
        # the folds are closed at first, and nothing of the call is on the page
        assert not chrome.js("document.body.textContent.includes('zebra-violet')")
        assert chrome.js(
            "[...document.querySelectorAll('.agchev')]"
            ".every((b) => b.getAttribute('aria-expanded') === 'false')"
        )
        # open the Bash step's fold from its chevron
        chrome.js(
            "[...document.querySelectorAll('tr.agsrow')]"
            ".find((r) => r.textContent.includes('Bash')).querySelector('.agchev').click()"
        )
        until(chrome, "!!document.querySelector('.agcall.tooltext')")
        text = chrome.js("document.querySelector('.agfold').textContent")
        assert "zebra-violet" in text and "exit 2" in text and "Show all 101 lines" in text
        assert chrome.js("document.querySelector('.agout').textContent.split('\\n').length") == 40
        assert chrome.js("document.activeElement.classList.contains('agchev')")
        assert chrome.js("document.activeElement.getAttribute('aria-expanded')") == "true"
        # no horizontal page scroll at 390 px
        assert chrome.js("document.documentElement.scrollWidth <= window.innerWidth + 1")
        # Ask Roger: a selection over the open fold sends none of it
        picked = chrome.js(
            "(() => { const r = document.createRange();"
            " r.selectNodeContents(document.getElementById('main'));"
            " return window.capcomAsk.pickedText(r); })()"
        )
        assert "zebra" not in json.dumps(picked) and "kumquat" not in json.dumps(picked)
        assert "tool details" in picked["leftOut"]
        # Escape closes it and drops the text
        chrome.js(
            "document.querySelector('.agfold').dispatchEvent(new KeyboardEvent('keydown',"
            " { key: 'Escape', bubbles: true }))"
        )
        until(chrome, "!document.querySelector('.agfold')")
        assert not chrome.js("document.body.textContent.includes('zebra-violet')")
        # a thinking step's fold: its request's tokens, from the index
        chrome.js(
            "[...document.querySelectorAll('tr.agsrow')]"
            ".find((r) => r.textContent.includes('Thinking')).click()"
        )
        until(chrome, "!!document.querySelector('table.agtok')")
        tok = chrome.js("document.querySelector('table.agtok').textContent")
        assert "Cache read" in tok and "5,000" in tok and "of it thinking" in tok
