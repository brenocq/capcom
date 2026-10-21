"""`capcom limits`: the usage windows in the terminal, the seed from the index's
rate-limit errors, and the status line change that starts recording."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from capcom.config import Config
from capcom.cost.prices import load_prices
from capcom.limits.model import (
    analyze_all,
    append_samples,
    load_samples,
    samples_path,
    seed_samples,
)
from capcom.limits.view import NOT_RECORDING, format_views
from capcom.types import Index

PREFIX = "capcom record-limits"


def _open(config: Config) -> Index | None:
    from capcom.store.index import open_index

    try:
        return open_index(config.index_path, readonly=True)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as err:
        print(f"capcom: cannot read the index at {config.index_path} ({err})", file=sys.stderr)
        print("run `capcom scan` first", file=sys.stderr)
        return None


def _status_line(settings: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The statusLine object of `settings` (read-only) and an error, if any."""
    try:
        with settings.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as err:
        return None, f"cannot read {settings} ({err})"
    if not isinstance(data, dict):
        return None, f"{settings} does not hold a JSON object"
    sl = data.get("statusLine")
    return (sl if isinstance(sl, dict) else None), None


def where_capcom() -> tuple[bool, str | None]:
    """Whether `capcom` is on PATH outside a virtual environment, and the path found
    (`uv run` puts the project's environment on PATH, which Claude Code's shell lacks)."""
    found = shutil.which("capcom")
    if found is None:
        return False, None
    in_venv = sys.prefix != sys.base_prefix and Path(found).resolve().is_relative_to(
        Path(sys.prefix).resolve()
    )
    return not in_venv, found


def install_text(settings: Path, on_path: bool, found: str | None = None) -> str:
    """The change to `statusLine` that puts the recorder in front of the current
    command (or alone, printing its own line, when there is none), and its undo."""
    sl, err = _status_line(settings)
    if err is not None:
        return f"capcom limits: {err}"
    q = json.dumps
    lines = [f"File: {settings} (capcom does not change it; make the change yourself)", ""]
    cmd = sl.get("command") if sl is not None else None
    if isinstance(cmd, str) and cmd.strip().startswith(PREFIX):
        rest = cmd.strip()[len(PREFIX) :].lstrip()
        old = rest.removeprefix("|").strip() if rest.startswith("|") else None
        lines += ["The recorder is already installed:", f'    "command": {q(cmd)}', ""]
        if old:
            lines += ["To undo, set it back to:", f'    "command": {q(old)}']
        else:
            lines += ['To undo, remove the "statusLine" entry.']
        return "\n".join(lines)
    if isinstance(cmd, str) and cmd.strip():
        new = f"{PREFIX} | {cmd}"
        lines += [
            'Change "statusLine" → "command":',
            f'  - "command": {q(cmd)}',
            f'  + "command": {q(new)}',
            "",
            "Your status line stays the same: the recorder passes its input through",
            "unchanged and appends a sample to capcom's cache when a percentage moves.",
            "",
            'Undo: set the command back to the "-" line above.',
        ]
    else:
        block = {"type": "command", "command": f"{PREFIX} --line"}
        lines += [
            'There is no status line command. Add this "statusLine" entry:',
            f'  + "statusLine": {q(block)}',
            "",
            "The recorder then prints a short line of its own (model, 5-hour and weekly",
            "percentages) and appends a sample to capcom's cache when a percentage moves.",
            "",
            'Undo: remove the "statusLine" entry.',
        ]
    if not on_path:
        lines += [
            "",
            "Note: Claude Code runs the status line in a shell where `capcom` must be on",
            "PATH, and it is not"
            + (" (this one is only in a virtual environment:" if found else "."),
        ]
        if found:
            lines += [f"  {found})."]
        lines += [
            "Install it where that shell finds it (`uv tool install <capcom folder>`),",
            "or write its full path in the command.",
        ]
    return "\n".join(lines)


def run_limits(
    config: Config,
    seed: bool = False,
    dry_run: bool = False,
    install: bool = False,
    out: TextIO | None = None,
    now: datetime | None = None,
    index: Index | None = None,
) -> int:
    out = out or sys.stdout
    if install:
        on_path, where = where_capcom()
        print(install_text(config.claude_root / "settings.json", on_path, where), file=out)
        return 0
    now = now or datetime.now(UTC)
    index = index or _open(config)
    if index is None:
        return 1
    if seed:
        found = seed_samples(index)
        print(
            f"{found.errors} rate-limit errors in the index; {len(found.samples)} name the "
            f"window they hit, {found.unknown_window} do not (skipped).",
            file=out,
        )
        if dry_run:
            for s in found.samples:
                when = s.time.astimezone().strftime("%Y-%m-%d %H:%M")
                reset = s.resets_at.astimezone().strftime("%Y-%m-%d %H:%M") if s.resets_at else "?"
                print(f"  {when}  {s.window.value:<9}  100 %  resets {reset}", file=out)
            print("dry run: nothing written", file=out)
            return 0
        added = append_samples(config.cache_dir, found.samples)
        print(f"{len(added)} new samples in {samples_path(config.cache_dir)}", file=out)
        return 0
    samples = load_samples(samples_path(config.cache_dir))
    if not samples:
        print(f"No samples in {samples_path(config.cache_dir)}.", file=out)
        print(NOT_RECORDING, file=out)
        return 0
    from capcom.timeline.groups import ProjectNames

    names = ProjectNames.build(index.sessions(), config.openclaw_root)
    views = analyze_all(index, load_prices(), samples, now, lambda d: names.label(names.key(d)))
    print(f"capcom limits ({len(samples)} samples, {samples_path(config.cache_dir)})\n", file=out)
    print(format_views(views, samples, now), file=out)
    return 0
