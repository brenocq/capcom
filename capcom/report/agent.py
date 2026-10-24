"""One thread's life, step by step: the data of the agent timeline page.

A thread is the main thread of a session or one of its subagents. Its life is cut
into steps: the model working on a reply ("Thinking and writing", from the moment
it had what it needed to the reply's time) and each batch of tool calls a reply
launched (from the reply to the last result coming back). Steps are grouped into
phases: the thread's own to-do list (TodoWrite, or TaskCreate and TaskUpdate) when
it kept one, an item's phase running from when it went in progress to when it was
completed; a stretch with no item is cut where the work changed (reading, editing,
commands) and named from the files and commands it touched (`guessed`).

Everything comes from the index (requests and tool results) plus one read-only
pass over the thread's own log files for what the index does not keep: the
to-do list, the Bash commands (only a short label of each: the program and its
first plain words) and the times of the user's prompts (never their text).
"""

from __future__ import annotations

import json
import re
import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from capcom.types import Request

KINDS = ("think", "test", "files", "other", "shot")
KIND_LABELS = {
    "think": "Thinking and writing",
    "test": "Tests and builds",
    "files": "Reading and editing files",
    "other": "Other commands",
    "shot": "Screenshots",
}
FILE_TOOLS = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead", "Glob", "Grep", "LS"}
)
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"})
READ_PROGRAMS = frozenset(
    {"cat", "head", "tail", "sed", "grep", "rg", "find", "ls", "wc", "tree", "fd", "less"}
    | {"awk", "stat", "file", "diff", "nl", "du", "realpath", "readlink", "jq", "bat"}
)
SETUP_PROGRAMS = frozenset(
    {"cd", "export", "source", ".", "set", "echo", "printf", "sleep", "true", "false"}
    | {"pushd", "popd", "unset", "local", "read", "mkdir", "test", "[", "[["}
)
WRAPPERS = frozenset(
    {"sudo", "env", "time", "nice", "nohup", "timeout", "exec", "command", "stdbuf"}
    | {"ionice", "xargs", "watch", "setsid", "caffeinate"}
)
TEST_WORDS = frozenset(
    {"pytest", "py.test", "jest", "vitest", "mocha", "tox", "nox", "make", "cmake", "ninja"}
    | {"gradle", "gradlew", "mvn", "tsc", "mypy", "pyright", "ruff", "eslint", "ctest"}
    | {"bazel", "bazelisk", "meson", "rspec", "phpunit", "flake8", "pylint", "unittest"}
    | {"xcodebuild", "golangci-lint", "biome", "prettier", "black", "isort", "shellcheck"}
    | {"nextest", "colcon", "catkin", "scons", "msbuild", "gcc", "g++", "clang", "clang++"}
    | {"rustc", "javac", "ava", "tap", "karma", "cypress", "behave", "ward", "hypothesis"}
)
# a program that is a test or a build only with one of these words after it
TEST_SUBCOMMANDS = {
    "cargo": frozenset({"test", "build", "check", "clippy", "bench", "nextest"}),
    "go": frozenset({"test", "build", "vet"}),
    "npm": frozenset({"test", "build", "lint", "typecheck", "check"}),
    "pnpm": frozenset({"test", "build", "lint", "typecheck", "check"}),
    "yarn": frozenset({"test", "build", "lint", "typecheck", "check"}),
    "bun": frozenset({"test", "build", "lint", "typecheck", "check"}),
    "deno": frozenset({"test", "check", "lint"}),
    "dotnet": frozenset({"build", "test"}),
    "swift": frozenset({"build", "test"}),
    "zig": frozenset({"build", "test"}),
    "mix": frozenset({"test", "compile"}),
    "stack": frozenset({"test", "build"}),
    "cabal": frozenset({"test", "build"}),
    "uv": frozenset({"build"}),
    "playwright": frozenset({"test"}),
}
SHOT_WORDS = frozenset(
    {"headless", "playwright", "puppeteer", "chromium", "chrome", "google-chrome"}
    | {"chromium-browser", "shot-scraper", "wkhtmltoimage", "scrot", "grim", "screencapture"}
)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
SHOT_TOOL = re.compile(r"screenshot|chrome|browser|playwright|puppeteer|computer", re.IGNORECASE)
WORD = re.compile(r"^[A-Za-z][\w.:+-]{0,23}$")
OPERATORS = frozenset({"&&", "||", ";", "|", "&", "|&"})
IDLE_FALLBACK = 10 * 60  # seconds: a turn's end this long before the next request waited for you
MAX_GUESSED = 8  # phases one stretch without a to-do item is cut into, at most
NAME_MAX = 60


