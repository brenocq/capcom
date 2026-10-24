"""Builds every picture in the README, from synthetic logs.

    uv run python .github/scripts/build_all.py [--out DIR] [--check DIR] [--no-demo] [--keep DIR]
        [--video MP4]

1. demo/data.py writes four made-up weeks of Claude Code logs into a temporary
   folder and runs capcom on them there (never on ~/.claude or ~/.cache/capcom).
2. banner/build.py and features/build.py draw banner.svg and the eight
   feature-*.svg from capcom's numbers.
3. demo/record.py records the real `capcom serve` on that data in headless
   Chrome, once per GitHub theme: demo-light.webp, demo-dark.webp and a still
   of each, demo-light.png and demo-dark.png.
4. social/build.py composes social.png, the 1280 x 640 social preview card, from
   the banner and its chart (uploaded by hand: Settings, Social preview).
5. With --video (the narrated launch video, made in its own project from the
   same synthetic logs), video/poster.sh draws video-poster.png from one of its
   frames. publish-assets.sh needs it, so pass it whenever the assets change.

Everything lands in --out (default .github/assets/, gitignored); then
.github/scripts/publish-assets.sh puts it on the `assets` branch. With --check,
contact sheets of every SVG (several moments, light and dark), the demo's
stills (each step's last frame, both themes) and screenshots of a local README
preview go to that folder. Needs google-chrome (or CHROME=) and
ffmpeg with libwebp for the demo; Chrome runs in a memory-capped scope (2 GB, or
CHROME_MEMORY_MAX).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
for sub in ("", "demo", "banner", "features", "preview", "social"):
    sys.path.insert(0, str(HERE / sub))

MAX_SVG = 100 * 1024


def load(path: Path, name: str):  # type: ignore[no-untyped-def]
    """A generator script as a module (two of them are called build.py)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the README's media from synthetic logs.")
    ap.add_argument("--out", type=Path, default=HERE.parent / "assets")
    ap.add_argument("--check", type=Path, help="write contact sheets and a preview here")
    ap.add_argument("--no-demo", action="store_true", help="skip the demo recording")
    ap.add_argument("--keep", type=Path, help="build the synthetic data here and keep it")
    ap.add_argument("--video", type=Path, help="the launch video's MP4, for video-poster.png")
    a = ap.parse_args()

    import data  # demo/data.py

    work = a.keep.resolve() if a.keep else Path(tempfile.mkdtemp(prefix="capcom-readme-"))
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    try:
        if a.keep and work.exists():
            shutil.rmtree(work)
        numbers = data.build(work, datetime.now(UTC))
        print(
            f"data: ${numbers['spend_project']['totals']['usd']:.2f} over 14 days, "
            f"{numbers['spikes']['count']} spikes, {len(numbers['checks']['findings'])} findings"
        )

        banner = load(HERE / "banner" / "build.py", "banner_build")
        features = load(HERE / "features" / "build.py", "features_build")
        svgs = [banner.build(numbers, out), *features.build(numbers, out)]
        for p in svgs:
            size = p.stat().st_size
            print(f"{p.name}: {size / 1024:.1f} KiB")
            if size > MAX_SVG:
                sys.exit(f"{p} is over 100 KiB")

        stills = a.check.resolve() if a.check else None
        if not a.no_demo:
            import record  # demo/record.py

            sys.argv = ["record.py", str(work), str(out)] + (
                ["--stills", str(stills)] if stills else []
            )
            record.main()
        social = load(HERE / "social" / "build.py", "social_build")
        print(social.build(out, numbers))
        if a.video:
            poster = out / "video-poster.png"
            subprocess.run(
                [str(HERE / "video" / "poster.sh"), str(a.video), str(poster)], check=True
            )
            print(poster)

        if stills:
            import page
            import sheets
            from chrome import Chrome

            stills.mkdir(parents=True, exist_ok=True)
            with Chrome(1200, 800) as c:
                sheets.sheets(stills, svgs, [0.6, 1.5, 3, 4.5, 7], c)
            for p in page.build(out, stills):
                print(p)
        print(json.dumps({p.name: p.stat().st_size for p in sorted(out.iterdir())}))
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
