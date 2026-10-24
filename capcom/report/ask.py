"""Ask Roger: the website's chat, answered by the user's own `claude` CLI.

`POST /api/ask` (server.py) takes a question about a part of a page, builds a
prompt from the few fields allowed in (`build_prompt`), and runs `claude -p`
headless with every tool, MCP server, slash command and setting source off and
no session saved (`command`). The answer streams back to the page as it is
written, one JSON object per line: `{"type": "text", "text": ...}` pieces, then
`{"type": "done", "suggestions": [...]}`, or `{"type": "error", ...}`.

capcom itself makes no network request: the CLI does, with the user's own
sign-in, and only when the user asks in the page. What goes in is what the page
sends: the page's name, a part's label and its numbers and labels in capcom's own
wording, the text the user selected on the page (without the private pieces it
crossed, which are only named), where it sits, the chat's earlier turns and the
question. The page never sends text from the logs beyond tool names, file paths
and short labels (static/ask.js). With a part comes a reference to what it is
about (a finding, a marked change, a session, a day, the page's range: `Ref`),
and the server adds what stands behind it from the index, in capcom's own words
(askfacts.py): totals by project, model, agent type and day, the top session ids,
and for an agent the files that define it and the projects that spawned it.

For what the page did not anticipate, the CLI may look things up with capcom's own
read-only tools, and only those (asktools.py, an MCP server it starts for the one
question): aggregates, labels, ids and file paths again, never a session's title
or other text from the logs. Each lookup shows in the chat as it starts.

The child runs in an empty temporary folder (no CLAUDE.md or AGENTS.md to pick
up), reads the prompt on stdin (not in its arguments, which `ps` shows), and is
killed by its PID, with its process group, when the page goes away, when it runs
past the timeout, or when the server's reply fails.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

MODEL = "claude-haiku-5-5"
MAX_BODY = 20 * 1024  # bytes: a question, a part, a selection and the earlier turns
TIMEOUT = 90.0  # seconds a question may take, from start to the last word, lookups included
MAX_TURNS_CLI = 6  # the CLI's turns per question: a few lookups, then the answer
SLOTS = 2  # questions answered at once
MAX_PAGE = 40
MAX_LABEL = 160
MAX_PART = 3000
MAX_SELECTION = 1000
MAX_WHERE = 200
MAX_TURNS = 8
MAX_TURN = 1500
MAX_QUESTION = 600
MAX_SUGGESTIONS = 3
MAX_SUGGESTION = 80
TICK = 0.1  # seconds between two looks at the child's output and the page

SYSTEM = (
    "You are the helper inside capcom, a local tool that shows a person where their "
    "Claude Code tokens and dollars go. The person reads English as a second language. "
    "Answer in plain, everyday words and short sentences: two to four sentences, under "
    "80 words, unless the message says otherwise. No headings, lists, tables or "
    "markdown. If you need a technical word, say what it means in a few words. Use "
    "only the numbers you are given and never make numbers up. Keep what each number "
    "means exactly as given: a total over many requests stays a total, never an amount "
    "per request. If the numbers do not answer the question, say so in one sentence. "
    "capcom's dollars are API-equivalent cost at list prices, not the person's bill. A finding's "
    "saving is an estimate of what a change would save in a week at this pace. "
    "When the person selected some text on the page, explain what that text means, "
    "using the part of the page around it as context. Pieces marked [left out] were "
    "private and removed: never guess what they said. "
    "After the answer, write a line that is exactly SUGGESTIONS: and then two or three "
    "short follow-up questions the person might ask next, one per line, each under eight "
    "words, written the way the person would ask them."
)

# Added when the CLI has capcom's tools (asktools.py).
SYSTEM_TOOLS = (
    " A <facts> block, when there is one, says what stands behind the part, from "
    "capcom's index. When the page and the facts do not answer, look it up with "
    "capcom's tools (spend, session, findings, where_defined): at most three lookups, "
    "then answer. Write nothing before a lookup. The tools never give session titles: "
    "name a session by its project and date, and a file by its full path."
)

EXPLAIN = "What does this show, and what stands out?"
EXPLAIN_SELECTION = "Explain the text I selected, very shortly and simply."
# How to answer an "Explain" mark (EXPLAIN on a part): the card, then its main
# insights, from its own numbers.
EXPLAIN_HOW = (
    "How to answer: first say in one plain sentence what this part shows. Then say the "
    "one to three most important things its numbers tell: for example the biggest item "
    "and its share of the total, what changed, anything unusual, and what it means for "
    "the person's spend or limits. Quote the real numbers from the part, and never make "
    'one up. A row named only "a session" is a session whose title was left out: never '
    "guess its name. Up to five short sentences, under 110 words."
)

# What a selection may have crossed and lost (static/ask.js, PRIVATE): only these
# names come in, so the page cannot send anything else in their place.
LEFT_OUT = (
    "session names",
    "agent names",
    "evidence rows",
    "tool details",
    "reply text",
    "your notes",
    "pasted config",
)


# A part's reference (`Ref`): what kinds there are, and the fields each takes.
REF_KINDS = ("finding", "mark", "session", "day", "view")
_REF_FINDING = re.compile(r"[a-z][a-z0-9_]{0,59}")
_REF_SESSION = re.compile(r"[0-9A-Za-z_-]{8,64}")
_REF_DAY = re.compile(r"\d{4}-\d\d-\d\d")
MAX_SCOPE = 200
MAX_DAYS = 90


class AskError(Exception):
    """A body that cannot be asked: its HTTP status, a code and a message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ------------------------------------------------------------------ the prompt

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(value: Any, limit: int, name: str, *, required: bool = False) -> str:
    """A string field, without control characters (but newlines), at most `limit`."""
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AskError(400, "bad_request", f"{name} must be a string.")
    text = _CONTROL.sub(" ", value.replace("\r\n", "\n")).strip()
    if required and not text:
        raise AskError(400, "bad_request", f"{name} is empty.")
    if len(text) > limit:
        raise AskError(413, "too_large", f"{name} is longer than {limit} characters.")
    return text


