"""The marks file: $XDG_DATA_HOME/capcom/changes.jsonl, append-only.

Each line is one event, replayed in order: `mark` (a new mark with its id),
`edit` (a new date or note for an id) and `unmark` (the id is gone). Ids are never
reused: the next id is one past the largest ever written. Writers take an
exclusive lock on the file, reread it under the lock and append one line, so two
`capcom mark` running at once never get the same id. A line that is not complete
JSON (a writer that died mid-line) is skipped, and so are unknown events and
fields: a newer capcom may write more.

This is the only file capcom writes besides its cache; it never lives under
~/.claude or ~/.openclaw.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any

from capcom.types import ChangeMark

MAX_NOTE_CHARS = 500
WHATS = frozenset({"suggested", "other"})
SOURCES = frozenset({"user", "noticed"})


def store_path(env: dict[str, str] | None = None) -> Path:
    """$XDG_DATA_HOME/capcom/changes.jsonl (default ~/.local/share/capcom/)."""
    env = dict(os.environ) if env is None else env
    home = Path(env.get("HOME") or str(Path.home()))
    data = env.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    path = Path(data).expanduser() / "capcom" / "changes.jsonl"
    _refuse_log_dirs(path, env)
    return path


def _refuse_log_dirs(path: Path, env: dict[str, str] | None = None) -> None:
    """Raise ValueError when `path` lies under a log folder capcom only reads."""
    env = dict(os.environ) if env is None else env
    home = Path(env.get("HOME") or str(Path.home()))
    roots = {home / ".claude", home / ".openclaw"}
    for var in ("CLAUDE_CONFIG_DIR", "OPENCLAW_STATE_DIR"):
        if env.get(var):
            roots.add(Path(env[var]).expanduser())
    target = path.expanduser().resolve()
    for root in roots:
        try:
            target.relative_to(root.expanduser().resolve())
        except ValueError:
            continue
        raise ValueError(f"refusing to write marks under {root}: capcom only reads it")


# --------------------------------------------------------------------------- reading


def _lines(fh: IO[bytes]) -> Iterator[dict[str, Any]]:
    """The file's complete JSON object lines; a partial or bad line is skipped."""
    for raw in fh:
        if not raw.endswith(b"\n"):
            return  # a writer is mid-line (or died there): read it next time
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _from_event(obj: dict[str, Any]) -> ChangeMark | None:
    mid, kind, scope = obj.get("id"), obj.get("kind"), obj.get("scope")
    applied = _date(obj.get("applied_on"))
    if not isinstance(mid, int) or not isinstance(kind, str) or not isinstance(scope, str):
        return None
    if applied is None:
        return None
    expected = obj.get("expected_usd_per_week")
    return ChangeMark(
        id=mid,
        kind=kind,
        scope=scope,
        applied_on=applied,
        title=_str(obj.get("title")),
        what=_str(obj.get("what"), "suggested"),
        note=_str(obj.get("note")),
        expected_usd_per_week=float(expected) if isinstance(expected, int | float) else None,
        marked_at=_when(obj.get("marked_at")),
        source=_str(obj.get("source"), "user"),
    )


def _replay(fh: IO[bytes]) -> tuple[dict[int, ChangeMark], int]:
    """(open marks by id, the largest id ever written)."""
    marks: dict[int, ChangeMark] = {}
    top = 0
    for obj in _lines(fh):
        event, mid = obj.get("event"), obj.get("id")
        if not isinstance(mid, int):
            continue
        if event == "mark":
            top = max(top, mid)  # even a mark this capcom cannot read keeps its id
            m = _from_event(obj)
            if m is not None:
                marks[m.id] = m
        elif event == "edit" and mid in marks:
            m = marks[mid]
            applied = _date(obj.get("applied_on"))
            note = obj.get("note")
            marks[mid] = ChangeMark(
                id=m.id,
                kind=m.kind,
                scope=m.scope,
                applied_on=applied or m.applied_on,
                title=m.title,
                what=m.what,
                note=note if isinstance(note, str) else m.note,
                expected_usd_per_week=m.expected_usd_per_week,
                marked_at=m.marked_at,
                source=m.source,
            )
        elif event == "unmark":
            marks.pop(mid, None)
    return marks, top