# ----------------------------------------------------------------- the log extras


@dataclass(slots=True)
class TodoEvent:
    """One change to the to-do list, at the reply that made it."""

    tool_use_id: str
    time: datetime
    op: str  # "snapshot" (TodoWrite), "create" (TaskCreate), "update" (TaskUpdate)
    items: list[tuple[str, str, str]] = field(default_factory=list)  # snapshot: key, name, status
    key: str | None = None  # create: the task id once known; update: its id
    name: str | None = None
    status: str | None = None


@dataclass(slots=True)
class Extras:
    """What the index does not keep, read from the thread's own log files."""

    # Bash tool_use_id -> (short label, kind)
    commands: dict[str, tuple[str, str]] = field(default_factory=dict)
    prompts: list[datetime] = field(default_factory=list)  # the user's messages (times only)
    todos: list[TodoEvent] = field(default_factory=list)


def _time(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _text(v: Any, limit: int = 200) -> str | None:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v[:limit] or None


def read_extras(paths: Iterable[Path], agent_id: str | None) -> Extras:
    """Stream the thread's log files read-only. A main log may hold a subagent's
    records too (older versions): only the thread's own are read. A last line
    without its newline is still being written and is skipped."""
    out = Extras()
    creates: dict[str, TodoEvent] = {}
    for path in paths:
        try:
            f = path.open("rb")
        except OSError:
            continue
        with f:
            for line in f:
                if not line.endswith(b"\n"):
                    break
                _read_line(line, agent_id, out, creates)
    # a task whose id never came back: Claude Code numbers them 1, 2, ... in order
    n = 0
    for ev in out.todos:
        if ev.op == "create":
            n += 1
            if ev.key is None:
                ev.key = str(n)
    out.prompts.sort()
    out.todos.sort(key=lambda e: e.time)
    return out


def _read_line(
    line: bytes, agent_id: str | None, out: Extras, creates: dict[str, TodoEvent]
) -> None:
    tool_use = b'"tool_use"' in line and any(
        w in line for w in (b'"Bash"', b'"TodoWrite"', b'"TaskCreate"', b'"TaskUpdate"')
    )
    user = b'"type":"user"' in line
    task_result = user and b'"tool_result"' in line and bool(creates) and b'"task' in line
    prompt = user and b'"tool_result"' not in line
    if not (tool_use or task_result or prompt):
        return
    try:
        rec = json.loads(line)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return
    sidechain = rec.get("isSidechain") is True
    if agent_id is None and sidechain:
        return
    if agent_id is not None and sidechain and rec.get("agentId") not in (None, agent_id):
        return
    t = _time(rec.get("timestamp"))
    msg = rec.get("message")
    if t is None or not isinstance(msg, dict):
        return
    content = msg.get("content")
    if rec.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                _tool_use(block, t, out, creates)
    elif rec.get("type") == "user":
        if prompt:
            if rec.get("isMeta") or rec.get("isCompactSummary") or "toolUseResult" in rec:
                return
            if isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
            ):
                out.prompts.append(t)
        elif isinstance(content, list):
            _task_result(rec, content, creates)


def _tool_use(
    block: Mapping[str, Any], t: datetime, out: Extras, creates: dict[str, TodoEvent]
) -> None:
    name, tid, inp = block.get("name"), block.get("id"), block.get("input")
    if not isinstance(tid, str) or not isinstance(inp, dict):
        return
    if name == "Bash":
        command = inp.get("command")
        label = command_label(command)
        if label and isinstance(command, str):
            out.commands[tid] = (label, command_kind(command)[0])
    elif name == "TodoWrite":
        todos = inp.get("todos")
        if not isinstance(todos, list):
            return
        items = []
        for it in todos:
            if not isinstance(it, dict):
                continue
            key = _text(it.get("content")) or _text(it.get("id"))
            if key is None:
                continue
            title = _text(it.get("activeForm")) or _text(it.get("content")) or key
            items.append((key, title, str(it.get("status") or "pending")))
        out.todos.append(TodoEvent(tid, t, "snapshot", items=items))
    elif name == "TaskCreate":
        subject = _text(inp.get("activeForm")) or _text(inp.get("subject"))
        ev = TodoEvent(tid, t, "create", name=subject, status="pending")
        creates[tid] = ev
        out.todos.append(ev)
    elif name == "TaskUpdate":
        key = inp.get("taskId")
        if isinstance(key, (str, int)):
            subject = _text(inp.get("activeForm")) or _text(inp.get("subject"))
            status = inp.get("status")
            out.todos.append(
                TodoEvent(
                    tid,
                    t,
                    "update",
                    key=str(key),
                    name=subject,
                    status=status if isinstance(status, str) else None,
                )
            )


