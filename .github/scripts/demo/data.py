"""capcom run on the synthetic logs, in a sandbox: the numbers behind every picture.

    python .github/scripts/demo/data.py <work-dir>

Fills <work-dir>/claude with synthetic.py's two weeks, then runs capcom with
CLAUDE_CONFIG_DIR, XDG_CACHE_HOME, XDG_STATE_HOME and OPENCLAW_STATE_DIR inside
<work-dir> (never the real ~/.claude, ~/.openclaw, ~/.cache/capcom or
~/.local/state/capcom) and HOME set to the made-up /home/dev, so project paths
read ~/code/<name>. It scans, has limits.py play the status-line recorder over the
same weeks (so the usage windows have samples), then asks a `capcom serve` on
127.0.0.1 for what the website shows (the same JSON API) and keeps it in
<work-dir>/data.json, with `capcom calibrate`'s table. The banner, the cards and
the demo all draw from that file or that server.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import limits
import synthetic

FAKE_HOME = "/home/dev"


def sandbox_env(work: Path) -> dict[str, str]:
    """The environment capcom runs in: every root it reads or writes inside `work`."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    env.update(
        CLAUDE_CONFIG_DIR=str(work / "claude"),
        XDG_CACHE_HOME=str(work / "cache"),
        XDG_STATE_HOME=str(work / "state"),
        OPENCLAW_STATE_DIR=str(work / "openclaw"),
        HOME=FAKE_HOME,
    )
    return env


def capcom(work: Path, *args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "capcom.cli", *args],
        env=sandbox_env(work),
        check=True,
        capture_output=True,
        text=True,
        cwd=work,
    )
    return out.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def serve(work: Path) -> Iterator[str]:
    """A `capcom serve` on the sandbox; yields its base URL. It scans (only the
    sandbox's synthetic logs), so the sidebar's Live now shows the running session
    rather than saying the server is paused."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "capcom.cli", "serve", "--port", str(port)],
        env=sandbox_env(work),
        cwd=work,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                get(base, "/api/status")
                break
            except OSError:
                time.sleep(0.1)
        yield base
    finally:
        proc.terminate()
        proc.wait(5)


def get(base: str, path: str) -> Any:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.load(r)


def calibration(text: str) -> list[dict[str, Any]]:
    """`capcom calibrate`'s rows: cost-state against capcom, per session."""
    rows = []
    for line in text.splitlines():
        # session, project, title, cost-state, capcom, spread, [what it compares,] scope
        m = re.match(r"(.+?)\s+\$([\d,.]+)\s+\$([\d,.]+)\s+([+-]?[\d.]+)%\s+(?:.*\s)?(\S+)$", line)
        if m:
            rows.append(
                {
                    "title": m[1].strip(),
                    "cost_state": float(m[2].replace(",", "")),
                    "capcom": float(m[3].replace(",", "")),
                    "spread": float(m[4]),
                    "scope": m[5],
                }
            )
    return rows


def build(work: Path, now: datetime | None = None) -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=True)
    for d in ("cache", "openclaw", "state"):
        (work / d).mkdir(exist_ok=True)
    now = now or datetime.now(UTC)
    if not (work / "claude" / "projects").exists():
        facts = synthetic.generate(work / "claude", now)
    else:
        facts = json.loads((work / "claude" / "synthetic.json").read_text())
    (work / "claude" / "synthetic.json").write_text(json.dumps(facts, indent=1))
    capcom(work, "scan")
    data: dict[str, Any] = {"facts": facts}
    data["calibrate"] = calibration(capcom(work, "calibrate"))
    with serve(work) as base:
        limits.write(work, lambda q: get(base, q), now)
        data["limits"] = get(base, "/api/limits?days=14")
        data["overview"] = get(base, "/api/overview")
        for group in ("project", "model", "category", "agent"):
            data[f"spend_{group}"] = get(base, f"/api/spend?days=14&group={group}")
        data["spikes"] = get(base, "/api/spikes?days=14")
        data["checks"] = get(base, "/api/checks?days=14")
        fan = facts["sessions"].get("fan-out")
        if fan:
            data["fan_out_session"] = get(base, f"/api/session/{fan}")
    (work / "data.json").write_text(json.dumps(data, indent=1))
    return data


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    data = build(Path(sys.argv[1]).resolve())
    spikes, checks = data["spikes"], data["checks"]
    print(
        f"spend ${data['spend_project']['totals']['usd']:.2f}, {spikes['count']} spikes,"
        f" {len(checks['findings'])} findings, {checks['total'] and checks['total']['text']}"
    )


if __name__ == "__main__":
    main()
