"""A stand-in for capcom.changes with its package-level functions, for the website
tests: marks kept as events in a JSONL file, and measures made up from the mark alone
(no log is read). All data here is synthetic."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from capcom.types import (
    ChangeMark,
    ChangeResult,
    Finding,
    Index,
    MeasurePoint,
    NoticedChange,
    PriceTable,
    Verdict,
)


class FakeChanges:
    def __init__(self, path: Path, noticed_kind: str | None = None) -> None:
        self.path = path
        self.noticed_kind = noticed_kind
        self.calls: list[str] = []

    def store_path(self) -> Path:
        return self.path

    def _events(self, path: Path | None) -> list[dict[str, Any]]:
        p = path or self.path
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def _append(self, event: dict[str, Any], path: Path | None) -> None:
        with (path or self.path).open("a") as f:
            f.write(json.dumps(event) + "\n")

    def load(self, path: Path | None = None) -> list[ChangeMark]:
        marks: dict[int, ChangeMark] = {}
        for e in self._events(path):
            if e["event"] == "mark":
                marks[e["id"]] = ChangeMark(
                    e["id"], e["kind"], e["scope"], date.fromisoformat(e["applied_on"]),
                    e["title"], e["what"], e["note"], e["expected"],
                    datetime.fromisoformat(e["marked_at"]), e["source"],
                )  # fmt: skip
            elif e["event"] == "edit" and e["id"] in marks:
                m = marks[e["id"]]
                if "applied_on" in e:
                    m = replace(m, applied_on=date.fromisoformat(e["applied_on"]))
                if "note" in e:
                    m = replace(m, note=e["note"])
                marks[e["id"]] = m
            elif e["event"] == "unmark":
                marks.pop(e["id"], None)
        return list(marks.values())

    def mark(
        self,
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
        self.calls.append("mark")
        new_id = 1 + max((e["id"] for e in self._events(path)), default=0)
        self._append(
            {
                "event": "mark", "id": new_id, "kind": kind, "scope": scope,
                "applied_on": applied_on.isoformat(), "title": title, "what": what,
                "note": note, "expected": expected_usd_per_week,
                "marked_at": datetime(2026, 10, 21, 12, 0, tzinfo=UTC).isoformat(),
                "source": source,
            },
            path,
        )  # fmt: skip
        return next(m for m in self.load(path) if m.id == new_id)

    def edit(
        self,
        id: int,
        *,
        applied_on: date | None = None,
        note: str | None = None,
        path: Path | None = None,
    ) -> ChangeMark:
        self.calls.append("edit")
        event: dict[str, Any] = {"event": "edit", "id": id}
        if applied_on is not None:
            event["applied_on"] = applied_on.isoformat()
        if note is not None:
            event["note"] = note
        self._append(event, path)
        return next(m for m in self.load(path) if m.id == id)

    def unmark(self, id: int, path: Path | None = None) -> None:
        self.calls.append("unmark")
        self._append({"event": "unmark", "id": id}, path)

    def measure(
        self, index: Index, table: PriceTable, mark: ChangeMark, now: datetime
    ) -> ChangeResult:
        today = now.date()
        start = mark.applied_on - timedelta(days=28)
        weeks = [
            MeasurePoint(start + timedelta(days=7 * i), v, 1000 + i, False)
            for i, v in enumerate((36.0, 41.0, 33.0, 43.0))
        ]
        after_day = mark.applied_on + timedelta(days=1)
        i = 0
        while after_day + timedelta(days=7 * i) <= today:
            weeks.append(MeasurePoint(after_day + timedelta(days=7 * i), 4.0 + i, 1100, True))
            i += 1
        early = (today - mark.applied_on).days < 7
        return ChangeResult(
            mark=mark,
            measure="Tool-wait rewrites per 1,000 requests",
            before=38.2,
            after=4.1 if i else None,
            before_range=(33.0, 43.0),
            weeks=tuple(weeks),
            requests_per_week_before=9420.0,
            requests_per_week_after=11050.0 if i else 0.0,
            measured_usd_per_week=None if early else 124.0,
            verdict=Verdict.TOO_EARLY if early else Verdict.WORKING,
            verdict_due=mark.applied_on + timedelta(days=8) if early else None,
            reason="Synthetic reason with the numbers.",
            evidence=(("Long waits per week", "101", "3"),),
            quality=(("Turns per spawn", "41", "44"),) if mark.kind == "model" else (),
        )

    def noticed(
        self, index: Index, table: PriceTable, findings: Sequence[Finding], now: datetime
    ) -> list[NoticedChange]:
        where = Path("/home/user/.claude/agents/coder.md")
        since = now - timedelta(hours=2)
        return [
            NoticedChange(f.kind, f.scope, f.title, where, "model: sonnet", since)
            for f in findings
            if f.kind == self.noticed_kind
        ]