def _fence(text: str) -> str:
    """Text inside a <tag>...</tag> block, unable to close the block early."""
    return re.sub(
        r"</?(part|selection|chat|message|facts)\b",
        lambda m: m.group(0).replace("<", "\u2039"),
        text,
    )


@dataclass(frozen=True, slots=True)
class Question:
    """The fields of a body that go into the prompt, cleaned; nothing else does."""

    page: str
    label: str
    part: str
    history: tuple[tuple[str, str], ...]
    question: str
    selection: str = ""  # the text the user selected, its private pieces left out
    where: str = ""  # where it sits: the page, the card's and the section's headings
    left_out: tuple[str, ...] = ()  # what kinds of private pieces it lost (LEFT_OUT)
    ref: dict[str, Any] | None = None  # what the part is about (`parse_ref`), checked


def parse_ref(raw: Any) -> dict[str, Any] | None:
    """A part's reference, only its known fields, each checked; None for anything
    that is not one (a page of an older capcom, a part that has none)."""
    if not isinstance(raw, dict) or raw.get("kind") not in REF_KINDS:
        return None
    kind = raw["kind"]

    def days() -> int | None:
        d = raw.get("days")
        return d if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= MAX_DAYS else None

    if kind == "finding":
        finding, scope = raw.get("finding"), raw.get("scope")
        if not (isinstance(finding, str) and _REF_FINDING.fullmatch(finding)):
            return None
        if not (isinstance(scope, str) and 0 < len(scope) <= MAX_SCOPE) or _CONTROL.search(scope):
            return None
        return {"kind": kind, "finding": finding, "scope": scope, "days": days()}
    if kind == "mark":
        i = raw.get("id")
        ok = isinstance(i, int) and not isinstance(i, bool) and 0 < i < 10**9
        return {"kind": kind, "id": i} if ok else None
    if kind == "session":
        i = raw.get("id")
        return {"kind": kind, "id": i} if isinstance(i, str) and _REF_SESSION.fullmatch(i) else None
    if kind == "day":
        d = raw.get("date")
        return {"kind": kind, "date": d} if isinstance(d, str) and _REF_DAY.fullmatch(d) else None
    return {"kind": kind, "days": days()}


