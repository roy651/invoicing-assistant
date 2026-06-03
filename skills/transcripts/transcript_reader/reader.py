"""
Core reader logic — read_folder().

EvidenceRecord is imported from mail_evidence (the package that owns the
unified schema). This module populates the email-specific fields as None/[]
per the schema contract for transcript-sourced records.

Field mapping:
  EvidenceRecord.id          ← "transcripts/<stem>"
  EvidenceRecord.thread_id   ← same as id (transcripts are not threaded)
  EvidenceRecord.date        ← from filename / VTT / mtime
  EvidenceRecord.source      ← "transcript"
  EvidenceRecord.from_       ← None  (email-only)
  EvidenceRecord.to          ← []    (email-only)
  EvidenceRecord.cc          ← []    (email-only)
  EvidenceRecord.subject     ← None  (email-only)
  EvidenceRecord.participants ← extracted from VTT cues / first lines
  EvidenceRecord.body_text   ← full normalised text
  EvidenceRecord.filename    ← original filename

VTT parsing:
  WebVTT files (Zoom / Teams cloud transcripts) have cue blocks:
    HH:MM:SS.mmm --> HH:MM:SS.mmm
    Speaker Name: text
  We strip the timestamp lines and speaker-prefix the text lines so the LLM
  can attribute statements.  The full transcript is preserved in body_text.

Plain-text / markdown:
  Passed through as-is after stripping leading BOM if present.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from mail_evidence.records import EvidenceRecord  # noqa: F401 (re-exported for callers)

_log = logging.getLogger(__name__)

# File extensions we recognise.
_SUPPORTED_EXTENSIONS = {".txt", ".vtt", ".md"}

# Regex for ISO date in filename: YYYY-MM-DD or YYYYMMDD
_DATE_IN_NAME_RE = re.compile(
    r"(?:^|[_\-\s\.])(\d{4})[-_]?(\d{2})[-_]?(\d{2})(?:[_\-\s\.]|$)"
)

# VTT timestamp line: HH:MM:SS.mmm --> HH:MM:SS.mmm (or MM:SS.mmm)
_VTT_CUE_RE = re.compile(
    r"^(?:\d{2}:)?\d{2}:\d{2}\.\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}\.\d{3}"
)


# ── public API ────────────────────────────────────────────────────────────────


def read_folder(
    folder: str | Path,
    *,
    since: datetime | None = None,
    recursive: bool = False,
) -> list[EvidenceRecord]:
    """
    Read all transcript files in `folder` and return normalised EvidenceRecords.

    Args:
        folder:     Directory to scan.  Must exist.
        since:      If supplied, only return records with date > since.
                    Uses the same watermark semantics as imap-fetch.
        recursive:  If True, recurse into subdirectories.  Default: False.

    Returns records sorted by date ascending (oldest first).

    Never raises on individual file errors — logs a warning and skips the file.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f"transcript_reader: {folder!r} is not a directory")

    paths = _collect_files(folder, recursive=recursive)
    _log.info(
        "transcript-reader: found %d transcript file(s) in %s", len(paths), folder
    )

    records: list[EvidenceRecord] = []
    for path in paths:
        try:
            record = _parse_file(path, folder_root=folder)
            if since is not None and record.date <= since:
                continue
            records.append(record)
        except Exception:
            _log.warning(
                "transcript-reader: failed to parse %s — skipping", path, exc_info=True
            )

    records.sort(key=lambda r: r.date)
    _log.info("transcript-reader: returning %d record(s)", len(records))
    return records


# ── file collection ───────────────────────────────────────────────────────────


def _collect_files(folder: Path, *, recursive: bool) -> list[Path]:
    """Return all supported transcript files in folder."""
    if recursive:
        paths = [
            p
            for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTENSIONS
        ]
    else:
        paths = [
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in _SUPPORTED_EXTENSIONS
        ]
    return sorted(paths)  # deterministic order


# ── per-file parsing ──────────────────────────────────────────────────────────


def _parse_file(path: Path, *, folder_root: Path) -> EvidenceRecord:
    """Parse a single transcript file into an EvidenceRecord."""
    # stem = path.stem
    rel = path.relative_to(folder_root)
    record_id = (
        f"transcripts/{rel.with_suffix('')}"  # e.g. "transcripts/2026-04-15_call"
    )

    date = _extract_date(path)

    raw_text = path.read_text(
        encoding="utf-8-sig", errors="replace"
    )  # utf-8-sig strips BOM

    if path.suffix.lower() == ".vtt":
        body_text, participants = _parse_vtt(raw_text)
    else:
        body_text = raw_text.strip()
        participants = _extract_plain_text_participants(body_text)

    return EvidenceRecord(
        id=record_id,
        thread_id=record_id,  # transcripts are not threaded
        source="transcript",
        date=date,
        body_text=body_text,
        # Email-specific fields: always None/[] for transcripts (§6.6 invariant).
        from_=None,
        to=[],
        cc=[],
        subject=None,
        participants=participants,
        filename=path.name,
    )


