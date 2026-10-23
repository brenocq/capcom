"""Usage windows per account: two synthetic accounts with their own weekly reset
times, a switch in the middle of the week, a session still on the old account after
it, and samples recorded before accounts were. All data is synthetic."""

from __future__ import annotations

import io
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from capcom.config import Config
from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.limits import record as rec
from capcom.limits.accounts import (
    UNKNOWN,
    Known,
    Owners,
    analyze_accounts,
    attribute,
    load_known,
)
from capcom.limits.cli import run_limits
from capcom.limits.model import load_samples, sample_json, samples_path, segments
from capcom.limits.view import limits_json
from capcom.types import LimitSample, LimitWindow, Request, Usage
from tests.limits.fakes import T0, FakeIndex

TABLE = load_prices()
A, B = "aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b"
EMAIL_A, EMAIL_B = "first@example.test", "second@example.test"
RESET_A = T0 + timedelta(days=4)  # account A's week resets Monday 08:00
RESET_B = T0 + timedelta(days=6, hours=3)  # account B's on another day and hour
SWITCH = T0 + timedelta(days=1)
NOW = T0 + timedelta(days=2, hours=6)
_n = iter(range(1, 1_000_000))


def req(when: datetime, session: str, n: int = 1) -> list[Request]:
    """`n` identical requests of `session` at `when` (one dollar figure each)."""
    out = []
    for k in range(n):
        out.append(
            Request(
                message_id=f"m{next(_n)}",
                session_id=session,
                agent_id=None,
                project_dir="-home-user-alpha",
                timestamp=when + timedelta(seconds=k),
                model="claude-opus-5-5",
                usage=Usage(input=1000, output=2000),
            )
        )
    return out


ONE = price(req(T0, "x")[0], TABLE).total  # the price of one request


def weekly(
    when: datetime, pct: float, session: str, resets: datetime, account: str | None
) -> LimitSample:
    return LimitSample(when, LimitWindow.SEVEN_DAY, pct, resets, session, "statusline", account)


def scenario(labeled: bool = True) -> tuple[list[LimitSample], FakeIndex]:
    """Account A climbs 0 -> 30 % (4 requests per 10 points), then the user switches
    to B, which climbs 0 -> 30 % (2 requests per 10 points); session a1 stays on A
    after the switch and keeps reporting A's 30-31 %, interleaved with B's readings."""
    a = A if labeled else None
    b = B if labeled else None
    h = timedelta(hours=1)
    samples = [weekly(T0 + h * i, 10 * i, "a1", RESET_A, a) for i in range(4)]
    reqs = [r for i in range(3) for r in req(T0 + h * i + timedelta(minutes=30), "a1", 4)]
    for i in range(4):
        t = SWITCH + h * i
        samples.append(weekly(t, 10 * i, "b1", RESET_B, b))
        samples.append(weekly(t + timedelta(minutes=5), 30 + (i > 1), "a1", RESET_A, a))
        if i < 3:
            reqs += req(t + timedelta(minutes=30), "b1", 2)
    return samples, FakeIndex(reqs)


def known() -> Known:
    return Known(B, {A: EMAIL_A, B: EMAIL_B}, [], [(T0, A), (SWITCH, B)])


def test_old_single_timeline_mixed_the_accounts() -> None:
    """Without accounts, B's low readings after the switch are dropped as stale (its
    reset is earlier) or start a new window: the reason for keeping them apart."""
    samples, _ = scenario()
    mixed = segments(samples, LimitWindow.SEVEN_DAY)
    assert len(mixed) > 1 or len(mixed[0].samples) < len(samples) - 4