def _task_result(rec: Mapping[str, Any], content: list[Any], creates: dict[str, TodoEvent]) -> None:
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        ev = creates.get(str(block.get("tool_use_id")))
        if ev is None or ev.key is not None:
            continue
        result = rec.get("toolUseResult")
        task = result.get("task") if isinstance(result, dict) else None
        if isinstance(task, dict) and isinstance(task.get("id"), (str, int)):
            ev.key = str(task["id"])
            continue
        body = block.get("content")
        text = body if isinstance(body, str) else ""
        if isinstance(body, list):
            text = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
        m = re.search(r"#(\d+)", text)
        if m:
            ev.key = m.group(1)


# ----------------------------------------------------------------- commands


def _tokens(command: str) -> list[str]:
    head = command.split("<<", 1)[0]  # a heredoc's body is text, not commands
    head = re.sub(r"(\|\||&&|\|&|[;|&\n])", r" \1 ", head)
    try:
        return shlex.split(head)
    except ValueError:
        return head.split()


def _segments(command: str) -> list[list[str]]:
    segs: list[list[str]] = [[]]
    for tok in _tokens(command):
        if tok in OPERATORS or tok == "\n":
            segs.append([])
        else:
            segs[-1].append(tok)
    out = []
    for seg in segs:
        while seg and (re.match(r"^\w+=", seg[0]) or seg[0] in ("(", "{", "!")):
            seg = seg[1:]
        while seg and Path(seg[0]).name in WRAPPERS:
            seg = seg[1:]
            while seg and (seg[0].startswith("-") or re.match(r"^[\d.]+[smhd]?$", seg[0])):
                seg = seg[1:]
        if seg:
            out.append(seg)
    return out


def _base(tok: str) -> str:
    return Path(tok).name.lower()


def _test_word(seg: Sequence[str]) -> str | None:
    words = [_base(t) for t in seg]
    for i, w in enumerate(words):
        if w in TEST_WORDS:
            return w
        subs = TEST_SUBCOMMANDS.get(w)
        if subs:
            nxt = [x for x in words[i + 1 : i + 3] if not x.startswith("-")]
            hit = next((x for x in nxt if x in subs), None)
            if hit:
                return f"{w} {hit}"
    return None


def _shot_word(seg: Sequence[str]) -> str | None:
    words = [_base(t) for t in seg]
    for i, w in enumerate(words):
        if w == "playwright" and "test" in words[i + 1 : i + 3]:
            continue  # a test runner
        if "screenshot" in w or w in SHOT_WORDS or re.match(r"^shot[\w-]*\.(m?js|py|sh)$", w):
            return "screenshot" if "screenshot" in w or w.startswith("shot") else w
    return None


def command_kind(command: str) -> tuple[str, str | None]:
    """A Bash command's kind (KINDS) and the word that made it so."""
    segs = _segments(command)
    for seg in segs:
        w = _shot_word(seg)
        if w:
            return "shot", w
    for seg in segs:
        w = _test_word(seg)
        if w:
            return "test", w
    work = [s for s in segs if _base(s[0]) not in SETUP_PROGRAMS]
    if work and all(_base(s[0]) in READ_PROGRAMS for s in work):
        return "files", None
    return "other", None


