"""Previews of scanner DNGs that carry no embedded JPEG preview."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PIL import Image

from filmex.services import previews as pv

SHORT, LONG = 3, 4
RED, BLUE, GREEN = (220, 30, 30), (30, 30, 220), (30, 200, 30)


def _short(value: int) -> bytes:
    return struct.pack("<HH", value, 0)


def _long(value: int) -> bytes:
    return struct.pack("<I", value)


def write_scanner_dng(
    path: Path,
    full: Image.Image,
    thumb: Image.Image,
    *,
    orientation: int = 1,
    full_photometric: int = 34892,
    linearization: bool = True,
    strips: int = 2,
) -> Path:
    """A minimal little-endian DNG laid out the way VueScan writes one: IFD0
    is a small RGB thumbnail and points at a SubIFD holding the full scan as
    uncompressed RGB in several strips, tagged Linear Raw with a
    linearization table."""
    tw, th = thumb.size
    fw, fh = full.size
    thumb_px, full_px = thumb.tobytes(), full.tobytes()
    rows = -(-fh // strips)
    strip_sizes = [min(rows, fh - i * rows) * fw * 3 for i in range(strips)]

    n0, n1 = 12, 11 + int(linearization)
    ifd0_at = 8
    ifd1_at = ifd0_at + 2 + 12 * n0 + 4
    bits0_at = ifd1_at + 2 + 12 * n1 + 4
    bits1_at = bits0_at + 6
    offsets_at = bits1_at + 6
    counts_at = offsets_at + 4 * strips
    table_at = counts_at + 4 * strips
    thumb_at = table_at + (512 if linearization else 0)
    full_at = thumb_at + len(thumb_px)
    strip_offsets = [full_at + sum(strip_sizes[:i]) for i in range(strips)]

    ifd0 = [
        (254, LONG, 1, _long(1)),
        (256, LONG, 1, _long(tw)),
        (257, LONG, 1, _long(th)),
        (258, SHORT, 3, _long(bits0_at)),
        (259, SHORT, 1, _short(1)),
        (262, SHORT, 1, _short(2)),
        (273, LONG, 1, _long(thumb_at)),
        (274, SHORT, 1, _short(orientation)),
        (277, SHORT, 1, _short(3)),
        (278, LONG, 1, _long(th)),
        (279, LONG, 1, _long(len(thumb_px))),
        (330, LONG, 1, _long(ifd1_at)),
    ]
    ifd1 = [
        (254, LONG, 1, _long(0)),
        (256, LONG, 1, _long(fw)),
        (257, LONG, 1, _long(fh)),
        (258, SHORT, 3, _long(bits1_at)),
        (259, SHORT, 1, _short(1)),
        (262, SHORT, 1, _short(full_photometric)),
        (273, LONG, strips, _long(offsets_at)),
        (277, SHORT, 1, _short(3)),
        (278, LONG, 1, _long(rows)),
        (279, LONG, strips, _long(counts_at)),
        (284, SHORT, 1, _short(1)),
    ]
    if linearization:
        ifd1.append((50712, SHORT, 256, _long(table_at)))

    def ifd(entries: list[tuple[int, int, int, bytes]]) -> bytes:
        body = b"".join(struct.pack("<HHI", tag, kind, count) + value for tag, kind, count, value in entries)
        return struct.pack("<H", len(entries)) + body + _long(0)

    blob = b"II*\x00" + _long(ifd0_at) + ifd(ifd0) + ifd(ifd1)
    blob += struct.pack("<3H", 8, 8, 8) * 2
    blob += b"".join(_long(o) for o in strip_offsets) + b"".join(_long(c) for c in strip_sizes)
    if linearization:
        blob += b"".join(struct.pack("<H", i * 257) for i in range(256))
    path.write_bytes(blob + thumb_px + full_px)
    return path


def halves(size: tuple[int, int], left: tuple[int, int, int], right: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", size, right)
    image.paste(left, (0, 0, size[0] // 2, size[1]))
    return image


def close(pixel: tuple[int, ...], color: tuple[int, int, int], tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], color, strict=True))


def test_scanner_dng_gives_its_full_scan_upright(tmp_path, data_dir):
    # Stored on its side like a VueScan scan: red left half, blue right half,
    # orientation "rotate 270 CW", so upright the red half is at the bottom.
    path = write_scanner_dng(
        tmp_path / "scan.dng", halves((60, 40), RED, BLUE), Image.new("RGB", (15, 10), GREEN), orientation=8
    )
    preview, created = pv.ensure_preview(path, large=True)
    assert created
    with Image.open(preview) as image:
        assert image.size == (40, 60)
        rgb = image.convert("RGB")
        assert close(rgb.getpixel((20, 50)), RED)
        assert close(rgb.getpixel((20, 10)), BLUE)


@pytest.mark.parametrize(
    "photometric, linearization",
    [(32803, True), (34892, False)],
    ids=["sensor-mosaic", "linear-without-table"],
)
def test_scanner_dng_falls_back_to_its_thumbnail(tmp_path, data_dir, photometric, linearization):
    # A mosaic, or truly linear data, cannot be shown as stored: the small
    # thumbnail stands in, turned upright the same way.
    path = write_scanner_dng(
        tmp_path / "scan.dng",
        halves((60, 40), RED, BLUE),
        halves((30, 20), GREEN, GREEN),
        orientation=8,
        full_photometric=photometric,
        linearization=linearization,
    )
    preview, _ = pv.ensure_preview(path)
    with Image.open(preview) as image:
        assert image.size == (20, 30)
        assert close(image.convert("RGB").getpixel((10, 15)), GREEN)


def test_unreadable_dng_has_no_preview(tmp_path, data_dir):
    path = tmp_path / "scan.dng"
    path.write_bytes(b"not a dng at all")
    with pytest.raises(pv.PreviewUnavailable):
        pv.ensure_preview(path)


def test_scanner_dng_thumbnail_and_viewer_routes(client, tmp_path):
    folder = tmp_path / "2014"
    folder.mkdir()
    write_scanner_dng(folder / "Scan-140108-0001.dng", halves((90, 60), RED, BLUE), Image.new("RGB", (15, 10), GREEN))
    params = {"folder": str(folder), "name": "Scan-140108-0001.dng"}

    built = client.post("/api/previews/create", json={"folder": str(folder), "invert": False})
    assert built.status_code == 200, built.text
    assert client.get("/api/files/thumbnail", params=params).status_code == 200
    large = client.get("/api/files/preview", params=params)
    assert large.status_code == 200
    assert large.headers["content-type"].startswith("image/jpeg")