def parse(body: Any) -> Question:
    """The allowed fields of a POST /api/ask body; any other field is dropped."""
    if not isinstance(body, dict):
        raise AskError(400, "bad_request", "The body must be a JSON object.")
    page = _clean(body.get("page"), MAX_PAGE, "page") or "unknown"
    if not re.fullmatch(r"[A-Za-z][A-Za-z ?'-]*", page):
        raise AskError(400, "bad_request", "page is not a page name.")
    part = body.get("part")
    label = text = ""
    if part is not None:
        if not isinstance(part, dict):
            raise AskError(400, "bad_request", "part must be an object.")
        label = _clean(part.get("label"), MAX_LABEL, "part.label")
        text = _clean(part.get("text"), MAX_PART, "part.text")
    raw = body.get("history") or []
    if not isinstance(raw, list):
        raise AskError(400, "bad_request", "history must be a list.")
    turns: list[tuple[str, str]] = []
    for turn in raw[-MAX_TURNS:]:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            raise AskError(400, "bad_request", "Each turn needs a role, user or assistant.")
        said = _clean(turn.get("text"), MAX_TURN, "a turn's text")
        if said:
            turns.append((str(turn["role"]), said))
    selection = _clean(body.get("selection"), MAX_SELECTION, "selection")
    where = " ".join(_clean(body.get("where"), MAX_WHERE, "where").split())
    raw = body.get("left_out") or []
    if not isinstance(raw, list) or any(k not in LEFT_OUT for k in raw):
        raise AskError(400, "bad_request", "left_out lists only what a selection may lose.")
    left_out = tuple(k for k in LEFT_OUT if k in raw)
    picked = bool(selection or left_out)
    default = EXPLAIN_SELECTION if picked else EXPLAIN
    question = _clean(body.get("question"), MAX_QUESTION, "question") or default
    ref = parse_ref(part.get("ref")) if isinstance(part, dict) else None
    return Question(page, label, text, tuple(turns), question, selection, where, left_out, ref)


def build_prompt(q: Question, facts: str = "") -> str:
    """The user message the CLI gets: the page, the part and what stands behind it
    (`facts`, askfacts.Facts.context), the chat so far, the question."""
    out = [f"The person is looking at capcom's {q.page} page."]
    if q.selection or q.left_out:
        at = f' (at "{_fence(q.where)}")' if q.where else ""
        out.append(f"They selected this text on the page{at}:")
        shown = _fence(q.selection) or "[left out]"
        out.append(f"<selection>\n{shown}\n</selection>")
        if q.left_out:
            out.append(
                "Private pieces of it were left out and marked [left out]: "
                + ", ".join(q.left_out)
                + "."
            )
        if q.part:
            out.append(
                f'It sits in this part of the page ("{_fence(q.label) or "a part"}"). '
                "What the part shows, in capcom's own words and numbers:"
            )
            out.append(f"<part>\n{_fence(q.part)}\n</part>")
    elif q.label or q.part:
        out.append(
            f'They picked this part of the page ("{_fence(q.label) or "a part"}"). '
            "What it shows, in capcom's own words and numbers:"
        )
        out.append(f"<part>\n{_fence(q.part)}\n</part>")
    if facts:
        out.append(
            "What stands behind it, from capcom's index, in capcom's own words:\n"
            f"<facts>\n{_fence(facts)}\n</facts>"
        )
    if q.history:
        who = {"user": "Person", "assistant": "You"}
        lines = [f"{who[role]}: {_fence(text)}" for role, text in q.history]
        out.append("The chat so far:\n<chat>\n" + "\n".join(lines) + "\n</chat>")
    out.append(f"Their message:\n<message>\n{_fence(q.question)}\n</message>")
    if q.question == EXPLAIN and q.part and not (q.selection or q.left_out):
        out.append(EXPLAIN_HOW)
    return "\n\n".join(out) + "\n"


# ------------------------------------------------------------------ the command


# belt and braces over `--tools ""`: the tools that act, denied by name too
DENIED = (
    "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Agent,Skill,"
    "Artifact,ArtifactComments,ArtifactData"
)
# the claude.ai connectors are MCP servers too, loaded apart from --mcp-config
ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def find_claude() -> str | None:
    """The user's `claude` on PATH, or None."""
    return shutil.which("claude")


