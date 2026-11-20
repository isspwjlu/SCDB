"""Screenshot the running website with a headless browser.

The pages of the site are captured at the width they are read at, so
the figures in the paper are the figures a reader sees.

Usage::

    python tools/shoot_site.py --out ../paper/figures/site
"""

from __future__ import annotations

import argparse
from pathlib import Path

PAGES = (
    ("charts", "http://127.0.0.1:5173/charts"),
    ("materials", "http://127.0.0.1:5173/materials"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="../paper/figures/site")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1100)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
        )
        for name, url in PAGES:
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(2500)
            path = out / f"{name}_raw.png"
            page.screenshot(path=str(path))
            print(f"{path.name}  {path.stat().st_size / 1024:.0f} KB")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
