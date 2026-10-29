"""Match lab scans to the raws of the same roll by picture, and link them.

A roll scanned twice, once by the lab (positive JPEGs or TIFFs under the
lab's names, ``R1-09034-024A.JPG``) and once at home (raw negatives,
``DSC09040.ARW``), has two names for every frame scanned twice, and
nothing in the names says which goes with which: the home scan skips
frames, captures some twice, can run backwards through the roll and be
mirrored (shot through the base). The pictures say it.

Matching (``plan_matches``) proposes, it never acts:

- every frame becomes a small grid of tones: its preview cropped inside
  the film border, 24×24 grayscale, histogram-equalized, so a negative and
  its print differ only in the sign of their correlation;
- the roll's orientation (four turns, mirrored or not) and polarity are
  voted by the raws' best matches, then held for the whole roll;
- an in-order alignment, tried forwards and backwards through the lab
  frames, pairs the bulk and settles look-alikes (two shots of one scene);
- a raw the alignment left out takes its best lab frame when that is free
  and alike enough: a frame captured out of order;
- what is left says why: no picture (blank), another capture of a frame
  already paired, or nothing alike.

Linking (``apply_links``) renames each chosen lab frame's files to
``<raw stem>-<own name>`` (``DSC09040-R1-09034-024A.JPG``), so the frames
rule attaches them to the raw frame as its positive: its thumbnail, tagged
with it and moving with it, the lab's name still readable. Every rename
stays in the folder, is journaled and is undone by renaming back.
Lightroom knows files by path, so the UI says first that a catalog
holding them will show them as missing.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageOps
from sqlmodel import Session

from filmex.services import frames as fr
from filmex.services import manifest as mf
from filmex.services import operations, renditions
from filmex.services import previews as pv
from filmex.services.fileops import Cancelled, Move, PlanError, Progress, no_progress

KIND = "link"

GRID = 24
RAW_CROP = 0.86  # a home scan shows the film's border: keep the picture
LAB_CROP = 0.94  # a lab scan is cropped already: trim its edge
BLANK_SPREAD = 4.0  # tone spread (0-255) under which a frame shows nothing
VOTE_FLOOR = 0.6  # a raw votes for the roll's orientation with a match this alike
GAIN_FLOOR = 0.35  # the alignment pairs frames more alike than this
LIKELY = 0.6
SURE = 0.75
SAME_CAPTURE = 0.9  # two raws this alike are captures of one frame
CLOSER = 0.05  # a better match than the pair by more than this makes it unsure
LEAD = 0.15  # a faint pair still likely when no other lab frame comes this close

SURE_LEVEL, LIKELY_LEVEL, UNSURE_LEVEL = "sure", "likely", "unsure"
NO_PICTURE, ANOTHER_CAPTURE, NO_MATCH = "no_picture", "another_capture", "no_match"


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------- grids ----------


@dataclass(frozen=True)
class Grid:
    """A frame's tones as a unit vector (mean 0, length 1), so the dot
    product of two grids is their correlation, plus the tone spread before
    equalizing, which tells a blank frame."""

    values: tuple[float, ...]
    spread: float

    @property
    def blank(self) -> bool:
        return self.spread < BLANK_SPREAD


def grid_of(image: Image.Image, *, crop: float) -> Grid:
    gray = image.convert("L")
    w, h = gray.size
    dx, dy = w * (1 - crop) / 2, h * (1 - crop) / 2
    gray = gray.crop((round(dx), round(dy), round(w - dx), round(h - dy)))
    gray = gray.resize((GRID * 2, GRID * 2), Image.Resampling.BOX).filter(ImageFilter.GaussianBlur(1))
    gray = gray.resize((GRID, GRID), Image.Resampling.BOX)
    tones = list(gray.tobytes())
    mean = sum(tones) / len(tones)
    spread = math.sqrt(sum((t - mean) ** 2 for t in tones) / len(tones))
    return Grid(values=_unit(list(ImageOps.equalize(gray).tobytes())), spread=spread)


def _unit(values: list[int]) -> tuple[float, ...]:
    mean = sum(values) / len(values)
    centred = [v - mean for v in values]
    norm = math.sqrt(sum(c * c for c in centred))
    return tuple(c / norm for c in centred) if norm else tuple(0.0 for _ in centred)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# The eight ways a scan can lie against another: a left-right mirror or
# not, then 0-3 quarter turns counter-clockwise. "m90" is mirrored, then
# turned 90°.
ORIENTATIONS = tuple(f"{m}{d}" for m in ("", "m") for d in (0, 90, 180, 270))


def _permutation(name: str) -> list[int]:
    """For each cell of the grid turned ``name``, the cell it comes from."""
    n = GRID
    mirror = name.startswith("m")
    index = [r * n + (n - 1 - c if mirror else c) for r in range(n) for c in range(n)]
    for _ in range(int(name.lstrip("m")) // 90):
        index = [index[c * n + (n - 1 - r)] for r in range(n) for c in range(n)]
    return index


_PERMUTATIONS = {name: _permutation(name) for name in ORIENTATIONS}


def _turned(grid: Grid, name: str) -> tuple[float, ...]:
    return tuple(grid.values[i] for i in _PERMUTATIONS[name])


# ---------- matching ----------


@dataclass
class Pair:
    raw: str
    lab: str
    score: float
    level: str  # sure | likely | unsure
    note: str = ""


@dataclass
class Leftover:
    raw: str
    reason: str  # no_picture | another_capture | no_match
    lab: str | None = None  # its best lab frame
    score: float | None = None
    holder: str | None = None  # the raw that lab frame is paired with


@dataclass
class Matching:
    orientation: str  # how the raws lie against the lab scans, from ORIENTATIONS
    negative: bool  # the raws are negatives of the lab scans
    direction: str  # forward | reversed: the raws' capture order through the lab frames
    pairs: list[Pair]
    leftovers: list[Leftover]
    unmatched_labs: list[str]


def match_grids(raws: list[tuple[str, Grid]], labs: list[tuple[str, Grid]]) -> Matching:
    """Pair raw frames with lab frames by picture. Both lists come in their
    own capture order (the frames' name order)."""
    lab_grid = {key: grid for key, grid in labs}
    lab_keys = [key for key, grid in labs if not grid.blank]
    live = [(key, grid) for key, grid in raws if not grid.blank]
    raw_keys = [key for key, _ in live]
    turned = {key: {o: _turned(grid, o) for o in ORIENTATIONS} for key, grid in live}

    orientation, sign = _vote(turned, [lab_grid[k].values for k in lab_keys])
    sim = {(r, lab): sign * _dot(turned[r][orientation], lab_grid[lab].values) for r in raw_keys for lab in lab_keys}
    forward, backward = _align(raw_keys, lab_keys, sim), _align(raw_keys, lab_keys[::-1], sim)
    direction, aligned = ("forward", forward[1]) if forward[0] >= backward[0] else ("reversed", backward[1])

    paired = dict(aligned)  # raw -> lab
    holder = {lab: raw for raw, lab in aligned}
    best = {r: max(lab_keys, key=lambda lab, r=r: sim[r, lab]) for r in raw_keys} if lab_keys else {}
    rescued = _rescue([r for r in raw_keys if r not in paired], best, sim, paired, holder)

    def same_capture(a: str, b: str) -> bool:
        return _dot(turned[a]["0"], turned[b]["0"]) >= SAME_CAPTURE

    pairs = [_pair(r, paired[r], best[r], sim, holder, rescued, same_capture) for r in raw_keys if r in paired]
    leftovers = [Leftover(raw=key, reason=NO_PICTURE) for key, grid in raws if grid.blank]
    for r in raw_keys:
        if r in paired:
            continue
        lab = best.get(r)
        score = round(sim[r, lab], 2) if lab else None
        other = holder.get(lab) if lab else None
        if other and sim[r, lab] >= LIKELY and same_capture(r, other):
            leftovers.append(Leftover(raw=r, reason=ANOTHER_CAPTURE, lab=lab, score=score, holder=other))
        else:
            leftovers.append(Leftover(raw=r, reason=NO_MATCH, lab=lab, score=score, holder=other))
    order = {key: i for i, (key, _) in enumerate(raws)}
    leftovers.sort(key=lambda left: order[left.raw])
    return Matching(
        orientation=orientation,
        negative=sign < 0,
        direction=direction,
        pairs=pairs,
        leftovers=leftovers,
        unmatched_labs=[key for key, _ in labs if key not in holder],
    )


def _vote(turned: dict[str, dict[str, tuple[float, ...]]], labs: list[tuple[float, ...]]) -> tuple[str, int]:
    """The orientation and polarity (sign) most raws match their best lab
    frame in; upright negatives when nothing is alike enough to tell."""
    counts: dict[tuple[str, int], int] = {}
    for views in turned.values():
        best = (0.0, "0", -1)
        for orientation, view in views.items():
            for lab in labs:
                d = _dot(view, lab)
                if abs(d) > best[0]:
                    best = (abs(d), orientation, 1 if d > 0 else -1)
        if best[0] >= VOTE_FLOOR:
            counts[best[1:]] = counts.get(best[1:], 0) + 1
    if not counts:
        return "0", -1
    return max(counts, key=lambda k: (counts[k], k == ("0", -1)))


def _align(raws: list[str], labs: list[str], sim: dict[tuple[str, str], float]) -> tuple[float, list[tuple[str, str]]]:
    """Best in-order pairing of ``raws`` with ``labs`` where either side may
    skip frames; a pair is worth its likeness above GAIN_FLOOR."""
    rows, cols = len(raws), len(labs)
    score = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    step = [[""] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows + 1):
        for j in range(cols + 1):
            options = []
            if i:
                options.append((score[i - 1][j], "raw"))
            if j:
                options.append((score[i][j - 1], "lab"))
            if i and j:
                options.append((score[i - 1][j - 1] + sim[raws[i - 1], labs[j - 1]] - GAIN_FLOOR, "pair"))
            if options:
                score[i][j], step[i][j] = max(options)
    pairs: list[tuple[str, str]] = []
    i, j = rows, cols
    while i or j:
        if step[i][j] == "pair":
            pairs.append((raws[i - 1], labs[j - 1]))
            i, j = i - 1, j - 1
        elif step[i][j] == "raw":
            i -= 1
        else:
            j -= 1
    return score[rows][cols], pairs[::-1]


def _rescue(
    left_out: list[str],
    best: dict[str, str],
    sim: dict[tuple[str, str], float],
    paired: dict[str, str],
    holder: dict[str, str],
) -> set[str]:
    """Raws captured out of order: a raw the alignment skipped takes its
    best lab frame when that is alike enough and free, or held by a raw that
    matches it worse and looks more like another frame (the alignment filled
    a gap with it). Most alike first; a raw it displaces is left out."""
    rescued: set[str] = set()
    for r in sorted((r for r in left_out if r in best), key=lambda r: -sim[r, best[r]]):
        lab = best[r]
        if sim[r, lab] < LIKELY:
            continue
        other = holder.get(lab)
        if other is not None:
            if best[other] == lab or sim[other, lab] + CLOSER >= sim[r, lab]:
                continue
            del paired[other]
            rescued.discard(other)
        paired[r], holder[lab] = lab, r
        rescued.add(r)
    return rescued


def _pair(
    r: str,
    lab: str,
    best: str,
    sim: dict[tuple[str, str], float],
    holder: dict[str, str],
    rescued: set[str],
    same_capture: Callable[[str, str], bool],
) -> Pair:
    """How sure the pair is, and why not when it is not."""
    score = sim[r, lab]
    if best != lab and sim[r, best] > score + CLOSER:
        other = holder.get(best)
        note = f"looks more like {best} ({sim[r, best]:.2f})"
        if other:
            note += f", paired with {other}"
            if same_capture(r, other):
                note += ": probably another capture of it"
        return Pair(r, lab, round(score, 2), UNSURE_LEVEL, note)
    if r in rescued:
        return Pair(r, lab, round(score, 2), LIKELY_LEVEL, "captured out of order")
    if score >= SURE:
        return Pair(r, lab, round(score, 2), SURE_LEVEL)
    runner_up = max((s for (raw, other), s in sim.items() if raw == r and other != lab), default=0.0)
    if score >= LIKELY or score - runner_up >= LEAD:
        return Pair(r, lab, round(score, 2), LIKELY_LEVEL)
    return Pair(r, lab, round(score, 2), UNSURE_LEVEL, "not much alike")


# ---------- plan ----------


@dataclass
class MatchPlan:
    folder: str
    raw_count: int
    lab_count: int
    matching: Matching | None
    files: dict[str, list[dict[str, str]]]  # lab frame -> its files' new names, for each proposed pair
    problems: list[str]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "folder": self.folder,
            "raw_count": self.raw_count,
            "lab_count": self.lab_count,
            "problems": list(self.problems),
        }
        if self.matching:
            data.update(asdict(self.matching))
            for pair in data["pairs"]:
                pair["files"] = self.files.get(pair["lab"], [])
        return data


