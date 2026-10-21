"""The Claude Code log adapter: projects folder -> normalized records.

Layout read (docs/data-sources.md):

    <projects>/<project>/<session>.jsonl                                  SESSION
    <projects>/<project>/<session>/subagents/agent-<id>.jsonl             SUBAGENT
    <projects>/<project>/<session>/subagents/workflows/<wf>/agent-<id>.jsonl  SUBAGENT

Files are opened read-only and streamed line by line in binary mode, so offsets are
byte positions. A last line without its newline is a write in progress: it is left
for the next scan. Every field is optional; bad JSON and unknown record types are
skipped, never raised.

No conversation text leaves this module: records carry counts, lengths, tool names,
paths, a command's first word, agent types, skill and server names, hosts.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from capcom.types import (
    AgentInfo,
    AttrValue,
    Batch,
    CostState,
    Event,
    EventKind,
    LogFile,
    LogKind,
    ModelUsage,
    Record,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
    estimate_tokens,
)

# Batch bounds: whichever comes first.
MAX_BATCH_LINES = 2_000
MAX_BATCH_BYTES = 8 * 1024 * 1024

# The scouts' rule (docs/data-sources.md, "Output counts"): in subagent logs a logged
# output below this, on a reply with a tool call or thinking, is a stream-start value.
STREAM_START_OUTPUT = 20

# A tool result image, as a flagged estimate (about what a screenshot costs).
IMAGE_TOKENS = 1_600

# Bounds on the per-parse-call memory (tool calls awaiting their result, replies
# whose streaming records may still come).
_MAX_PENDING_TOOLS = 20_000
_MAX_RECENT_REPLIES = 256

_SHORT = 200  # longest label kept (descriptions, targets)

# Top-level record types the adapter uses. A line is decoded only when it contains
# `"type":"<one of these>"`, or when one of its `"type"` keys is not written compactly
# (so a format change can never make the cheap check drop a needed record).
_USED_TYPES = (
    "assistant",
    "user",
    "system",
    "attachment",
    "api-request",
    "api-request-shape",
    "ai-title",
    "cost-state",
)
_MARKERS = tuple(f'"type":"{t}"'.encode() for t in _USED_TYPES)
_TYPE_KEY = b'"type"'
_TYPE_KEY_COMPACT = b'"type":"'
_FIRST_TYPE = re.compile(rb'"type":"([^"\\]{1,64})"')

# Record types known and deliberately ignored (not counted as unknown).
_IGNORED_TYPES = frozenset(
    {
        "last-prompt",
        "mode",
        "permission-mode",
        "queue-operation",
        "frame-link",
        "atis-latch",
        "bridge-session",
        "file-history-snapshot",
        "file-history-delta",
        "api-request-blob",
        "progress",
        "summary",
        "custom-title",
        "tag",
        "agent-name",
        "agent-color",
        "pr-link",
        "fork-context-ref",
        "artifact-autoreact-ledger",
        "artifact-comment-monitor",
    }
)

_AGENT_TOOLS = frozenset({"Agent", "Task"})
_PATH_TOOLS = {
    "Read": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# --------------------------------------------------------------------------- helpers


def _ts(value: object) -> datetime | None:
    """An ISO 8601 timestamp as an aware UTC datetime (naive ones are taken as UTC)."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    if t.tzinfo is None:
        return t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _str(value: object, limit: int = _SHORT) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _names(value: object) -> tuple[str, ...]:
    return tuple(x for x in _list(value) if isinstance(x, str))


def _text_len(value: object) -> int:
    """Length of a string, or the summed lengths of a list of strings."""
    if isinstance(value, str):
        return len(value)
    return sum(len(x) for x in _list(value) if isinstance(x, str))


