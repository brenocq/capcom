"""`capcom record-limits`: record the subscription's usage windows from Claude Code's
status line.

Claude Code runs the `statusLine` command on every refresh and writes a JSON object to
its stdin; for Pro and Max plans it holds `rate_limits.five_hour` and
`rate_limits.seven_day`, each with `used_percentage` and `resets_at` (epoch seconds).
This command sits in front of the user's own status line command
(`capcom record-limits | <their command>`): it copies stdin to stdout byte for byte,
then appends one JSONL `LimitSample` per window to `<state>/capcom/limits.jsonl` when
the window's percentage rose above its running maximum for the current reset, or the
reset moved on (a new window). Each open session reports the limits as of its own last
API reply, so at the same moment sessions disagree; within one window usage only rises,
so a lower reading is stale and skipped. With `--line`
(no status line of one's own) it prints a short line of its own instead of the copy.

It must never break the status line: every error is swallowed, the copy still goes
out, and the exit status is 0. It is on the status line's path, so it imports only
the standard library modules it needs (no index, no prices), and holds a lock only
while appending. Concurrent sessions are serialized by `flock` on a lock file; a
writer that cannot get the lock within a few milliseconds skips this refresh (the
next one records the value).

Each sample is labeled with the account it belongs to (`account`, the account's
`oauthAccount.accountUuid`), so a user who switches accounts gets one timeline per
account. The account comes from Claude Code's own config, `~/.claude.json`, of which
capcom reads exactly two keys, `oauthAccount.accountUuid` and
`oauthAccount.emailAddress`, and drops the rest at once (the file can hold MCP
configs with secrets); it is parsed again only when its modification time or size
changes. A session started before a `/login` switch may still run on the old
account while the config already names the new one, so a session keeps the account
it first reported with, unless its weekly reset time says otherwise: each account's
weekly window resets at its own time of the week, so a reading whose weekly reset
falls on another account's cycle belongs to that account. The emails are kept in
`limits.accounts.json` (with the current account), and the account each session
was first seen on, and each switch, in `limits.sessions.jsonl`. Any error there
leaves the sample without an account; the status line is never affected.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, BinaryIO

WINDOWS = ("five_hour", "seven_day")
SAMPLES_FILE = "limits.jsonl"
STATE_FILE = "limits.state.json"
LOCK_FILE = "limits.lock"
ACCOUNTS_FILE = "limits.accounts.json"  # {"current": uuid, "emails": {uuid: email}}
SESSIONS_FILE = "limits.sessions.jsonl"  # session first seen on an account; switches
CONFIG_FILE = ".claude.json"
MAX_CONFIG = 64 << 20  # a larger config is not parsed
MAX_SESSIONS = 256  # sessions whose account the state remembers
WEEK_S = 7 * 86400
MAX_INPUT = 1 << 20  # larger stdin is passed through but not parsed
RESET_SLACK_S = 600  # reset times closer than this are the same reset (they drift)
READ_DEADLINE_S = 1.0  # stdin still open after this: pass through what arrived
LOCK_TRIES = 25  # times 2 ms: give up on the lock after about 50 ms

Reading = tuple[str, float, float | None]  # window, used_percentage, resets_at (epoch s)


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def readings(payload: Any) -> tuple[list[Reading], str | None]:
    """The windows a status line payload reports, and its session id. Every field is
    optional; a window without a numeric `used_percentage` is left out."""
    if not isinstance(payload, dict):
        return [], None
    sid = payload.get("session_id")
    session = sid if isinstance(sid, str) and 0 < len(sid) <= 128 else None
    limits = payload.get("rate_limits")
    out: list[Reading] = []
    if isinstance(limits, dict):
        for w in WINDOWS:
            win = limits.get(w)
            if not isinstance(win, dict):
                continue
            used = _number(win.get("used_percentage"))
            if used is None or not 0 <= used <= 1000:
                continue
            resets = _number(win.get("resets_at"))
            if resets is not None and not 0 < resets < 1e11:  # not epoch seconds
                resets = None
            out.append((w, used, resets))
    return out, session


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _stale(prev: Any, resets: float | None, now: float) -> bool:
    """Whether a reading reports a window already over: its reset is not after `now`,
    or is before the last sample's reset (an idle session showing the old window)."""
    if resets is None:
        return False
    if resets <= now:
        return True
    p_resets = _number(prev[1]) if isinstance(prev, list) and len(prev) == 2 else None
    return p_resets is not None and p_resets - resets >= RESET_SLACK_S