def command_label(command: Any) -> str | None:
    """A short label for a Bash command: the program and up to two plain words after
    it ("uv run pytest", "git commit"), never its arguments' text."""
    if not isinstance(command, str) or not command.strip():
        return None
    segs = _segments(command)
    if not segs:
        return None
    word = command_kind(command)[1]
    pick = None
    if word:
        pick = next((s for s in segs if (_shot_word(s) or _test_word(s)) == word), None)
    if pick is None:
        pick = next((s for s in segs if _base(s[0]) not in SETUP_PROGRAMS), segs[0])
    words = [Path(pick[0]).name]
    for tok in pick[1:3]:
        if not WORD.match(tok):
            break
        words.append(tok)
    label = " ".join(words)
    if word and word.split()[-1] not in {w.lower() for w in words}:
        label += " … " + word.split()[-1]
    return label[:NAME_MAX]


# programs that run another one: a segment shows the one they run
RUNNERS: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pdm", "run"),
    ("pipenv", "run"),
    ("npx",),
    ("bunx",),
    ("pnpm", "exec"),
    ("yarn", "exec"),
    ("bundle", "exec"),
)


def _short_of(label: str) -> str:
    """The word a segment shows: "pytest" for "uv run pytest" or "python3 … pytest"."""
    if " … " in label:
        return label.rsplit(" … ", 1)[-1]
    words = label.split()
    for prefix in RUNNERS:
        if len(words) > len(prefix) and tuple(words[: len(prefix)]) == prefix:
            return " ".join(words[len(prefix) :])
    return label


# ----------------------------------------------------------------- steps


@dataclass(slots=True)
class Call:
    name: str
    target: str | None
    end: datetime | None
    kind: str
    label: str  # "Bash (uv run pytest)", "Read app.js"
    short: str  # "pytest", "app.js"
    id: str = ""  # the tool_use id: what the page's fold asks /api/tool for


@dataclass(slots=True)
class Step:
    start: datetime
    end: datetime
    kind: str
    label: str
    short: str
    request: int  # the reply it produced (think) or that launched it (tools)
    calls: list[Call] = field(default_factory=list)
    usd: float = 0.0
    next: int = -1  # tools: the request that read the results (its cost is this step's)
    phase: int = -1
    moved: bool = False  # think: its request's cost is counted on the tool step that read it

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(slots=True)
class Result:
    time: datetime
    is_image: bool = False


def _call(c: Any, results: Mapping[str, Result], commands: Mapping[str, tuple[str, str]]) -> Call:
    call = _kind_call(c, results, commands)
    call.id = c.tool_use_id
    return call


def _kind_call(
    c: Any, results: Mapping[str, Result], commands: Mapping[str, tuple[str, str]]
) -> Call:
    name, target = c.name, c.target
    res = results.get(c.tool_use_id)
    end = res.time if res else None
    base = Path(target).name if target else None
    if name == "Bash":
        known = commands.get(c.tool_use_id)
        label, kind = known or (target or "command", command_kind(target or "")[0])
        return Call(name, target, end, kind, f"Bash ({label})", _short_of(label))
    if name in PATH_TOOLS and target and target.lower().endswith(IMAGE_EXT):
        return Call(name, target, end, "shot", f"{name} {base}", base or name)
    if name in FILE_TOOLS:
        return Call(name, target, end, "files", f"{name} {base}" if base else name, base or name)
    if SHOT_TOOL.search(name) or (res is not None and res.is_image):
        return Call(name, target, end, "shot", name, name)
    if name == "Agent" and target:
        return Call(name, target, end, "other", f"Agent {target}", f"Agent {target}")
    return Call(name, target, end, "other", name, name)


