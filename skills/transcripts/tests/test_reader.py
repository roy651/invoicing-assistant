"""
Tests for transcript_reader.reader.

All file I/O uses tmp_path — no fixtures, no real transcripts.

Coverage:
- read_folder: discovery, since filter, sort order, bad-file skip
- Date extraction: filename (YYYY-MM-DD, YYYYMMDD), VTT header, mtime fallback
- VTT parsing: speaker attribution, cue-time stripping, participant list
- Plain-text parsing: speaker extraction, pass-through
- Markdown: treated as plain text
- EvidenceRecord shape: id, thread_id, source, participants, body_text, filename
- ID stability: same file → same id across multiple read_folder calls
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from transcript_reader.reader import (
    _date_from_filename,
    _extract_plain_text_participants,
    _parse_vtt,
    read_folder,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


def _write(tmp_path: Path, name: str, content: str) -> Path:
    """Write a transcript file and return its path."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


_SAMPLE_VTT = """\
WEBVTT

00:00:00.000 --> 00:00:05.000
Roy Abitbol: Let's start the call.

00:00:05.500 --> 00:00:10.000
Client Name: Sure, I wanted to discuss the website.

00:00:10.500 --> 00:00:15.000
Roy Abitbol: Great, it's almost done.
"""

_SAMPLE_TXT = """\
Roy Abitbol: So the logo is finished.
Client Name: Looks great!
Roy Abitbol: I'll send you the invoice by end of month.
"""

_SAMPLE_MD = """\
# Call notes — 2026-05-01

Discussed website scope. Client confirmed the scrollable design is approved.
Invoice to be sent for 70% completion.
"""


# ── read_folder ───────────────────────────────────────────────────────────────


def test_read_folder_finds_txt_vtt_md(tmp_path):
    _write(tmp_path, "2026-05-01_call.txt", _SAMPLE_TXT)
    _write(tmp_path, "2026-05-02_meeting.vtt", _SAMPLE_VTT)
    _write(tmp_path, "2026-05-03_notes.md", _SAMPLE_MD)
    _write(tmp_path, "ignore_me.csv", "not a transcript")

    records = read_folder(tmp_path)
    assert len(records) == 3


def test_read_folder_ignores_unsupported_extensions(tmp_path):
    _write(tmp_path, "file.pdf", "pdf content")
    _write(tmp_path, "file.docx", "docx content")
    _write(tmp_path, "file.txt", "some text")
    records = read_folder(tmp_path)
    assert len(records) == 1


def test_read_folder_sorted_ascending_by_date(tmp_path):
    _write(tmp_path, "2026-05-03_late.txt", "late")
    _write(tmp_path, "2026-05-01_early.txt", "early")
    _write(tmp_path, "2026-05-02_middle.txt", "middle")
    records = read_folder(tmp_path)
    assert [r.filename for r in records] == [
        "2026-05-01_early.txt",
        "2026-05-02_middle.txt",
        "2026-05-03_late.txt",
    ]


def test_read_folder_since_filter(tmp_path):
    _write(tmp_path, "2026-04-30_old.txt", "old")
    _write(tmp_path, "2026-05-01_new.txt", "new")
    _write(tmp_path, "2026-05-02_newer.txt", "newer")
    cutoff = datetime(2026, 4, 30, tzinfo=timezone.utc)
    records = read_folder(tmp_path, since=cutoff)
    assert len(records) == 2
    assert all(r.date > cutoff for r in records)


def test_read_folder_empty_dir_returns_empty(tmp_path):
    assert read_folder(tmp_path) == []


def test_read_folder_raises_on_missing_dir():
    with pytest.raises((ValueError, FileNotFoundError)):
        read_folder("/nonexistent/path/xyz")


def test_read_folder_skips_bad_file(tmp_path):
    """A file that can't be parsed must not crash the whole batch."""
    _ = _write(tmp_path, "2026-05-01_good.txt", "Good content")
    bad = tmp_path / "2026-05-02_bad.txt"
    bad.write_bytes(b"\xff\xfe\x00")  # not valid UTF-8 or UTF-16 plain text
    # Should not raise; at least the good file must come through.
    records = read_folder(tmp_path)
    filenames = [r.filename for r in records]
    assert "2026-05-01_good.txt" in filenames


def test_read_folder_recursive(tmp_path):
    sub = tmp_path / "month"
    sub.mkdir()
    _write(tmp_path, "2026-05-01_root.txt", "root")
    _write(sub, "2026-05-02_sub.txt", "sub")
    records_flat = read_folder(tmp_path, recursive=False)
    records_recursive = read_folder(tmp_path, recursive=True)
    assert len(records_flat) == 1
    assert len(records_recursive) == 2


# ── EvidenceRecord shape ──────────────────────────────────────────────────────


def test_evidence_record_shape_txt(tmp_path):
    _write(tmp_path, "2026-05-15_call.txt", _SAMPLE_TXT)
    records = read_folder(tmp_path)
    r = records[0]
    assert r.id == "transcripts/2026-05-15_call"
    assert r.thread_id == r.id  # transcripts are not threaded
    assert r.source == "transcript"
    assert r.filename == "2026-05-15_call.txt"
    assert r.date == datetime(2026, 5, 15, tzinfo=timezone.utc)
    assert "logo is finished" in r.body_text


def test_evidence_record_id_is_stable(tmp_path):
    """Same file → same id across multiple read_folder() calls."""
    _write(tmp_path, "2026-05-01_stable.txt", "content")
    r1 = read_folder(tmp_path)[0]
    r2 = read_folder(tmp_path)[0]
    assert r1.id == r2.id


