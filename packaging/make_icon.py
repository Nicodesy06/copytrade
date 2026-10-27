#!/usr/bin/env python3
"""Genera l'icona dell'app (PNG 1024 + iconset macOS) con Pillow. Uso: make_icon.py <cartella_uscita>"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw(size: int) -> Image.Image:
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            px[x, y] = (int(94 + (10 - 94) * t), int(92 + (132 - 92) * t), int(230 + (255 - 230) * t), 255)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((80, 80, s - 80, s - 80), radius=190, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    pts = [(250, 700), (440, 450), (590, 570), (790, 300)]
    d.line(pts, fill="white", width=70, joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - 48, y - 48, x + 48, y + 48), fill="white")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon")
    iconset = out / "M4CopyDesk.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    base = draw(1024)
    base.save(out / "icon.png")
    for n in SIZES[:-1]:
        base.resize((n, n), Image.LANCZOS).save(iconset / f"icon_{n}x{n}.png")
        base.resize((n * 2, n * 2), Image.LANCZOS).save(iconset / f"icon_{n}x{n}@2x.png")
    print(f"icona generata in {out}")


if __name__ == "__main__":
    main()
