"""Suggested cuts: where one scan session's frames probably change roll.

Three signals, from strongest to weakest:

- **Counter reset**: the number at the end of the file name goes down
  (``000036`` → ``000001``, ``R1-36`` → ``R2-01``). Lab scanners restart
  numbering per roll; a camera's counter never goes backwards.
- **Aspect change**: the frame shape changes (3:2 to 1:1, 35 mm to 120).
- **Time gap**: a long pause between two frames. Changing rolls on a
  scanner takes minutes; frames within a roll follow in seconds. A pause
  alone is ambiguous (a coffee break looks the same), so a gap is weighed
  by the **frames-per-roll rhythm**: a gap 36 frames after the last cut is
  a new roll, a gap 12 frames in is probably a break.

Every suggestion is a marker the user confirms on the seam (last frame of
one roll next to the first of the next); nothing is cut automatically.

Facts come from one ExifTool run over the whole folder (capture time and
pixel size for JPEG, TIFF and raw alike). Without ExifTool, Pillow reads
the size of JPEG and TIFF files and the file's modification time stands in
for the capture time; a copy made with Finder or ``cp -p`` keeps it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image

from filmex.services import exiftool_locator as locator
from filmex.services import frames as fr

SURE = "sure"
LIKELY = "likely"
MAYBE = "maybe"

# Plausible roll lengths around a nominal count: 135 film gives 36 to 38
# frames, 24-exposure rolls 24 to 26, 120 film 12/15/16.
_ROLL_SLACK = (0, 1, 2)
# A pause counts as a gap when it is both this much longer than the typical
# spacing and at least this long. Below that it is just slow scanning.
_GAP_RATIO = 5.0
_GAP_MIN_SECONDS = 60.0
# A pause this long is a new roll whatever the rhythm says.
_GAP_SURE_SECONDS = 30 * 60.0
# Aspect ratios closer than this count as the same frame shape.
_ASPECT_TOLERANCE = 0.08

_COUNTER_RE = re.compile(r"(\d+)$")
_EXIF_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")


@dataclass
class FrameFact:
    key: str
    taken_at: float | None = None  # seconds since the epoch
    time_source: str | None = None  # "exif" | "file"
    width: int | None = None
    height: int | None = None
    counter: int | None = None

    @property
    def aspect(self) -> float | None:
        if not self.width or not self.height:
            return None
        return max(self.width, self.height) / min(self.width, self.height)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aspect"] = round(self.aspect, 3) if self.aspect else None
        return data


@dataclass
class Cut:
    """A new roll would start at ``key`` (the frame after the seam)."""

    key: str
    previous: str
    index: int  # sheet position of ``key``
    strength: str  # sure | likely | maybe
    frames_before: int  # frames since the previous suggested cut (or the start)
    reasons: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CutReport:
    cuts: list[Cut]
    typical_gap_seconds: float | None
    time_source: str | None  # "exif" | "file" | "mixed" | None
    frames_per_roll: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cuts": [c.to_dict() for c in self.cuts],
            "typical_gap_seconds": self.typical_gap_seconds,
            "time_source": self.time_source,
            "frames_per_roll": self.frames_per_roll,
        }


# ---------- facts ----------


def frame_facts(folder: Path, frames: list[fr.Frame]) -> list[FrameFact]:
    """One fact per frame, read from its preview image (JPEG over raw)."""
    paths = {f.key: folder / f.scan_image for f in frames if f.scan_image}
    exif = read_exif_facts(list(paths.values()))
    facts: list[FrameFact] = []
    for frame in frames:
        fact = FrameFact(key=frame.key, counter=counter_of(frame.key))
        path = paths.get(frame.key)
        if path is not None:
            row = exif.get(str(path), {})
            _fill_from_exif(fact, row)
            if fact.width is None:
                _fill_size_with_pillow(fact, path)
            if fact.taken_at is None:
                _fill_time_from_file(fact, path)
        facts.append(fact)
    return facts


def read_exif_facts(paths: list[Path]) -> dict[str, dict[str, Any]]:
    """Capture time, size and orientation for many files in one ExifTool run,
    keyed by path. Empty when ExifTool is missing or fails."""
    command = locator.command()
    if command is None or not paths:
        return {}
    argv = [
        *command, "-j", "-n", "-fast2", "-charset", "filename=utf8",
        "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight", "-Orientation",
        "--", *[str(p) for p in paths],
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, check=False, timeout=120)
        rows = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    return {str(Path(row.get("SourceFile", ""))): row for row in rows if isinstance(row, dict)}


def _fill_from_exif(fact: FrameFact, row: dict[str, Any]) -> None:
    stamp = parse_exif_datetime(row.get("DateTimeOriginal") or row.get("CreateDate"))
    if stamp is not None:
        fact.taken_at, fact.time_source = stamp, "exif"
    width, height = row.get("ImageWidth"), row.get("ImageHeight")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        if int(row.get("Orientation") or 1) in (5, 6, 7, 8):
            width, height = height, width
        fact.width, fact.height = width, height


def _fill_size_with_pillow(fact: FrameFact, path: Path) -> None:
    if path.suffix.lower() not in fr.JPEG_EXTS | fr.TIFF_EXTS:
        return
    try:
        with Image.open(path) as image:
            fact.width, fact.height = image.size
    except Exception:  # noqa: BLE001 - an unreadable file simply has no size
        return


def _fill_time_from_file(fact: FrameFact, path: Path) -> None:
    try:
        fact.taken_at, fact.time_source = os.stat(path).st_mtime, "file"
    except OSError:
        return


def parse_exif_datetime(value: Any) -> float | None:
    """``YYYY:MM:DD HH:MM:SS`` (sub-seconds and zone ignored) as epoch seconds,
    in local time, which is how a scanner stamps it."""
    if not isinstance(value, str):
        return None
    match = _EXIF_DATE_RE.match(value.strip())
    if not match:
        return None
    try:
        return datetime(*(int(g) for g in match.groups())).timestamp()
    except ValueError:
        return None


def counter_of(key: str) -> int | None:
    match = _COUNTER_RE.search(key)
    return int(match.group(1)) if match else None


# ---------- suggestions ----------


def suggest_cuts(facts: list[FrameFact], *, frames_per_roll: int = 36) -> CutReport:
    """Walk the frames in sheet order and mark where a new roll probably
    starts. ``frames_per_roll`` of 0 turns the rhythm off."""
    typical = typical_gap(facts)
    cuts: list[Cut] = []
    last_cut = 0  # sheet index where the current roll started
    for i in range(1, len(facts)):
        prev, cur = facts[i - 1], facts[i]
        reasons: list[dict[str, Any]] = []
        strength: str | None = None

        if prev.counter is not None and cur.counter is not None and cur.counter < prev.counter:
            reasons.append({"kind": "counter_reset", "from": prev.counter, "to": cur.counter})
            strength = SURE
        if prev.aspect and cur.aspect and abs(cur.aspect - prev.aspect) / prev.aspect > _ASPECT_TOLERANCE:
            reasons.append({"kind": "aspect", "from": _aspect_label(prev.aspect), "to": _aspect_label(cur.aspect)})
            strength = SURE

        gap = _gap_seconds(prev, cur)
        if gap is not None and typical is not None and gap >= max(_GAP_RATIO * typical, _GAP_MIN_SECONDS):
            frames_before = i - last_cut
            fit = rhythm_fit(frames_before, frames_per_roll)
            reasons.append({"kind": "gap", "seconds": round(gap), "frames_before": frames_before, "rhythm_fit": round(fit, 2)})
            if strength is None:
                if gap >= _GAP_SURE_SECONDS or fit >= 0.8:
                    strength = LIKELY
                else:
                    strength = MAYBE

        if strength is None:
            continue
        cuts.append(Cut(key=cur.key, previous=prev.key, index=i, strength=strength, frames_before=i - last_cut, reasons=reasons))
        if strength != MAYBE:
            last_cut = i

    return CutReport(cuts=cuts, typical_gap_seconds=typical, time_source=_time_source(facts), frames_per_roll=frames_per_roll)


def typical_gap(facts: list[FrameFact]) -> float | None:
    """Median spacing between consecutive frames: the scanner's pace."""
    gaps = [g for g in (_gap_seconds(a, b) for a, b in zip(facts, facts[1:], strict=False)) if g is not None and g >= 0]
    return median(gaps) if gaps else None


