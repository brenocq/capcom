"""A local preview of the README as GitHub lays it out, with the media from a folder.

    python .github/scripts/preview/page.py <assets-dir> <out-dir>

Turns README.md into <out-dir>/readme.html (a small Markdown subset: the one this
README uses), with the https://github.com/brenocq/capcom/raw/assets/ URLs pointing
at <assets-dir>, in a column as wide as GitHub's, then screenshots it in light and
dark (readme-light.png, readme-dark.png, downscaled by half) with headless
Chrome, the <details> opened.
"""

from __future__ import annotations

# ruff: noqa: E501 (the CSS below)
import re
import sys
import time
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chrome import Chrome

ROOT = Path(__file__).resolve().parents[3]
URL = "https://github.com/brenocq/capcom/raw/assets/"
CSS = """
:root { color-scheme: light; --fg: #1f2328; --muted: #59636e; --border: #d1d9e0; --code: #f6f8fa; --bg: #ffffff; --link: #0969da; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --fg: #f0f6fc; --muted: #9198a1; --border: #3d444d; --code: #151b23; --bg: #0d1117; --link: #4493f8; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; }
article { max-width: 830px; margin: 24px auto; padding: 32px; border: 1px solid var(--border); border-radius: 6px; }
h2 { font-size: 1.5em; font-weight: 600; padding-bottom: .3em; border-bottom: 1px solid var(--border); margin: 24px 0 16px; }
a { color: var(--link); text-decoration: none; }
code { font: 85% ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code); padding: .2em .4em; border-radius: 6px; }
pre { background: var(--code); padding: 16px; border-radius: 6px; overflow: auto; }
pre code { background: none; padding: 0; font-size: 85%; }
table { border-collapse: collapse; margin: 0 0 16px; }
td, th { border: 1px solid var(--border); padding: 6px 13px; text-align: left; }
th { font-weight: 600; }
tr:nth-child(2n) td { background: var(--code); }
img { max-width: 100%; }
p, details { margin: 0 0 16px; }
"""


def inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f"<code>{escape(p[1:-1])}</code>")
            continue
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        p = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', p)
        out.append(p)
    return "".join(out)


def render(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    lines = md.split("\n")
    i = 0

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            out.append(f"<pre><code>{escape(chr(10).join(lines[i + 1 : j]))}</code></pre>")
            i = j + 1
            continue
        if line.startswith("## "):
            flush()
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            head, body = rows[0], rows[2:]
            out.append(
                "<table><tr>"
                + "".join(f"<th>{inline(c)}</th>" for c in head)
                + "</tr>"
                + "".join(
                    "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body
                )
                + "</table>"
            )
            continue
        elif line.lstrip().startswith("<"):
            flush()
            out.append(line)
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "\n".join(out)


def build(assets: Path, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    md = (ROOT / "README.md").read_text().replace(URL, assets.resolve().as_uri() + "/")
    html = out / "readme.html"
    html.write_text(
        f"<!doctype html><meta charset=utf-8><title>capcom README</title>"
        f"<style>{CSS}</style><article>{render(md)}</article>"
    )
    shots = []
    with Chrome(900, 1000, scale=0.5) as c:
        for theme in ("light", "dark"):
            c.media(dark=theme == "dark")
            c.goto(html.as_uri(), settle=1.0)
            h = c.eval(
                "document.querySelectorAll('details').forEach(d => d.open = true);"
                " document.documentElement.scrollHeight"
            )
            c.viewport(900, int(h), 0.5)
            time.sleep(1.5)  # let the demo and the cards play a while
            png = out / f"readme-{theme}.png"
            c.screenshot(png)
            shots.append(png)
    return shots


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    for p in build(Path(sys.argv[1]), Path(sys.argv[2])):
        print(p)


if __name__ == "__main__":
    main()