def _instance(prev: Any, resets: float | None) -> tuple[float, float | None] | None:
    """The running maximum and reset of the window instance `resets` belongs to, or None
    when the reading starts a new instance (no state yet, or a later reset)."""
    if not isinstance(prev, list) or len(prev) != 2:
        return None
    p_max, p_resets = _number(prev[0]), _number(prev[1])
    if p_max is None:
        return None
    if resets is None:
        return p_max, p_resets  # no reset to tell windows apart: assume the same one
    if p_resets is None or abs(resets - p_resets) >= RESET_SLACK_S:
        return None
    return p_max, p_resets


def config_path(env: Mapping[str, str]) -> str:
    """Claude Code's config file: `$CLAUDE_CONFIG_DIR/.claude.json`, or
    `~/.claude.json`."""
    home = env.get("HOME") or os.path.expanduser("~")
    folder = env.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(folder) if folder else home, CONFIG_FILE)


def _uuid(v: Any) -> str | None:
    if not isinstance(v, str) or not 8 <= len(v) <= 64:
        return None
    return v.lower() if all(c in "0123456789abcdefABCDEF-" for c in v) else None


def _email(v: Any) -> str | None:
    if not isinstance(v, str) or not 3 <= len(v) <= 254 or "@" not in v:
        return None
    return v if v.isprintable() and not any(c.isspace() for c in v) else None


def read_account(path: str) -> tuple[str, str | None] | None:
    """The account Claude Code is logged in with: `oauthAccount.accountUuid` and
    `oauthAccount.emailAddress` of the config at `path`, and nothing else from it;
    None when there is none (or the file cannot be read). Raises nothing."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG:
            return None
        doc = json.loads(raw)
        del raw
        acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
        uuid = _uuid(acct.get("accountUuid")) if isinstance(acct, dict) else None
        email = _email(acct.get("emailAddress")) if isinstance(acct, dict) else None
        del doc, acct  # the rest of the file is never kept
    except BaseException:  # never fail the status line
        return None
    return None if uuid is None else (uuid, email)


def same_cycle(a: float, b: float, slack: float = RESET_SLACK_S) -> bool:
    """Whether two weekly reset times (epoch seconds) fall at the same time of the
    week, whole weeks apart allowed: one account's weekly cycle."""
    d = (a - b) % WEEK_S
    return d < slack or WEEK_S - d < slack


def _anchor(state: Mapping[str, Any], account: str) -> float | None:
    acct = state.get("acct")
    st = acct.get(account) if isinstance(acct, dict) else None
    prev = st.get("seven_day") if isinstance(st, dict) else None
    return _number(prev[1]) if isinstance(prev, list) and len(prev) == 2 else None


def decide(
    state: Mapping[str, Any], session: str | None, current: str | None, weekly: float | None
) -> str | None:
    """The account of a reading: the one its session first reported with (or the
    config's current one for a new session), unless its weekly reset `weekly` falls
    on another known account's cycle, or on none known while the current account has
    no cycle yet (a session that followed a switch)."""
    sessions = state.get("sessions")
    cand = sessions.get(session) if isinstance(sessions, dict) and session else None
    if not isinstance(cand, str):
        cand = current
    if weekly is None:
        return cand
    acct = state.get("acct")
    known = [a for a in acct if isinstance(a, str)] if isinstance(acct, dict) else []
    anchors = {a: p for a in known if (p := _anchor(state, a)) is not None}

    def on(a: str | None) -> bool:
        p = anchors.get(a) if a is not None else None
        return p is not None and same_cycle(p, weekly)

    if on(cand):
        return cand
    others = [a for a in anchors if a != cand and on(a)]
    if len(others) == 1:
        return others[0]
    if cand is None or cand not in anchors:
        return cand
    if current is not None and current != cand and current not in anchors:
        return current
    return cand


def _account(state: dict[str, Any], directory: str, config: str, now: float) -> str | None:
    """The config's current account, parsed again only when the file changed (its
    modification time and size are kept in the state with the account); the emails
    and switches are written beside the samples. Updates `state`."""
    st = os.stat(config)
    stamp = [st.st_mtime_ns, st.st_size]
    cached = state.get("config")
    prev = cached[2] if isinstance(cached, list) and len(cached) == 3 else None
    prev = prev if isinstance(prev, str) else None
    if isinstance(cached, list) and cached[:2] == stamp:
        return prev
    found = read_account(config)
    if found is None:
        return prev  # being rewritten, or logged out: keep the last one seen
    uuid, email = found
    state["config"] = [*stamp, uuid]
    if uuid != prev:
        _log(directory, {"time": _iso(now), "account": uuid})
    path = os.path.join(directory, ACCOUNTS_FILE)
    known: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            known = loaded
    except (OSError, ValueError):
        pass
    emails = known.get("emails") if isinstance(known.get("emails"), dict) else {}
    assert isinstance(emails, dict)
    if known.get("current") != uuid or (email is not None and emails.get(uuid) != email):
        if email is not None:
            emails[uuid] = email
        _write_json(path, {"current": uuid, "emails": emails})
    return uuid


