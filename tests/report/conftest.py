"""A synthetic index (written through the store), fake analysis modules and a running
server for the website tests. All data here is made up."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import capcom.timeline
from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.report.analysis import Analysis
from capcom.report.server import Site, make_server
from capcom.store.index import SqliteIndex, open_index
from capcom.types import (
    AgentInfo,
    Batch,
    CostState,
    Event,
    EventKind,
    Explanation,
    FileState,
    Finding,
    Index,
    LogFile,
    LogKind,
    ModelUsage,
    PriceTable,
    Record,
    Request,
    SessionInfo,
    Spike,
    ToolCall,
    Usage,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
TABLE = load_prices()
_n = iter(range(1, 1_000_000))


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def req(
    session: str,
    when: datetime,
    project: str,
    agent: str | None = None,
    model: str = "claude-opus-5-5",
    tools: Sequence[str] = (),
    estimated: bool = False,
    **usage: int,
) -> Request:
    i = next(_n)
    return Request(
        message_id=f"msg_{i}",
        session_id=session,
        agent_id=agent,
        project_dir=project,
        timestamp=when,
        model=model,
        usage=Usage(**usage),
        output_estimated=estimated,
        tool_calls=tuple(ToolCall(f"tu_{i}_{k}", name) for k, name in enumerate(tools)),
    )


ALPHA = "-home-user-alpha"
BETA = "-home-user-beta"
TMP = "-tmp-scratch-1"

# s1: today, alpha, a main thread with a pause, a scout subagent, a compaction, live.
S1_MAIN = [
    req("s1", ago(hours=6), ALPHA, input=100, cache_write_1h=20_000, output=500, tools=["Read"]),
    req("s1", ago(hours=6) + timedelta(minutes=2), ALPHA, cache_read=20_000, output=300),
    req("s1", ago(hours=5, minutes=30), ALPHA, cache_read=20_000, cache_write_5m=5_000, output=50),
    req("s1", ago(minutes=3), ALPHA, cache_read=60_000, output=800, tools=["Bash", "Edit"]),
]
S1_AGENT = [
    req(
        "s1", ago(hours=6) + timedelta(minutes=3), ALPHA, "a1", tools=["Grep"], cache_write_5m=8_000
    ),
    req("s1", ago(hours=6) + timedelta(minutes=4), ALPHA, "a1", cache_read=8_000, output=200),
]
# s2: yesterday, a temporary folder.
S2 = [req("s2", ago(days=1), TMP, input=3_000, output=1_000)]
# s3: three days ago, beta, an estimated output.
S3 = [req("s3", ago(days=3), BETA, cache_read=100_000, output=40, estimated=True)]
# s4: ten days ago, beta (inside 14 days, outside 7).
S4 = [req("s4", ago(days=10), BETA, input=500, output=2_000)]
# A placeholder reply, never counted.
SYNTH = [req("s1", ago(hours=4), ALPHA, model="<synthetic>", output=999_999)]

ALL = S1_MAIN + S1_AGENT + S2 + S3 + S4 + SYNTH
COUNTED = S1_MAIN + S1_AGENT + S2 + S3 + S4


def usd(reqs: Sequence[Request]) -> float:
    return sum(price(r, TABLE).total for r in reqs)


def _log(project: str, session: str, agent: str | None = None) -> LogFile:
    if agent:
        path = Path(f"/logs/{project}/{session}/subagents/agent-{agent}.jsonl")
        return LogFile(path, LogKind.SUBAGENT, project, session, agent)
    return LogFile(Path(f"/logs/{project}/{session}.jsonl"), LogKind.SESSION, project, session)


def _put(ix: SqliteIndex, log: LogFile, records: Sequence[Record], pos: int = 100) -> None:
    ix.apply(log, Batch(list(records), pos), FileState(log.path, 1, 100, 1, pos))


def model_usage(reqs: Sequence[Request]) -> dict[str, ModelUsage]:
    """cost-state's modelUsage for `reqs`: their tokens per model, as logged."""
    out: dict[str, ModelUsage] = {}
    for r in reqs:
        m = out.get(r.model, ModelUsage())
        u = r.usage
        out[r.model] = ModelUsage(
            input=m.input + u.input,
            output=m.output + u.output,
            cache_read=m.cache_read + u.cache_read,
            cache_write=m.cache_write + u.cache_write,
            cost_usd=m.cost_usd + usd([r]),
        )
    return out