def test_each_account_has_its_own_rising_week() -> None:
    samples, index = scenario()
    accounts, _ = analyze_accounts(index, TABLE, samples, known(), NOW)
    assert [a.account.id for a in accounts] == [B, A]  # the current one first
    assert accounts[0].account.current and accounts[0].account.label == EMAIL_B
    by = {a.account.id: a.views[LimitWindow.SEVEN_DAY] for a in accounts}
    for acct, peak in ((A, 31), (B, 30)):
        segs = by[acct].segments
        assert len(segs) == 1  # no early reset from the other account's readings
        pts = [s.used_percentage for s in segs[0].samples]
        assert pts == sorted(pts) and pts[0] == 0 and pts[-1] == peak
    # each fit from its own sessions' spend: A costs 4 requests per 10 points, B 2
    fa, fb = by[A].fit, by[B].fit
    assert fa is not None and fb is not None
    assert fa.usd_per_percent == pytest.approx(0.4 * ONE, rel=0.05)
    assert fb.usd_per_percent == pytest.approx(0.2 * ONE, rel=0.05)
    # and each forecast from its own reading and reset
    fc = by[B].forecast
    assert fc is not None and fc.reading == 30 and fc.resets_at == RESET_B
    assert by[A].forecast is not None and by[A].forecast.resets_at == RESET_A


def test_combined_dollars_left() -> None:
    samples, index = scenario()
    _, comb = analyze_accounts(index, TABLE, samples, known(), NOW)
    shares = {s.account.id: s for s in comb.shares}
    for acct, per in ((A, 0.4 * ONE), (B, 0.2 * ONE)):
        s = shares[acct]
        assert s.percent is not None and s.usd_per_percent is not None
        assert s.usd_left == pytest.approx((100 - s.percent) * s.usd_per_percent)
        assert s.usd_per_percent == pytest.approx(per, rel=0.05)
    assert comb.usd_left == pytest.approx(sum(s.usd_left or 0 for s in comb.shares))
    # nothing spent in the last 24 hours: no pace, no run-out time
    assert comb.pace_usd_per_hour == 0 and comb.runs_out_at is None
    busy = FakeIndex(index.reqs + req(NOW - timedelta(hours=2), "b1", 48))
    _, comb2 = analyze_accounts(busy, TABLE, samples, known(), NOW)
    assert comb2.pace_usd_per_hour == pytest.approx(48 * ONE / 24, rel=1e-6)
    assert comb2.runs_out_at is not None and comb2.usd_left is not None
    hours = (comb2.runs_out_at - NOW) / timedelta(hours=1)
    assert hours == pytest.approx(comb2.usd_left / comb2.pace_usd_per_hour, rel=1e-6)


def test_old_samples_go_to_the_account_whose_cycle_matches() -> None:
    labeled, _ = scenario()
    # a week earlier, before accounts were recorded: the same cycles, a week back
    old = [
        replace(s, account=None, time=s.time - timedelta(days=7), session_id="old" + s.session_id)
        for s in labeled
        if s.resets_at is not None
    ]
    old = [replace(s, resets_at=(s.resets_at or NOW) - timedelta(days=7)) for s in old]
    five = LimitSample(old[0].time, LimitWindow.FIVE_HOUR, 3.0, None, "olda1", "statusline")
    stray = weekly(T0 - timedelta(days=3), 5, "z", T0 + timedelta(hours=13), None)
    tagged = attribute([*old, five, stray, *labeled], known())
    got = {(s.session_id, s.window): s.account for s in tagged}
    assert got[("olda1", LimitWindow.SEVEN_DAY)] == A
    assert got[("oldb1", LimitWindow.SEVEN_DAY)] == B
    assert got[("olda1", LimitWindow.FIVE_HOUR)] == A  # by its session
    assert got[("z", LimitWindow.SEVEN_DAY)] == UNKNOWN  # on neither account's cycle


def test_unlabeled_history_of_one_account_stays_one_account() -> None:
    samples, _ = scenario(labeled=False)
    tagged = attribute(samples, Known())
    assert {s.account for s in tagged} == {UNKNOWN}
    only_b = [s for s in scenario()[0] if s.session_id == "b1"]
    five = LimitSample(SWITCH, LimitWindow.FIVE_HOUR, 2.0, None, "elsewhere", "statusline")
    tagged = attribute([*only_b, five], Known())
    assert {s.account for s in tagged} == {B}


def test_sessions_without_samples_are_inferred_by_time() -> None:
    samples, index = scenario()
    o = Owners(attribute(samples, known()), known())
    late = req(SWITCH + timedelta(hours=5), "quiet")[0]
    early = req(T0 + timedelta(hours=5), "quiet")[0]
    assert o(late) == (B, True) and o(early) == (A, True)
    assert o(req(SWITCH + timedelta(hours=5), "a1")[0]) == (A, False)
    index = FakeIndex(index.reqs + req(SWITCH + timedelta(hours=2, minutes=40), "quiet", 3))
    accounts, _ = analyze_accounts(index, TABLE, samples, known(), NOW)
    b = next(a for a in accounts if a.account.id == B).views[LimitWindow.SEVEN_DAY]
    seg = b.shown[-1]
    assert seg.inferred_usd == pytest.approx(3 * ONE) and seg.usd > seg.inferred_usd


