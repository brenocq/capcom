"""Writes wordmark.py: the outlines of "capcom" for the banner, so the build needs no font.

    python .github/scripts/banner/extract_wordmark.py [font.otf]

Run once, by hand, with a Python that has fontTools (the system one often does); the
build itself reads only wordmark.py. The default font is C059 Roman, URW's
Century Schoolbook (AGPL with the font exception, which allows embedding its
outlines in a document): a book serif like the website's brand.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

FONT = "/usr/share/fonts/gsfonts/C059-Roman.otf"
WORD = "capcom"


def main() -> None:
    font = TTFont(sys.argv[1] if len(sys.argv) > 1 else FONT)
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    out = {}
    for ch in sorted(set(WORD)):
        name = cmap[ord(ch)]
        pen = SVGPathPen(glyphs, ntos=lambda v: f"{round(v)}")
        glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, 0, 0)))  # y down, baseline at 0
        out[ch] = (glyphs[name].width, pen.getCommands())
    lines = [
        "# ruff: noqa: E501",
        '"""Outlines of the letters of "capcom" (C059 Roman, y down, baseline at 0), made by',
        'extract_wordmark.py. Do not edit by hand."""',
        "",
        f"UPM = {upm}",
        "GLYPHS: dict[str, tuple[int, str]] = {",
        *[f"    {ch!r}: ({w}, {d!r})," for ch, (w, d) in out.items()],
        "}",
        "",
    ]
    Path(__file__).with_name("wordmark.py").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
