"""
CLI for manual testing of the IMAP fetch skill.

Usage:
  uv run python -m imap_fetch.cli fetch [--since YYYY-MM-DD] [--limit N] [--mailbox NAME]
  uv run python -m imap_fetch.cli watermark

Commands:
  fetch       Fetch messages since watermark (or --since override).
              Prints a summary of each message; does NOT advance the watermark
              (add --advance to persist it).
  watermark   Print the current watermark and exit.

Credentials are loaded from .env automatically (same walk-up logic as the bridge).

Examples:
  # Fetch last 10 messages since current watermark
  uv run python -m imap_fetch.cli fetch --limit 10

  # Cold-start fetch from 2026-04-01, print and advance watermark
  uv run python -m imap_fetch.cli fetch --since 2026-04-01 --advance

  # Check current watermark
  uv run python -m imap_fetch.cli watermark
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _cmd_watermark() -> None:
    from imap_fetch.watermark import load_watermark

    wm = load_watermark()
    if wm is None:
        print("No watermark — next fetch will be a cold start (all messages).")
    else:
        print(f"Current watermark: {wm.isoformat()}")


def _cmd_fetch(args: list[str]) -> None:
    from imap_fetch.client import ImapClient
    from imap_fetch.fetch import fetch_since
    from imap_fetch.watermark import load_watermark, save_watermark

    since_override: datetime | None = None
    limit: int | None = None
    advance = False
    mailbox = "INBOX"

    i = 0
    while i < len(args):
        if args[i] == "--since" and i + 1 < len(args):
            since_override = datetime.fromisoformat(args[i + 1]).replace(
                tzinfo=timezone.utc
            )
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--mailbox" and i + 1 < len(args):
            mailbox = args[i + 1]
            i += 2
        elif args[i] == "--advance":
            advance = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    watermark = since_override if since_override is not None else load_watermark()

    print(
        f"Fetching since: {watermark.isoformat() if watermark else 'beginning (cold start)'}"
    )
    print(f"Mailbox: {mailbox}")
    print()

    with ImapClient.from_env(mailbox=mailbox) as client:
        messages = fetch_since(client, watermark)

    if limit is not None:
        messages = messages[:limit]

    print(f"=== {len(messages)} message(s) ===")
    print()

    for msg in messages:
        print(f"ID:      {msg.id}")
        print(f"Thread:  {msg.thread_id}")
        print(f"Date:    {msg.date.isoformat()}")
        print(f"From:    {msg.from_}")
        print(f"To:      {', '.join(str(a) for a in msg.to)}")
        if msg.cc:
            print(f"CC:      {', '.join(str(a) for a in msg.cc)}")
        print(f"Subject: {msg.subject}")
        if msg.attachments_meta:
            for att in msg.attachments_meta:
                print(
                    f"  [attachment] {att.filename}  {att.mime_type}  {att.size_bytes} bytes"
                )
        body_preview = msg.body_text[:200].replace("\n", " ").strip()
        if body_preview:
            print(f"Body:    {body_preview}{'...' if len(msg.body_text) > 200 else ''}")
        print()

    if not messages:
        print("(no messages)")
    elif advance:
        # Advance to the date of the most recent message we successfully fetched.
        new_wm = max(m.date for m in messages)
        save_watermark(new_wm)
        print(f"Watermark advanced to: {new_wm.isoformat()}")
    else:
        print("Watermark NOT advanced (pass --advance to persist).")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    rest = args[1:]

    match cmd:
        case "fetch":
            _cmd_fetch(rest)
        case "watermark":
            _cmd_watermark()
        case "--help" | "-h" | "help":
            print(__doc__)
        case _:
            print(f"Unknown command: {cmd!r}.  Use: fetch | watermark", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