# --------------------------------------------------------------------------- recorder

SECRET = "sk-test-NOT-A-REAL-SECRET-7f3a9c"


def write_config(path: Path, uuid: str, email: str) -> None:
    doc = {
        "numStartups": 12,
        "mcpServers": {"x": {"command": "x", "env": {"API_KEY": SECRET}}},
        "oauthAccount": {"accountUuid": uuid, "emailAddress": email, "organizationName": SECRET},
        "projects": {"/home/user/alpha": {"history": [{"display": SECRET}]}},
    }
    path.write_text(json.dumps(doc))


def status(session: str, week: float, week_reset: float, five: float = 1.0) -> bytes:
    return json.dumps(
        {
            "session_id": session,
            "rate_limits": {
                "five_hour": {"used_percentage": five, "resets_at": week_reset - 86400},
                "seven_day": {"used_percentage": week, "resets_at": week_reset},
            },
        }
    ).encode()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "XDG_STATE_HOME": str(tmp_path / "state")}


def feed(env: dict[str, str], data: bytes) -> bytes:
    out = io.BytesIO()
    assert rec.run([], stdin=io.BytesIO(data), stdout=out, env=env) == 0
    return out.getvalue()


def bump(path: Path) -> None:
    """A new modification time, as Claude Code's own rewrites give."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_recorder_labels_accounts_across_a_switch(env: dict[str, str]) -> None:
    cfg = Path(env["HOME"]) / ".claude.json"
    state = Path(env["XDG_STATE_HOME"]) / "capcom"
    ra = time.time() + 3 * 86400
    rb = time.time() + 5 * 86400 + 7000
    write_config(cfg, A, EMAIL_A)
    feed(env, status("a1", 90, ra))
    feed(env, status("a1", 95, ra))
    write_config(cfg, B, EMAIL_B)  # /login to B
    bump(cfg)
    feed(env, status("b1", 2, rb))  # a new session: the config's account
    feed(env, status("a1", 96, ra))  # the old session, still on A
    feed(env, status("late", 97, ra))  # first seen after the switch, but on A's cycle
    feed(env, status("b1", 3, rb))
    feed(env, status("a1", 4, rb))  # a1 now runs on B (its weekly reset says so)
    got = [
        (s.session_id, s.used_percentage, s.account)
        for s in load_samples(state / rec.SAMPLES_FILE)
        if s.window == LimitWindow.SEVEN_DAY
    ]
    assert got == [
        ("a1", 90, A),
        ("a1", 95, A),
        ("b1", 2, B),
        ("a1", 96, A),
        ("late", 97, A),
        ("b1", 3, B),
        ("a1", 4, B),
    ]
    k = load_known(state)
    assert k.current == B and k.emails == {A: EMAIL_A, B: EMAIL_B}
    assert [a for _, a in k.switches or []] == [A, B]
    assert {(s, a) for _, s, a in k.sessions or []} >= {("a1", A), ("b1", B), ("late", A)}


def test_recorder_keeps_nothing_else_from_the_config(env: dict[str, str]) -> None:
    cfg = Path(env["HOME"]) / ".claude.json"
    write_config(cfg, A, EMAIL_A)
    data = status("s", 10, time.time() + 86400 * 3)
    assert feed(env, data) == data  # the status line is unchanged
    feed(env, status("s", 11, time.time() + 86400 * 3))
    files = list((Path(env["XDG_STATE_HOME"]) / "capcom").iterdir())
    assert files
    for f in files:
        text = f.read_text()
        assert SECRET not in text and "organizationName" not in text and "mcp" not in text
    acc = json.loads((Path(env["XDG_STATE_HOME"]) / "capcom" / rec.ACCOUNTS_FILE).read_text())
    assert acc == {"current": A, "emails": {A: EMAIL_A}}


def test_recorder_parses_the_config_only_when_it_changes(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Path(env["HOME"]) / ".claude.json"
    write_config(cfg, A, EMAIL_A)
    calls: list[str] = []
    real = rec.read_account
    monkeypatch.setattr(rec, "read_account", lambda p: calls.append(p) or real(p))
    reset = time.time() + 86400 * 3
    for pct in (10, 11, 12):
        feed(env, status("s", pct, reset))
    assert len(calls) == 1
    bump(cfg)
    feed(env, status("s", 13, reset))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'{"oauthAccount": "x"}', b'{"oauthAccount": {"accountUuid": 5}}'],
)
def test_recorder_without_a_usable_account(env: dict[str, str], content: bytes) -> None:
    (Path(env["HOME"]) / ".claude.json").write_bytes(content)
    data = status("s", 10, time.time() + 86400 * 3)
    assert feed(env, data) == data
    samples = load_samples(Path(env["XDG_STATE_HOME"]) / "capcom" / rec.SAMPLES_FILE)
    assert samples and all(s.account is None for s in samples)


def test_recorder_without_a_config(env: dict[str, str]) -> None:
    data = status("s", 10, time.time() + 86400 * 3)
    assert feed(env, data) == data
    samples = load_samples(Path(env["XDG_STATE_HOME"]) / "capcom" / rec.SAMPLES_FILE)
    assert [s.account for s in samples] == [None, None]


# --------------------------------------------------------------------------- outputs


def _state(tmp_path: Path) -> Path:
    samples, _ = scenario()
    state = tmp_path / "state"
    state.mkdir()
    samples_path(state).write_text("".join(sample_json(s) + "\n" for s in samples))
    (state / rec.ACCOUNTS_FILE).write_text(
        json.dumps({"current": B, "emails": {A: EMAIL_A, B: EMAIL_B}})
    )
    return state


def test_cli_prints_each_account_and_the_combined_line(tmp_path: Path) -> None:
    state = _state(tmp_path)
    cfg = Config(
        claude_root=tmp_path / "claude",
        openclaw_root=tmp_path / "openclaw",
        cache_dir=tmp_path / "cache",
        state_dir=state,
    )
    _, index = scenario()
    out = io.StringIO()
    assert run_limits(cfg, out=out, now=NOW, index=index) == 0  # type: ignore[arg-type]
    text = out.getvalue()
    assert text.index(f"== {EMAIL_B} (current)") < text.index(f"== {EMAIL_A}")
    assert "All accounts: about $" in text and SECRET not in text
    one = io.StringIO()
    assert run_limits(cfg, out=one, now=NOW, index=index, account=EMAIL_A.upper()) == 0  # type: ignore[arg-type]
    assert EMAIL_B not in one.getvalue() and "Weekly limit: 31%" in one.getvalue()
    bad = io.StringIO()
    assert run_limits(cfg, out=bad, now=NOW, index=index, account="nobody@x.test") == 1  # type: ignore[arg-type]


def test_json_has_accounts_and_combined(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _, index = scenario()
    d = limits_json(index, TABLE, state, NOW, UTC, days=14)  # type: ignore[arg-type]
    assert [(a["id"], a["email"], a["current"]) for a in d["accounts"]] == [
        (B, EMAIL_B, True),
        (A, EMAIL_A, False),
    ]
    assert d["windows"] == d["accounts"][0]["windows"]
    assert d["accounts"][1]["windows"]["seven_day"]["reading"]["percent"] == 31
    c = d["combined"]
    assert c is not None and len(c["accounts"]) == 2
    assert c["usd_left"] == pytest.approx(sum(a["usd_left"] for a in c["accounts"]), abs=0.02)
    seg = d["accounts"][0]["windows"]["seven_day"]["segments"][0]
    assert "inferred_usd" in seg


def test_json_with_one_account_has_no_combined(tmp_path: Path) -> None:
    samples, index = scenario(labeled=False)
    state = tmp_path / "s"
    state.mkdir()
    samples_path(state).write_text("".join(sample_json(s) + "\n" for s in samples))
    d = limits_json(index, TABLE, state, NOW, UTC)  # type: ignore[arg-type]
    assert d["combined"] is None and [a["id"] for a in d["accounts"]] == [UNKNOWN]
    assert d["accounts"][0]["email"] is None
