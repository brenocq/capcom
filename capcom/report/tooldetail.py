"""One tool call's input and result, read from its log file when the page asks.

The agent page's fold on a tool step shows the call in full: the command, the file
and the change, the pattern, and what came back. That text is the logs' own, so
it is read only here, only for `capcom serve`'s `GET /api/tool/<session>` (on a
click), and never kept: not in the index, not in the API cache, not in a file.

The call is found by its tool_use id, which must belong to a request of the asked
thread in the index; the files read are the index's files of that thread, and only
those under the configured log roots. Nothing the client sends is opened as a path.

A log may be a gigabyte. The scan starts near the request's time: a binary search
over the file's byte offsets by the records' timestamps (the logs are appended in
time order), backed off a little, then forward to the result. When the call is not
found from there (timestamps out of order), it reads the file from the start, so
the search is only ever a shortcut. Lines are split on their own: a line without
its newline yet (a live session writing it) is skipped. Every field is optional,
and a reply's records repeat while it streams: the last record with the call wins.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_OUTPUT = 200_000  # bytes of a result's text sent at most
MAX_FIELD = 50_000  # bytes of one input field (a command, an edit's old text) at most
MAX_LINE = 64 << 20  # a longer line is skipped (never parsed)
CHUNK = 4 << 20
BACK_OFF = 2 << 20  # bytes read before the binary search's offset
LEAD_SECONDS = 120.0  # the search looks for a record this long before the request
_TS = re.compile(rb'"timestamp": ?"([0-9T:.+-]{19,40}Z?)"')
_EXIT = re.compile(r"^Exit code (-?\d+)")
# the input fields each tool's fold names, in this order; any other key is listed after
FIELDS: dict[str, tuple[str, ...]] = {
    "Bash": ("command", "description", "timeout", "run_in_background"),
    "Read": ("file_path", "offset", "limit", "pages"),
    "Write": ("file_path",),
    "Edit": ("file_path", "replace_all"),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "cell_id", "edit_mode"),
    "Grep": ("pattern", "path", "glob", "type", "output_mode", "-i", "-n", "multiline"),
    "Glob": ("pattern", "path"),
}
HIDDEN = {"Edit": {"old_string", "new_string"}, "Write": {"content"}, "MultiEdit": {"edits"}}


def valid_id(x: str) -> bool:
    return ID_RE.fullmatch(x) is not None


def allowed(paths: Iterable[Path], roots: Sequence[Path]) -> list[Path]:
    """The paths that are regular .jsonl files under one of `roots`, resolved (no
    link or `..` leads out of them)."""
    real_roots = []
    for r in roots:
        try:
            real_roots.append(r.resolve(strict=True))
        except OSError:
            continue
    out: list[Path] = []
    for p in paths:
        try:
            real = p.resolve(strict=True)
        except OSError:
            continue
        if real.suffix != ".jsonl" or not real.is_file():
            continue
        if any(real.is_relative_to(r) for r in real_roots):
            out.append(real)
    return out


def _time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _line_time(f: BinaryIO, pos: int) -> tuple[int, datetime | None]:
    """The first whole line starting after `pos`: its start and its record's time
    (None when no time is found within a few lines)."""
    f.seek(pos)
    if pos > 0:  # the rest of the line `pos` fell in, a megabyte at a time
        while (part := f.readline(1 << 20)) and not part.endswith(b"\n"):
            pass
    start = f.tell()
    for _ in range(8):
        line = f.readline(1 << 20)
        if not line:
            return start, None
        m = _TS.search(line)
        if m:
            t = _time(m.group(1).decode())
            if t is not None:
                return start, t
    return start, None


def seek_near(f: BinaryIO, size: int, when: datetime) -> int:
    """A byte offset at or before the records of `when`, found by a binary search
    over the line times (0 when it finds none before)."""
    target = when.timestamp() - LEAD_SECONDS
    lo, hi, best = 0, size, 0
    for _ in range(40):
        if hi - lo < CHUNK:
            break
        mid = (lo + hi) // 2
        start, t = _line_time(f, mid)
        if t is None or t.timestamp() >= target:
            hi = mid
        else:
            best, lo = start, mid
    return max(0, best - BACK_OFF)


def _lines_with(f: BinaryIO, needle: bytes, start: int, stop: int | None) -> Iterable[bytes]:
    """The whole lines from `start` (to `stop`, or the end) holding `needle`."""
    f.seek(start)
    carry = b""
    skipping = False  # inside a line longer than MAX_LINE
    pos = start
    while stop is None or pos < stop:
        chunk = f.read(CHUNK)
        if not chunk:
            return  # what is left in carry has no newline yet: a partial last line
        pos += len(chunk)
        buf = carry + chunk
        cut = buf.rfind(b"\n")
        if cut < 0:
            carry = b"" if skipping or len(buf) > MAX_LINE else buf
            skipping = skipping or len(buf) > MAX_LINE
            continue
        whole, carry = buf[: cut + 1], buf[cut + 1 :]
        if skipping:
            whole = whole[whole.find(b"\n") + 1 :]
            skipping = False
        i = whole.find(needle)
        while i >= 0:
            a = whole.rfind(b"\n", 0, i) + 1
            b = whole.find(b"\n", i)
            yield whole[a:b]
            i = whole.find(needle, b)
        if len(carry) > MAX_LINE:
            carry, skipping = b"", True


def _blocks(rec: Mapping[str, Any]) -> list[Any]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, Mapping) else None
    return content if isinstance(content, list) else []


def find_records(
    path: Path, tool_use_id: str, when: datetime | None
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]] | None, Mapping[str, Any] | None]:
    """((the call's record, its tool_use block) or None, the result's record or None)."""
    needle = f'"{tool_use_id}"'.encode()
    call: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    result: Mapping[str, Any] | None = None
    with open(path, "rb") as f:  # read-only
        size = f.seek(0, 2)
        begin = seek_near(f, size, when) if when is not None else 0
        spans = [(begin, None)] + ([(0, begin)] if begin > 0 else [])
        for start, stop in spans:
            for line in _lines_with(f, needle, start, stop):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                for b in _blocks(rec):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                        call = (rec, b)  # a streamed reply repeats it: the last wins
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") == tool_use_id:
                        result = rec
                if call is not None and result is not None:
                    return call, result
            if call is not None:
                break  # found from the search's offset: the result is not written yet
    return call, result


def _cut(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode(errors="ignore"), True


def _compact(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, separators=(", ", ": "))


def _lines(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _input(name: str, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The call's input as ordered key: value fields, plus an edit's change."""
    order = FIELDS.get(name, ())
    hidden = HIDDEN.get(name, set())
    keys = [k for k in order if k in inp] + [k for k in inp if k not in order and k not in hidden]
    fields = []
    for k in keys:
        v = inp[k]
        if v is None or v == "" or v is False:  # a flag left off says nothing
            continue
        text, cut = _cut(_compact(v), MAX_FIELD)
        fields.append({"key": k, "value": text, "cut": cut})
    out: dict[str, Any] = {"fields": fields}
    if name == "Edit":
        old, new = inp.get("old_string"), inp.get("new_string")
        old = old if isinstance(old, str) else ""
        new = new if isinstance(new, str) else ""
        out["edit"] = _edit([(old, new)])
    elif name == "MultiEdit" and isinstance(inp.get("edits"), list):
        pairs = [
            (str(e.get("old_string") or ""), str(e.get("new_string") or ""))
            for e in inp["edits"]
            if isinstance(e, Mapping)
        ]
        out["edit"] = _edit(pairs)
    elif name == "Write" and isinstance(inp.get("content"), str):
        content = inp["content"]
        out["write"] = {"lines": _lines(content), "bytes": len(content.encode())}
    return out


def _edit(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    removed = sum(_lines(o) for o, _ in pairs)
    added = sum(_lines(n) for _, n in pairs)
    hunks = []
    budget = MAX_FIELD * 2
    cut = False
    for old, new in pairs:
        o, c1 = _cut(old, max(0, budget // 2))
        n, c2 = _cut(new, max(0, budget // 2))
        budget -= len(o.encode()) + len(n.encode())
        cut = cut or c1 or c2
        hunks.append({"old": o, "new": n})
        if budget <= 0:
            cut = cut or len(hunks) < len(pairs)
            break
    return {"removed": removed, "added": added, "hunks": hunks, "cut": cut}


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, Mapping):
                continue
            if c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif c.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts)


def _result(rec: Mapping[str, Any], tool_use_id: str, began: datetime | None) -> dict[str, Any]:
    block: Mapping[str, Any] = next(
        (
            b
            for b in _blocks(rec)
            if isinstance(b, dict)
            and b.get("type") == "tool_result"
            and b.get("tool_use_id") == tool_use_id
        ),
        {},
    )
    text = _result_text(block)
    total = len(text.encode())
    shown, cut = _cut(text, MAX_OUTPUT)
    error = block.get("is_error") is True
    m = _EXIT.match(text)
    extra = rec.get("toolUseResult")
    extra = extra if isinstance(extra, Mapping) else {}
    end = _time(rec.get("timestamp"))
    return {
        "error": error,
        "exit": int(m.group(1)) if m else None,
        "interrupted": extra.get("interrupted") is True,
        "seconds": round((end - began).total_seconds(), 2) if end and began else None,
        "text": shown,
        "lines": _lines(text),
        "bytes": total,
        "cut": cut,
    }


def detail(paths: Sequence[Path], tool_use_id: str, when: datetime | None) -> dict[str, Any] | None:
    """The fold's content for one call, or None when no file holds it any more."""
    for path in paths:
        try:
            call, result = find_records(path, tool_use_id, when)
        except OSError:
            continue
        if call is None:
            continue
        rec, block = call
        name = block.get("name") if isinstance(block.get("name"), str) else "tool"
        inp = block.get("input")
        began = _time(rec.get("timestamp"))
        return {
            "id": tool_use_id,
            "name": name,
            "input": _input(str(name), inp if isinstance(inp, Mapping) else {}),
            "result": _result(result, tool_use_id, began) if result is not None else None,
        }
    return None


class NotFound(Exception):
    """No such call in the index, or no log file holds it any more (`gone`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def lookup(
    index: Any,
    session_id: str,
    agent: str,
    tool_use_id: str,
    roots: Sequence[Path],
    hint: datetime | None = None,
) -> dict[str, Any]:
    """`GET /api/tool/<session>?agent=&id=`: the call `tool_use_id` of the thread
    `agent` ("main" or an agent id), from the thread's own log files. Raises
    ValueError on a malformed id, NotFound when the index or the files lack it."""
    if not (valid_id(session_id) and valid_id(agent) and valid_id(tool_use_id)):
        raise ValueError("bad id")
    aid = None if agent == "main" else agent
    when = None
    # `hint`, the request's time as the page knows it, only narrows where to look
    windows = [(hint - timedelta(seconds=2), hint + timedelta(seconds=2))] if hint else []
    for since, until in [*windows, (None, None)]:
        for r in index.requests(since=since, until=until, session_id=session_id):
            if r.agent_id == aid and any(c.tool_use_id == tool_use_id for c in r.tool_calls):
                when = r.timestamp
                break
        if when is not None:
            break
    if when is None:
        raise NotFound("not_found", "No such tool call in this thread.")
    paths = allowed(index.thread_files(session_id, aid), roots)
    found = detail(paths, tool_use_id, when) if paths else None
    if found is None:
        raise NotFound("gone", "The log that held this call is gone or was rewritten.")
    return found


def hint_of(raw: str | None) -> datetime | None:
    """`?t=` (epoch ms) as a time, or None when absent or malformed."""
    if not raw or not raw.isdigit() or len(raw) > 15:
        return None
    return datetime.fromtimestamp(int(raw) / 1000, UTC)
