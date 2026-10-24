"""One color per project, agent type and model, the same on every page (/api/colors).

A color follows its entity, never its rank in a view: it is assigned once over every
entity the index knows, so a project has the same color in a chart, in the bars
beside it and on another page, whatever else those show.

Where a color comes from, for each kind (projects, agent types, models apart):

1. The color the user set in Claude Code. A session's `/color` (SessionInfo.color)
   gives its project one: the color most of the project's last RECENT_COLORED
   colored sessions use, the newest of them on a tie, so one session colored to tell
   it apart from a parallel one does not repaint the project, and a habit changed
   since wins after a few sessions. An agent type takes the `color:` of its
   definition (capcom.checks.agents.find_colors). Models have none.
2. An entity whose Claude color another entity of its kind already holds takes the
   next shade of that color (SHADES): a darker or lighter step, or a small hue
   shift, still nearer that Claude color than any other Claude Code color, in both
   themes, and at least SHADE_APART from the shades before it. Entities with a
   Claude color are placed first, the earliest seen first.
3. Every other entity, the earliest seen first, takes the first color of GENERAL
   that no entity of its kind holds and that is not within NEAR of a Claude color
   held in its kind (a color that near counts as held, by the Claude color's
   holder). GENERAL is the dataviz reference palette (its eight slots in their
   CVD-safe order), then four more shades kept apart from those.

The main thread is the one exception: it always takes MAIN_COLOR, a magenta no
other color in this module comes near, so the agent views (where main sits beside
every agent type) never show it next to a look-alike.

When every color of a pool is held, the new entity shares the one whose holders
made requests on the fewest days of the SHARE_DAYS before it was first seen, so they
are unlikely to show side by side. An entity's color depends only on the
entities with a Claude color and those seen before it, so a new project never
repaints an older one.

Every color is a (light, dark) pair of hex values, at least 3:1 against the cards of
its theme (the GENERAL slots the dataviz palette marks below 3:1 keep their labels
beside them). "(other)" stays the neutral gray, and the token categories keep their
own colors: both are the pages' (lib.js), not this module's.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from capcom.timeline.groups import ProjectNames, is_temporary
from capcom.types import Index, PriceTable, SessionInfo

Pair = tuple[str, str]  # (light theme, dark theme)

# Claude Code's /color names, as the pages draw a session's dot (style.css --sc-*).
CLAUDE: dict[str, Pair] = {
    "red": ("#C8402F", "#EE6B5B"),
    "orange": ("#C8601A", "#F0904A"),
    "yellow": ("#A87E00", "#E8C14A"),
    "green": ("#3C8A3A", "#6CC36A"),
    "cyan": ("#12879A", "#4CC3D4"),
    "blue": ("#2F6FD0", "#6EA2F2"),
    "purple": ("#8A55C9", "#B78AF0"),
    "pink": ("#C2477F", "#EE85BC"),
}

# Each Claude color, then its shades in the order they are handed out. Found by a
# search in OKLCH around the base (lightness, chroma and up to 12 degrees of hue),
# keeping only steps nearer the base than any other Claude color by at least 1.5,
# at least 8 from the base and from each other, at least 3:1 on the theme's cards;
# tests/report/test_colors.py checks every rule.
SHADES: dict[str, tuple[Pair, ...]] = {
    "red": (CLAUDE["red"], ("#A51A0B", "#CE4E40"), ("#F34451", "#BF7171")),
    "orange": (CLAUDE["orange"], ("#F1643B", "#CD7E0E"), ("#C48069", "#EDAE8E")),
    "yellow": (CLAUDE["yellow"], ("#83630E", "#BEAA29")),
    "green": (CLAUDE["green"], ("#1A6C1A", "#4CA34B"), ("#699D66", "#8CE489")),
    "cyan": (CLAUDE["cyan"], ("#026D81", "#1FA3B4")),
    "blue": (CLAUDE["blue"], ("#0C50AF", "#5083D0"), ("#4D8EF2", "#96BBFE")),
    "purple": (CLAUDE["purple"], ("#6D36A8", "#986BCE"), ("#A874EB", "#CDAFF8")),
    "pink": (CLAUDE["pink"], ("#A02562", "#CC669D"), ("#E1639A", "#FBAFD4")),
}

# The colors of entities without a Claude color, in the order they are handed out:
# the dataviz reference palette (its documented light and dark steps, in its order),
# then plum, deep blue, olive and brick, each at least 12 from those eight, at OKLCH
# chroma 0.10 or more (the dataviz floor, below which a hue reads as gray).
GENERAL: tuple[Pair, ...] = (
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
    ("#6250d6", "#9085e9"),
    ("#e34948", "#e66767"),
    ("#822871", "#f996e2"),
    ("#085788", "#81c5fd"),
    ("#475b10", "#ced51f"),
    ("#8e3f36", "#fda296"),
)

# The main thread's own color, a magenta in the widest gap GENERAL and the Claude
# shades leave: at least 12 from GENERAL (and 4 under simulated protan and deutan
# vision) and 11 from every Claude shade, in both themes.
MAIN_COLOR: Pair = ("#cf40c8", "#d031bf")

RECENT_COLORED = 10  # a project's color: the most used of its last this many colored sessions
SHADE_APART = 8.0  # ΔE (OKLab times 100) a shade keeps from the base and the shades before it
NEAR = 9.0  # ΔE: a GENERAL color this near a held Claude color (either theme) is taken
SHARE_DAYS = 28  # a shared color goes to the slot least active over these days

# the chart series that merge folders (capcom.timeline.groups): their own keys
OPENCLAW_SERIES = "openclaw"
TEMPORARY_SERIES = "temporary"
MAIN_THREAD = "main"
UNKNOWN_AGENT_KEYS = ("unknown", "(unknown agent)")


# --------------------------------------------------------------------------- OKLab


def _linear(c: int) -> float:
    x = c / 255
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    r, g, b = (_linear(int(h[i : i + 2], 16)) for i in (0, 2, 4))
    return r, g, b


def oklab(hex_: str) -> tuple[float, float, float]:
    """The OKLab coordinates of a color (#rrggbb)."""
    r, g, b = _rgb(hex_)
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (math.copysign(abs(x) ** (1 / 3), x) for x in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def delta_e(a: str, b: str) -> float:
    """Euclidean distance in OKLab times 100 (the dataviz method's ΔE)."""
    return 100 * math.dist(oklab(a), oklab(b))


def pair_delta(a: Pair, b: Pair) -> float:
    """The smaller of the two themes' ΔE."""
    return min(delta_e(a[0], b[0]), delta_e(a[1], b[1]))


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colors."""

    def lum(h: str) -> float:
        r, g, b_ = _rgb(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# --------------------------------------------------------------------------- assignment


@dataclass(slots=True)
class Entity:
    """One project, agent type or model: the keys and labels the pages name it by
    (the first is its own), when it was first seen, its Claude Code color if any,
    and the days it made requests (whole days since the Unix epoch, UTC). A `fixed`
    color is used as it is, outside every pool (the main thread's)."""

    keys: tuple[str, ...]
    first_seen: datetime | None = None
    claude: str | None = None
    days: set[int] = field(default_factory=set)
    fixed: Pair | None = None


def _order(e: Entity) -> tuple[int, float, str]:
    return (e.first_seen is None, e.first_seen.timestamp() if e.first_seen else 0.0, e.keys[0])


def epoch_day(t: datetime) -> int:
    return math.floor(t.timestamp() / 86400)


def _quietest(
    slots: Sequence[int],
    holders: Mapping[int, list[Entity]],
    near: Mapping[int, int],
    e: Entity,
) -> int:
    """Of `slots`, the one whose holders made requests on the fewest days of the
    SHARE_DAYS up to the day `e` was first seen (each holder's days counted, and a
    Claude color held `near` it as if busy every day), then the one idle longest by
    then, then the first. A color is so shared with entities unlikely to show beside
    `e`, and only days up to `e`'s first decide, so the choice never changes later."""
    today = epoch_day(e.first_seen) if e.first_seen else math.inf

    def busy(i: int) -> tuple[int, float, int]:
        days = [d for h in holders.get(i, []) for d in h.days if d <= today]
        recent = sum(1 for d in days if d > today - SHARE_DAYS) + SHARE_DAYS * near.get(i, 0)
        return recent, max(days, default=-math.inf), slots.index(i)

    return min(slots, key=busy)


def assign(entities: Iterable[Entity]) -> dict[str, Pair]:
    """Each entity's color, by its first key (the rules in the module docstring)."""
    ents = sorted(entities, key=_order)
    out: dict[str, Pair] = {e.keys[0]: e.fixed for e in ents if e.fixed}
    ents = [e for e in ents if not e.fixed]
    claude: list[tuple[Pair, Entity]] = []  # the Claude colors and shades held, by whom
    for name, shades in SHADES.items():
        holders: dict[int, list[Entity]] = {}
        for e in (e for e in ents if e.claude == name):
            free = [i for i in range(len(shades)) if i not in holders]
            i = free[0] if free else _quietest(range(len(shades)), holders, {}, e)
            holders.setdefault(i, []).append(e)
            out[e.keys[0]] = shades[i]
            claude.append((shades[i], e))
    slots = list(range(len(GENERAL)))
    # a general color near a held Claude color is taken by its holder, if less so
    near = {i: sum(pair_delta(GENERAL[i], c) < NEAR for c, _ in claude) for i in slots}
    taken: dict[int, list[Entity]] = {}
    for e in ents:
        if e.claude in SHADES:
            continue
        free = [i for i in slots if i not in taken and not near[i]]
        i = free[0] if free else _quietest(slots, taken, near, e)
        taken.setdefault(i, []).append(e)
        out[e.keys[0]] = GENERAL[i]
    return out


def project_color(sessions: Sequence[SessionInfo]) -> str | None:
    """A project's Claude color from its sessions': the most used of its last
    RECENT_COLORED colored sessions (by when each started), the newest on a tie."""
    colored = [s for s in sessions if s.color in CLAUDE]
    colored.sort(key=lambda s: (s.first_seen.timestamp() if s.first_seen else 0.0, s.session_id))
    recent = colored[-RECENT_COLORED:]
    if not recent:
        return None
    counts: dict[str, int] = {}
    newest: dict[str, int] = {}
    for i, s in enumerate(recent):
        assert s.color is not None
        counts[s.color] = counts.get(s.color, 0) + 1
        newest[s.color] = i
    return max(counts, key=lambda c: (counts[c], newest[c]))


# --------------------------------------------------------------------------- the index


class _Seen:
    """Per key: its first request and the days it made requests."""

    def __init__(self) -> None:
        self.first: dict[str, datetime] = {}
        self.days: dict[str, set[int]] = {}

    def add(self, key: str, day: int, first: datetime | None) -> None:
        self.days.setdefault(key, set()).add(day)
        if first is not None and (key not in self.first or first < self.first[key]):
            self.first[key] = first


def _session_days(s: SessionInfo) -> list[tuple[int, datetime | None]]:
    times = [t for t in (s.first_seen, s.last_seen) if t is not None]
    return [(epoch_day(t), s.first_seen or t) for t in times]


def _activity(
    index: Index, table: PriceTable, infos: Mapping[str, SessionInfo], names: ProjectNames
) -> tuple[_Seen, _Seen, _Seen]:
    """When each project row, agent type and model made requests (the store's
    `activity_days`); an index without it falls back on the sessions' first and
    last times and their cost records."""
    from capcom.cost.prices import canonical, is_placeholder

    projects, agents, models = _Seen(), _Seen(), _Seen()
    types = {a.agent_id: a.agent_type or UNKNOWN_AGENT_KEYS[0] for a in index.agents()}

    def project(project_dir: str, day: int, first: datetime | None) -> None:
        projects.add(names.key(project_dir), day, first)
        if is_temporary(project_dir) and not names.is_openclaw(project_dir):
            projects.add(TEMPORARY_SERIES, day, first)

    activity = getattr(index, "activity_days", None)
    if callable(activity):
        for project_dir, agent_id, model, day, first in activity():
            if is_placeholder(model):
                continue
            project(project_dir, day, first)
            kind = MAIN_THREAD if agent_id is None else types.get(agent_id, UNKNOWN_AGENT_KEYS[0])
            agents.add(kind, day, first)
            models.add(canonical(table, model), day, first)
        return projects, agents, models
    for s in infos.values():
        for day, first in _session_days(s):
            project(s.project_dir, day, first)
            agents.add(MAIN_THREAD, day, first)
    for a in index.agents():
        info = infos.get(a.session_id)
        for day, first in _session_days(info) if info else []:
            agents.add(a.agent_type or UNKNOWN_AGENT_KEYS[0], day, first)
    for cs in index.last_cost_states():
        info = infos.get(cs.session_id)
        for m in cs.models:
            for day, first in _session_days(info) if info else []:
                models.add(canonical(table, m), day, first)
    return projects, agents, models


def _first(times: Iterable[datetime | None]) -> datetime | None:
    seen = [t for t in times if t is not None]
    return min(seen, key=lambda t: t.timestamp()) if seen else None


def project_entities(
    sessions: Sequence[SessionInfo], names: ProjectNames, seen: _Seen
) -> list[Entity]:
    """One entity per project row (ProjectNames.key), named by its key and label,
    and the chart series that merges the temporary folders. OpenClaw's merged
    series is keyed "openclaw", the label of its state dir's row, so it takes that
    row's color."""
    groups: dict[str, list[SessionInfo]] = {}
    temporary: list[SessionInfo] = []
    claw: set[str] = set()
    for s in sessions:
        key = names.key(s.project_dir)
        groups.setdefault(key, []).append(s)
        if names.is_openclaw(s.project_dir):
            claw.add(key)
        elif is_temporary(s.project_dir):
            temporary.append(s)
    for key in seen.days:
        groups.setdefault(key, [])
    if temporary:
        groups.setdefault(TEMPORARY_SERIES, [])
    out: list[Entity] = []
    labels: set[str] = set()
    for key, ss in groups.items():
        if key == TEMPORARY_SERIES:
            keys: tuple[str, ...] = (key, "temporary folders")
            ss = temporary
        else:
            label = names.label(key)
            labels.add(label)
            keys = (key, label) if label != key else (key,)
        first = seen.first.get(key) or _first(s.first_seen for s in ss)
        out.append(Entity(keys, first, project_color(ss), seen.days.get(key, set())))
    if claw and OPENCLAW_SERIES not in labels and OPENCLAW_SERIES not in groups:
        # no state-dir row: the merged series takes the earliest OpenClaw folder's
        first_claw = min((e for e in out if e.keys[0] in claw), key=_order)
        first_claw.keys = (*first_claw.keys, OPENCLAW_SERIES)
    return out


def agent_entities(seen: _Seen, colors: Mapping[str, str]) -> list[Entity]:
    """One entity per agent type that made requests, the main thread and the unknown
    type among them; a type's color is its definition's `color:` (`colors`), when
    it is a Claude one, and the main thread's is MAIN_COLOR."""
    seen.days.setdefault(MAIN_THREAD, set())
    seen.days.setdefault(UNKNOWN_AGENT_KEYS[0], set())
    out = []
    for kind, days in seen.days.items():
        claude = colors.get(kind)
        keys = UNKNOWN_AGENT_KEYS if kind == UNKNOWN_AGENT_KEYS[0] else (kind,)
        if kind == MAIN_THREAD:
            out.append(Entity(keys, seen.first.get(kind), None, days, MAIN_COLOR))
            continue
        out.append(Entity(keys, seen.first.get(kind), claude if claude in CLAUDE else None, days))
    return out


def model_entities(seen: _Seen) -> list[Entity]:
    """One entity per model the requests name, by its price-table id (as the pages
    name it)."""
    return [Entity((m,), seen.first.get(m), None, days) for m, days in seen.days.items()]


def colors_json(
    entities: Mapping[str, Sequence[Entity]],
) -> dict[str, Any]:
    """The reply of /api/colors: `palette`, every color in use as [light, dark] (the
    GENERAL ones first, `general` of them, for a key the server does not know), and
    for each kind a map from every key and label to its palette index."""
    palette: list[Pair] = list(GENERAL)
    where: dict[Pair, int] = {c: i for i, c in enumerate(palette)}
    out: dict[str, Any] = {"general": len(GENERAL)}
    for kind, ents in entities.items():
        colors = assign(ents)
        names: dict[str, int] = {}
        for e in ents:
            c = colors[e.keys[0]]
            if c not in where:
                where[c] = len(palette)
                palette.append(c)
            for k in e.keys:
                names.setdefault(k, where[c])
        out[kind] = names
    out["palette"] = [list(c) for c in palette]
    return out


def entity_colors(
    index: Index,
    table: PriceTable,
    infos: Mapping[str, SessionInfo],
    names: ProjectNames,
    agent_dirs: Sequence[Path],
) -> dict[str, Any]:
    """/api/colors from the index: projects, agent types and models."""
    from capcom.checks.agents import find_colors

    projects, agents, models = _activity(index, table, infos, names)
    return colors_json(
        {
            "project": project_entities(list(infos.values()), names, projects),
            "agent": agent_entities(agents, find_colors(agent_dirs)),
            "model": model_entities(models),
        }
    )