def command(claude: str, tools: str | None = None) -> list[str]:
    """`claude -p` with nothing to act with: no built-in tools, no MCP servers (with
    `ENV`, not the claude.ai connectors either) but capcom's own when `tools` gives
    its `--mcp-config` (asktools.mcp_config), and then only its tools allowed and
    anything else denied without asking; no slash commands or skills, no settings
    files (so no hooks or plugins from them), no session saved, a few turns; the
    answer as stream-json with the partial messages."""
    lookups: list[str] = []
    system = SYSTEM
    if tools is not None:
        from capcom.report.asktools import allowed

        lookups = [
            "--mcp-config",
            tools,
            "--allowedTools",
            allowed(),
            "--permission-mode",
            "dontAsk",
        ]
        system = SYSTEM + SYSTEM_TOOLS
    return [
        claude,
        "-p",
        "--model",
        MODEL,
        "--tools",
        "",
        "--strict-mcp-config",
        *lookups,
        "--setting-sources",
        "",
        "--disallowedTools",
        DENIED,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--max-turns",
        str(MAX_TURNS_CLI),
        "--effort",
        "low",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--system-prompt",
        system,
    ]


# ------------------------------------------------------------------ the answer

_MARK = "SUGGESTIONS:"
_MARK_RE = re.compile(r"(?:^|\n)[ \t*_#>-]*suggestions[ \t*_]*:[ \t*_]*", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•>]+|\d+[.)])\s*")


@dataclass(slots=True)
class Splitter:
    """Parts the streamed text into the answer, sent as it comes, and the suggested
    replies after the SUGGESTIONS: line, held back. A line that may be the start of
    that marker waits until it is clearly not."""

    text: str = ""
    sent: int = 0
    cut: int | None = None  # where the marker starts, once seen
    after: int = 0  # where the suggestions start

    def feed(self, piece: str) -> str:
        """Add a piece of the answer; return the part of it safe to show now."""
        self.text += piece
        if self.cut is not None:
            return ""
        m = _MARK_RE.search(self.text)
        if m:
            self.cut, self.after = m.start(), m.end()
            return self._take(self.cut)
        line = self.text.rfind("\n") + 1
        tail = self.text[line:].lstrip(" \t*_#>-").upper()
        if tail:
            maybe = len(tail) < len(_MARK) and _MARK.startswith(tail)
            hold = line if maybe else len(self.text)
        else:  # only spaces or bullets on the new line so far, or nothing
            hold = line if line < len(self.text) else len(self.text)
        return self._take(hold)

    def _take(self, end: int) -> str:
        if end <= self.sent:
            return ""
        out, self.sent = self.text[self.sent : end], end
        return out

    def finish(self) -> tuple[str, list[str]]:
        """The rest of the answer, and the suggested replies."""
        if self.cut is None:
            return self._take(len(self.text)), []
        out: list[str] = []
        for line in self.text[self.after :].splitlines():
            s = _BULLET.sub("", line).strip().strip("\"“”'*_").strip()
            if s and len(s) <= MAX_SUGGESTION and s not in out:
                out.append(s)
            if len(out) == MAX_SUGGESTIONS:
                break
        return "", out


def text_of(event: dict[str, Any]) -> str | None:
    """The answer's text in one stream-json line: a text delta of a partial message."""
    if event.get("type") != "stream_event":
        return None
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        text = delta.get("text")
        return text if isinstance(text, str) else None
    return None


# What the chat says while a lookup runs: capcom's own words for each tool
# (asktools.TOOLS), never the CLI's arguments but the grouping's name.
LOOKUPS = {
    "spend": "Looking up the spend",
    "session": "Looking up a session",
    "findings": "Looking up the findings",
    "where_defined": "Looking up where it is defined",
}
LOOKUP_ANY = "Looking up something"
_BY = {
    "project": "project",
    "model": "model",
    "agent_type": "agent type",
    "day": "day",
    "session": "session",
}
_TOOL_PREFIX = "mcp__capcom__"


