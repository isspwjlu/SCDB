"""Assemble the website screenshots into the paper's appendix figure.

The captures are screen grabs of the running site, cropped to the
content, trimmed to the same height and laid out as a 2x2 panel with
lettered labels.

Usage::

    python tools/make_site_figure.py [--src ../paper/figures/site]
                                      [--out ../paper/figures/site_overview.png]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BACKEND = Path(__file__).resolve().parents[1]

PANELS = (
    ("charts_raw.png", "(a)"),
    ("materials_raw.png", "(b)"),
)

BACKGROUND = (250, 248, 245)


def trim(image: Image.Image) -> Image.Image:
    """Cut the empty margin on the left and the right."""
    width, height = image.size
    pixels = image.convert("RGB").load()
    left = 0
    while left < width:
        column = [pixels[left, y] for y in range(0, height, 8)]
        if any(abs(c[0] - BACKGROUND[0]) + abs(c[1] - BACKGROUND[1])
               + abs(c[2] - BACKGROUND[2]) > 24 for c in column):
            break
        left += 1
    right = width - 1
    while right > left:
        column = [pixels[right, y] for y in range(0, height, 8)]
        if any(abs(c[0] - BACKGROUND[0]) + abs(c[1] - BACKGROUND[1])
               + abs(c[2] - BACKGROUND[2]) > 24 for c in column):
            break
        right -= 1
    pad = 12
    return image.crop((max(0, left - pad), 0, min(width, right + pad), height))


def label_font(size: int):
    """Arial Bold if the system has it, the bitmap default otherwise."""
    for name in ("arialbd.ttf", "arial.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=str(BACKEND.parent / "paper" / "figures" / "site"))
    parser.add_argument(
        "--out", default=str(BACKEND.parent / "paper" / "figures" / "site_overview.png")
    )
    args = parser.parse_args()

    src = Path(args.src)
    images = []
    for name, label in PANELS:
        path = src / name
        if not path.exists():
            print(f"!! 缺截图：{path}")
            return 1
        images.append((label, trim(Image.open(path).convert("RGB"))))

    # Every panel is scaled to the same height, so the grid is even.
    panel_height = min(image.size[1] for _, image in images)
    scaled = []
    for label, image in images:
        ratio = panel_height / image.size[1]
        new = image.resize(
            (max(1, int(image.size[0] * ratio)), panel_height), Image.LANCZOS
        )
        scaled.append((label, new))

    gap = 30
    label_height = 62
    cell_width = max(image.size[0] for _, image in scaled)
    total_width = cell_width * 2 + gap
    total_height = panel_height + label_height

    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = label_font(44)
    for index, (label, image) in enumerate(scaled):
        x = index * (cell_width + gap)
        y = 0
        draw.text((x + 2, y), label, fill=(20, 20, 26), font=font)
        canvas.paste(image, (x, y + label_height))
        draw.rectangle(
            [x, y + label_height, x + image.size[0] - 1, y + label_height + image.size[1] - 1],
            outline=(205, 200, 192),
            width=2,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The figure is 17.2 cm wide in the paper, so the dpi has to say so.
    dpi = round(total_width / (17.2 / 2.54))
    canvas.save(out, dpi=(dpi, dpi))
    print(f"{out}  {canvas.size[0]}x{canvas.size[1]}  @{dpi} dpi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