def rhythm_fit(frames_before: int, frames_per_roll: int) -> float:
    """1.0 when ``frames_before`` is a plausible roll length for the rhythm,
    falling off linearly; 1.0 when the rhythm is off."""
    if frames_per_roll <= 0:
        return 1.0
    lengths = {frames_per_roll + s for s in _ROLL_SLACK}
    if frames_per_roll == 36:
        lengths |= {24 + s for s in _ROLL_SLACK}  # a 24-exposure roll in a 36 session
    best = 0.0
    for length in lengths:
        best = max(best, 1.0 - abs(frames_before - length) / length)
    return max(0.0, best)


def _gap_seconds(a: FrameFact, b: FrameFact) -> float | None:
    if a.taken_at is None or b.taken_at is None:
        return None
    return b.taken_at - a.taken_at


def _aspect_label(aspect: float) -> str:
    for label, value in (("1:1", 1.0), ("5:4", 1.25), ("4:3", 1.333), ("7:6", 1.167), ("3:2", 1.5), ("16:9", 1.778), ("2:1", 2.0), ("3:1", 3.0)):
        if abs(aspect - value) / value <= _ASPECT_TOLERANCE / 2:
            return label
    return f"{aspect:.2f}:1"


def _time_source(facts: list[FrameFact]) -> str | None:
    sources = {f.time_source for f in facts if f.time_source}
    if not sources:
        return None
    return next(iter(sources)) if len(sources) == 1 else "mixed"
