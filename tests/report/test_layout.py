"""Every page of the website, at phone to wide-desktop widths and in both themes:
no sideways page scroll, no overlapping text or controls, targets big enough to
hit, no clipped text (tests/report/layout.py). Runs when Chrome is installed;
CAPCOM_LAYOUT_OUT=<folder> also writes the contact sheets there."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.report import layout

pytestmark = pytest.mark.skipif(
    layout.find_chrome() is None or os.environ.get("CAPCOM_LAYOUT") == "0",
    reason="needs Chrome (set CAPCOM_CHROME), or CAPCOM_LAYOUT=0 skips it",
)


def test_every_page_fits_every_width() -> None:
    out = os.environ.get("CAPCOM_LAYOUT_OUT")
    res = layout.run(Path(out) if out else None)
    assert len(res.pages) >= 8, f"found only {res.pages}"
    assert not res.problems, "\n".join(res.lines())