def plan_matches(folder: Path | str) -> MatchPlan:
    """Propose which lab frame of ``folder`` each raw frame shows. Frames
    already linked (``linked_scans`` in the manifest) take no part."""
    path = Path(folder).expanduser()
    listing = fr.list_frames(path)
    linked = {entry.get("raw") for entry in (_read_manifest_or_none(path) or {}).get("linked_scans", [])}
    raw_frames = [f for f in listing.frames if f.has_raw and f.key not in linked]
    lab_frames = [f for f in listing.frames if not f.has_raw]
    plan = MatchPlan(folder=str(path), raw_count=len(raw_frames), lab_count=len(lab_frames), matching=None, files={}, problems=[])
    if not raw_frames or not lab_frames:
        plan.problems.append("nothing to match: the folder needs both raw frames and lab scans without a raw")
        return plan

    raws = _grids(path, [(f.key, _raw_image(f)) for f in raw_frames], crop=RAW_CROP, problems=plan.problems)
    labs = _grids(path, [(f.key, f.scan_image) for f in lab_frames], crop=LAB_CROP, problems=plan.problems)
    plan.matching = match_grids(raws, labs)
    by_key = fr.frames_by_key(listing)
    for pair in plan.matching.pairs:
        plan.files[pair.lab] = [
            {"from": name, "to": fr.positive_name(pair.raw, name)} for name in by_key[pair.lab].files
        ]
    return plan


