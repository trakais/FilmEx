"""Tagging plan values that are easy to get subtly wrong."""

from __future__ import annotations

from filmex.services.tagging import CaptureDate


def test_capture_date_forms():
    date = CaptureDate("2026-05-14")
    assert date.exif == "2026:05:14 12:00:00"  # noon local stands in for day precision
    assert date.precision == "day" and date.source == "user_override"
    none = CaptureDate()
    assert none.exif is None and none.precision == "unknown" and none.source is None
