"""`capcom limits --install` and `--uninstall`: put the recorder in front of Claude
Code's status line command, or take it out, after asking.

This is the one place capcom writes under `~/.claude/`: the `statusLine` entry of
`settings.json`, and only after the user answers yes (or passes `--yes`). The file
is read again just before the write, so an edit Claude Code made while the question
waited is kept; a copy goes to `settings.json.capcom-backup` first; and the write
replaces the file in one step, through a symlink to its target so a dotfiles link
survives.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from capcom.config import Config

PREFIX = "capcom record-limits"
# the recorder at the head of a command, by name or full path, and what follows it
INSTALLED = re.compile(r"\s*(?:\S*/)?capcom\s+record-limits(?![\w-])(.*)", re.DOTALL)
# the wrapper `wrap` puts around the user's command
WRAPPED = re.compile(r"\|\s*\{ (.*)\n\}\s*", re.DOTALL)
BACKUP_SUFFIX = ".capcom-backup"

Ask = Callable[[str], str]


def wrap(cmd: str) -> str:
    """The recorder piped into `cmd` as a brace group, so a compound command
    (`a; b`, `x=$(cat); echo "$x" | ...`, a trailing `# comment`) all reads the copy.
    The newline before `}` ends a trailing comment."""
    return f"{PREFIX} | {{ {cmd}\n}}"


def unwrap(cmd: str) -> tuple[bool, str | None]:
    """Whether `cmd` already runs the recorder first, and the user's own command
    behind it (None when the recorder runs alone)."""
    m = INSTALLED.fullmatch(cmd)
    if m is None:
        return False, None
    rest = m.group(1).strip()
    inner = WRAPPED.fullmatch(rest)
    if inner is not None:
        return True, inner.group(1).strip() or None
    if rest.startswith("|"):
        return True, rest[1:].strip() or None
    return True, None


def where_capcom() -> tuple[bool, str | None]:
    """Whether `capcom` is on PATH outside the running environment's own bin folder,
    and the path found. `uv run` puts that folder first on PATH, and Claude Code's
    shell lacks it; a `uv tool install` also runs from an environment, but links its
    command into a folder of its own (`~/.local/bin`), which that shell does find."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    if sys.prefix != sys.base_prefix:
        own = (Path(sys.prefix) / "bin").resolve()
        dirs = [d for d in dirs if d and Path(d).resolve() != own]
    found = shutil.which("capcom", path=os.pathsep.join(dirs))
    if found is not None:
        return True, found
    return False, shutil.which("capcom")


@dataclass(frozen=True, slots=True)
class Plan:
    """The change to `statusLine.command`: `before` is the command now (None: no
    command), `after` the command it becomes (None: the entry goes)."""

    before: str | None
    after: str | None


def read_settings(path: Path) -> dict[str, Any]:
    """The settings object, {} when the file does not exist. Raises OSError when it
    cannot be read and ValueError when it does not hold a JSON object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("it does not hold a JSON object")
    return data


def _command(data: Mapping[str, Any]) -> str | None:
    sl = data.get("statusLine")
    cmd = sl.get("command") if isinstance(sl, dict) else None
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def plan_install(data: Mapping[str, Any]) -> Plan | None:
    """The recorder in front of the current command, or alone printing its own line;
    None when it already runs."""
    cmd = _command(data)
    if cmd is not None and unwrap(cmd)[0]:
        return None
    return Plan(cmd, wrap(cmd) if cmd is not None else f"{PREFIX} --line")


def plan_uninstall(data: Mapping[str, Any]) -> Plan | None:
    """The command without the recorder (None: the entry it added goes); None when
    the recorder does not run."""
    cmd = _command(data)
    if cmd is None:
        return None
    installed, old = unwrap(cmd)
    return Plan(cmd, old) if installed else None


def apply(data: Mapping[str, Any], plan: Plan) -> dict[str, Any]:
    """`data` with the plan's command; every other key as it was."""
    out = dict(data)
    if plan.after is None:
        out.pop("statusLine", None)
        return out
    sl = out.get("statusLine")
    entry = dict(sl) if isinstance(sl, dict) else {"type": "command"}
    entry.setdefault("type", "command")
    entry["command"] = plan.after
    out["statusLine"] = entry
    return out


def write_settings(path: Path, data: Mapping[str, Any]) -> Path | None:
    """Replace the file at `path` with `data` in one step, after copying it to the
    backup beside it; return the backup (None when there was no file)."""
    target = path.resolve()
    backup = None
    if target.exists():
        backup = target.with_name(target.name + BACKUP_SUFFIX)
        shutil.copy2(target, backup)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.capcom.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if backup is not None:
        tmp.chmod(target.stat().st_mode & 0o7777)
    os.replace(tmp, target)
    return backup


