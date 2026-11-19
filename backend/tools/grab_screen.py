"""Save a region of the screen to a PNG file.

Used to capture the website for the paper's appendix: the browser is
brought to the front, and this grabs the page area of the screen.

Usage::

    python tools/grab_screen.py out.png [left top right bottom]
"""

from __future__ import annotations

import sys

from PIL import ImageGrab


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    out = sys.argv[1]
    if len(sys.argv) >= 6:
        box = tuple(int(x) for x in sys.argv[2:6])
    else:
        box = None
    image = ImageGrab.grab(bbox=box, all_screens=True)
    image.save(out)
    print(f"{out}  {image.size[0]}x{image.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
