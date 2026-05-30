"""
transcript-reader — Task 1.6.

Thin reader that normalises Zoom/call transcript text files into EvidenceRecord
objects — the same evidence shape consumed by the invoicing rules skill (1.7).

Public API:
  read_folder(folder_path, since=None) -> list[EvidenceRecord]
  EvidenceRecord    shared evidence shape (used for both email and transcripts)

Why a shared shape:
  The invoicing rules skill (docs/05) consumes email (via imap-fetch) and
  transcripts through a single reasoning pass.  Using one evidence type means
  1.7 never needs to branch on source type for the match/infer steps.

Supported formats:
  .txt  — plain text (Zoom auto-transcripts, call notes)
  .vtt  — WebVTT subtitle format (Zoom / Teams cloud transcripts)
  .md   — markdown notes

Date extraction (in priority order):
  1. Filename contains YYYY-MM-DD or YYYYMMDD (e.g. 2026-04-15_client-call.txt)
  2. First timestamp cue in a .vtt file header
  3. File modification time (os.stat mtime) — reliable on a Mac with Time Machine

ID: "transcripts/<filename-stem>" — stable across re-reads of the same folder.

This skill is read-only.  It never writes to the watched folder.
"""

from transcript_reader.reader import EvidenceRecord, read_folder

__all__ = ["EvidenceRecord", "read_folder"]