class Paint:
    """ANSI colors on a terminal; plain text elsewhere and under NO_COLOR."""

    def __init__(self, stream: TextIO, env: Mapping[str, str] = os.environ) -> None:
        isatty = getattr(stream, "isatty", None)
        tty = bool(isatty()) if callable(isatty) else False
        self.on = tty and "NO_COLOR" not in env and env.get("TERM") != "dumb"

    def _sgr(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, text: str) -> str:
        return self._sgr("1", text)

    def dim(self, text: str) -> str:
        return self._sgr("2", text)

    def green(self, text: str) -> str:
        return self._sgr("32", text)

    def red(self, text: str) -> str:
        return self._sgr("31", text)

    def yellow(self, text: str) -> str:
        return self._sgr("33", text)

    def cyan(self, text: str) -> str:
        return self._sgr("36", text)


def _home(path: Path | str) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def _source_folder() -> str:
    """The checkout this capcom runs from, for the `uv tool install` line."""
    root = Path(__file__).resolve().parents[2]
    return str(root) if (root / "pyproject.toml").exists() else "<capcom folder>"


def _show(p: Paint, plan: Plan, out: TextIO) -> None:
    pad = "\n    "
    if plan.before is not None:
        print("  " + p.red("- " + plan.before.replace("\n", pad)), file=out)
    if plan.after is not None:
        print("  " + p.green("+ " + plan.after.replace("\n", pad)), file=out)
    else:
        print("  " + p.green("+ (no status line: the entry the recorder added goes)"), file=out)


def _confirm(p: Paint, question: str, out: TextIO, yes: bool, ask: Ask | None) -> bool | None:
    """True on yes, False on anything else; None when nobody can answer."""
    if yes:
        return True
    if ask is None:
        if not sys.stdin.isatty():
            print(
                p.yellow("!") + " No terminal to answer in: run it in one, or add --yes.", file=out
            )
            return None
        ask = input
    try:
        reply = ask(p.bold(question) + " [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        reply = ""
    return reply.strip().lower() in {"y", "yes"}


def run_install(
    config: Config, out: TextIO, *, remove: bool = False, yes: bool = False, ask: Ask | None = None
) -> int:
    """Install the recorder (or remove it), showing the change and asking first."""
    p = Paint(out)
    path = config.claude_root / "settings.json"
    where = _home(path)
    plan_for = plan_uninstall if remove else plan_install
    if not config.claude_root.is_dir():
        print(
            f"{p.yellow('!')} {_home(config.claude_root)} does not exist: is Claude Code set up?",
            file=out,
        )
        return 1
    try:
        plan = plan_for(read_settings(path))
    except (OSError, ValueError) as err:
        print(f"{p.yellow('!')} Cannot read {where} ({err}); nothing changed.", file=out)
        return 1
    if plan is None:
        if remove:
            print(
                f"{p.green('✓')} The recorder is not in your status line: nothing to undo.",
                file=out,
            )
        else:
            print(
                f"{p.green('✓')} Already recording: your status line runs the recorder.", file=out
            )
            print(f"  To stop: {p.cyan('capcom limits --uninstall')}", file=out)
        return 0
    if not remove:
        on_path, found = where_capcom()
        if not on_path:
            print(p.bold("One step first: put capcom on PATH"), file=out)
            print(
                "Claude Code runs the status line in a shell of its own, where `capcom` is",
                file=out,
            )
            print(
                "not found"
                + (f" (this one runs from a virtual environment: {found})." if found else "."),
                file=out,
            )
            print("Install it, then run this again:", file=out)
            print(f"  {p.cyan('uv tool install ' + _source_folder())}", file=out)
            return 1

    if remove:
        print(p.bold("Stop recording your usage limits"), file=out)
        print(f"capcom takes its recorder out of your status line command, in {where}:", file=out)
    else:
        print(p.bold("Record your usage limits"), file=out)
        print(
            "capcom records the 5-hour and weekly usage from Claude Code's status line.", file=out
        )
        print(f"It puts its recorder in front of your status line command, in {where}:", file=out)
    print(file=out)
    _show(p, plan, out)
    print(file=out)
    if remove:
        note = f"Your samples stay in {_home(config.state_dir)}."
    elif plan.before is None:
        note = "With no status line of your own, the recorder prints a short one: model and usage."
    else:
        note = "Your status line looks the same: the recorder passes its input through unchanged."
    print(p.dim(note), file=out)
    print(p.dim(f"A copy of the file is saved as {path.name}{BACKUP_SUFFIX} first."), file=out)
    print(file=out)

    answer = _confirm(p, "Make this change?", out, yes, ask)
    if answer is None:
        return 1
    if not answer:
        print("Nothing changed.", file=out)
        return 0
    try:
        data = read_settings(path)  # Claude Code may have rewritten it while we asked
        if plan_for(data) != plan:
            print(
                f"{p.yellow('!')} {where} changed meanwhile; nothing changed. Run it again.",
                file=out,
            )
            return 1
        write_settings(path, apply(data, plan))
    except (OSError, ValueError) as err:
        print(f"{p.yellow('!')} Cannot write {where} ({err}); nothing changed.", file=out)
        return 1
    if remove:
        print(f"{p.green('✓ Done.')} Your status line runs as it did before.", file=out)
    else:
        print(f"{p.green('✓ Done.')} Recording starts at the next status line refresh.", file=out)
        see, undo = p.cyan("capcom limits"), p.cyan("capcom limits --uninstall")
        print(f"  See it with {see} or on the Limits page; undo with {undo}.", file=out)
    return 0
