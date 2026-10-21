"""The marks file: append-only events replayed, edits, unmarks, locking."""

from __future__ import annotations

import json
import multiprocessing
from datetime import date
from pathlib import Path

import pytest

from capcom.changes import edit, load, mark, store_path, unmark

D = date(2026, 10, 7)


def test_the_store_lives_under_xdg_data_home(tmp_path: Path) -> None:
    assert store_path() == tmp_path / "data" / "capcom" / "changes.jsonl"
    env = {"HOME": str(tmp_path / "h")}
    assert store_path(env) == tmp_path / "h" / ".local" / "share" / "capcom" / "changes.jsonl"


def test_a_path_under_the_claude_or_openclaw_folders_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "roots"
    for bad in (home / ".claude" / "x.jsonl", home / ".openclaw" / "capcom" / "c.jsonl"):
        with pytest.raises(ValueError, match="only reads"):
            mark("k", "all", D, "t", path=bad)
        assert not bad.exists()
    with pytest.raises(ValueError):
        store_path({"HOME": str(home), "XDG_DATA_HOME": str(home / ".claude")})


def test_marks_get_increasing_ids_and_replay(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    a = mark("idle_cache_rewrites", "-p", D, "Idle", note="lunch breaks", path=p)
    b = mark("model_mix", "worker", D, "Mix", expected_usd_per_week=4.5, what="other", path=p)
    assert (a.id, b.id) == (1, 2)
    got = load(p)
    assert [m.id for m in got] == [1, 2]
    assert got[0].note == "lunch breaks" and got[0].marked_at is not None
    assert got[1].expected_usd_per_week == 4.5 and got[1].what == "other"


def test_edits_and_unmarks_replay_and_ids_are_never_reused(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    mark("a", "s", D, "A", path=p)
    mark("b", "s", D, "B", path=p)
    e = edit(1, applied_on=date(2026, 10, 9), note="really the 9th", path=p)
    assert e.applied_on == date(2026, 10, 9) and e.note == "really the 9th"
    unmark(2, path=p)
    assert [(m.id, m.applied_on, m.note) for m in load(p)] == [
        (1, date(2026, 10, 9), "really the 9th")
    ]
    assert mark("b", "s", D, "B again", path=p).id == 3
    with pytest.raises(KeyError):
        unmark(2, path=p)
    with pytest.raises(KeyError):
        edit(2, note="x", path=p)


def test_a_duplicate_open_mark_is_refused_but_another_date_is_fine(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    mark("a", "s", D, "A", path=p)
    with pytest.raises(ValueError, match="already"):
        mark("a", "s", D, "A", path=p)
    mark("a", "s", date(2026, 10, 8), "A", path=p)
    with pytest.raises(ValueError, match="already"):
        edit(2, applied_on=D, path=p)
    unmark(1, path=p)
    mark("a", "s", D, "A", path=p)  # the first one is gone


def test_a_long_note_or_an_unknown_what_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    with pytest.raises(ValueError, match="500"):
        mark("a", "s", D, "A", note="x" * 501, path=p)
    with pytest.raises(ValueError):
        mark("a", "s", D, "A", what="maybe", path=p)
    mark("a", "s", D, "A", note="x" * 500, path=p)
    with pytest.raises(ValueError):
        edit(1, note="y" * 501, path=p)


def test_a_partial_last_line_and_unknown_fields_and_events_are_tolerated(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    lines = [
        {
            "event": "mark",
            "id": 1,
            "kind": "a",
            "scope": "s",
            "applied_on": "2026-10-07",
            "title": "A",
            "future_field": [1, 2],
        },
        {"event": "rename", "id": 1, "title": "B"},
        {"event": "mark", "id": 2, "kind": "b", "scope": "s", "applied_on": "not a date"},
        "not json",
    ]
    text = "".join((json.dumps(x) if isinstance(x, dict) else x) + "\n" for x in lines)
    p.write_text(text + '{"event": "unmark", "id": 1')  # a writer mid-line
    assert [m.id for m in load(p)] == [1]
    # the next write ends the torn line first, so it stays one bad line
    m = mark("c", "s", D, "C", path=p)
    assert m.id == 3
    assert [x.id for x in load(p)] == [1, 3]


def test_a_missing_file_has_no_marks(tmp_path: Path) -> None:
    assert load(tmp_path / "none.jsonl") == []


def _mark_many(args: tuple[str, int]) -> list[int]:
    path, worker = args
    return [mark(f"k{worker}", f"s{i}", D, "t", path=Path(path)).id for i in range(10)]


def test_concurrent_writers_never_share_an_id(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    with multiprocessing.get_context("fork").Pool(4) as pool:
        ids = [i for chunk in pool.map(_mark_many, [(str(p), w) for w in range(4)]) for i in chunk]
    assert sorted(ids) == list(range(1, 41))
    assert len(load(p)) == 40
