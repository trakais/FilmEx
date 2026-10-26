"""Generate the FilmEx app icon (.icns) with Pillow and iconutil.

Usage: python packaging/make_icon.py packaging/FilmEx.icns

A dark rounded square with a yellow film frame and sprocket holes, drawn
procedurally so the repository carries no binary icon assets.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
BACKGROUND = (12, 12, 12, 255)
YELLOW = (243, 190, 34, 255)
SIZES = (16, 32, 64, 128, 256, 512, 1024)


def render(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / SIZE
    d.rounded_rectangle([64 * s, 64 * s, 960 * s, 960 * s], radius=200 * s, fill=BACKGROUND)
    d.rounded_rectangle(
        [200 * s, 330 * s, 824 * s, 694 * s], radius=28 * s, outline=YELLOW, width=max(1, int(26 * s))
    )
    for x in range(236, 800, 96):
        d.rounded_rectangle([x * s, 236 * s, (x + 44) * s, 290 * s], radius=8 * s, fill=YELLOW)
        d.rounded_rectangle([x * s, 734 * s, (x + 44) * s, 788 * s], radius=8 * s, fill=YELLOW)
    return img


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "packaging/FilmEx.icns")
    master = render()
    if out.suffix == ".png":
        master.save(out)
        return 0
    if shutil.which("iconutil") is None:
        print("iconutil not found (macOS only); writing PNG instead", file=sys.stderr)
        master.save(out.with_suffix(".png"))
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "FilmEx.iconset"
        iconset.mkdir()
        for size in SIZES:
            img = master.resize((size, size), Image.LANCZOS)
            if size <= 512:
                img.save(iconset / f"icon_{size}x{size}.png")
            if size >= 32:
                half = size // 2
                img.save(iconset / f"icon_{half}x{half}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
