"""`capcom limits`: the install text, the seed and the terminal view."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.limits.cli import PREFIX, install_text, run_limits, unwrap, wrap
from capcom.limits.model import load_samples, sample_json, samples_path
from capcom.types import Event, EventKind, LimitWindow
from tests.limits.fakes import T0, FakeIndex, minutes, req, sample


def config(tmp_path: Path) -> Config:
    return Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache" / "capcom")


def settings(tmp_path: Path, body: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(body))
    return path


def test_install_wraps_the_current_command(tmp_path: Path) -> None:
    cmd = 'node "/home/user/.claude/hooks/line.js"'
    path = settings(tmp_path, {"statusLine": {"type": "command", "command": cmd}, "x": 1})
    before = path.read_bytes()
    text = install_text(path, on_path=True)
    assert f'  - "command": {json.dumps(cmd)}' in text
    assert f'  + "command": {json.dumps("capcom record-limits | { " + cmd + chr(10) + "}")}' in text
    assert "Undo" in text and "not on" not in text
    assert path.read_bytes() == before  # never written


def test_install_without_a_status_line(tmp_path: Path) -> None:
    text = install_text(settings(tmp_path, {"model": "opus"}), on_path=False)
    assert '"command": "capcom record-limits --line"' in text
    assert "must be on" in text and "and it is not." in text
    assert "capcom record-limits" in install_text(tmp_path / "missing.json", on_path=True)


def test_install_when_already_installed(tmp_path: Path) -> None:
    path = settings(tmp_path, {"statusLine": {"command": "capcom record-limits | my-line"}})
    text = install_text(path, on_path=True)
    assert "already installed" in text and '"command": "my-line"' in text
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert install_text(bad, on_path=True).startswith("capcom limits: cannot read")


def test_install_command_reads_settings_only(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.claude_root.mkdir()
    (cfg.claude_root / "settings.json").write_text('{"statusLine": {"command": "x"}}')
    out = io.StringIO()
    assert run_limits(cfg, install=True, out=out) == 0
    assert '"capcom record-limits | { x\\n}"' in out.getvalue()
    assert sorted(p.name for p in cfg.claude_root.iterdir()) == ["settings.json"]


def _errors() -> FakeIndex:
    def ev(i: int, **attrs: object) -> Event:
        return Event(EventKind.API_ERROR, "s1", None, T0 + minutes(i), f"k{i}", attrs)  # type: ignore[arg-type]

    return FakeIndex(
        events=[
            ev(1, status=429, error="rate_limit", rate_limit_type="five_hour"),
            ev(2, status=429, error="rate_limit"),
        ]
    )


def test_seed_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    out = io.StringIO()
    assert run_limits(cfg, seed=True, dry_run=True, out=out, index=_errors()) == 0  # type: ignore[arg-type]
    text = out.getvalue()
    assert "2 rate-limit errors" in text and "1 name the window" in text and "1 do not" in text
    assert "five_hour" in text and "dry run" in text
    assert not cfg.cache_dir.exists()
    out = io.StringIO()
    run_limits(cfg, seed=True, out=out, index=_errors())  # type: ignore[arg-type]
    assert "1 new samples" in out.getvalue()
    assert [s.source for s in load_samples(samples_path(cfg.cache_dir))] == ["rate_limit_error"]


def test_view_without_samples(tmp_path: Path) -> None:
    out = io.StringIO()
    run_limits(config(tmp_path), out=out, index=FakeIndex())  # type: ignore[arg-type]
    assert "Not recording yet: run `capcom limits --install`" in out.getvalue()


def test_view_with_samples(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    reset = T0 + timedelta(hours=4)
    reqs = [req(T0 - timedelta(hours=1) + minutes(10 * i), output=20_000) for i in range(12)]
    ss = [sample(T0 - minutes(55 - 10 * i), 4 * i + 4, resets=reset) for i in range(6)]
    ss.append(sample(T0, 30, resets=reset))
    samples_path(cfg.cache_dir).write_text("".join(sample_json(s) + "\n" for s in ss))
    out = io.StringIO()
    run_limits(cfg, out=out, now=T0 + minutes(5), index=FakeIndex(reqs))  # type: ignore[arg-type]
    text = out.getvalue()
    assert "5-hour window: 30%" in text and "at the reset" in text and "80% range" in text
    assert "% per API-equivalent dollar" in text and "too few samples" in text
    assert "Weekly limit: no samples." in text
    assert "this window by project" in text
    assert LimitWindow.FIVE_HOUR.value not in text  # labels, not ids


SHAPES = [
    'input=$(cat); echo "$input" | tr a-z A-Z',
    "head -c 20; echo done",
    "cat | wc -c # the line's length",
    "tr -d '\\n' && echo",
]


@pytest.mark.parametrize("cmd", SHAPES)
def test_wrapped_command_behaves_like_the_command(tmp_path: Path, cmd: str) -> None:
    data = b'{"session_id": "s", "rate_limits": {}, "model": {"display_name": "abc"}}\n'
    env = os.environ | {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    alone = subprocess.run(["sh", "-c", cmd], input=data, capture_output=True, timeout=30)
    new = wrap(cmd).replace(PREFIX, f"{sys.executable} -m capcom.cli record-limits", 1)
    piped = subprocess.run(["sh", "-c", new], input=data, capture_output=True, env=env, timeout=30)
    assert piped.returncode == alone.returncode == 0
    assert piped.stdout == alone.stdout and piped.stderr == alone.stderr == b""
    # settings.json holds it as one JSON string, and undo gives the command back
    assert json.loads(json.dumps(wrap(cmd))) == wrap(cmd)
    assert unwrap(wrap(cmd)) == (True, cmd)


def test_unwrap_finds_the_recorder_by_any_path() -> None:
    assert unwrap("/home/user/.local/bin/capcom record-limits | { my-line\n}") == (
        True,
        "my-line",
    )
    assert unwrap("~/bin/capcom record-limits --line") == (True, None)
    assert unwrap("  capcom record-limits | old-style") == (True, "old-style")
    assert unwrap("capcom record-limits-other | x") == (False, None)
    assert unwrap("my-line") == (False, None)


def test_install_when_already_installed_wrapped(tmp_path: Path) -> None:
    cmd = "/opt/capcom/bin/capcom record-limits | { a; b # note\n}"
    text = install_text(settings(tmp_path, {"statusLine": {"command": cmd}}), on_path=True)
    assert "already installed" in text and '"command": "a; b # note"' in text
