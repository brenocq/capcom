"""`capcom limits`: the seed and the terminal view (the install is test_install)."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

from capcom.config import Config
from capcom.limits.cli import run_limits
from capcom.limits.model import load_samples, sample_json, samples_path
from capcom.types import Event, EventKind, LimitWindow
from tests.limits.fakes import T0, FakeIndex, minutes, req, sample


def config(tmp_path: Path) -> Config:
    return Config(
        tmp_path / "claude",
        tmp_path / "openclaw",
        tmp_path / "cache" / "capcom",
        tmp_path / "state" / "capcom",
    )


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
    assert not cfg.state_dir.exists()
    out = io.StringIO()
    run_limits(cfg, seed=True, out=out, index=_errors())  # type: ignore[arg-type]
    assert "1 new samples" in out.getvalue()
    assert [s.source for s in load_samples(samples_path(cfg.state_dir))] == ["rate_limit_error"]


def test_view_adopts_the_samples_left_in_the_cache(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.cache_dir.mkdir(parents=True)
    line = sample_json(sample(T0, 30, resets=T0 + timedelta(hours=4))) + "\n"
    samples_path(cfg.cache_dir).write_text(line)
    out = io.StringIO()
    run_limits(cfg, out=out, now=T0 + minutes(5), index=FakeIndex())  # type: ignore[arg-type]
    assert "5-hour window: 30%" in out.getvalue()
    assert samples_path(cfg.state_dir).read_text() == line
    assert not samples_path(cfg.cache_dir).exists()


def test_view_without_samples(tmp_path: Path) -> None:
    out = io.StringIO()
    run_limits(config(tmp_path), out=out, index=FakeIndex())  # type: ignore[arg-type]
    assert "Not recording yet: run `capcom limits --install`" in out.getvalue()


def test_view_with_samples(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    cfg.state_dir.mkdir(parents=True)
    reset = T0 + timedelta(hours=4)
    reqs = [req(T0 - timedelta(hours=1) + minutes(10 * i), output=20_000) for i in range(12)]
    ss = [sample(T0 - minutes(55 - 10 * i), 4 * i + 4, resets=reset) for i in range(6)]
    ss.append(sample(T0, 30, resets=reset))
    samples_path(cfg.state_dir).write_text("".join(sample_json(s) + "\n" for s in ss))
    out = io.StringIO()
    run_limits(cfg, out=out, now=T0 + minutes(5), index=FakeIndex(reqs))  # type: ignore[arg-type]
    text = out.getvalue()
    assert "5-hour window: 30%" in text and "at the reset" in text and "80% range" in text
    assert "% per API-equivalent dollar" in text and "too few samples" in text
    assert "Weekly limit: no samples." in text
    assert "this window by project" in text
    assert LimitWindow.FIVE_HOUR.value not in text  # labels, not ids