# ── date extraction ───────────────────────────────────────────────────────────


def _extract_date(path: Path) -> datetime:
    """
    Extract a UTC-aware datetime for the transcript file.

    Priority:
    1. YYYY-MM-DD or YYYYMMDD in the filename stem.
    2. First WEBVTT timestamp cue (only for .vtt files).
    3. File modification time (mtime) from the filesystem.
    """
    # 1. Filename.
    dt = _date_from_filename(path.stem)
    if dt is not None:
        return dt

    # 2. VTT first-cue timestamp (for .vtt files with no date in filename).
    if path.suffix.lower() == ".vtt":
        dt = _date_from_vtt_mtime_header(path)
        if dt is not None:
            return dt

    # 3. Mtime fallback.
    mtime = os.stat(path).st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def _date_from_filename(stem: str) -> datetime | None:
    """Extract date from filename.  Returns midnight UTC of the date found."""
    m = _DATE_IN_NAME_RE.search(stem)
    if not m:
        return None
    try:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _date_from_vtt_mtime_header(path: Path) -> datetime | None:
    """
    Some VTT exporters embed a creation timestamp in the WEBVTT header line,
    e.g. "WEBVTT 2026-04-15T10:30:00Z".  Try to extract it.
    """
    try:
        first_line = path.read_text(encoding="utf-8-sig", errors="replace").split("\n")[
            0
        ]
        # Look for an ISO datetime in the first line.
        m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", first_line)
        if m:
            dt = datetime.fromisoformat(m.group(1))
            return (
                dt.replace(tzinfo=timezone.utc)
                if dt.tzinfo is None
                else dt.astimezone(timezone.utc)
            )
    except Exception:
        pass
    return None


# ── VTT parsing ───────────────────────────────────────────────────────────────


def _parse_vtt(raw: str) -> tuple[str, list[str]]:
    """
    Parse a WebVTT transcript.

    Returns (body_text, participants):
      body_text:    Cue text lines joined, speaker-prefixed, no timestamps.
                    All speakers' text is preserved in order.
      participants: Unique speaker names extracted from cues, in order of first
                    appearance.  Empty if no "Speaker: text" pattern found.

    VTT format:
      WEBVTT
      (optional metadata)

      00:00:00.000 --> 00:00:05.000
      Speaker Name: sentence they said.

      00:00:05.500 --> 00:00:10.000
      Next line of text (no speaker prefix = continuation).
    """
    lines = raw.replace("\r\n", "\n").split("\n")
    text_lines: list[str] = []
    seen_speakers: dict[str, None] = {}  # ordered set

    _SPEAKER_RE = re.compile(r"^([A-Za-zÀ-ÿ\u0590-\u05FF][^:]{0,40}):\s+(.+)$")

    # skip_next = False
    for line in lines:
        line = line.strip()

        # Skip WEBVTT header and NOTE blocks.
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            # skip_next = False
            continue

        # Skip cue identifiers (numeric or UUID lines before timestamps).
        if re.match(r"^[\da-f-]+$", line, re.I) and "-->" not in line:
            continue

        # Skip timestamp lines.
        if _VTT_CUE_RE.match(line):
            # skip_next = False
            continue

        if not line:
            continue

        # Text line — try to extract speaker.
        m = _SPEAKER_RE.match(line)
        if m:
            speaker, text = m.group(1).strip(), m.group(2).strip()
            seen_speakers[speaker] = None
            text_lines.append(f"{speaker}: {text}")
        else:
            text_lines.append(line)

    body_text = "\n".join(text_lines).strip()
    participants = list(seen_speakers.keys())
    return body_text, participants


# ── plain-text participant extraction ─────────────────────────────────────────


def _extract_plain_text_participants(text: str) -> list[str]:
    """
    Best-effort participant extraction from plain-text transcripts.

    Looks for lines of the form "Speaker Name:" at the start of lines,
    which is the Zoom plain-text export format.

    Returns unique names in order of first appearance.  Empty list if none found.
    """
    _SPEAKER_LINE_RE = re.compile(
        r"^([A-Za-zÀ-ÿ\u0590-\u05FF][^:\n]{1,40}):\s", re.MULTILINE
    )
    seen: dict[str, None] = {}
    for m in _SPEAKER_LINE_RE.finditer(text):
        name = m.group(1).strip()
        # Filter out lines that look like timestamps or URLs.
        if len(name) < 2 or re.search(r"\d{2}:\d{2}", name) or "http" in name:
            continue
        seen[name] = None
    return list(seen.keys())