def _json_len(value: object) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _first_word(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    for word in command.split():
        if _ENV_ASSIGN.match(word):
            continue  # FOO=1 cmd ...: the command is the next word
        return word[:64]
    return None


def tool_target(name: str, tool_input: object) -> str | None:
    """The short label of a tool call (see `ToolCall`), never content."""
    args = _dict(tool_input)
    if name in _PATH_TOOLS:
        return _str(args.get(_PATH_TOOLS[name]) or args.get("file_path"), 1024)
    if name == "Bash":
        return _first_word(args.get("command"))
    if name in _AGENT_TOOLS:
        return _str(args.get("subagent_type"))
    if name == "Skill":
        return _str(args.get("skill") or args.get("name") or args.get("command"))
    if name == "WebFetch":
        url = args.get("url")
        if not isinstance(url, str):
            return None
        try:
            return urlsplit(url).hostname
        except ValueError:
            return None
    if name in ("Grep", "Glob"):
        return _str(args.get("path"), 1024)
    return None


def _usage(raw: Mapping[str, Any]) -> Usage:
    split = _dict(raw.get("cache_creation"))
    total_write = _int(raw.get("cache_creation_input_tokens"))
    w5 = _int(split.get("ephemeral_5m_input_tokens"))
    w1 = _int(split.get("ephemeral_1h_input_tokens"))
    if w5 + w1 < total_write:
        # No split (older logs) or a partial one: the rest is the 5-minute default.
        w5 += total_write - (w5 + w1)
    server = _dict(raw.get("server_tool_use"))
    details = _dict(raw.get("output_tokens_details"))
    return Usage(
        input=_int(raw.get("input_tokens")),
        cache_read=_int(raw.get("cache_read_input_tokens")),
        cache_write_5m=w5,
        cache_write_1h=w1,
        output=_int(raw.get("output_tokens")),
        thinking=_int(details.get("thinking_tokens")),
        web_searches=_int(server.get("web_search_requests")),
        web_fetches=_int(server.get("web_fetch_requests")),
    )


def _cache_ttl(cache_control: object) -> tuple[str | None, tuple[str, ...]]:
    """(the request's cache lifetime, every lifetime named): '1h' wins when present;
    a breakpoint without a ttl is the API default, '5m'."""
    ttls: list[str] = []
    for entry in _list(cache_control):
        value = _dict(_dict(entry).get("value")) or _dict(entry)
        ttl = value.get("ttl")
        if not isinstance(ttl, str):
            ttl = "5m" if value.get("type") == "ephemeral" else None
        if ttl and ttl not in ttls:
            ttls.append(ttl)
    if not ttls:
        return None, ()
    return ("1h" if "1h" in ttls else ttls[0]), tuple(ttls)


def _result_size(content: object) -> tuple[int, int]:
    """(chars of text, number of images) of a tool_result's content."""
    if isinstance(content, str):
        return len(content), 0
    chars = images = 0
    for block in _list(content):
        if isinstance(block, str):
            chars += len(block)
            continue
        b = _dict(block)
        kind = b.get("type")
        if kind == "text":
            chars += _text_len(b.get("text"))
        elif kind == "image":
            images += 1
        elif kind == "tool_reference":
            chars += _text_len(b.get("tool_name"))
        elif b:
            chars += _json_len(b)
    return chars, images


# --------------------------------------------------------------------------- state


@dataclass(slots=True)
class _Reply:
    """What the streaming records of one message.id say so far."""

    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    blocks_seen: set[tuple[str, int, int]] = field(default_factory=set)
    content_chars: int = 0  # thinking + text + JSON of tool inputs (subagent logs only)
    tool_or_thinking: bool = False
    stop_reason: str | None = None


@dataclass(slots=True)
class _Pending:
    """The last record of a message.id within the current batch."""

    rec: dict[str, Any]
    reply: _Reply
    timestamp: datetime
    agent_id: str | None


@dataclass(slots=True)
class _BatchState:
    records: list[Record] = field(default_factory=list)
    pending: dict[str, _Pending] = field(default_factory=dict)
    lines: int = 0
    bytes: int = 0
    skipped: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    cwd: str | None = None
    version: str | None = None
    entrypoint: str | None = None
    git_branch: str | None = None
    title: str | None = None


class _Parser:
    """One parse call over one file: carries tool names and replies across batches."""

    def __init__(self, source: ClaudeCodeSource, log: LogFile) -> None:
        self.source = source
        self.log = log
        self.subagent = log.kind == LogKind.SUBAGENT
        self.tools: OrderedDict[str, tuple[str, str | None]] = OrderedDict()
        self.replies: OrderedDict[str, _Reply] = OrderedDict()
        self.last_ts: datetime | None = None
        self.batch = _BatchState()

    # -- batches

    def flush(self, end_offset: int) -> Batch:
        b = self.batch
        for mid, p in b.pending.items():
            req = self._request(mid, p)
            if req is not None:
                b.records.append(req)
            else:
                b.skipped += 1
        if b.first_seen is not None or b.title is not None:
            if self.subagent:
                info = SessionInfo(
                    session_id=self.log.session_id,
                    project_dir=self.log.project_dir,
                    title=b.title,
                    first_seen=b.first_seen,
                    last_seen=b.last_seen,
                )
            else:
                info = SessionInfo(
                    session_id=self.log.session_id,
                    project_dir=self.log.project_dir,
                    cwd=b.cwd,
                    title=b.title,
                    version=b.version,
                    entrypoint=b.entrypoint,
                    git_branch=b.git_branch,
                    first_seen=b.first_seen,
                    last_seen=b.last_seen,
                )
            b.records.append(info)
        batch = Batch(records=b.records, end_offset=end_offset, lines=b.lines, skipped=b.skipped)
        self.batch = _BatchState()
        return batch

    # -- lines

    def line(self, raw: bytes, offset: int) -> None:
        b = self.batch
        b.lines += 1
        b.bytes += len(raw)
        if not any(m in raw for m in _MARKERS) and raw.count(_TYPE_KEY) == raw.count(
            _TYPE_KEY_COMPACT
        ):
            # A record type the adapter does not use: not decoded. A line that cannot
            # be one JSON object (garbage, a torn write) still counts as skipped.
            body = raw.strip()
            if not (body.startswith(b"{") and body.endswith(b"}")):
                b.skipped += 1
                return
            m = _FIRST_TYPE.search(body)
            if m is not None:
                kind = m.group(1).decode("utf-8", "replace")
                if kind not in _IGNORED_TYPES:
                    self.source.unknown_types[kind] += 1
            return
        try:
            rec = json.loads(raw)
        except ValueError:
            b.skipped += 1
            return
        if not isinstance(rec, dict):
            b.skipped += 1
            return
        kind = rec.get("type")
        if kind == "assistant":
            self._seen(rec)
            self._assistant(rec, offset)
        elif kind == "user":
            self._seen(rec)
            self._user(rec, offset)
        elif kind == "system":
            self._seen(rec)
            self._system(rec, offset)
        elif kind == "attachment":
            self._seen(rec)
            self._attachment(rec, offset)
        elif kind == "api-request-shape":
            self._seen(rec)
            self._shape(rec, offset)
        elif kind == "api-request":
            self._seen(rec)
            self._api_request(rec, offset)
        elif kind == "ai-title":
            title = _str(rec.get("aiTitle"), 500)
            if title:
                b.title = title
        elif kind == "cost-state":
            self._cost_state(rec, offset)
        elif isinstance(kind, str) and kind in _IGNORED_TYPES:
            pass
        else:
            self.source.unknown_types[str(kind)] += 1

    def _seen(self, rec: dict[str, Any]) -> datetime | None:
        """Feed the batch's SessionInfo; returns the record's time (or the last one)."""
        b = self.batch
        t = _ts(rec.get("timestamp"))
        if t is not None:
            self.last_ts = t
            if b.first_seen is None or t < b.first_seen:
                b.first_seen = t
            if b.last_seen is None or t > b.last_seen:
                b.last_seen = t
        if not self.subagent:
            b.cwd = _str(rec.get("cwd"), 4096) or b.cwd
            b.version = _str(rec.get("version")) or b.version
            b.entrypoint = _str(rec.get("entrypoint")) or b.entrypoint
            b.git_branch = _str(rec.get("gitBranch"), 500) or b.git_branch
        return t or self.last_ts

    def _agent_id(self, rec: Mapping[str, Any]) -> str | None:
        if self.subagent:
            return self.log.agent_id
        if rec.get("isSidechain") is True:
            return _str(rec.get("agentId"))
        return None

    def _event(
        self,
        kind: EventKind,
        rec: Mapping[str, Any],
        offset: int,
        attrs: dict[str, AttrValue],
        suffix: str | None = None,
    ) -> None:
        t = _ts(rec.get("timestamp")) or self.last_ts
        if t is None:
            return
        key = _str(rec.get("uuid")) or str(offset)
        if suffix:
            key = f"{key}:{suffix}"
        self.batch.records.append(
            Event(
                kind=kind,
                session_id=self.log.session_id,
                agent_id=self._agent_id(rec),
                timestamp=t,
                key=key,
                attrs={k: v for k, v in attrs.items() if v is not None},
            )
        )

    # -- assistant

    def _reply(self, mid: str) -> _Reply:
        reply = self.replies.get(mid)
        if reply is None:
            reply = self.replies[mid] = _Reply()
            if len(self.replies) > _MAX_RECENT_REPLIES:
                self.replies.popitem(last=False)
        else:
            self.replies.move_to_end(mid)
        return reply

    def _assistant(self, rec: dict[str, Any], offset: int) -> None:
        msg = _dict(rec.get("message"))
        mid = _str(msg.get("id"), 500) or _str(rec.get("requestId"), 500)
        if mid is None:
            uuid = _str(rec.get("uuid"), 500)
            mid = f"uuid:{uuid}" if uuid else None
        if mid is None:
            self.batch.skipped += 1
            return
        reply = self._reply(mid)
        for block in _list(msg.get("content")):
            self._block(rec, offset, reply, _dict(block))
        stop = _str(msg.get("stop_reason"))
        if stop:
            reply.stop_reason = stop
        if rec.get("isApiErrorMessage") is True:
            label = rec.get("error")
            status = _opt_int(rec.get("apiErrorStatus"))
            self._event(
                EventKind.API_ERROR,
                rec,
                offset,
                {
                    "status": status,
                    "error": label if isinstance(label, str) and len(label) <= 64 else None,
                    "source": "assistant",
                },
            )
        t = _ts(rec.get("timestamp")) or self.last_ts
        if t is None:
            self.batch.skipped += 1
            return
        # Keep only what the Request needs from the last record (no content).
        slim = {
            k: rec.get(k)
            for k in ("requestId", "cwd", "version", "effort", "isApiErrorMessage", "agentId")
        }
        slim["isSidechain"] = rec.get("isSidechain")
        slim["model"] = msg.get("model")
        slim["usage"] = _dict(msg.get("usage"))
        self.batch.pending.pop(mid, None)  # keep the batch in arrival order of last records
        self.batch.pending[mid] = _Pending(slim, reply, t, self._agent_id(rec))

    def _block(
        self, rec: Mapping[str, Any], offset: int, reply: _Reply, block: Mapping[str, Any]
    ) -> None:
        kind = block.get("type")
        if kind == "tool_use" or kind == "server_tool_use":
            name = _str(block.get("name")) or "unknown"
            tid = _str(block.get("id"), 500) or f"{offset}:{len(reply.tool_calls)}"
            reply.tool_or_thinking = True
            if tid in reply.tool_calls:
                return
            tool_input = block.get("input")
            target = tool_target(name, tool_input)
            reply.tool_calls[tid] = ToolCall(tool_use_id=tid, name=name, target=target)
            self.tools[tid] = (name, target)
            if len(self.tools) > _MAX_PENDING_TOOLS:
                self.tools.popitem(last=False)
            if self.subagent:
                reply.content_chars += _json_len(tool_input)
            if name in _AGENT_TOOLS:
                args = _dict(tool_input)
                self._event(
                    EventKind.SPAWN,
                    rec,
                    offset,
                    {
                        "tool_use_id": tid,
                        "agent_type": _str(args.get("subagent_type")),
                        "description": _str(args.get("description")),
                    },
                    suffix=tid,
                )
        elif kind in ("thinking", "redacted_thinking"):
            reply.tool_or_thinking = True
            if self.subagent:
                self._count_text(reply, "thinking", block.get("thinking"))
        elif kind == "text" and self.subagent:
            self._count_text(reply, "text", block.get("text"))

    @staticmethod
    def _count_text(reply: _Reply, kind: str, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        sig = (kind, len(text), hash(text))
        if sig in reply.blocks_seen:
            return  # the same block written again by a later streaming record
        reply.blocks_seen.add(sig)
        reply.content_chars += len(text)

    def _request(self, mid: str, p: _Pending) -> Request:
        rec, reply = p.rec, p.reply
        usage = _usage(rec["usage"])
        estimated = False
        if self.subagent and usage.output < STREAM_START_OUTPUT and reply.tool_or_thinking:
            estimate = estimate_tokens(reply.content_chars)
            usage = Usage(
                input=usage.input,
                cache_read=usage.cache_read,
                cache_write_5m=usage.cache_write_5m,
                cache_write_1h=usage.cache_write_1h,
                output=max(usage.output, estimate),
                thinking=usage.thinking,
                web_searches=usage.web_searches,
                web_fetches=usage.web_fetches,
            )
            estimated = True
        raw_usage = rec["usage"]
        return Request(
            message_id=mid,
            session_id=self.log.session_id,
            agent_id=p.agent_id,
            project_dir=self.log.project_dir,
            timestamp=p.timestamp,
            model=_str(rec.get("model")) or "unknown",
            usage=usage,
            request_id=_str(rec.get("requestId"), 500),
            cwd=_str(rec.get("cwd"), 4096),
            version=_str(rec.get("version")),
            effort=_str(rec.get("effort")),
            speed=_str(raw_usage.get("speed")),
            service_tier=_str(raw_usage.get("service_tier")),
            inference_geo=_str(raw_usage.get("inference_geo")),
            stop_reason=reply.stop_reason,
            is_api_error=rec.get("isApiErrorMessage") is True,
            output_estimated=estimated,
            tool_calls=tuple(reply.tool_calls.values()),
        )

    # -- user

    def _user(self, rec: dict[str, Any], offset: int) -> None:
        content = _dict(rec.get("message")).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            b = _dict(block)
            if b.get("type") != "tool_result":
                continue
            tid = _str(b.get("tool_use_id"), 500)
            chars, images = _result_size(b.get("content"))
            name, target = self.tools.pop(tid, (None, None)) if tid else (None, None)
            self._event(
                EventKind.TOOL_RESULT,
                rec,
                offset,
                {
                    "tool_use_id": tid,
                    "tool": name,
                    "target": target,
                    "chars": chars,
                    "size_tokens": estimate_tokens(chars) + IMAGE_TOKENS * images,
                    "is_image": images > 0,
                    "images": images or None,
                    "is_error": b.get("is_error") is True,
                },
                suffix=tid or str(offset),
            )

    # -- system

    def _system(self, rec: dict[str, Any], offset: int) -> None:
        sub = rec.get("subtype")
        if sub == "compact_boundary":
            meta = _dict(rec.get("compactMetadata"))
            self._event(
                EventKind.COMPACTION,
                rec,
                offset,
                {
                    "trigger": _str(meta.get("trigger")),
                    "pre_tokens": _opt_int(meta.get("preTokens")),
                    "post_tokens": _opt_int(meta.get("postTokens")),
                    "duration_ms": _opt_int(meta.get("durationMs")),
                },
            )
        elif sub == "away_summary":
            self._event(EventKind.AWAY, rec, offset, {})
        elif sub == "turn_duration":
            self._event(
                EventKind.TURN, rec, offset, {"duration_ms": _opt_int(rec.get("durationMs"))}
            )
        elif sub == "api_error":
            err = rec.get("error")
            status = _opt_int(rec.get("status"))
            label: str | None = None
            if isinstance(err, dict):
                status = status if status is not None else _opt_int(err.get("status"))
                label = _str(_dict(err.get("connection")).get("code"), 64)
                if label is None and err.get("isNetworkDown") is True:
                    label = "network_down"
                if label is None and isinstance(err.get("type"), str):
                    label = _str(err.get("type"), 64)
            elif isinstance(err, str) and len(err) <= 64:
                label = err
            self._event(
                EventKind.API_ERROR,
                rec,
                offset,
                {
                    "status": status,
                    "error": label or "api_error",
                    "retry_attempt": _opt_int(rec.get("retryAttempt")),
                    "source": "system",
                },
            )

    # -- attachments

    def _attachment(self, rec: dict[str, Any], offset: int) -> None:
        a = _dict(rec.get("attachment"))
        kind = a.get("type")
        if kind == "skill_listing":
            self._event(
                EventKind.TOOL_SET,
                rec,
                offset,
                {
                    "source": kind,
                    "names": _names(a.get("names")),
                    "size_tokens": estimate_tokens(_text_len(a.get("content"))),
                    "is_initial": _bool(a.get("isInitial")),
                },
            )
        elif kind == "deferred_tools_delta":
            self._event(
                EventKind.TOOL_SET,
                rec,
                offset,
                {
                    "source": kind,
                    "added": _names(a.get("addedNames")),
                    "removed": _names(a.get("removedNames")),
                    "size_tokens": estimate_tokens(_text_len(a.get("addedLines"))),
                },
            )
        elif kind == "deferred_tools_record":
            entries = _list(a.get("entries"))
            names = tuple(n for e in entries if isinstance(n := _dict(e).get("name"), str))
            self._event(
                EventKind.TOOL_SET,
                rec,
                offset,
                {
                    "source": kind,
                    "names": names,
                    "size_tokens": estimate_tokens(_json_len(entries)),
                },
            )
        elif kind == "mcp_instructions_delta":
            self._event(
                EventKind.TOOL_SET,
                rec,
                offset,
                {
                    "source": kind,
                    "added": _names(a.get("addedNames")),
                    "removed": _names(a.get("removedNames")),
                    "size_tokens": estimate_tokens(_text_len(a.get("addedBlocks"))),
                },
            )
        elif kind == "agent_listing_delta":
            self._event(
                EventKind.TOOL_SET,
                rec,
                offset,
                {
                    "source": kind,
                    "added": _names(a.get("addedTypes")),
                    "removed": _names(a.get("removedTypes")),
                    "size_tokens": estimate_tokens(_text_len(a.get("addedLines"))),
                    "is_initial": _bool(a.get("isInitial")),
                },
            )
        elif kind == "model":
            model = _str(_dict(a.get("identity")).get("modelId")) or _str(a.get("model"))
            self._event(EventKind.MODEL, rec, offset, {"model": model})
        elif kind == "instructions":
            files = [_dict(f) for f in _list(a.get("files"))]
            paths = tuple(p for f in files if isinstance(p := f.get("path"), str))
            chars = sum(_text_len(f.get("content")) for f in files)
            self._event(
                EventKind.MEMORY,
                rec,
                offset,
                {"source": kind, "paths": paths, "size_tokens": estimate_tokens(chars)},
            )
        elif kind == "nested_memory":
            content = a.get("content")
            inner = _dict(content)
            path = _str(a.get("path"), 4096) or _str(inner.get("path"), 4096)
            chars = _text_len(inner.get("content")) if inner else _text_len(content)
            self._event(
                EventKind.MEMORY,
                rec,
                offset,
                {
                    "source": kind,
                    "paths": (path,) if path else (),
                    "size_tokens": estimate_tokens(chars),
                },
            )
        elif kind == "invoked_skills":
            skills = [_dict(s) for s in _list(a.get("skills"))]
            names = tuple(n for s in skills if isinstance(n := s.get("name"), str))
            chars = sum(_text_len(s.get("content")) for s in skills)
            self._event(
                EventKind.SKILLS_INVOKED,
                rec,
                offset,
                {"names": names, "size_tokens": estimate_tokens(chars)},
            )

    # -- request records

    def _shape(self, rec: dict[str, Any], offset: int) -> None:
        shape = _dict(rec.get("shape"))
        tools = _list(shape.get("tools"))
        names = tuple(n for t in tools if isinstance(n := _dict(t).get("name"), str))
        size = _json_len({"system": shape.get("system"), "tools": tools})
        self._event(
            EventKind.TOOL_SET,
            rec,
            offset,
            {
                "source": "api-request-shape",
                "shape_hash": _str(rec.get("shapeHash")),
                "names": names,
                "size_tokens": estimate_tokens(size),
            },
        )

    def _api_request(self, rec: dict[str, Any], offset: int) -> None:
        params = _dict(rec.get("params"))
        ttl, ttls = _cache_ttl(rec.get("cacheControl"))
        self._event(
            EventKind.API_REQUEST,
            rec,
            offset,
            {
                "request_id": _str(rec.get("id"), 500),
                "query_source": _str(rec.get("querySource")),
                "shape_hash": _str(rec.get("shapeHash")),
                "model": _str(params.get("model")),
                "cache_ttl": ttl,
                "cache_ttls": ttls or None,
            },
        )

    def _cost_state(self, rec: dict[str, Any], offset: int) -> None:
        models: dict[str, ModelUsage] = {}
        for model, raw in _dict(rec.get("modelUsage")).items():
            m = _dict(raw)
            models[str(model)] = ModelUsage(
                input=_int(m.get("inputTokens")),
                output=_int(m.get("outputTokens")),
                thinking=_int(m.get("thinkingTokens")),
                cache_read=_int(m.get("cacheReadInputTokens")),
                cache_write=_int(m.get("cacheCreationInputTokens")),
                web_searches=_int(m.get("webSearchRequests")),
                cost_usd=_float(m.get("costUSD")),
            )
        self.batch.records.append(
            CostState(
                session_id=self.log.session_id,
                offset=offset,
                total_cost_usd=_float(rec.get("totalCostUSD")),
                models=models,
                start_time_ms=_opt_int(rec.get("startTime")),
                has_unknown_model_cost=_bool(rec.get("hasUnknownModelCost")),
            )
        )


# --------------------------------------------------------------------------- the source


def _sorted_entries(path: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def read_agent_info(log: LogFile) -> AgentInfo:
    """The subagent's AgentInfo from its meta.json (ids only when it is missing or bad)."""
    agent_id = log.agent_id or ""
    meta_path = log.meta_path or log.path.with_name(f"agent-{agent_id}.meta.json")
    try:
        with meta_path.open("rb") as f:
            meta = json.loads(f.read(4 * 1024 * 1024))
    except (OSError, ValueError):
        meta = None
    m = _dict(meta)
    return AgentInfo(
        agent_id=agent_id,
        session_id=log.session_id,
        agent_type=_str(m.get("agentType")),
        description=_str(m.get("description")),
        tool_use_id=_str(m.get("toolUseId"), 500),
        spawn_depth=_opt_int(m.get("spawnDepth")),
        is_built_in=_bool(m.get("isBuiltIn")),
        finished=_bool(m.get("finished")),
    )


class ClaudeCodeSource:
    """`capcom.types.Source` for Claude Code's projects folder."""

    def __init__(self) -> None:
        # Diagnostics: record types seen in decoded lines that the adapter does not
        # know (they are skipped).
        self.unknown_types: Counter[str] = Counter()

    def discover(self, root: Path) -> Iterator[LogFile]:
        """Session logs and subagent logs under `root` (the projects folder), sorted by
        project, then session; each session's subagents follow its own log."""
        for project in _sorted_entries(root):
            if not _is_dir(project):
                continue
            pdir = Path(project.path)
            entries = _sorted_entries(pdir)
            sessions: dict[str, tuple[Path | None, Path | None]] = {}
            for e in entries:
                if e.name.endswith(".jsonl") and _is_file(e):
                    sid = e.name[: -len(".jsonl")]
                    sessions[sid] = (Path(e.path), sessions.get(sid, (None, None))[1])
                elif _is_dir(e):
                    sessions[e.name] = (sessions.get(e.name, (None, None))[0], Path(e.path))
            for sid in sorted(sessions):
                log_path, folder = sessions[sid]
                if log_path is not None:
                    yield LogFile(
                        path=log_path,
                        kind=LogKind.SESSION,
                        project_dir=project.name,
                        session_id=sid,
                    )
                if folder is not None:
                    yield from self._subagents(folder / "subagents", project.name, sid)

    def _subagents(self, folder: Path, project_dir: str, session_id: str) -> Iterator[LogFile]:
        entries = _sorted_entries(folder)
        names = {e.name for e in entries}
        for e in entries:
            name = e.name
            if name.startswith("agent-") and name.endswith(".jsonl") and _is_file(e):
                agent_id = name[len("agent-") : -len(".jsonl")]
                meta = f"agent-{agent_id}.meta.json"
                yield LogFile(
                    path=Path(e.path),
                    kind=LogKind.SUBAGENT,
                    project_dir=project_dir,
                    session_id=session_id,
                    agent_id=agent_id,
                    meta_path=folder / meta if meta in names else None,
                )
            elif _is_dir(e):
                # subagents/workflows/<wf>/agent-<id>.jsonl (workflow agents)
                yield from self._subagents(Path(e.path), project_dir, session_id)

    def parse(self, log: LogFile, start: int) -> Iterator[Batch]:
        """Stream `log` from byte `start`. Always yields at least one batch (a subagent
        log's first batch carries its AgentInfo); a missing file yields nothing."""
        try:
            f = log.path.open("rb")
        except FileNotFoundError:
            return
        with f:
            f.seek(start)
            parser = _Parser(self, log)
            if log.kind == LogKind.SUBAGENT:
                parser.batch.records.append(read_agent_info(log))
            offset = start
            yielded = False
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a write in progress: read it next time
                parser.line(raw, offset)
                offset += len(raw)
                b = parser.batch
                if b.lines >= MAX_BATCH_LINES or b.bytes >= MAX_BATCH_BYTES:
                    yield parser.flush(offset)
                    yielded = True
            if not yielded or parser.batch.lines or parser.batch.records:
                yield parser.flush(offset)