def test_evidence_record_source_is_transcript(tmp_path):
    _write(tmp_path, "2026-05-01_call.txt", "anything")
    records = read_folder(tmp_path)
    assert records[0].source == "transcript"


# ── date extraction ───────────────────────────────────────────────────────────


def test_date_from_filename_dashes():
    dt = _date_from_filename("2026-04-15_client-call")
    assert dt == datetime(2026, 4, 15, tzinfo=timezone.utc)


def test_date_from_filename_compact():
    dt = _date_from_filename("20260415_zoom_transcript")
    assert dt == datetime(2026, 4, 15, tzinfo=timezone.utc)


def test_date_from_filename_prefix():
    dt = _date_from_filename("2026-05-01")
    assert dt == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_date_from_filename_no_date():
    assert _date_from_filename("client_call_notes") is None


def test_date_from_filename_invalid_date():
    assert _date_from_filename("2026-99-01_bad") is None


def test_date_fallback_to_mtime(tmp_path):
    """Files with no date in name use mtime."""
    _ = _write(tmp_path, "nodatename.txt", "some content")
    records = read_folder(tmp_path)
    assert records[0].date.tzinfo is not None
    # Mtime should be close to now.
    delta = abs((records[0].date - datetime.now(tz=timezone.utc)).total_seconds())
    assert delta < 10


def test_date_from_vtt_header(tmp_path):
    vtt_with_header = (
        "WEBVTT 2026-03-10T09:00:00\n\n00:00:01.000 --> 00:00:02.000\nHello\n"
    )
    _write(tmp_path, "no_date_in_name.vtt", vtt_with_header)
    records = read_folder(tmp_path)
    assert records[0].date == datetime(2026, 3, 10, 9, 0, 0, tzinfo=timezone.utc)


# ── VTT parsing ───────────────────────────────────────────────────────────────


def test_vtt_strips_cue_timestamps():
    body, _ = _parse_vtt(_SAMPLE_VTT)
    assert "-->" not in body


def test_vtt_preserves_speaker_attribution():
    body, _ = _parse_vtt(_SAMPLE_VTT)
    assert "Roy Abitbol: Let's start the call." in body
    assert "Client Name: Sure" in body


def test_vtt_extracts_participants():
    _, participants = _parse_vtt(_SAMPLE_VTT)
    assert "Roy Abitbol" in participants
    assert "Client Name" in participants


def test_vtt_participants_in_order_of_first_appearance():
    _, participants = _parse_vtt(_SAMPLE_VTT)
    assert participants.index("Roy Abitbol") < participants.index("Client Name")


def test_vtt_no_cue_ids_in_body():
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nSpeaker: Text\n"
    body, _ = _parse_vtt(vtt)
    # The cue id "1" must not appear as a standalone line in body.
    body_lines = [line.strip() for line in body.split("\n") if line.strip()]
    assert "1" not in body_lines


def test_vtt_empty_file_produces_empty_body():
    body, participants = _parse_vtt("WEBVTT\n")
    assert body == ""
    assert participants == []


def test_vtt_no_speaker_prefix_passes_through():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nThis has no speaker prefix.\n"
    body, participants = _parse_vtt(vtt)
    assert "This has no speaker prefix" in body
    assert participants == []


def test_vtt_round_trip_in_read_folder(tmp_path):
    _write(tmp_path, "2026-05-10_zoom.vtt", _SAMPLE_VTT)
    records = read_folder(tmp_path)
    assert len(records) == 1
    r = records[0]
    assert r.participants == ["Roy Abitbol", "Client Name"]
    assert "Let's start the call" in r.body_text


# ── plain-text participant extraction ─────────────────────────────────────────


def test_plain_text_participants_zoom_format():
    participants = _extract_plain_text_participants(_SAMPLE_TXT)
    assert "Roy Abitbol" in participants
    assert "Client Name" in participants


def test_plain_text_participants_empty_for_no_speakers():
    participants = _extract_plain_text_participants(
        "No speaker prefixes here.\nJust plain notes."
    )
    assert participants == []


def test_plain_text_no_timestamp_names(tmp_path):
    """Lines like '10:30 - meeting started' must not be treated as speakers."""
    txt = "10:30: meeting started\nActual Speaker: hello\n"
    participants = _extract_plain_text_participants(txt)
    assert "10:30" not in participants


# ── markdown ──────────────────────────────────────────────────────────────────


def test_markdown_parsed_as_plain_text(tmp_path):
    _write(tmp_path, "2026-05-01_notes.md", _SAMPLE_MD)
    records = read_folder(tmp_path)
    assert "scrollable design is approved" in records[0].body_text


def test_markdown_body_text_preserved(tmp_path):
    content = "# Heading\n\nSome notes about the project."
    _write(tmp_path, "2026-05-01_notes.md", content)
    records = read_folder(tmp_path)
    assert "# Heading" in records[0].body_text


# ── BOM handling ──────────────────────────────────────────────────────────────


def test_bom_stripped(tmp_path):
    """UTF-8 BOM must be stripped from plain text files."""
    p = tmp_path / "2026-05-01_bom.txt"
    p.write_bytes(b"\xef\xbb\xbf" + "BOM test content".encode("utf-8"))
    records = read_folder(tmp_path)
    assert not records[0].body_text.startswith("\ufeff")
    assert "BOM test content" in records[0].body_text