def _write_json(path: str, value: Any) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(tmp, path)


def _log(directory: str, entry: Mapping[str, Any]) -> None:
    line = json.dumps(entry, separators=(",", ":")).encode() + b"\n"
    fd = os.open(
        os.path.join(directory, SESSIONS_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _remember(
    state: dict[str, Any], directory: str, session: str, account: str, now: float
) -> bool:
    """Keep `session`'s account in the state (the most recent `MAX_SESSIONS`) and log
    it when it is new or changed; True when the state changed."""
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        sessions = state["sessions"] = {}
    if sessions.get(session) == account:
        return False
    sessions.pop(session, None)
    sessions[session] = account
    while len(sessions) > MAX_SESSIONS:
        sessions.pop(next(iter(sessions)))
    _log(directory, {"time": _iso(now), "session_id": session, "account": account})
    return True


def _under(path: str, roots: Sequence[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _folder(env: Mapping[str, str], var: str, default: str) -> str | None:
    """`$<var>/capcom` (or `~/<default>/capcom`); None when that would be inside
    Claude Code's or OpenClaw's folders, which capcom never writes."""
    home = env.get("HOME") or os.path.expanduser("~")
    base = env.get(var) or os.path.join(home, default)
    target = os.path.join(os.path.expanduser(base), "capcom")
    claude = env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    openclaw = env.get("OPENCLAW_STATE_DIR") or os.path.join(home, ".openclaw")
    if _under(target, [os.path.expanduser(claude), os.path.expanduser(openclaw)]):
        return None
    return target


def state_dir(env: Mapping[str, str]) -> str | None:
    """`$XDG_STATE_HOME/capcom` (or `~/.local/state/capcom`): the samples are history
    no scan rebuilds, so they stay out of the cache, which may be cleared."""
    return _folder(env, "XDG_STATE_HOME", os.path.join(".local", "state"))


def cache_dir(env: Mapping[str, str]) -> str | None:
    """`$XDG_CACHE_HOME/capcom` (or `~/.cache/capcom`), where the samples lived before
    the state folder; `adopt` moves them out."""
    return _folder(env, "XDG_CACHE_HOME", ".cache")


def adopt(old: str | None, new: str) -> None:
    """Move the samples and their state from `old` into `new`, once: nothing happens
    when `new` already holds samples or `old` holds none. Raises on I/O errors."""
    if old is None or os.path.exists(os.path.join(new, SAMPLES_FILE)):
        return
    if not os.path.exists(os.path.join(old, SAMPLES_FILE)):
        return
    import shutil

    os.makedirs(new, mode=0o700, exist_ok=True)
    lock = os.open(os.path.join(new, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock(lock):
            return
        for name in (SAMPLES_FILE, STATE_FILE):
            src, dst = os.path.join(old, name), os.path.join(new, name)
            if os.path.exists(dst) or not os.path.exists(src):
                continue
            # a copy, then the old one removed: the two folders may be on different disks
            tmp = f"{dst}.{os.getpid()}.tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            os.unlink(src)
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(old, LOCK_FILE))
    finally:
        os.close(lock)  # releases the flock


def _lock(fd: int) -> bool:
    import fcntl

    for _ in range(LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(0.002)
    return False


def record(data: bytes, directory: str, now: float | None = None, config: str | None = None) -> int:
    """Append a sample per window of the status line payload `data` whose value
    changed since its last sample of the same account; return how many were
    appended. `config` is Claude Code's config file, which names the account (None:
    no account). Raises on I/O errors (the caller swallows them)."""
    if not data or len(data) > MAX_INPUT:
        return 0
    try:
        payload = json.loads(data)
    except ValueError:
        return 0
    found, session = readings(payload)
    if not found:
        return 0
    now = time.time() if now is None else now
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock = os.open(os.path.join(directory, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock(lock):
            return 0
        state_path = os.path.join(directory, STATE_FILE)
        state: dict[str, Any] = {}
        try:
            with open(state_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            pass
        dirty = False
        account: str | None = None
        try:
            before = json.dumps([state.get("config"), state.get("sessions")])
            current = _account(state, directory, config, now) if config else None
            weekly = next((r for w, _, r in found if w == "seven_day"), None)
            account = decide(state, session, current, weekly)
            if account is not None and session is not None:
                _remember(state, directory, session, account, now)
            dirty = before != json.dumps([state.get("config"), state.get("sessions")])
        except Exception:  # no account rather than no sample
            account = None
        # each account keeps its own running maxima: after a switch the new account's
        # low readings are not stale views of the old one's window
        slot: dict[str, Any] = state
        if account is not None:
            accts = state.get("acct")
            if not isinstance(accts, dict):
                accts = state["acct"] = {}
            got = accts.get(account)
            slot = got if isinstance(got, dict) else {}
            accts[account] = slot
        lines: list[bytes] = []
        for window, used, resets in found:
            prev = slot.get(window)
            if _stale(prev, resets, now):
                continue
            inst = _instance(prev, resets)
            kept = resets  # the reset the state keeps for this instance
            if inst is not None:
                # usage only rises within a window: a reading at or below the running
                # maximum is an older session's stale view (or says nothing new)
                if used <= inst[0]:
                    continue
                kept = inst[1] if inst[1] is not None else resets
            sample = {
                "time": _iso(now),
                "window": window,
                "used_percentage": used,
                "resets_at": None if resets is None else _iso(resets),
                "session_id": session,
                "source": "statusline",
            }
            if account is not None:
                sample["account"] = account
            lines.append(json.dumps(sample, separators=(",", ":")).encode() + b"\n")
            slot[window] = [used, kept]  # the instance's running maximum
        if not lines:
            if dirty:
                _write_json(state_path, state)
            return 0
        out = os.open(
            os.path.join(directory, SAMPLES_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        try:
            os.write(out, b"".join(lines))  # one write: whole lines, even unlocked
        finally:
            os.close(out)
        _write_json(state_path, state)
        return len(lines)
    finally:
        os.close(lock)  # releases the flock


def status_text(data: bytes) -> str:
    """The short line `--line` prints: the model and both windows' percentages."""
    try:
        payload = json.loads(data)
    except ValueError:
        return "capcom"
    found, _ = readings(payload)
    parts: list[str] = []
    model = payload.get("model") if isinstance(payload, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    if isinstance(name, str) and name:
        parts.append(name[:40])
    labels = {"five_hour": "5h", "seven_day": "7d"}
    for window, used, resets in found:
        text = f"{labels[window]} {used:.0f}%"
        if resets is not None and window == "five_hour":
            with contextlib.suppress(OverflowError, ValueError, OSError):
                text += f" (resets {datetime.fromtimestamp(resets).strftime('%H:%M')})"
        parts.append(text)
    return " · ".join(parts) or "capcom"


def read_input(stream: BinaryIO, deadline_s: float = READ_DEADLINE_S) -> bytes:
    """What `stream` holds until it closes or `deadline_s` passes, whichever comes
    first (a writer that never closes its end cannot hang the status line)."""
    import select

    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.read()  # not a real file (tests): read it whole
    chunks: list[bytes] = []
    end = time.monotonic() + deadline_s
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            break
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def run(
    argv: Sequence[str],
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """The command: always exits 0, and stdout always gets the copy of stdin (or the
    short line with `--line`) whatever happens to the recording."""
    real_stdout = stdout is None
    data = b""
    with contextlib.suppress(BaseException):
        data = read_input(stdin or sys.stdin.buffer)
    try:
        with contextlib.suppress(BaseException):
            out = stdout or sys.stdout.buffer
            if "--line" in argv:
                out.write(status_text(data).encode("utf-8", "replace") + b"\n")
            else:
                out.write(data)
            out.flush()
    finally:
        if real_stdout:
            # let the next command in the pipe see the end of its input now, and give
            # the exit-time flush nowhere to fail (a reader that closed its end would
            # make it print BrokenPipeError and exit 120)
            with contextlib.suppress(BaseException):
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                os.close(devnull)
    environ = os.environ if env is None else env
    directory: str | None = None
    with contextlib.suppress(BaseException):
        directory = state_dir(environ)
        if directory is not None:
            adopt(cache_dir(environ), directory)
    with contextlib.suppress(BaseException):
        if directory is not None:
            record(data, directory, config=config_path(environ))
    return 0