def load(path: Path | None = None) -> list[ChangeMark]:
    """The open marks, by id. A missing file has none."""
    path = path or store_path()
    try:
        with path.open("rb") as fh:
            marks, _ = _replay(fh)
    except FileNotFoundError:
        return []
    return [marks[k] for k in sorted(marks)]


# --------------------------------------------------------------------------- writing


@contextmanager
def _locked(path: Path) -> Iterator[IO[bytes]]:
    """The file opened for reading and appending under an exclusive lock."""
    _refuse_log_dirs(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        try:
            import fcntl
        except ImportError:  # no flock on this platform: a single writer is assumed
            fcntl = None  # type: ignore[assignment]
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            yield fh
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _append(fh: IO[bytes], obj: dict[str, Any]) -> None:
    fh.seek(0, os.SEEK_END)
    if fh.tell() > 0:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) != b"\n":  # a torn line: end it, so it stays one bad line
            fh.write(b"\n")
    fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    fh.flush()
    os.fsync(fh.fileno())


def _check_note(note: str) -> None:
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"the note is {len(note)} characters; at most {MAX_NOTE_CHARS}")


def _duplicate(marks: dict[int, ChangeMark], kind: str, scope: str, on: date, skip: int) -> None:
    for m in marks.values():
        if m.id != skip and (m.kind, m.scope, m.applied_on) == (kind, scope, on):
            raise ValueError(f"mark {m.id} already records {kind}@{scope} on {on}")


def mark(
    kind: str,
    scope: str,
    applied_on: date,
    title: str,
    *,
    what: str = "suggested",
    note: str = "",
    expected_usd_per_week: float | None = None,
    source: str = "user",
    path: Path | None = None,
) -> ChangeMark:
    """Record that the change for finding `kind`@`scope` was made on `applied_on`.
    Refuses a second open mark of the same finding on the same date."""
    if what not in WHATS:
        raise ValueError(f"what must be one of {sorted(WHATS)}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {sorted(SOURCES)}")
    _check_note(note)
    path = path or store_path()
    with _locked(path) as fh:
        marks, top = _replay(fh)
        _duplicate(marks, kind, scope, applied_on, skip=0)
        m = ChangeMark(
            id=top + 1,
            kind=kind,
            scope=scope,
            applied_on=applied_on,
            title=title,
            what=what,
            note=note,
            expected_usd_per_week=expected_usd_per_week,
            marked_at=datetime.now(UTC),
            source=source,
        )
        _append(
            fh,
            {
                "event": "mark",
                "id": m.id,
                "kind": kind,
                "scope": scope,
                "applied_on": applied_on.isoformat(),
                "title": title,
                "what": what,
                "note": note,
                "expected_usd_per_week": expected_usd_per_week,
                "marked_at": m.marked_at.isoformat() if m.marked_at else None,
                "source": source,
            },
        )
    return m


def edit(
    id: int,
    *,
    applied_on: date | None = None,
    note: str | None = None,
    path: Path | None = None,
) -> ChangeMark:
    """A new date or note for an open mark; KeyError when there is no such mark."""
    if note is not None:
        _check_note(note)
    path = path or store_path()
    with _locked(path) as fh:
        marks, _ = _replay(fh)
        if id not in marks:
            raise KeyError(f"no open mark {id}")
        old = marks[id]
        if applied_on is not None:
            _duplicate(marks, old.kind, old.scope, applied_on, skip=id)
        obj: dict[str, Any] = {"event": "edit", "id": id, "at": datetime.now(UTC).isoformat()}
        if applied_on is not None:
            obj["applied_on"] = applied_on.isoformat()
        if note is not None:
            obj["note"] = note
        _append(fh, obj)
    return ChangeMark(
        id=old.id,
        kind=old.kind,
        scope=old.scope,
        applied_on=applied_on or old.applied_on,
        title=old.title,
        what=old.what,
        note=old.note if note is None else note,
        expected_usd_per_week=old.expected_usd_per_week,
        marked_at=old.marked_at,
        source=old.source,
    )


def unmark(id: int, path: Path | None = None) -> None:
    """Remove an open mark (an `unmark` event); KeyError when there is no such mark."""
    path = path or store_path()
    with _locked(path) as fh:
        marks, _ = _replay(fh)
        if id not in marks:
            raise KeyError(f"no open mark {id}")
        _append(fh, {"event": "unmark", "id": id, "at": datetime.now(UTC).isoformat()})