def lookups_of(event: dict[str, Any]) -> list[dict[str, str]]:
    """The lookups an assistant message starts, as `{"type": "tool", ...}` lines."""
    if event.get("type") != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    out: list[dict[str, str]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        short = (
            name[len(_TOOL_PREFIX) :]
            if isinstance(name, str) and name.startswith(_TOOL_PREFIX)
            else ""
        )
        label = LOOKUPS.get(short, LOOKUP_ANY)
        args = block.get("input")
        by = args.get("group_by") if isinstance(args, dict) else None
        if short == "spend" and isinstance(by, str) and by in _BY:
            label += f" by {_BY[by]}"
        tool = short if short in LOOKUPS else ""
        done = "Looked up" + label[len("Looking up") :]
        out.append({"type": "tool", "tool": tool, "label": label + "…", "done": done})
    return out


@dataclass(slots=True)
class Child:
    """A running `claude -p`: started in its own process group, killed by PID."""

    proc: subprocess.Popen[bytes]
    folder: str
    killed: bool = False
    stderr_tail: bytes = field(default=b"")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.killed = True
            if hasattr(os, "killpg"):
                with contextlib.suppress(OSError):
                    os.killpg(self.proc.pid, signal.SIGKILL)  # its group: the pgid is its PID
            with contextlib.suppress(OSError):
                self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)

    def close(self) -> None:
        self.kill()
        for pipe in (self.proc.stdout, self.proc.stdin):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        shutil.rmtree(self.folder, ignore_errors=True)


def start(claude: str, prompt: str, tools: str | None = None) -> Child:
    """Start the CLI on the prompt, in an empty temporary folder; with `tools` (an
    `--mcp-config`), able to look things up with capcom's tools only."""
    folder = tempfile.mkdtemp(prefix="capcom-ask-")
    try:
        proc = subprocess.Popen(
            command(claude, tools),
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ | ENV,
        )
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass  # it exited at once; its exit code says so below
    return Child(proc, folder)


def stream(
    child: Child,
    gone: Callable[[], bool],
    timeout: float = TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[dict[str, Any]]:
    """The reply's lines, as the child writes its answer. Stops, and kills the child,
    when `gone()` says the page left or the timeout passes."""
    out = child.proc.stdout
    assert out is not None
    fd = out.fileno()
    split = Splitter()
    buf = b""
    error: str | None = None
    end = clock() + timeout
    eof = False
    looked = False  # a lookup started since the last piece of text
    while not eof:
        if gone():
            child.kill()
            return
        left = end - clock()
        if left <= 0:
            child.kill()
            yield {
                "type": "error",
                "error": "timeout",
                "message": f"Claude took more than {timeout:.0f} s, so capcom stopped it.",
            }
            return
        ready, _, _ = select.select([fd], [], [], min(TICK, left))
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            eof = True
        buf += chunk
        *lines, buf = buf.split(b"\n")
        if eof and buf:
            lines.append(buf)
        for raw in lines:
            try:
                event = json.loads(raw)
            except ValueError:
                continue  # not a stream-json line
            if not isinstance(event, dict):
                continue
            piece = text_of(event)
            if piece:
                if looked and split.text and not split.text[-1].isspace():
                    piece = " " + piece  # the answer goes on after a lookup
                looked = False
                shown = split.feed(piece)
                if shown:
                    yield {"type": "text", "text": shown}
            elif event.get("type") == "assistant":
                for line in lookups_of(event):
                    looked = True
                    yield line
            elif event.get("type") == "result" and event.get("subtype") == "error_max_turns":
                if not split.text:
                    error = "Roger used all his lookups without an answer. Ask a narrower question."
            elif event.get("type") == "result" and event.get("is_error"):
                said = event.get("result")
                ok = isinstance(said, str) and bool(said)
                error = str(said)[:300] if ok else "Claude answered with an error."
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.proc.wait(timeout=5)
    rest, suggestions = split.finish()
    if rest:
        yield {"type": "text", "text": rest}
    code = child.proc.returncode
    if error or (not split.text and code != 0):
        message = error or (
            f"claude stopped without an answer (exit code {code}). Run claude in a terminal "
            "once to check it is signed in."
        )
        yield {"type": "error", "error": "claude_failed", "message": message}
        return
    yield {"type": "done", "suggestions": suggestions}
