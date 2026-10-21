"""The contracts between capcom's modules.

Every module talks to the others through these types only. The pipeline:

    sources --(Batch of Record)--> store --(Request, Event, ...)--> cost, timeline, ...

Rules that hold for every type here:

- Times are timezone-aware datetimes in UTC.
- Token counts are exact integers from the logs, except where a field or flag says
  "estimate"; estimates from text length use one rule everywhere,
  `estimate_tokens(chars)` (about 4 characters per token).
- No conversation text. A type may carry a tool name, a file path, a command's first
  word, an agent type, a skill or server name, a model name, or a spawn's short
  description, never a prompt, a reply, file contents or a tool's output.

Change policy (AGENTS.md): never change an existing field or signature; adding an
optional field (with a default) or a new type is fine.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    """Token estimate for a text of `chars` characters (the one rule capcom uses)."""
    return (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# --------------------------------------------------------------------------- files


class LogKind(StrEnum):
    SESSION = "session"  # <project>/<session_id>.jsonl
    SUBAGENT = "subagent"  # <project>/<session_id>/subagents/agent-<agent_id>.jsonl


@dataclass(frozen=True, slots=True)
class LogFile:
    """One log file found under the Claude Code projects folder."""

    path: Path
    kind: LogKind
    project_dir: str  # folder name under projects/, e.g. "-home-breno-Github-kittex"
    session_id: str
    agent_id: str | None = None  # set for subagent logs
    meta_path: Path | None = None  # subagents/agent-<agent_id>.meta.json, when present


@dataclass(frozen=True, slots=True)
class FileState:
    """What the store remembers about a log file between scans.

    `offset` is the byte position just after the last complete line read. A file
    whose inode changed, or whose size is now below `offset`, is read again from 0.
    """

    path: Path
    inode: int
    size: int
    mtime_ns: int
    offset: int


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts of one model reply, as billed."""

    input: int = 0  # uncached input
    cache_read: int = 0
    cache_write_5m: int = 0  # cache writes with the 5-minute lifetime
    cache_write_1h: int = 0  # cache writes with the 1-hour lifetime
    output: int = 0  # includes thinking
    thinking: int = 0  # the part of output that was thinking (0 when not logged)
    web_searches: int = 0
    web_fetches: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def context(self) -> int:
        """Prompt size of the request: everything sent, cached or not."""
        return self.input + self.cache_read + self.cache_write


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call made by a reply. `target` is a short label, never content.

    target by tool: Read/Edit/Write/NotebookEdit: the file path; Bash: the first word
    of the command; Agent: the spawned agent type; Skill: the skill name; WebFetch:
    the host; Grep/Glob: the pattern's path argument if any; MCP tools: None
    (the server is in the name); others: None.
    """

    tool_use_id: str
    name: str
    target: str | None = None


@dataclass(frozen=True, slots=True)
class Request:
    """One model reply, counted once: the last record carrying its `message_id`.

    `output_estimated` is True when the logged output count was a stream-start value
    and capcom replaced it with an estimate from the reply's content length.
    """

    message_id: str
    session_id: str
    agent_id: str | None  # None on the main thread
    project_dir: str
    timestamp: datetime
    model: str
    usage: Usage
    request_id: str | None = None
    cwd: str | None = None
    version: str | None = None  # Claude Code version that wrote the record
    effort: str | None = None
    speed: str | None = None  # usage.speed: "fast" (fast mode, priced higher) or "standard"
    service_tier: str | None = None
    inference_geo: str | None = None  # usage.inference_geo, e.g. "us" (priced higher)
    stop_reason: str | None = None
    is_api_error: bool = False
    output_estimated: bool = False
    tool_calls: tuple[ToolCall, ...] = ()


class EventKind(StrEnum):
    """Things in the logs that can explain a change in spend.

    `Event.attrs` keys by kind (all optional; unknown keys allowed):

    - COMPACTION: trigger (str), pre_tokens, post_tokens, duration_ms (int)
    - AWAY: none (a `system/away_summary`: the user was away)
    - SPAWN: tool_use_id, agent_type, description (str): an Agent tool call
    - TOOL_RESULT: tool_use_id, tool (str), chars, size_tokens (int, estimate),
      is_image, is_error (bool)
    - TOOL_SET: source (str: the record or attachment type), shape_hash (str),
      names (tuple: the full list when the record gives one), added, removed (tuple),
      size_tokens (int, estimate of the listing's size)
    - MODEL: model (str): a `model` attachment (the session's model was set or changed)
    - MEMORY: source ("instructions" | "nested_memory"), paths (tuple),
      size_tokens (int, estimate)
    - SKILLS_INVOKED: names (tuple)
    - API_REQUEST: request_id, query_source, shape_hash, model, cache_ttl (str)
    - API_ERROR: status (int), error (str)
    - TURN: duration_ms (int)
    """

    COMPACTION = "compaction"
    AWAY = "away"
    SPAWN = "spawn"
    TOOL_RESULT = "tool_result"
    TOOL_SET = "tool_set"
    MODEL = "model"
    MEMORY = "memory"
    SKILLS_INVOKED = "skills_invoked"
    API_REQUEST = "api_request"
    API_ERROR = "api_error"
    TURN = "turn"


AttrValue = str | int | float | bool | None | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened in a session at a point in time.

    `key` identifies the event within its log file (the record's uuid, or the byte
    offset of its line as a string when the record has no uuid), so rereading a file
    replaces events instead of duplicating them.
    """

    kind: EventKind
    session_id: str
    agent_id: str | None
    timestamp: datetime
    key: str
    attrs: Mapping[str, AttrValue] = field(default_factory=dict, hash=False)


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """A subagent, from its meta.json and its log."""

    agent_id: str
    session_id: str
    agent_type: str | None = None
    description: str | None = None  # the spawn's short description
    tool_use_id: str | None = None  # the parent's Agent call that spawned it
    spawn_depth: int | None = None
    is_built_in: bool | None = None
    finished: bool | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """What a batch of records says about its session. The store merges these:
    `first_seen` is the minimum, `last_seen` the maximum, and every other field
    the latest non-None value."""

    session_id: str
    project_dir: str
    cwd: str | None = None
    title: str | None = None  # the `ai-title` record
    version: str | None = None
    entrypoint: str | None = None
    git_branch: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """One model's totals in a `cost-state` record."""

    input: int = 0
    output: int = 0
    thinking: int = 0
    cache_read: int = 0
    cache_write: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class CostState:
    """Claude Code's own running cost estimate for a session.

    The records carry no timestamp: `offset` (the line's byte offset in its file)
    orders them, and the last one holds the session's totals.
    """

    session_id: str
    offset: int
    total_cost_usd: float
    models: Mapping[str, ModelUsage] = field(default_factory=dict, hash=False)
    start_time_ms: int | None = None
    has_unknown_model_cost: bool | None = None
    # The record has no time of its own: the time of the last timestamped record
    # before it in the same file (None when there is none).
    timestamp: datetime | None = None


Record = Request | Event | AgentInfo | SessionInfo | CostState


@dataclass(slots=True)
class Batch:
    """Records parsed from consecutive complete lines of one file.

    `end_offset` is the byte position after the last complete line consumed (a
    trailing line without a newline is left for the next scan). `skipped` counts
    lines that were not valid JSON or had no use (unknown types are not errors).
    """

    records: list[Record]
    end_offset: int
    lines: int = 0
    skipped: int = 0


# --------------------------------------------------------------------------- cost


@dataclass(frozen=True, slots=True)
class LongContextPrice:
    """Prices that apply when a request's prompt is above `threshold_tokens`."""

    threshold_tokens: int
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens for one model."""

    model: str
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    long_context: LongContextPrice | None = None
    fast_multiplier: float | None = None  # applies to every category when speed == "fast"
    # True when the table's geo_multipliers apply (models 4.6 and later)
    geo_pricing: bool = False


@dataclass(frozen=True, slots=True)
class PriceTable:
    checked: date
    sources: tuple[str, ...]
    models: Mapping[str, Price] = field(hash=False)
    web_search_per_1k: float = 0.0
    web_fetch_per_1k: float = 0.0
    # inference_geo -> multiplier on every token category (e.g. {"us": 1.1})
    geo_multipliers: Mapping[str, float] = field(default_factory=dict, hash=False)
    # Older prices per model: (valid_until, price) pairs, newest first. A request
    # before `valid_until` (exclusive) uses that price; `models` holds today's.
    previous: Mapping[str, tuple[tuple[date, Price], ...]] = field(default_factory=dict, hash=False)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The API-equivalent cost of one request, in USD."""

    input: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0
    web: float = 0.0
    priced_as: str = ""  # the price-table model used
    exact_price: bool = True  # False when the model was priced as its closest family

    @property
    def total(self) -> float:
        return self.input + self.cache_write + self.cache_read + self.output + self.web


@dataclass(frozen=True, slots=True)
class Calibration:
    """capcom's total for a session against Claude Code's last `cost-state`."""

    session_id: str
    capcom_usd: float
    claude_code_usd: float
    per_model: Mapping[str, tuple[float, float]] = field(default_factory=dict, hash=False)
    # False when cost-state counts calls the logs lack (away summaries, compactions,
    # WebFetch/WebSearch side calls, a resume) or capcom estimated output; `reasons`
    # names them. The 2 % target applies to comparable sessions only.
    comparable: bool = True
    reasons: tuple[str, ...] = ()

    @property
    def spread(self) -> float:
        """Relative difference, (capcom - claude_code) / claude_code."""
        if self.claude_code_usd == 0:
            return 0.0 if self.capcom_usd == 0 else float("inf")
        return (self.capcom_usd - self.claude_code_usd) / self.claude_code_usd


# --------------------------------------------------------------------------- scans


@dataclass(slots=True)
class ScanStats:
    files_seen: int = 0
    files_read: int = 0  # files with new bytes
    files_reset: int = 0  # files read again from 0 (inode changed or shrank)
    bytes_read: int = 0
    lines: int = 0
    records: int = 0
    skipped: int = 0
    seconds: float = 0.0


# --------------------------------------------------------------------------- module boundaries


class Source(Protocol):
    """A log source (capcom/sources). One adapter per source."""

    def discover(self, root: Path) -> Iterator[LogFile]:
        """Every log file under `root`, in a stable order."""
        ...

    def parse(self, log: LogFile, start: int) -> Iterator[Batch]:
        """Parse `log` from byte `start`, in batches of bounded size. Never loads the
        whole file; tolerates partial last lines, bad lines and unknown types."""
        ...


class Index(Protocol):
    """The persistent index (capcom/store)."""

    def file_state(self, path: Path) -> FileState | None: ...

    def apply(self, log: LogFile, batch: Batch, state: FileState) -> None:
        """Store a batch and the file's new state in one transaction. A Request
        replaces any earlier row with the same message_id; an Event replaces the one
        with the same (file, key)."""
        ...

    def forget_file(self, path: Path) -> None:
        """Drop every row that came from `path` (before rereading it from 0)."""
        ...

    def requests(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
    ) -> Iterator[Request]:
        """Requests ordered by timestamp, `since` inclusive, `until` exclusive."""
        ...

    def events(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        session_id: str | None = None,
        kinds: frozenset[EventKind] | None = None,
    ) -> Iterator[Event]: ...

    def sessions(self) -> Iterator[SessionInfo]:
        """One merged SessionInfo per session."""
        ...

    def agents(self, session_id: str | None = None) -> Iterator[AgentInfo]: ...

    def last_cost_states(self) -> Iterator[CostState]:
        """The last cost-state of each session that has one."""
        ...