def _raw_image(frame: fr.Frame) -> str | None:
    return next((n for n in frame.images if os.path.splitext(n)[1].lower() in fr.RAW_EXTS), None)


def _grids(folder: Path, frames: list[tuple[str, str | None]], *, crop: float, problems: list[str]) -> list[tuple[str, Grid]]:
    """Each frame's grid, from the preview the contact sheet uses (built and
    cached when missing). A frame without a preview is left out and said."""
    grids: list[tuple[str, Grid]] = []
    for key, name in frames:
        if name is None:
            continue
        try:
            preview, _ = pv.ensure_preview(folder / name)
            with Image.open(preview) as image:
                grids.append((key, grid_of(image, crop=crop)))
        except (pv.PreviewUnavailable, OSError) as exc:
            problems.append(f"{name}: no preview to compare ({exc})")
    return grids


# ---------- link ----------


@dataclass
class Link:
    raw: str
    lab: str
    moves: list[Move] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "lab": self.lab, "moves": [m.to_dict() for m in self.moves]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Link:
        return cls(raw=data["raw"], lab=data["lab"], moves=[Move.from_dict(m) for m in data.get("moves", [])])


@dataclass
class LinkResult:
    folder: str
    links: list[Link] = field(default_factory=list)
    manifest_updated: bool = False
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "folder": self.folder,
            "links": [link.to_dict() for link in self.links],
            "manifest_updated": self.manifest_updated,
            "errors": list(self.errors),
            "cancelled": self.cancelled,
        }
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkResult:
        return cls(
            folder=data["folder"],
            links=[Link.from_dict(link) for link in data.get("links", [])],
            manifest_updated=bool(data.get("manifest_updated", False)),
            errors=list(data.get("errors", [])),
            cancelled=bool(data.get("cancelled", False)),
            operation_id=data.get("operation_id"),
        )


