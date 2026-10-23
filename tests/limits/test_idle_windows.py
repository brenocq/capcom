"""The windows an idle 5-hour limit seems to start, and the 5-hour readings a session
places only later. All data is synthetic."""

from __future__ import annotations

from datetime import timedelta

from capcom.limits.accounts import Known, attribute
from capcom.limits.model import segments
from capcom.types import LimitSample, LimitWindow
from tests.limits.fakes import T0, minutes, sample

H5, D7 = LimitWindow.FIVE_HOUR, LimitWindow.SEVEN_DAY
A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"


# ------------------------------------------------------------------ idle windows


def test_idle_refreshes_are_not_windows_of_their_own() -> None:
    # at 0 % Claude Code reports the window as starting now, its reset moving on ten
    # minutes at a time; the window really starts with the first use (08:20)
    t = T0  # 08:00
    ss = [
        sample(t + timedelta(seconds=7), 0.0, resets=t + timedelta(hours=5)),
        sample(t + minutes(10), 0.0, resets=t + timedelta(hours=5, minutes=10)),
        sample(t + minutes(21), 0.0, resets=t + timedelta(hours=5, minutes=20)),
        sample(t + minutes(30), 4.0, resets=t + timedelta(hours=5, minutes=20)),
        sample(t + minutes(60), 30.0, resets=t + timedelta(hours=5, minutes=20)),
    ]
    segs = segments(ss, H5)
    assert len(segs) == 1
    assert segs[0].start == t + minutes(20) and segs[0].start_known
    assert segs[0].last.used_percentage == 30.0


def test_the_latest_idle_window_stays() -> None:
    # where the account stands now: nothing used since the last reset
    t = T0
    ss = [
        sample(t, 50.0, resets=t + minutes(30)),
        sample(t + minutes(40), 0.0, resets=t + timedelta(hours=5, minutes=40)),
    ]
    segs = segments(ss, H5)
    assert [s.last.used_percentage for s in segs] == [50.0, 0.0]


# ------------------------------------------------------------------ attribution


def test_a_five_hour_reading_goes_with_the_account_sharing_its_reset() -> None:
    # session "late" is seen on account A only from the next day, but its first
    # reading continues B's window (same reset): it is B's, not a window of A's
    t = T0
    reset = t + timedelta(hours=3)
    week_a, week_b = t + timedelta(days=5), t + timedelta(days=2, hours=7)
    ss = [
        LimitSample(t, D7, 10.0, week_b, "b1", account=B),
        LimitSample(t, H5, 16.0, reset, "b1"),
        LimitSample(t + minutes(5), H5, 17.0, reset, "b1"),
        LimitSample(t + minutes(10), H5, 18.0, reset, "late"),
        LimitSample(t + minutes(15), H5, 19.0, reset, "b1"),
        LimitSample(t + timedelta(days=1), D7, 5.0, week_a, "late", account=A),
        LimitSample(t + timedelta(days=1), H5, 2.0, t + timedelta(days=1, hours=4), "late"),
    ]
    known = Known(None, {}, [], [])
    got = attribute(ss, known)
    stray = got[3]
    assert stray.session_id == "late" and stray.account == B
    assert got[6].account == A  # the same session's own reading the next day
    a5 = segments([s for s in got if s.account == A], H5)
    assert len(a5) == 1 and a5[0].last.used_percentage == 2.0
