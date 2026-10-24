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

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    # OpenClaw's own database (agents/main/agent/openclaw-agent.sqlite). For this
    # kind, FileState.offset is the last rowid read, not a byte offset, and the file
    # changes in place: the store keeps the cursor instead of the head-hash checks.
    OPENCLAW_DB = "openclaw_db"


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
    # The cost the source itself reported (OpenClaw's usage.cost.total for its direct
    # provider calls), in USD. capcom prices from its own table first and uses this
    # only for a model the table lacks (flagged inexact, priced_as "openclaw-estimate").
    reported_cost_usd: float | None = None


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
      size_tokens (int, estimate of the listing's size); an api-request-shape also
      has deferred_names (tuple: its tools marked defer_loading, sent without their
      schema) and tools_size_tokens (int: the schemas of the other tools)
    - MODEL: model (str): a `model` attachment (the session's model was set or changed)
    - MEMORY: source ("instructions" | "nested_memory"), paths (tuple),
      size_tokens (int, estimate)
    - SKILLS_INVOKED: names (tuple), size_tokens (int); source "slash_command" when
      the user typed `/<name>` (built-in commands are named too)
    - API_REQUEST: request_id, query_source, shape_hash, model, cache_ttl (str)
    - API_ERROR: status (int), error (str)
    - TURN: duration_ms (int)
    - PROMPT: source (str: the record's promptSource when logged, e.g. "typed"): a
      message the user sent on the main thread (a typed or queued prompt, a slash
      command, a headless `claude -p` prompt); never tool results, subagent prompts,
      task notifications or other text Claude Code added
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
    PROMPT = "prompt"


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
    the latest non-None value. Read back from the store, `title` is the session's
    name: its `custom_title` when it has a non-empty one, else its `ai-title`."""

    session_id: str
    project_dir: str
    cwd: str | None = None
    title: str | None = None  # the `ai-title` record (from a source)
    version: str | None = None
    entrypoint: str | None = None
    git_branch: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    # The `custom-title` record: the name the user gave it (`/rename`, `claude -n`),
    # "" when cleared. It wins over any `ai-title`, earlier or later.
    custom_title: str | None = None
    # The `agent-color` record: the color the user gave it (`/color`), a lowercase
    # name such as "purple"; "" when reset. The latest one wins.
    color: str | None = None
    # Read back from the store: whether the session's prompts (EventKind.PROMPT) are
    # counted, i.e. its Claude Code main log was read by a capcom that records them.
    # False when that log was gone by then (the count is unknown), None from a source.
    prompts_counted: bool | None = None


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
    # The time of the first timestamped record after it in the same file (None until
    # one is written). Calibration ends the process's window there, exclusive: the
    # records just before a cost-state can be out of order, and background subagents
    # keep spending after the main log's last record.
    next_timestamp: datetime | None = None
    # `totalDuration`: the wall-clock milliseconds of the processes these totals
    # cover. A resumed process that restores the totals carries `startTime` and this
    # on, so the growth from one record to the next is the span of the process that
    # wrote the later one (calibration compares per such step).
    total_duration_ms: int | None = None


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
    # The time of the first timestamped record in the batch, in file order (None if
    # none): what fills the next_timestamp of earlier cost-states.
    first_timestamp: datetime | None = None


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
    # Logged spend in no process step a cost-state covers: a Claude Code process
    # that wrote no cost-state (crashed, killed, or still running). Not in
    # `capcom_usd` and never a mismatch: cost-state cannot count it.
    outside_usd: float = 0.0
    # Per model, cost-state's output tokens above capcom's where capcom estimated
    # output, priced at the output rate: the true output can reach this much more
    # (the log has no true count). So capcom's total is "at least capcom_usd, up to
    # capcom_usd + output_above_usd".
    output_above: Mapping[str, float] = field(default_factory=dict, hash=False)
    # Per model, then per category ("cache_read", "input", "output", "cache_write"),
    # cost-state's tokens above the log's in the compared window, priced: calls
    # Claude Code counted that the log does not record.
    unlogged: Mapping[str, Mapping[str, float]] = field(default_factory=dict, hash=False)
    # The other way round: per model and category, the log's tokens above
    # cost-state's in the compared window, priced the same way.
    logged_more: Mapping[str, Mapping[str, float]] = field(default_factory=dict, hash=False)

    @property
    def output_above_usd(self) -> float:
        return sum(self.output_above.values())

    @property
    def unlogged_usd(self) -> float:
        return sum(sum(v.values()) for v in self.unlogged.values())

    @property
    def logged_more_usd(self) -> float:
        return sum(sum(v.values()) for v in self.logged_more.values())

    @property
    def rest_usd(self) -> float:
        """cost-state's total less capcom's, the output above its estimate and the
        calls the log does not record, plus what the log counts more of: the gap
        nothing here explains."""
        return (
            self.claude_code_usd
            - self.capcom_usd
            - self.output_above_usd
            - self.unlogged_usd
            + self.logged_more_usd
        )

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
    files_failed: int = 0  # files whose scan raised (retried next scan)


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

    def shared_replies(
        self, session_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        """Replies of `session_id` timed in [since, until) whose row another session's
        log holds (a copied or forked session), so this session is missing them."""
        ...

    def last_cost_states(self) -> Iterator[CostState]:
        """The last cost-state of each session that has one. (The store also offers
        `cost_states(session_id)`, every cost-state of the file holding the last,
        in file order; optional, so callers reach it through getattr.)"""
        ...


# --------------------------------------------------------------------------- timeline


class GroupBy(StrEnum):
    """How a chart splits spend into series."""

    CATEGORY = "category"  # uncached input, cache read, cache write, output, web
    PROJECT = "project"  # project_dir (temporary /tmp folders and OpenClaw grouped)
    MODEL = "model"  # canonical price-table model id
    AGENT = "agent"  # "main" for main threads, else AgentInfo.agent_type
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class Series:
    """One line of a chart: a value at each time step (USD unless the producer says
    otherwise). `times` are the step starts, aware UTC, equally spaced."""

    key: str  # stable id of the group member (category name, project dir, model, ...)
    label: str  # display label
    times: tuple[datetime, ...]
    values: tuple[float, ...]  # same length as times


class SpikeKind(StrEnum):
    SINGLE_REQUEST = "single_request"  # one request far above its recent median
    SLOPE_CHANGE = "slope_change"  # the spend rate rose and stayed up


@dataclass(frozen=True, slots=True)
class Spike:
    """A jump in spend, in one session (`session_id` set) or machine-wide (None)."""

    kind: SpikeKind
    start: datetime
    end: datetime  # exclusive
    cost_usd: float  # spent in [start, end) within the scope
    baseline_usd: float  # what the baseline rate would have spent over the same span
    session_id: str | None = None
    agent_id: str | None = None
    message_ids: tuple[str, ...] = ()  # the requests that make it up, in time order
    # "thread": one agent thread (agent_id None = the main thread); "session": all
    # threads of the session together, which is where subagent fan-out shows.
    scope: str = "thread"

    @property
    def excess_usd(self) -> float:
        return max(0.0, self.cost_usd - self.baseline_usd)


# --------------------------------------------------------------------------- explain


class CauseKind(StrEnum):
    """The causes in design.md, "Causes (explain)"."""

    # A cache rewritten after a gap past its lifetime, by who was waiting (the stable
    # sub-kinds checks use too: `capcom.explain.shared.EXPIRY_SUB_KINDS`):
    IDLE_REWRITE = "idle_rewrite"  # "idle_expiry": the user was away (main thread, no tool)
    TOOL_WAIT_EXPIRY = "tool_wait_expiry"  # the agent's own tool call ran past the lifetime
    LATE_RESUME_EXPIRY = "late_resume_expiry"  # a finished subagent was resumed late
    COMPACTION = "compaction"  # cache rewritten after a compaction (or one ran late)
    MODEL_SWITCH = "model_switch"
    TOOL_SET_CHANGE = "tool_set_change"  # tools, connectors or skills changed mid-session
    LARGE_TOOL_RESULT = "large_tool_result"  # written once, reread by later turns
    CONTEXT_GROWTH = "context_growth"
    FAN_OUT = "fan_out"  # a burst of subagent spawns
    LONG_REPORT = "long_report"  # a long subagent report reread by the parent
    THINKING_HEAVY = "thinking_heavy"
    POLLING = "polling"  # the same tool call repeated in a row
    PARALLEL_SUBAGENTS = "parallel_subagents"  # many subagents' own work at once
    UNEXPLAINED = "unexplained"


class Confidence(StrEnum):
    CLEAR = "clear"
    LIKELY = "likely"
    UNEXPLAINED = "unexplained"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact from the logs behind an explanation or a finding.

    `label` is a short sentence generated only from allowed fields (counts, times,
    sizes, models, tool names, file paths, agent types), never conversation text.
    The ids point back to the record so a page can link to it."""

    label: str
    session_id: str | None = None
    agent_id: str | None = None
    timestamp: datetime | None = None
    message_id: str | None = None
    event_key: str | None = None


@dataclass(frozen=True, slots=True)
class Explanation:
    """A spike matched to a cause. Several explanations may share one spike; their
    `excess_usd` never add up to more than the spike's excess."""

    spike: Spike
    cause: CauseKind
    confidence: Confidence
    excess_usd: float  # the part of the spike's excess this cause accounts for
    title: str  # short generated headline, e.g. "Cache rebuilt after 74 minutes idle"
    evidence: tuple[Evidence, ...] = ()
    advice: str = ""
    summary: str = ""  # one or two generated sentences for the page's body (no log text)


# --------------------------------------------------------------------------- checks


@dataclass(frozen=True, slots=True)
class Finding:
    """A setup warning drawn from real usage (design.md, "Setup checks")."""

    kind: str  # stable id, e.g. "claudeai_connectors_unused"
    title: str
    scope: str  # what it applies to: a project dir, an agent type, or "all"
    summary: str  # one or two generated sentences with the numbers
    saving_usd_per_week: float  # estimated, at the window's pace
    advice: str
    change: str | None = None  # text to paste: a settings snippet, frontmatter, a line
    evidence: tuple[Evidence, ...] = ()
    metrics: Mapping[str, AttrValue] = field(default_factory=dict, hash=False)


# --------------------------------------------------------------------------- module entry points
#
# Each analysis module exposes these functions at package level (capcom.timeline,
# capcom.explain, capcom.checks); the report module and the CLI call them only.
# `since` inclusive, `until` exclusive, both aware datetimes.


class TimelineModule(Protocol):
    def cumulative(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        by: GroupBy,
        step: timedelta,
    ) -> list[Series]:
        """Cumulative USD per group member, largest final value first."""
        ...

    def per_step(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        by: GroupBy,
        step: timedelta,
    ) -> list[Series]:
        """USD spent in each step per group member (bars, heatmaps)."""
        ...

    def find_spikes(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        session_id: str | None = None,
    ) -> list[Spike]:
        """Spikes in the window, per session (or in one session), time order."""
        ...


class ExplainModule(Protocol):
    def explain(
        self, index: Index, table: PriceTable, spikes: Sequence[Spike]
    ) -> list[Explanation]:
        """One or more explanations per spike; UNEXPLAINED when no rule fits."""
        ...


class ChecksModule(Protocol):
    def run_checks(
        self, index: Index, table: PriceTable, since: datetime, until: datetime
    ) -> list[Finding]:
        """Findings ranked by saving_usd_per_week, largest first."""
        ...


# --------------------------------------------------------------------------- limits


class LimitWindow(StrEnum):
    """The subscription's usage windows, as Claude Code's status line reports them."""

    FIVE_HOUR = "five_hour"
    SEVEN_DAY = "seven_day"


@dataclass(frozen=True, slots=True)
class LimitSample:
    """One reading of a usage window, recorded by `capcom record-limits` from the
    status line's stdin (rate_limits.<window>.used_percentage and resets_at)."""

    time: datetime
    window: LimitWindow
    used_percentage: float
    resets_at: datetime | None = None
    session_id: str | None = None
    source: str = "statusline"  # "statusline" or "rate_limit_error" (a seed from the logs)
    account: str | None = None  # the account's oauthAccount.accountUuid; None: not labeled


# --------------------------------------------------------------------------- did it work?
#
# The user marks a finding as applied on a date; capcom measures the finding's own
# rate before and after that date (design.md, "Did it work?"). Marks are the only
# thing capcom writes besides its cache: $XDG_DATA_HOME/capcom/changes.jsonl.


@dataclass(frozen=True, slots=True)
class ChangeMark:
    """A change the user made, tied to the finding it answers (kind and scope)."""

    id: int  # small, stable, shown in the CLI and URLs
    kind: str  # the finding's kind, e.g. "subagent_cache_expiry"
    scope: str  # the finding's scope: a project dir, an agent type, or "all"
    applied_on: date  # local date the change was made; "after" starts the next day
    title: str  # the finding's title when marked
    what: str = "suggested"  # "suggested" (the finding's change) or "other"
    note: str = ""  # the user's own words, kept on this machine
    expected_usd_per_week: float | None = None  # the finding's saving when marked
    marked_at: datetime | None = None
    source: str = "user"  # "user", or "noticed" (a detected change the user confirmed)


class Verdict(StrEnum):
    WORKING = "working"  # the after rate sits below the before weeks' range
    NO_EFFECT = "no_effect"  # inside that range
    WORSE = "worse"  # above it
    TOO_EARLY = "too_early"  # under the minimum days or requests since the date
    NO_DATA = "no_data"  # nothing to measure in that scope (before or after)


@dataclass(frozen=True, slots=True)
class MeasurePoint:
    """One week of a change's measure: the rate and the requests behind it."""

    week_start: date
    value: float | None  # None when the week had no requests in scope
    requests: int
    after: bool  # True for weeks starting after the applied date


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """A mark measured on the logs. Rates, so a busier week doesn't count against it."""

    mark: ChangeMark
    measure: str  # e.g. "Tool-wait rewrites per 1,000 requests"
    before: float | None  # the rate over the before window
    after: float | None  # the rate since the day after the date
    before_range: tuple[float, float] | None  # lowest and highest full before week
    weeks: tuple[MeasurePoint, ...]
    requests_per_week_before: float
    requests_per_week_after: float
    measured_usd_per_week: float | None  # (before - after) x after workload x unit price
    verdict: Verdict
    verdict_due: date | None  # when a too-early verdict can be given
    reason: str  # one generated sentence with the numbers
    evidence: tuple[tuple[str, str, str], ...] = ()  # (label, before, after)
    quality: tuple[tuple[str, str, str], ...] = ()  # model changes: signals to judge by


@dataclass(frozen=True, slots=True)
class NoticedChange:
    """A change capcom saw in a file it already reads (an agent definition's
    frontmatter), matching a finding's suggestion; shown for the user to confirm."""

    kind: str
    scope: str
    title: str
    path: Path
    detail: str  # e.g. "model: sonnet"
    since: datetime  # the file's modification time
