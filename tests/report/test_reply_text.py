"""A thinking step's fold: what its request wrote (reply text) and the thinking
summary the log kept, read from the thread's log only when the fold opens
(`GET /api/reply`, capcom/report/tooldetail.py), under the tool fold's rules; and a
fold that fails retries a busy index once and says the server's reason. Synthetic
logs only."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from capcom.config import Config
from capcom.report import ask, tooldetail
from capcom.report.server import ApiCache, Site, background_scan
from tests.fixtures.generate import T0, Reply, SessionWriter, at
from tests.report import layout
from tests.report.conftest import Client, serve
from tests.report.test_tool_detail import (  # noqa: F401  (fixtures)
    chrome,
    chrome_only,
    config,
    lines,
    result,
    site,
    tool,
    until,
)

CWD = "/home/user/proj"
SECRET_REPLY = "I will look at the quince-harbor file first."
SECRET_SUMMARY = "Considering the walnut-lantern approach"
LONG = "\n".join(f"line-of-prose-{n} about the plum-orchard" for n in range(60))


def session(root: Path) -> dict[str, str]:
    """A reply with a thinking summary and text streamed as several records, then one
    whose thinking kept no text and whose text is 60 lines, then a call still running
    (no result yet)."""
    w = SessionWriter(root, CWD, "sess-r")
    w.user_prompt(at(0))
    first = w.assistant(
        Reply(
            at(10),
            thinking_text=SECRET_SUMMARY,
            thinking=40,
            text=SECRET_REPLY,
            tools=[("Bash", {"command": "ls"})],
            records=4,  # thinking, text, the call, the call again
            output=200,
        )
    )
    (bash,) = w.last_tool_ids
    result(w, bash, 12, "a.py")
    second = w.assistant(Reply(at(20), thinking=30, text=LONG, output=900))
    w.user_prompt(at(30))
    w.assistant(Reply(at(40), tools=[("Bash", {"command": "until false; do :; done"})]))
    (running,) = w.last_tool_ids
    return {"first": first, "second": second, "running": running}


@pytest.fixture
def ids(config: Config) -> dict[str, str]:  # noqa: F811
    got = session(config.claude_root)
    background_scan(config)
    return got


@pytest.fixture
def client(config: Config, ids: dict[str, str]) -> Iterator[tuple[Client, Site]]:  # noqa: F811
    s = site(config, cache=ApiCache())
    for c in serve(s):
        yield c, s


# ------------------------------------------------------------------ the endpoint


def test_a_reply_and_its_thinking_summary(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/reply/sess-r?agent=main&id={ids['first']}")
    assert status == 200
    # streamed as four records with one message.id: each block once
    assert body["text"]["text"] == SECRET_REPLY
    assert body["text"]["lines"] == 1 and body["text"]["cut"] is False
    assert body["thinking"]["blocks"] == 1 and body["thinking"]["saved"] == 1
    assert body["thinking"]["summary"]["text"] == SECRET_SUMMARY


def test_thinking_without_text_is_said_not_saved(
    client: tuple[Client, Site], ids: dict[str, str]
) -> None:
    c, s = client
    status, body = tool(c, s, f"/api/reply/sess-r?agent=main&id={ids['second']}")
    assert status == 200
    assert body["thinking"] == {"blocks": 1, "saved": 0, "summary": None}
    assert body["text"]["lines"] == 60


def test_guards_token_ids_and_no_store(client: tuple[Client, Site], ids: dict[str, str]) -> None:
    c, s = client
    path = f"/api/reply/sess-r?agent=main&id={ids['first']}"
    status, body = tool(c, s, path, token=False)
    assert status == 403 and "quince" not in json.dumps(body)
    _, _, headers = c.get(path, headers={"X-Capcom-Token": s.token})
    assert headers["Cache-Control"] == "no-store"
    assert tool(c, s, "/api/reply/sess-r?agent=main&id=msg_nothere")[0] == 404
    # a tool_use id is no request id, and a request of the main thread is not a subagent's
    assert tool(c, s, f"/api/reply/sess-r?agent=main&id={ids['running']}")[0] == 404
    assert tool(c, s, f"/api/reply/sess-r?agent=agent-x&id={ids['first']}")[0] == 404
    for bad in (
        "/api/reply/sess-r?agent=main&id=",
        "/api/reply/sess-r?agent=main&id=../../etc/passwd",
        "/api/reply/sess-r?agent=..%2F..%2Fx&id=" + ids["first"],
        "/api/reply/..%2F..%2F.claude?agent=main&id=" + ids["first"],
        "/api/reply/sess-r?agent=main&id=a%00b",
    ):
        status, body = tool(c, s, bad)
        assert status in (400, 404), bad
        assert "quince" not in json.dumps(body), bad
    assert tool(c, s, "/api/reply/sess-r/../../x?id=" + ids["first"])[0] == 404


def test_the_text_never_reaches_the_cache_or_the_agent_reply(
    client: tuple[Client, Site], ids: dict[str, str]
) -> None:
    c, s = client
    status, d, _ = c.get("/api/agent/sess-r?agent=main")
    assert status == 200
    for _ in range(3):
        assert tool(c, s, f"/api/reply/sess-r?agent=main&id={ids['first']}")[0] == 200
    reply = json.dumps(d)
    for secret in ("quince", "walnut", "plum-orchard"):
        assert secret not in reply
    # what the fold asks by: each request's message id
    assert ids["first"] in d["requests"]["id"] and ids["second"] in d["requests"]["id"]
    assert s.cache is not None
    cached = b"".join(body for _, body in s.cache._entries.values())
    assert cached and b"quince" not in cached and b"walnut" not in cached


def test_a_busy_index_says_so(
    client: tuple[Client, Site], ids: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, s = client

    def busy(*_: Any, **__: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tooldetail, "reply_lookup", busy)
    status, body = tool(c, s, f"/api/reply/sess-r?agent=main&id={ids['first']}")
    assert status == 503 and body["error"] == "index_busy"
    assert "scan" in body["message"]


# ------------------------------------------------------------------ the reader


def rec(mid: str, ts: str, *blocks: dict[str, Any], kind: str = "assistant") -> dict[str, Any]:
    return {"type": kind, "timestamp": ts, "message": {"id": mid, "content": list(blocks)}}


def test_streamed_records_count_each_block_once(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    think = {"type": "thinking", "thinking": "", "signature": "s1"}
    lines(
        p,
        [
            rec("m1", "2026-10-01T10:00:00.000Z", think),
            rec("m1", "2026-10-01T10:00:00.100Z", think, {"type": "text", "text": "Hel"}),
            rec("m1", "2026-10-01T10:00:00.200Z", think, {"type": "text", "text": "Hello"}),
            rec("m1", "2026-10-01T10:00:00.300Z", {"type": "redacted_thinking", "data": "x"}),
            rec("m1", "2026-10-01T10:00:00.300Z", {"type": "redacted_thinking", "data": "x"}),
            # the id quoted elsewhere: a user record, and another message's text
            rec("m1", "2026-10-01T10:00:00.400Z", {"type": "text", "text": "pasted"}, kind="user"),
            {
                "type": "assistant",
                "message": {"id": "m2", "content": [{"type": "text", "text": "m1"}]},
            },
        ],
    )
    with p.open("a") as f:  # the next record, still being written
        f.write(
            json.dumps(rec("m1", "2026-10-01T10:00:01Z", {"type": "text", "text": "late"}))[:50]
        )
    got = tooldetail.reply([p], "m1", None)
    assert got is not None
    assert got["text"]["text"] == "Hello"
    # a signature-only block and a redacted one: two blocks, neither kept its text
    assert got["thinking"] == {"blocks": 2, "saved": 0, "summary": None}
    assert tooldetail.reply([p], "m9", None) is None


def test_a_reply_with_no_thinking_and_a_big_text(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    big = "w" * 299 + "\n"
    lines(p, [rec("m1", "2026-10-01T10:00:00Z", {"type": "text", "text": big * 1000})])
    got = tooldetail.reply([p], "m1", None)
    assert got is not None and got["thinking"]["blocks"] == 0
    t = got["text"]
    assert t["cut"] is True and len(t["text"].encode()) == tooldetail.MAX_OUTPUT
    assert t["bytes"] == len(big) * 1000 - 1 and t["lines"] == 1000  # trimmed


# ------------------------------------------------------------------ Ask Roger


def test_ask_roger_leaves_the_reply_out() -> None:
    assert "reply text" in ask.LEFT_OUT
    js = (Path(ask.__file__).parent / "static" / "ask.js").read_text()
    assert "'.replytext'" in js.split("const DROP = [", 1)[1].split("]", 1)[0]
    assert "['.replytext', 'reply text']" in js
    assert ".replytext" in js.split("const PRIVATE_CELL = [", 1)[1].split("];", 1)[0]
    agent_js = (Path(ask.__file__).parent / "static" / "agent.js").read_text()
    assert "h('div.agcall.agreply.replytext')" in agent_js


# ------------------------------------------------------------------ the page, in Chrome


def open_page(chrome: layout.Chrome, c: Client, width: int = 1200) -> None:  # noqa: F811
    chrome.viewport(width, 1, "light")
    assert layout.load(chrome, c.base + "/session/sess-r/agent/main")
    until(chrome, "!!document.querySelector('.agprow')")
    chrome.js("document.querySelectorAll('.agprow').forEach((r) => r.click())")
    until(chrome, "document.querySelectorAll('.agchev').length > 0")


def click_row(chrome: layout.Chrome, text: str, n: int = 0) -> None:  # noqa: F811
    chrome.js(
        "[...document.querySelectorAll('tr.agsrow')]"
        f".filter((r) => r.textContent.includes({json.dumps(text)}))[{n}].click()"
    )


@chrome_only
def test_the_thinking_fold_shows_the_reply(
    config: Config,  # noqa: F811
    ids: dict[str, str],
    chrome: layout.Chrome,  # noqa: F811
) -> None:
    s = site(config)
    for c in serve(s):
        open_page(chrome, c, 390)
        assert not chrome.js("document.body.textContent.includes('quince')")
        click_row(chrome, "Thinking")
        until(chrome, "!!document.querySelector('.agreply.replytext')")
        text = chrome.js("document.querySelector('.agfold').textContent")
        assert "Thinking (summary)" in text and SECRET_SUMMARY in text
        assert "Writing" in text and SECRET_REPLY in text
        assert chrome.js("!!document.querySelector('.agfold table.agtok')")  # the numbers stay
        assert chrome.js("document.documentElement.scrollWidth <= window.innerWidth + 1")
        picked = chrome.js(
            "(() => { const r = document.createRange();"
            " r.selectNodeContents(document.getElementById('main'));"
            " return window.capcomAsk.pickedText(r); })()"
        )
        assert "quince" not in json.dumps(picked) and "walnut" not in json.dumps(picked)
        assert "reply text" in picked["leftOut"]
        click_row(chrome, "Thinking")  # closed: the text is dropped
        until(chrome, "!document.querySelector('.agfold')")
        assert not chrome.js("document.body.textContent.includes('quince')")
        # the second reply: its thinking kept no text, and its 60 lines are cut at 40
        click_row(chrome, "Thinking", 1)
        until(chrome, "!!document.querySelector('.agreply .agprose')")
        text = chrome.js("document.querySelector('.agfold').textContent")
        assert "Thinking not saved in the log." in text and "Show all 60 lines" in text
        assert chrome.js("document.querySelector('.agprose').textContent.split('\\n').length") == 40
        chrome.js("document.querySelector('.agreply .agall').click()")
        until(chrome, "document.querySelector('.agprose').textContent.split('\\n').length === 60")


@chrome_only
def test_a_busy_index_is_asked_once_more_then_its_reason_shown(
    config: Config,  # noqa: F811
    ids: dict[str, str],
    chrome: layout.Chrome,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = tooldetail.reply_lookup
    calls: list[int] = []

    def busy_once(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(tooldetail, "reply_lookup", busy_once)
    s = site(config)
    for c in serve(s):
        open_page(chrome, c)
        click_row(chrome, "Thinking")
        until(chrome, "!!document.querySelector('.agreply.replytext')")
        assert len(calls) == 2
        click_row(chrome, "Thinking")
        until(chrome, "!document.querySelector('.agfold')")

        def busy(*_: Any, **__: Any) -> Any:
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(tooldetail, "reply_lookup", busy)
        calls.clear()
        click_row(chrome, "Thinking")
        until(chrome, "document.querySelector('.agfold').textContent.includes('a scan')")
        assert len(calls) == 2
        assert "could not be read" not in chrome.js("document.querySelector('.agfold').textContent")


@chrome_only
def test_a_call_without_a_result_may_be_still_running(
    config: Config,  # noqa: F811
    ids: dict[str, str],
    chrome: layout.Chrome,  # noqa: F811
) -> None:
    s = site(config)
    for c in serve(s):
        # long after the call: no result in the log
        open_page(chrome, c)
        click_row(chrome, "Bash", 1)
        until(chrome, "!!document.querySelector('.agcall.tooltext')")
        head = chrome.js("document.querySelector('.agcall.tooltext .agcallhead').textContent")
        assert "no result in the log" in head
        # a minute after it started, it is the thread's last call: still running
        ms = int(at(100).timestamp() * 1000)
        assert at(100) > T0
        chrome.js(f"Date.now = () => {ms}")
        click_row(chrome, "Bash", 1)
        until(chrome, "!document.querySelector('.agfold')")
        click_row(chrome, "Bash", 1)
        until(chrome, "!!document.querySelector('.agcall.tooltext')")
        head = chrome.js("document.querySelector('.agcall.tooltext .agcallhead').textContent")
        assert "still running" in head
        # the earlier call, whose result came, says how it went
        click_row(chrome, "Bash", 0)
        until(chrome, "document.querySelectorAll('.agcall.tooltext').length === 2")
        assert "exit 0" in chrome.js("document.querySelector('.agcall.tooltext').textContent")
