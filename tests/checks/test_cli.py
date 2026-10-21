"""`capcom checks` output: long evidence lines wrap under their text, with no
stray dash on the continuation lines. All data here is made up."""

from __future__ import annotations

from capcom.checks.cli import WIDTH, format_findings
from capcom.types import Evidence, Finding


def test_long_evidence_wraps_without_a_dash() -> None:
    label = "a session rewrote its cache after an idle pause " * 4 + "($3.63 above a cache read)"
    f = Finding("k", "Title", "all", "summary", 1.0, "advice", evidence=(Evidence(label),))
    lines = format_findings([f], 5).splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("    - "))
    rest = lines[first + 1 :]
    assert rest, "the label should wrap"
    assert all(line.startswith("      ") and not line.lstrip().startswith("-") for line in rest)
    assert all(len(line) <= WIDTH for line in lines)
    assert " ".join(line.strip() for line in lines[first:]).removeprefix("- ") == label.strip()