@dataclass
class LinkUndoResult:
    restored: list[Move] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"restored": [m.to_dict() for m in self.restored], "errors": list(self.errors)}
        if self.operation_id:
            data["operation_id"] = self.operation_id
        return data


def plan_links(folder: Path | str, chosen: list[dict[str, str]]) -> list[Link]:
    """The renames for the chosen ``{"raw", "lab"}`` pairs, checked against
    the folder as it is now. Any problem raises ``PlanError`` listing all of
    them, and nothing runs."""
    path = Path(folder).expanduser()
    by_key = fr.frames_by_key(fr.list_frames(path))
    problems: list[str] = []
    links: list[Link] = []
    raws_seen: set[str] = set()
    labs_seen: set[str] = set()
    targets: set[str] = set()
    for item in chosen:
        raw, lab = item.get("raw", ""), item.get("lab", "")
        raw_frame, lab_frame = by_key.get(raw), by_key.get(lab)
        if raw_frame is None or not raw_frame.has_raw:
            problems.append(f"{raw}: no raw frame of that name")
            continue
        if lab_frame is None or lab_frame.has_raw:
            problems.append(f"{lab}: no lab scan of that name")
            continue
        if raw in raws_seen or lab in labs_seen:
            problems.append(f"{raw} and {lab}: chosen twice")
            continue
        raws_seen.add(raw)
        labs_seen.add(lab)
        link = Link(raw=raw, lab=lab)
        for name in lab_frame.files:
            new = fr.positive_name(raw, name)
            target = path / new
            if new in targets or target.exists() or target.is_symlink():
                problems.append(f"{new}: already exists")
                continue
            targets.add(new)
            link.moves.append(Move(source=str(path / name), target=str(target)))
        links.append(link)
    if not chosen:
        problems.append("no pairs chosen")
    if problems:
        raise PlanError(problems)
    return links