def build_steps(
    reqs: Sequence[Request],
    results: Mapping[str, Result],
    *,
    start: datetime | None = None,
    prompts: Sequence[datetime] = (),
    commands: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[list[Step], list[tuple[datetime, datetime]]]:
    """The thread's steps in time order, and its idle stretches (waiting for the
    user: from a turn's end to the prompt that started the next one)."""
    commands = commands or {}
    steps: list[Step] = []
    idle: list[tuple[datetime, datetime]] = []
    ready = start  # when the model had what it needed for its next reply
    turn_end = False  # the last reply launched no tools: the model waited for the user
    pi = 0
    last_prompt: datetime | None = None
    for i, r in enumerate(reqs):
        t = r.timestamp
        while pi < len(prompts) and prompts[pi] <= t:
            last_prompt = prompts[pi]
            pi += 1
        begin = ready
        if last_prompt is not None and (begin is None or last_prompt > begin):
            if i > 0 and begin is not None:  # (a subagent's first prompt is its task)
                idle.append((begin, last_prompt))
            begin = last_prompt
        elif turn_end and begin is not None and (t - begin).total_seconds() > IDLE_FALLBACK:
            idle.append((begin, t))  # no prompt in the log: a long gap after a turn's end
            begin = None
        if begin is not None and begin < t:
            steps.append(Step(begin, t, "think", KIND_LABELS["think"], "thinking", i))
        if r.tool_calls:
            nxt = reqs[i + 1].timestamp if i + 1 < len(reqs) else None
            calls = [_call(c, results, commands) for c in r.tool_calls]
            ends = [c.end for c in calls if c.end is not None and c.end >= t]
            end = max(ends) if ends else (nxt or t)
            if nxt is not None and end > nxt:
                end = nxt  # a background command: the next reply did not wait for it
            longest = max(calls, key=lambda c: c.end if c.end and c.end >= t else t)
            label = longest.label + (f" +{len(calls) - 1} more" if len(calls) > 1 else "")
            steps.append(Step(t, end, longest.kind, label, longest.short, i, calls))
            ready, turn_end = end, False
        else:
            ready, turn_end = t, True
    return steps, idle


def attribute_costs(steps: list[Step], costs: Sequence[float]) -> None:
    """Each request's cost goes to the step it closes: the tool step whose results it
    read (the previous reply's tools), else its own "Thinking and writing" step.
    The one place the rule lives, so it can change without touching the rest."""
    tools: dict[int, Step] = {}
    think: dict[int, Step] = {}
    for st in steps:
        (think if st.kind == "think" and not st.calls else tools)[st.request] = st
    for j, usd in enumerate(costs):
        target = tools.get(j - 1) or think.get(j) or tools.get(j)
        if target is None:
            continue
        target.usd += usd
        if target is tools.get(j - 1):
            target.next = j
            if j in think:
                think[j].moved = True


# ----------------------------------------------------------------- phases


@dataclass(slots=True)
class Phase:
    name: str
    guessed: bool
    steps: list[int]
    key: str | None = None  # the to-do item, for a phase from the list


def todo_intervals(
    todos: Sequence[TodoEvent], times: Mapping[str, datetime]
) -> list[tuple[datetime, datetime | None, str, str]]:
    """(start, end, name, key) of each to-do item while in progress, from the
    list's changes in time order (`times`: the reply time of each tool call, which
    the steps use too). An item still in progress at the end has no end."""
    state: dict[str, str] = {}
    names: dict[str, str] = {}
    open_: dict[str, datetime] = {}
    out: list[tuple[datetime, datetime | None, str, str]] = []

    def change(key: str, status: str, t: datetime) -> None:
        was = state.get(key)
        state[key] = status
        if status == "in_progress" and was != "in_progress":
            open_[key] = t
        elif was == "in_progress" and status != "in_progress" and key in open_:
            out.append((open_.pop(key), t, names.get(key, key), key))

    for ev in todos:
        t = times.get(ev.tool_use_id, ev.time)
        if ev.op == "snapshot":
            seen = set()
            for key, name, status in ev.items:
                seen.add(key)
                names[key] = name
                change(key, status, t)
            for key in [k for k in state if k not in seen]:
                change(key, "gone", t)
        elif ev.key is not None:
            if ev.name:
                names[ev.key] = ev.name
            if ev.status:
                change(ev.key, ev.status, t)
    for key, t in open_.items():
        out.append((t, None, names.get(key, key), key))
    out.sort(key=lambda x: x[0])
    return out


def _mode(st: Step) -> str:
    if any(c.name in EDIT_TOOLS for c in st.calls):
        return "edit"
    if st.kind == "files":
        return "read"
    return "cmd"


def _span(steps: Sequence[Step], idx: Sequence[int]) -> float:
    return sum(steps[i].seconds for i in idx)


def _merge_small(steps: Sequence[Step], runs: list[list[int]], limit: int) -> list[list[int]]:
    total = _span(steps, [i for r in runs for i in r]) or 1.0
    while len(runs) > 1:
        sizes = [_span(steps, r) for r in runs]
        k = min(range(len(runs)), key=lambda x: sizes[x])
        tools = sum(1 for i in runs[k] if steps[i].calls)
        if len(runs) <= limit and sizes[k] >= max(60.0, 0.04 * total) and tools >= 2:
            break
        if k == 0:
            other = 1
        elif k == len(runs) - 1:
            other = k - 1
        else:
            other = k - 1 if sizes[k - 1] <= sizes[k + 1] else k + 1
        lo, hi = min(k, other), max(k, other)
        runs[lo : hi + 1] = [runs[lo] + runs[hi]]
    return runs


def guess_name(steps: Sequence[Step]) -> str:
    """A stretch's name from what it touched: the files it edited or read, then the
    commands it spent the most time on."""
    edits: Counter[str] = Counter()
    reads: Counter[str] = Counter()
    cmds: dict[str, float] = {}
    for st in steps:
        for c in st.calls:
            secs = max(0.0, ((c.end or st.end) - st.start).total_seconds())
            base = Path(c.target).name if c.target else None
            if c.name in EDIT_TOOLS and base:
                edits[base] += 1
            elif c.kind == "files" and c.name in PATH_TOOLS and base:
                reads[base] += 1
            elif c.kind != "files":
                cmds[c.short] = cmds.get(c.short, 0.0) + secs + 1e-3
    parts = []
    files = edits or reads
    if files:
        top = [n for n, _ in files.most_common(2)]
        more = f" +{len(files) - 2}" if len(files) > 2 else ""
        parts.append(("Editing " if edits else "Reading ") + ", ".join(top) + more)
    elif any(st.kind == "files" for st in steps):
        parts.append("Searching files")
    if cmds:
        top_cmds = sorted(cmds, key=lambda n: -cmds[n])[: 2 if parts else 3]
        parts.append(", ".join(top_cmds))
    name = " · ".join(parts) or KIND_LABELS["think"]
    return name if len(name) <= NAME_MAX else name[: NAME_MAX - 1] + "…"


def _guess(steps: Sequence[Step], idx: list[int]) -> list[Phase]:
    """Cut a stretch without a to-do item where its work changed, and name each part."""
    modes: list[str | None] = [(_mode(steps[i]) if steps[i].calls else None) for i in idx]
    following = None
    for k in range(len(modes) - 1, -1, -1):  # a reply's thinking goes with what it launched
        if modes[k] is None:
            modes[k] = following
        else:
            following = modes[k]
    last = next((m for m in modes if m is not None), "think")
    runs: list[list[int]] = []
    prev: str | None = None
    for i, m in zip(idx, modes, strict=True):
        m = m or last
        if m == "read" and prev == "edit":
            m = "edit"  # reading while editing is part of the editing
        if runs and m == prev:
            runs[-1].append(i)
        else:
            runs.append([i])
        prev = m
    runs = _merge_small(steps, runs, MAX_GUESSED)
    return [Phase(guess_name([steps[i] for i in r]), True, r) for r in runs]


def build_phases(
    steps: list[Step], intervals: Sequence[tuple[datetime, datetime | None, str, str]]
) -> list[Phase]:
    """The phases of a thread, in order; sets each step's `phase`."""
    keys: list[int | None] = []
    for st in steps:
        hit = None
        for n, (a, b, _, _) in enumerate(intervals):
            if a <= st.start and (b is None or st.start < b):
                hit = n  # the latest started item that holds it
        keys.append(hit)
    # a short stretch between two items (the reply that closed one and opened the next)
    # stays with the item before it
    runs: list[tuple[int | None, list[int]]] = []
    for i, k in enumerate(keys):
        if runs and runs[-1][0] == k:
            runs[-1][1].append(i)
        else:
            runs.append((k, [i]))
    tidy: list[tuple[int | None, list[int]]] = []
    for k, idx in runs:
        small = _span(steps, idx) < 60 or len(idx) <= 2
        if tidy and (tidy[-1][0] == k or (k is None and small and tidy[-1][0] is not None)):
            tidy[-1][1].extend(idx)
        else:
            tidy.append((k, idx))
    phases: list[Phase] = []
    for k, idx in tidy:
        if k is None:
            phases.extend(_guess(steps, idx))
        else:
            phases.append(Phase(intervals[k][2], False, idx, intervals[k][3]))
    for n, ph in enumerate(phases):
        for i in ph.steps:
            steps[i].phase = n
    return phases


# ----------------------------------------------------------------- the page's data


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def busy_seconds(steps: Sequence[Step]) -> tuple[float, float]:
    """(model working, tools running) seconds of a thread's steps."""
    model = sum(st.seconds for st in steps if not st.calls)
    return model, sum(st.seconds for st in steps if st.calls)


def timeline_json(
    reqs: Sequence[Request],
    costs: Sequence[float],
    steps: Sequence[Step],
    idle: Sequence[tuple[datetime, datetime]],
    phases: Sequence[Phase],
    parts: Sequence[Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    """Steps, requests and phases as columns and rows, times in epoch ms. `parts`:
    each request's cost by token kind (input, cache_read, cache_write_5m,
    cache_write_1h, output, web), for a thinking step's fold, which also asks
    `capcom serve` for what the request wrote by its message id (`id`)."""
    step_cols: dict[str, list[Any]] = {
        k: []
        for k in (
            "start",
            "end",
            "kind",
            "label",
            "short",
            "calls",
            "phase",
            "usd",
            "next",
            "moved",
            "request",
            "tools",
        )
    }
    for st in steps:
        step_cols["start"].append(_ms(st.start))
        step_cols["end"].append(_ms(st.end))
        step_cols["kind"].append(st.kind)
        step_cols["label"].append(st.label)
        step_cols["short"].append(st.short)
        step_cols["calls"].append(len(st.calls))
        step_cols["phase"].append(st.phase)
        step_cols["usd"].append(round(st.usd, 6))
        step_cols["next"].append(st.next)
        step_cols["moved"].append(st.moved)
        step_cols["request"].append(st.request)
        # each call: [tool_use id, tool name, label, end (epoch ms) or null]
        step_cols["tools"].append(
            [[c.id, c.name, c.label, _ms(c.end) if c.end else None] for c in st.calls]
        )
    keys = ("t", "context", "usd", "output", "cache_read", "cache_write", "estimated")
    req_cols: dict[str, list[Any]] = {
        k: []
        for k in (
            *keys,
            "input",
            "cache_write_5m",
            "cache_write_1h",
            "thinking",
            "model",
            "parts",
            "id",
        )
    }
    for n, (r, usd) in enumerate(zip(reqs, costs, strict=True)):
        u = r.usage
        req_cols["input"].append(u.input)
        req_cols["cache_write_5m"].append(u.cache_write_5m)
        req_cols["cache_write_1h"].append(u.cache_write_1h)
        req_cols["thinking"].append(u.thinking)
        req_cols["model"].append(r.model)
        req_cols["id"].append(r.message_id)  # a thinking fold asks /api/reply by it
        req_cols["parts"].append(
            {k: round(v, 6) for k, v in parts[n].items()} if parts is not None else None
        )
        req_cols["t"].append(_ms(r.timestamp))
        req_cols["context"].append(r.usage.context)
        req_cols["usd"].append(round(usd, 6))
        req_cols["output"].append(r.usage.output)
        req_cols["cache_read"].append(r.usage.cache_read)
        req_cols["cache_write"].append(r.usage.cache_write)
        req_cols["estimated"].append(1 if r.output_estimated else 0)
    phase_rows = []
    for n, ph in enumerate(phases):
        own = [steps[i] for i in ph.steps]
        mix: dict[str, float] = {}
        for st in own:
            mix[st.kind] = mix.get(st.kind, 0.0) + st.seconds
        phase_rows.append(
            {
                "n": n + 1,
                "name": ph.name,
                "guessed": ph.guessed,
                "start": _ms(own[0].start),
                "end": _ms(own[-1].end),
                "seconds": round(sum(st.seconds for st in own), 1),
                "usd": round(sum(st.usd for st in own), 6),
                "kinds": {k: round(v, 1) for k, v in mix.items()},
                "first": ph.steps[0],
                "last": ph.steps[-1],
            }
        )
    kinds = []
    for k in KINDS:
        own = [st for st in steps if st.kind == k]
        kinds.append(
            {
                "key": k,
                "label": KIND_LABELS[k],
                "seconds": round(sum(st.seconds for st in own), 1),
                "steps": len(own),
                "usd": round(sum(st.usd for st in own), 6),
                "moved": any(st.moved for st in own) and sum(st.usd for st in own) < 0.005,
            }
        )
    return {
        "steps": step_cols,
        "requests": req_cols,
        "phases": phase_rows,
        "kinds": kinds,
        "idle": [[_ms(a), _ms(b)] for a, b in idle],
        "todo": any(not ph.guessed for ph in phases),
    }
