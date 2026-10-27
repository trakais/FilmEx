"""Generate the FilmEx app icon (.icns) with Pillow and iconutil.

Usage: python packaging/make_icon.py packaging/FilmEx.icns

An anodized black rounded square with a lit top edge, the gold frameline
corners of a 35mm viewfinder (the selection mark used throughout the app),
and the rangefinder camera glyph from the gear tiles framed by them. Drawn
procedurally, at four times the size and scaled down for clean edges, so
the repository carries no binary icon assets.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SUPERSAMPLE = 4
SIZES = (16, 32, 64, 128, 256, 512, 1024)

BODY = (100, 100, 924, 924)  # macOS icon grid: 824 px body, 185 px corners
BODY_RADIUS = 185
BODY_TOP = (34, 32, 30)
BODY_BOTTOM = (13, 12, 11)
GOLD = (220, 180, 89, 255)
ENGRAVING = (243, 241, 234, 255)

# A 3:2 frame, like the 35mm framelines, with an L at each corner.
FRAME = (226, 324, 798, 706)
BRACKET_ARM = 104
BRACKET_WIDTH = 22

# The camera glyph from TILE_GLYPHS.camera in app.js, in its 44 x 32 units.
GLYPH_SCALE = 8.6
GLYPH_CENTER = (22.0, 17.25)
GLYPH_STROKE = 1.5


def _gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    column = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return column.resize((size, size), Image.BILINEAR)


def _glyph(d: ImageDraw.ImageDraw, k: float, color: tuple[int, int, int, int]) -> None:
    s = GLYPH_SCALE * k
    ox = SIZE / 2 * k - GLYPH_CENTER[0] * s
    oy = SIZE / 2 * k - GLYPH_CENTER[1] * s
    w = GLYPH_STROKE * s

    def pt(x: float, y: float) -> tuple[float, float]:
        return ox + x * s, oy + y * s

    def rect(x: float, y: float, rw: float, rh: float, r: float) -> None:
        # SVG strokes straddle the path; Pillow's grow inward, so widen the box.
        x0, y0 = pt(x, y)
        x1, y1 = pt(x + rw, y + rh)
        d.rounded_rectangle(
            [x0 - w / 2, y0 - w / 2, x1 + w / 2, y1 + w / 2],
            radius=r * s + w / 2, outline=color, width=round(w),
        )

    def circle(cx: float, cy: float, r: float) -> None:
        x, y = pt(cx, cy)
        rr = r * s + w / 2
        d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=color, width=round(w))

    def polyline(*points: tuple[float, float]) -> None:
        d.line([pt(*p) for p in points], fill=color, width=round(w), joint="curve")
        for p in (points[0], points[-1]):  # round caps
            x, y = pt(*p)
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)

    rect(3, 9, 38, 19, 2.5)
    polyline((7, 9), (7, 6.5), (13, 6.5), (13, 9))
    polyline((30, 9), (30, 6.5), (35, 6.5), (35, 9))
    rect(6.5, 12, 6, 4, 0.8)
    rect(30, 12, 7.5, 4.5, 0.8)
    rect(24, 12.5, 2.5, 3, 0.5)
    circle(19, 20, 6.5)
    circle(19, 20, 3.8)


def render(size: int = SIZE, *, camera: tuple[int, int, int, int] = ENGRAVING) -> Image.Image:
    k = SUPERSAMPLE
    big = SIZE * k
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    body = [v * k for v in BODY]

    # Soft shadow under the body, as macOS draws on its own icons.
    shadow = Image.new("L", (big, big), 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [body[0], body[1] + 14 * k, body[2], body[3] + 14 * k], radius=BODY_RADIUS * k, fill=110
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(22 * k))
    img.paste((0, 0, 0, 255), (0, 0), shadow)

    # Anodized body: a top-lit gradient, a hairline rim, a brighter top edge.
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(body, radius=BODY_RADIUS * k, fill=255)
    img.paste(_gradient(big, BODY_TOP, BODY_BOTTOM), (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(body, radius=BODY_RADIUS * k, outline=(255, 255, 255, 22), width=3 * k)
    edge = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(body, radius=BODY_RADIUS * k, outline=(255, 255, 255, 60), width=4 * k)
    fade = Image.new("L", (big, big), 0)
    ImageDraw.Draw(fade).rectangle([0, 0, big, body[1] + BODY_RADIUS * k * 0.6], fill=255)
    img.alpha_composite(Image.composite(edge, Image.new("RGBA", (big, big), (0, 0, 0, 0)), fade))

    # Frameline corners.
    x0, y0, x1, y1 = (v * k for v in FRAME)
    arm, bw = BRACKET_ARM * k, BRACKET_WIDTH * k
    for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.rectangle(sorted_box(cx, cy, cx + sx * arm, cy + sy * bw), fill=GOLD)
        d.rectangle(sorted_box(cx, cy, cx + sx * bw, cy + sy * arm), fill=GOLD)

    _glyph(d, k, camera)
    out = img.resize((SIZE, SIZE), Image.LANCZOS)
    return out if size == SIZE else out.resize((size, size), Image.LANCZOS)


def sorted_box(xa: float, ya: float, xb: float, yb: float) -> list[float]:
    return [min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)]


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