def apply_links(
    session: Session,
    folder: Path | str,
    chosen: list[dict[str, str]],
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> LinkResult:
    """Rename each chosen lab frame's files so they join their raw frame,
    rename them in the manifest too, journal it. Planned again right
    before running."""
    path = Path(folder).expanduser()
    links = plan_links(path, chosen)
    emit = progress or no_progress
    op = operations.start(session, KIND, {"folder": str(path), "links": [link.to_dict() for link in links]})
    result = LinkResult(folder=str(path))
    total = sum(len(link.moves) for link in links)
    done = 0
    for link in links:
        if cancelled():
            result.cancelled = True
            result.errors.append("cancelled before every lab scan was linked")
            break
        moved = Link(raw=link.raw, lab=link.lab)
        for move in link.moves:
            name = Path(move.source).name
            try:
                _rename(Path(move.source), Path(move.target))
            except OSError as exc:
                result.errors.append(f"{name}: {exc}")
                emit({"type": "progress", "step": "link", "file": name, "ok": False, "error": str(exc)})
                continue
            moved.moves.append(move)
            done += 1
            emit({"type": "progress", "step": "link", "file": name, "to": Path(move.target).name, "done": done, "total": total, "ok": True})
        if moved.moves:
            result.links.append(moved)
    result.manifest_updated = _record_links(path, result.links)
    status = operations.FAILED if result.errors and not result.links else operations.DONE
    operations.finish(session, op.id, status, result.to_dict())
    result.operation_id = op.id
    return result


def undo_links(
    session: Session,
    op_id: str,
    *,
    progress: Progress | None = None,
    cancelled: Cancelled = lambda: False,
) -> LinkUndoResult:
    """Give every linked lab scan its own name back, in the manifest too."""
    entry = operations.load_for_undo(session, op_id, KIND)
    recorded = LinkResult.from_dict(entry["result"])
    emit = progress or no_progress
    undo = LinkUndoResult(operation_id=op_id)
    for link in reversed(recorded.links):
        for move in reversed(link.moves):
            if cancelled():
                undo.errors.append("cancelled before every lab scan got its name back")
                break
            current, original = Path(move.target), Path(move.source)
            try:
                if not current.exists():
                    raise OSError("the linked file is no longer there")
                _rename(current, original)
            except OSError as exc:
                undo.errors.append(f"{current.name}: {exc}")
                emit({"type": "progress", "step": "link", "file": current.name, "ok": False, "error": str(exc)})
                continue
            undo.restored.append(Move(source=str(current), target=str(original)))
            emit({"type": "progress", "step": "link", "file": current.name, "to": original.name, "ok": True})
    _unrecord_links(Path(recorded.folder), undo.restored)
    if undo.restored:
        operations.mark_undone(session, op_id, undo.to_dict())
    return undo


# ---------- helpers ----------


def _rename(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        raise OSError(f"{dst.name}: target already exists")
    os.rename(src, dst)  # inside one folder: atomic, never across volumes


def _read_manifest_or_none(folder: Path) -> dict[str, Any] | None:
    try:
        return mf.read_manifest(folder)
    except (OSError, ValueError):
        return None


def _record_links(folder: Path, links: list[Link]) -> bool:
    """The manifest follows the renames (tagged files, per-file overrides)
    and lists the links under ``linked_scans``. A folder never tagged gets a
    manifest holding only that: tagging later keeps it (FOLDER_HISTORY_KEYS)."""
    if not links:
        return False
    manifest = _read_manifest_or_none(folder) or _links_only_manifest(folder)
    stamp = _now()
    entries = []
    for link in links:
        names = {Path(m.source).name: Path(m.target).name for m in link.moves}
        entries.append({"raw": link.raw, "lab": link.lab, "files": names, "timestamp": stamp})
    _rename_files_in_manifest(manifest, {old: new for e in entries for old, new in e["files"].items()})
    manifest.setdefault("linked_scans", []).extend(entries)
    manifest["updated_at"] = stamp
    mf.write_manifest(folder, manifest)
    return True


def _unrecord_links(folder: Path, restored: list[Move]) -> None:
    manifest = _read_manifest_or_none(folder)
    if not manifest or not restored:
        return
    back = {Path(m.source).name: Path(m.target).name for m in restored}
    _rename_files_in_manifest(manifest, back)
    kept = [e for e in manifest.get("linked_scans", []) if not set(e.get("files", {}).values()) & set(back)]
    if kept:
        manifest["linked_scans"] = kept
    else:
        manifest.pop("linked_scans", None)
    if set(manifest) <= _LINKS_ONLY_KEYS and not manifest.get("apply_history"):
        # The manifest only ever held these links: the folder goes back to having none.
        mf.manifest_path_for(folder).unlink(missing_ok=True)
        return
    manifest["updated_at"] = _now()
    mf.write_manifest(folder, manifest)


_LINKS_ONLY_KEYS = frozenset({"manifest_version", "roll_id", "folder_relative_path", "created_at", "updated_at", "apply_history", "linked_scans"})


def _links_only_manifest(folder: Path) -> dict[str, Any]:
    stamp = _now()
    return {
        "manifest_version": mf.MANIFEST_VERSION,
        "roll_id": renditions.roll_id_for(folder),
        "folder_relative_path": folder.name,
        "created_at": stamp,
        "updated_at": stamp,
        "apply_history": [],
    }


def _rename_files_in_manifest(manifest: dict[str, Any], names: dict[str, str]) -> None:
    for entry in manifest.get("apply_history", []):
        entry["applied_files"] = [names.get(n, n) for n in entry.get("applied_files", [])]
        for diff in entry.get("diffs", []):
            if diff.get("file") in names:
                diff["file"] = names[diff["file"]]
    overrides = manifest.get("file_overrides")
    if overrides:
        manifest["file_overrides"] = {names.get(n, n): v for n, v in overrides.items()}
