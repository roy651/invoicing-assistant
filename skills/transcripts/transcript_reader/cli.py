"""
CLI for manual testing of the transcript reader.

Usage:
  uv run python -m transcript_reader.cli read <folder> [--since YYYY-MM-DD] [--recursive]
  uv run python -m transcript_reader.cli show <file>

Commands:
  read    Scan a folder and print a summary of each transcript found.
  show    Parse a single file and print the full evidence record.

Examples:
  # Read all transcripts in the transcripts/ folder
  uv run python -m transcript_reader.cli read ../../transcripts/

  # Read only transcripts since May 1st
  uv run python -m transcript_reader.cli read ../../transcripts/ --since 2026-05-01

  # Show a single VTT file
  uv run python -m transcript_reader.cli show meeting.vtt
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _cmd_read(args: list[str]) -> None:
    from transcript_reader.reader import read_folder

    if not args:
        print(
            "Usage: read <folder> [--since YYYY-MM-DD] [--recursive]", file=sys.stderr
        )
        sys.exit(1)

    folder = Path(args[0])
    since: datetime | None = None
    recursive = False

    i = 1
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since = datetime.fromisoformat(args[i + 1]).replace(tzinfo=timezone.utc)
            i += 2
        elif args[i] == "--recursive":
            recursive = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    records = read_folder(folder, since=since, recursive=recursive)
    print(f"=== {len(records)} transcript(s) ===")
    print()

    for r in records:
        print(f"ID:           {r.id}")
        print(f"File:         {r.filename}")
        print(f"Date:         {r.date.isoformat()}")
        print(
            f"Participants: {', '.join(r.participants) if r.participants else '(none detected)'}"
        )
        preview = r.body_text[:300].replace("\n", " ").strip()
        if preview:
            print(f"Text:         {preview}{'...' if len(r.body_text) > 300 else ''}")
        print()

    if not records:
        print("(no transcript files found)")


def _cmd_show(args: list[str]) -> None:
    from transcript_reader.reader import _parse_file

    if not args:
        print("Usage: show <file>", file=sys.stderr)
        sys.exit(1)

    path = Path(args[0]).resolve()
    record = _parse_file(path, folder_root=path.parent)

    print(f"ID:           {record.id}")
    print(f"File:         {record.filename}")
    print(f"Date:         {record.date.isoformat()}")
    print(f"Source:       {record.source}")
    print(
        f"Participants: {', '.join(record.participants) if record.participants else '(none)'}"
    )
    print()
    print("── Body text ──────────────────────────────────────────────────────────")
    print(record.body_text[:2000])
    if len(record.body_text) > 2000:
        print(f"... ({len(record.body_text) - 2000} more characters)")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    rest = args[1:]

    match cmd:
        case "read":
            _cmd_read(rest)
        case "show":
            _cmd_show(rest)
        case "--help" | "-h" | "help":
            print(__doc__)
        case _:
            print(f"Unknown command: {cmd!r}.  Use: read | show", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
