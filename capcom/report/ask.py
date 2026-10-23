"""Ask Claude: the website's chat, answered by the user's own `claude` CLI.

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
question. The page never sends text
from the logs beyond tool names, file paths and short labels (static/ask.js).

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
TIMEOUT = 60.0  # seconds a question may take, from start to the last word
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
    "80 words. No headings, lists, tables or markdown. If you need a technical word, say "
    "what it means in a few words. Use only the numbers you are given and never make "
    "numbers up. Keep what each number means exactly as given: a total over many "
    "requests stays a total, never an amount per request. If the numbers do not answer "
    "the question, say so in one sentence. capcom's "
    "dollars are API-equivalent cost at list prices, not the person's bill. A finding's "
    "saving is an estimate of what a change would save in a week at this pace. "
    "When the person selected some text on the page, explain what that text means, "
    "using the part of the page around it as context. Pieces marked [left out] were "
    "private and removed: never guess what they said. "
    "After the answer, write a line that is exactly SUGGESTIONS: and then two or three "
    "short follow-up questions the person might ask next, one per line, each under eight "
    "words, written the way the person would ask them."
)

EXPLAIN = "Explain this part to me very shortly and simply."
EXPLAIN_SELECTION = "Explain the text I selected, very shortly and simply."

# What a selection may have crossed and lost (static/ask.js, PRIVATE): only these
# names come in, so the page cannot send anything else in their place.
LEFT_OUT = (
    "session names",
    "agent names",
    "evidence rows",
    "table rows",
    "your notes",
    "pasted config",
)


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
        r"</?(part|selection|chat|message)\b", lambda m: m.group(0).replace("<", "\u2039"), text
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
    return Question(page, label, text, tuple(turns), question, selection, where, left_out)


def build_prompt(q: Question) -> str:
    """The user message the CLI gets: the page, the part, the chat so far, the question."""
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
    if q.history:
        who = {"user": "Person", "assistant": "You"}
        lines = [f"{who[role]}: {_fence(text)}" for role, text in q.history]
        out.append("The chat so far:\n<chat>\n" + "\n".join(lines) + "\n</chat>")
    out.append(f"Their message:\n<message>\n{_fence(q.question)}\n</message>")
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


def command(claude: str) -> list[str]:
    """`claude -p` with nothing to act with: no tools, no MCP servers (with `ENV`,
    not the claude.ai connectors either), no slash commands or skills, no settings
    files (so no hooks or plugins from them), no session saved; the answer as
    stream-json with the partial messages."""
    return [
        claude,
        "-p",
        "--model",
        MODEL,
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disallowedTools",
        DENIED,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        "--effort",
        "low",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--system-prompt",
        SYSTEM,
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


def start(claude: str, prompt: str) -> Child:
    """Start the CLI on the prompt, in an empty temporary folder."""
    folder = tempfile.mkdtemp(prefix="capcom-ask-")
    try:
        proc = subprocess.Popen(
            command(claude),
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
                shown = split.feed(piece)
                if shown:
                    yield {"type": "text", "text": shown}
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
