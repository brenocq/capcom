"""capcom.changes: "Did it work?" (design.md). The user marks a finding as applied on
a date; capcom measures that finding's own rate before and after and gives a
verdict.

- store.py: the marks file ($XDG_DATA_HOME/capcom/changes.jsonl, append-only
  events), `store_path`, `load`, `mark`, `edit`, `unmark`;
- measures.py: per finding kind, what is counted, from the checks' own facts;
- measure.py: the weekly windows, the verdict and the measured saving (`measure`);
- noticed.py: agent definitions that now carry a finding's suggested line
  (`noticed`);
- cli.py: `capcom mark`, `capcom changes`, `capcom unmark`.
"""

from __future__ import annotations

from capcom.changes.measure import measure
from capcom.changes.noticed import noticed
from capcom.changes.store import edit, load, mark, store_path, unmark

__all__ = ["edit", "load", "mark", "measure", "noticed", "store_path", "unmark"]
