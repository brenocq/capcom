"""`capcom limits --install` and `--uninstall`: the change shown, the question, the
write that keeps everything else, and the wrapper that keeps the status line as it
was. Settings files are synthetic, in a temporary folder."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from capcom.cli import build_parser
from capcom.config import Config
from capcom.limits import install
from capcom.limits.install import PREFIX, Paint, run_install, unwrap, where_capcom, wrap

LINE = 'node "/home/user/.claude/hooks/line.js"'


def config(tmp_path: Path) -> Config:
    cfg = Config(tmp_path / "claude", tmp_path / "openclaw", tmp_path / "cache", tmp_path / "state")
    cfg.claude_root.mkdir()
    return cfg


def settings_path(cfg: Config) -> Path:
    return cfg.claude_root / "settings.json"


def write(cfg: Config, body: object) -> bytes:
    raw = json.dumps(body, indent=2).encode()
    settings_path(cfg).write_bytes(raw)
    return raw


def read(cfg: Config) -> dict[str, object]:
    data = json.loads(settings_path(cfg).read_text())
    assert isinstance(data, dict)
    return data


def answer(reply: str, then: Callable[[], None] | None = None) -> Callable[[str], str]:
    def ask(question: str) -> str:
        assert "Make this change?" in question
        if then is not None:
            then()
        return reply

    return ask


def never(question: str) -> str:
    raise AssertionError("asked: " + question)


@pytest.fixture(autouse=True)
def on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "where_capcom", lambda: (True, "/usr/local/bin/capcom"))


def run(cfg: Config, **kw: object) -> tuple[int, str]:
    out = io.StringIO()
    code = run_install(cfg, out, **kw)  # type: ignore[arg-type]
    return code, out.getvalue()


def test_install_shows_the_change_asks_and_makes_it(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    raw = write(
        cfg, {"model": "opus", "statusLine": {"type": "command", "command": LINE, "padding": 0}}
    )
    code, text = run(cfg, ask=answer("y"))
    assert code == 0
    assert "- " + LINE in text and "+ capcom record-limits | { " + LINE in text
    assert "Done." in text and "capcom limits --uninstall" in text
    data = read(cfg)
    assert data["statusLine"] == {"type": "command", "command": wrap(LINE), "padding": 0}
    assert list(data) == ["model", "statusLine"] and data["model"] == "opus"
    assert (cfg.claude_root / "settings.json.capcom-backup").read_bytes() == raw


@pytest.mark.parametrize("reply", ["", "n", "no", "maybe"])
def test_anything_but_yes_changes_nothing(tmp_path: Path, reply: str) -> None:
    cfg = config(tmp_path)
    raw = write(cfg, {"statusLine": {"command": LINE}})
    code, text = run(cfg, ask=answer(reply))
    assert code == 0 and "Nothing changed." in text
    assert settings_path(cfg).read_bytes() == raw
    assert sorted(p.name for p in cfg.claude_root.iterdir()) == ["settings.json"]


def test_install_without_a_status_line_adds_one(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"model": "opus"})
    code, text = run(cfg, ask=answer("yes"))
    assert code == 0 and "prints a short one" in text
    assert read(cfg)["statusLine"] == {"type": "command", "command": f"{PREFIX} --line"}


def test_install_creates_a_missing_settings_file(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    code, _ = run(cfg, yes=True)
    assert code == 0 and read(cfg) == {
        "statusLine": {"type": "command", "command": f"{PREFIX} --line"}
    }
    assert not (cfg.claude_root / "settings.json.capcom-backup").exists()


def test_already_installed_asks_nothing(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"statusLine": {"command": wrap(LINE)}})
    code, text = run(cfg, ask=never)
    assert code == 0 and "Already recording" in text and "--uninstall" in text


def test_uninstall_gives_the_command_back(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"statusLine": {"type": "command", "command": LINE}})
    run(cfg, yes=True)
    code, text = run(cfg, remove=True, ask=answer("y"))
    assert code == 0 and "Your samples stay in" in text
    assert read(cfg)["statusLine"] == {"type": "command", "command": LINE}
    code, text = run(cfg, remove=True, ask=never)
    assert code == 0 and "nothing to undo" in text


def test_uninstall_removes_the_entry_the_recorder_added(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"model": "opus"})
    run(cfg, yes=True)
    code, _ = run(cfg, remove=True, yes=True)
    assert code == 0 and read(cfg) == {"model": "opus"}


def test_not_on_path_says_how_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "where_capcom", lambda: (False, "/repo/.venv/bin/capcom"))
    cfg = config(tmp_path)
    raw = write(cfg, {"statusLine": {"command": LINE}})
    code, text = run(cfg, ask=never)
    assert code == 1 and "uv tool install" in text and "/repo/.venv/bin/capcom" in text
    assert settings_path(cfg).read_bytes() == raw


def test_without_a_terminal_it_needs_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    cfg = config(tmp_path)
    raw = write(cfg, {"statusLine": {"command": LINE}})
    code, text = run(cfg)
    assert code == 1 and "--yes" in text and settings_path(cfg).read_bytes() == raw
    code, _ = run(cfg, yes=True)
    assert code == 0 and read(cfg)["statusLine"] == {"command": wrap(LINE), "type": "command"}


def test_keeps_what_changed_while_it_asked(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"statusLine": {"command": LINE}})
    code, _ = run(
        cfg, ask=answer("y", lambda: write(cfg, {"theme": "dark", "statusLine": {"command": LINE}}))
    )
    assert code == 0 and read(cfg)["theme"] == "dark"
    assert read(cfg)["statusLine"] == {"command": wrap(LINE), "type": "command"}


def test_refuses_when_the_status_line_changed_while_it_asked(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(cfg, {"statusLine": {"command": LINE}})
    raw = json.dumps({"statusLine": {"command": "other-line"}}).encode()
    code, text = run(cfg, ask=answer("y", lambda: settings_path(cfg).write_bytes(raw)))
    assert code == 1 and "changed meanwhile" in text
    assert settings_path(cfg).read_bytes() == raw


def test_writes_through_a_symlinked_settings_file(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"statusLine": {"command": LINE}}))
    target.chmod(0o640)
    settings_path(cfg).symlink_to(target)
    code, _ = run(cfg, yes=True)
    assert code == 0 and settings_path(cfg).is_symlink()
    assert json.loads(target.read_text())["statusLine"]["command"] == wrap(LINE)
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("body", ["{nope", "[1, 2]"])
def test_unreadable_settings_change_nothing(tmp_path: Path, body: str) -> None:
    cfg = config(tmp_path)
    settings_path(cfg).write_text(body)
    code, text = run(cfg, ask=never)
    assert code == 1 and "Cannot read" in text and settings_path(cfg).read_text() == body


def test_without_claude_code_folder(tmp_path: Path) -> None:
    cfg = Config(tmp_path / "none", tmp_path / "openclaw", tmp_path / "cache", tmp_path / "state")
    code, text = run(cfg, ask=never)
    assert code == 1 and "is Claude Code set up?" in text and not cfg.claude_root.exists()


def test_colors_only_on_a_terminal() -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    assert Paint(Terminal(), {}).green("ok") == "\033[32mok\033[0m"
    assert Paint(Terminal(), {"NO_COLOR": "1"}).green("ok") == "ok"
    assert Paint(Terminal(), {"TERM": "dumb"}).green("ok") == "ok"
    assert Paint(io.StringIO(), {}).green("ok") == "ok"


def test_install_and_uninstall_are_exclusive() -> None:
    args = build_parser().parse_args(["limits", "--install", "-y"])
    assert args.install and args.yes and not args.uninstall
    with pytest.raises(SystemExit):
        build_parser().parse_args(["limits", "--install", "--uninstall"])


# ---------------------------------------------------------------- the wrapper


def executable(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "capcom"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def run_from(monkeypatch: pytest.MonkeyPatch, env: Path, *path: Path) -> None:
    """As if running from the virtual environment at `env`, with `path` as PATH."""
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, path)))


def test_where_capcom_skips_the_uv_run_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / ".venv"
    own = executable(venv / "bin")
    run_from(monkeypatch, venv, venv / "bin", tmp_path / "usr-bin")
    assert where_capcom() == (False, str(own))
    installed = executable(tmp_path / "local-bin")
    run_from(monkeypatch, venv, venv / "bin", tmp_path / "local-bin")
    assert where_capcom() == (True, str(installed))


def test_where_capcom_finds_a_uv_tool_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tmp_path / "tools" / "capcom"
    local = tmp_path / "local-bin"
    local.mkdir()
    (local / "capcom").symlink_to(executable(tool / "bin"))
    run_from(monkeypatch, tool, local)
    assert where_capcom() == (True, str(local / "capcom"))


def test_where_capcom_finds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_from(monkeypatch, tmp_path / ".venv", tmp_path / "empty")
    assert where_capcom() == (False, None)


SHAPES = [
    'input=$(cat); echo "$input" | tr a-z A-Z',
    "head -c 20; echo done",
    "cat | wc -c # the line's length",
    "tr -d '\\n' && echo",
]


@pytest.mark.parametrize("cmd", SHAPES)
def test_wrapped_command_behaves_like_the_command(tmp_path: Path, cmd: str) -> None:
    data = b'{"session_id": "s", "rate_limits": {}, "model": {"display_name": "abc"}}\n'
    env = os.environ | {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
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


def test_uninstall_unwraps_a_compound_command(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    write(
        cfg, {"statusLine": {"command": "/opt/capcom/bin/capcom record-limits | { a; b # note\n}"}}
    )
    code, _ = run(cfg, remove=True, yes=True)
    assert code == 0 and read(cfg)["statusLine"] == {"command": "a; b # note", "type": "command"}
