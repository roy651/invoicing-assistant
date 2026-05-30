"""
Tests for imap_fetch.watermark.

Watermark load/save round-trips, cold-start (no file), corrupt file graceful
degradation, and UTC normalisation.
"""

from __future__ import annotations

from datetime import datetime, timezone


from imap_fetch.watermark import load_watermark, save_watermark


def test_cold_start_returns_none(tmp_path):
    """No watermark file → returns None (cold start)."""
    result = load_watermark(state_dir=tmp_path)
    assert result is None


def test_save_then_load_round_trips(tmp_path):
    wm = datetime(2026, 4, 30, 18, 0, 0, tzinfo=timezone.utc)
    save_watermark(wm, state_dir=tmp_path)
    loaded = load_watermark(state_dir=tmp_path)
    assert loaded is not None
    assert loaded == wm


def test_load_normalises_to_utc(tmp_path):
    """A watermark stored with a non-UTC timezone is normalised to UTC on load."""
    from datetime import timedelta

    tz_plus3 = timezone(timedelta(hours=3))
    wm_local = datetime(2026, 4, 30, 21, 0, 0, tzinfo=tz_plus3)
    save_watermark(wm_local, state_dir=tmp_path)
    loaded = load_watermark(state_dir=tmp_path)
    assert loaded is not None
    assert loaded.tzinfo == timezone.utc
    assert loaded.hour == 18  # 21:00+03:00 → 18:00 UTC


def test_save_creates_state_dir(tmp_path):
    state_dir = tmp_path / "nested" / "state"
    wm = datetime(2026, 5, 1, tzinfo=timezone.utc)
    save_watermark(wm, state_dir=state_dir)
    assert state_dir.exists()
    assert (state_dir / "imap_watermark.json").exists()


def test_corrupt_watermark_file_returns_none(tmp_path):
    """A corrupt watermark file must not crash — returns None (cold start)."""
    (tmp_path / "imap_watermark.json").write_text(
        "NOT VALID JSON {{{", encoding="utf-8"
    )
    result = load_watermark(state_dir=tmp_path)
    assert result is None


def test_watermark_file_format(tmp_path):
    """Watermark file must contain 'watermark_utc' key in ISO format."""
    import json

    wm = datetime(2026, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    save_watermark(wm, state_dir=tmp_path)
    data = json.loads((tmp_path / "imap_watermark.json").read_text())
    assert "watermark_utc" in data
    assert "2026-06-15" in data["watermark_utc"]
