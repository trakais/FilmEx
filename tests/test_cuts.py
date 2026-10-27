"""Cut suggestions: gaps weighed by the frames-per-roll rhythm, counter
resets, aspect changes, and the facts they are computed from."""

from __future__ import annotations

import os
from pathlib import Path

from filmex.services import cuts
from filmex.services import frames as fr
from filmex.services.cuts import FrameFact


def _session(pace: float = 8.0, breaks: dict[int, float] | None = None, count: int = 72, start: float = 1_700_000_000.0):
    """``count`` frames scanned ``pace`` seconds apart; ``breaks`` maps a
    frame index to the pause before it, in seconds."""
    facts = []
    t = start
    for i in range(count):
        if breaks and i in breaks:
            t += breaks[i]
        facts.append(FrameFact(key=f"scan_{i + 1:03d}", taken_at=t, time_source="exif", width=3000, height=2000, counter=i + 1))
        t += pace
    return facts


def test_gap_after_a_full_roll_is_a_likely_cut():
    report = cuts.suggest_cuts(_session(breaks={36: 10 * 60}), frames_per_roll=36)
    assert [(c.key, c.strength, c.frames_before) for c in report.cuts] == [("scan_037", "likely", 36)]
    assert report.cuts[0].reasons[0]["kind"] == "gap"
    assert report.cuts[0].reasons[0]["seconds"] == 600 + 8  # the pause plus the normal pace
    assert report.typical_gap_seconds == 8.0
    assert report.time_source == "exif"


def test_a_break_inside_a_roll_is_only_a_maybe_and_does_not_reset_the_rhythm():
    report = cuts.suggest_cuts(_session(breaks={12: 10 * 60, 36: 10 * 60}), frames_per_roll=36)
    assert [(c.key, c.strength, c.frames_before) for c in report.cuts] == [("scan_013", "maybe", 12), ("scan_037", "likely", 36)]


def test_a_very_long_pause_is_likely_whatever_the_rhythm_says():
    report = cuts.suggest_cuts(_session(breaks={12: 2 * 3600}), frames_per_roll=36)
    assert [(c.key, c.strength) for c in report.cuts] == [("scan_013", "likely")]


def test_rhythm_off_takes_every_real_gap():
    report = cuts.suggest_cuts(_session(breaks={12: 10 * 60, 36: 10 * 60}), frames_per_roll=0)
    assert [(c.key, c.strength) for c in report.cuts] == [("scan_013", "likely"), ("scan_037", "likely")]


def test_slow_scanning_is_not_a_gap():
    # Two minutes between frames, one three-minute pause: within the pace.
    report = cuts.suggest_cuts(_session(pace=120, breaks={36: 180}, count=48), frames_per_roll=36)
    assert report.cuts == []


def test_counter_reset_and_aspect_change_are_sure_cuts():
    facts = _session(count=40)
    for i in range(36, 40):  # the lab's second roll restarts at 000001 and is square
        facts[i] = FrameFact(key=f"roll2_{i - 35:06d}", taken_at=facts[i].taken_at, time_source="exif", width=2400, height=2400, counter=i - 35)
    report = cuts.suggest_cuts(facts, frames_per_roll=36)
    assert len(report.cuts) == 1
    cut = report.cuts[0]
    assert cut.key == "roll2_000001" and cut.strength == "sure" and cut.frames_before == 36
    assert [r["kind"] for r in cut.reasons] == ["counter_reset", "aspect"]
    assert cut.reasons[0] == {"kind": "counter_reset", "from": 36, "to": 1}
    assert cut.reasons[1] == {"kind": "aspect", "from": "3:2", "to": "1:1"}


def test_without_times_only_the_sure_signals_fire():
    facts = [FrameFact(key=f"DSC{i:04d}", counter=i) for i in (1, 2, 3)]
    report = cuts.suggest_cuts(facts, frames_per_roll=36)
    assert report.cuts == [] and report.typical_gap_seconds is None and report.time_source is None
    facts.append(FrameFact(key="DSC0000", counter=0))
    assert [(c.key, c.strength) for c in cuts.suggest_cuts(facts).cuts] == [("DSC0000", "sure")]
    # A frame without a counter is no evidence either way.
    facts.append(FrameFact(key="frame"))
    facts.append(FrameFact(key="DSC0000b", counter=0))
    assert len(cuts.suggest_cuts(facts).cuts) == 1


def test_rhythm_fit_values():
    assert cuts.rhythm_fit(36, 36) == 1.0
    assert cuts.rhythm_fit(38, 36) == 1.0
    assert cuts.rhythm_fit(24, 36) == 1.0  # a 24-exposure roll in a 36 session
    assert cuts.rhythm_fit(12, 36) == 0.5  # half a 24-exposure roll: a break, not a roll
    assert cuts.rhythm_fit(5, 36) < 0.25
    assert cuts.rhythm_fit(7, 0) == 1.0


def test_counter_and_exif_date_parsing():
    assert cuts.counter_of("000036") == 36
    assert cuts.counter_of("R2-01") == 1
    assert cuts.counter_of("DSC0001") == 1
    assert cuts.counter_of("frame") is None
    assert cuts.parse_exif_datetime("2026:05:14 12:30:05") is not None
    assert cuts.parse_exif_datetime("2026:05:14 12:30:05.123+02:00") == cuts.parse_exif_datetime("2026:05:14 12:30:05")
    assert cuts.parse_exif_datetime("2026-05-14") is None
    assert cuts.parse_exif_datetime(None) is None


def test_frame_facts_fall_back_to_pillow_and_file_times(tmp_path: Path, make_jpeg, monkeypatch):
    folder = tmp_path / "session"
    base = 1_700_000_000
    for i, (name, edge) in enumerate([("a_001.jpg", 300), ("a_002.jpg", 300), ("b_001.jpg", 200)]):
        path = make_jpeg(name, long_edge=edge, folder=folder)
        os.utime(path, (base + i * 8, base + i * 8))
    monkeypatch.setattr(cuts, "read_exif_facts", lambda paths: {})
    facts = cuts.frame_facts(folder, fr.list_frames(folder).frames)
    assert [(f.key, f.time_source, f.width, f.counter) for f in facts] == [
        ("a_001", "file", 300, 1), ("a_002", "file", 300, 2), ("b_001", "file", 200, 1),
    ]
    assert facts[1].taken_at - facts[0].taken_at == 8
    report = cuts.suggest_cuts(facts)
    assert [(c.key, c.strength) for c in report.cuts] == [("b_001", "sure")]
    assert report.cuts[0].reasons[0]["kind"] == "counter_reset"


def test_exif_rows_override_file_facts(tmp_path: Path, make_jpeg, monkeypatch):
    folder = tmp_path / "session"
    path = make_jpeg("DSC0001.jpg", long_edge=300, folder=folder)
    monkeypatch.setattr(cuts, "read_exif_facts", lambda paths: {
        str(path): {"SourceFile": str(path), "DateTimeOriginal": "2026:05:14 12:00:00", "ImageWidth": 6000, "ImageHeight": 4000, "Orientation": 6},
    })
    fact = cuts.frame_facts(folder, fr.list_frames(folder).frames)[0]
    assert fact.time_source == "exif"
    assert (fact.width, fact.height) == (4000, 6000)  # rotated by the orientation tag
    assert fact.to_dict()["aspect"] == 1.5