def build_index(path: Path) -> Path:
    ix = open_index(path)
    s1 = SessionInfo(
        "s1", ALPHA, cwd="/home/user/alpha", title="Alpha draft", git_branch="main",
        first_seen=S1_MAIN[0].timestamp, last_seen=S1_MAIN[-1].timestamp,
        custom_title="Alpha work", color="purple",  # renamed and colored in Claude Code
    )  # fmt: skip
    compaction = Event(
        EventKind.COMPACTION, "s1", None, ago(hours=5, minutes=40), "c1",
        {"trigger": "auto", "pre_tokens": 180_000, "post_tokens": 20_000},
    )  # fmt: skip
    cost_state = CostState(
        "s1", 99, total_cost_usd=round(usd(S1_MAIN + S1_AGENT), 6),
        models=model_usage(S1_MAIN + S1_AGENT),
    )  # fmt: skip
    prompts = [  # two messages the user sent (the counts of the Sessions table)
        Event(EventKind.PROMPT, "s1", None, ago(hours=5, minutes=50), "p1:prompt", {}),
        Event(EventKind.PROMPT, "s1", None, ago(hours=5, minutes=45), "p2:prompt", {}),
    ]
    _put(ix, _log(ALPHA, "s1"), [s1, *S1_MAIN, *SYNTH, compaction, cost_state, *prompts])
    scout = AgentInfo("a1", "s1", agent_type="scout", description="Map the parser", finished=True)
    _put(ix, _log(ALPHA, "s1", "a1"), [scout, *S1_AGENT])
    s2 = SessionInfo("s2", TMP, cwd="/tmp/scratch/1", first_seen=S2[0].timestamp)
    _put(ix, _log(TMP, "s2"), [s2, *S2])
    s3 = SessionInfo("s3", BETA, cwd="/home/user/beta", title="Beta fixes")
    _put(ix, _log(BETA, "s3"), [s3, *S3])
    # s4's log was queued for a reread (pos 0) and never read again: gone from disk,
    # so its prompts are unknown
    _put(ix, _log(BETA, "s4"), [SessionInfo("s4", BETA, cwd="/home/user/beta"), *S4], pos=0)
    ix.close()
    return path


# --------------------------------------------------------------------------- fake analysis


def real_spikes(
    index: Index, table: PriceTable, since: datetime, until: datetime, sid: str | None
) -> list[Spike]:
    return capcom.timeline.find_spikes(index, table, since, until, sid)


def not_yet(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("stub")


def fake_explain(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    return []


def fake_checks(*args: Any) -> list[Finding]:
    return [Finding("k", "A finding", "all", "summary", 3.5, "advice")]


# capcom.timeline is real; explain and checks are fakes until they are built
FAKE = Analysis(
    capcom.timeline.cumulative, capcom.timeline.per_step, real_spikes, fake_explain, fake_checks
)
STUBS = Analysis(not_yet, not_yet, not_yet, not_yet, not_yet)


# --------------------------------------------------------------------------- server


@dataclass
class Client:
    base: str

    def get(
        self, path: str, host: str | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any, dict[str, str]]:
        request = urllib.request.Request(self.base + path, headers=headers or {})
        if host is not None:
            request.add_header("Host", host)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                status, body, headers = resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as err:
            status, body, headers = err.code, err.read(), dict(err.headers)
        ctype = headers.get("Content-Type", "")
        data: Any = json.loads(body) if ctype.startswith("application/json") else body
        return status, data, headers

    def request(self, method: str, path: str) -> int:
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as err:
            return err.code


def serve(site: Site) -> Iterator[Client]:
    server = make_server(site, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session", autouse=True)
def scratch_data_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """capcom.changes keeps its marks under $XDG_DATA_HOME: never the real one here."""
    old = os.environ.get("XDG_DATA_HOME")
    os.environ["XDG_DATA_HOME"] = str(tmp_path_factory.mktemp("data"))
    yield
    if old is None:
        os.environ.pop("XDG_DATA_HOME", None)
    else:
        os.environ["XDG_DATA_HOME"] = old


@pytest.fixture(scope="session")
def index_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_index(tmp_path_factory.mktemp("cache") / "index.sqlite")


@pytest.fixture(scope="session")
def client(index_path: Path) -> Iterator[Client]:
    yield from serve(Site(index_path, TABLE, FAKE, clock=lambda: NOW, tz=UTC))


@pytest.fixture
def stub_client(index_path: Path) -> Iterator[Client]:
    yield from serve(Site(index_path, TABLE, STUBS, clock=lambda: NOW, tz=UTC))
