"""
Live mail-evidence runner / CLI (task 1.10).

The thin entry point that drives a real IMAP mailbox: probe the connection,
fetch a bounded batch, and EXPORT it to disk as mbox files the offline harness
re-ingests byte-identically. Reasoning + proforma creation stay a separate
phase2 step — this runner only does fetch → persist → advance watermark.

Usage:
  uv run python -m mail_evidence.runner probe        [--keychain] [--mailbox INBOX]
  uv run python -m mail_evidence.runner fetch        [--root fixtures] [--since YYYY-MM-DD]
                  [--window-days N] [--max N] [--inbox INBOX] [--sent Sent]
                  [--keychain] [--no-advance] [--dry-run]
  uv run python -m mail_evidence.runner watermark    [--root fixtures]

Batch/watermark contract (build-plan 1.10):
  Messages export to <root>/emails/<folder>.mbox; the watermark
  (mail_evidence_watermark.json) lives at <root> and is committed ONLY after the
  batch is durably written. A crash before commit re-fetches at most one batch.
  Re-export is idempotent: messages already present (by Message-ID) are skipped,
  so the day-granular IMAP SINCE overlap never duplicates mail in the mbox.

Read-only: ImapClient uses EXAMINE; no IMAP write command is ever issued.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import logging
import mailbox
import sys
from datetime import datetime, timezone
from pathlib import Path

from mail_evidence.config import FetchConfig
from mail_evidence.fetch.imap import ImapClient, RawBatch, fetch_raw_batch
from mail_evidence.fetch.watermark import commit_watermark, load_watermark

_log = logging.getLogger(__name__)


# ── credentials ───────────────────────────────────────────────────────────────


def _make_client(*, keychain: bool, mailbox_name: str) -> ImapClient:
    if keychain:
        return ImapClient.from_keychain(mailbox=mailbox_name)
    return ImapClient.from_env(mailbox=mailbox_name)


# ── mbox export (idempotent by Message-ID) ────────────────────────────────────


def _message_id(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    return (msg.get("Message-ID", "") or "").strip()


def _existing_message_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    box = mailbox.mbox(str(path))
    try:
        # get_bytes avoids decoding the `From ` envelope line, which crashes on
        # non-ASCII (Hebrew) senders — the same read the offline ingester uses.
        return {
            mid for key in box.iterkeys() if (mid := _message_id(box.get_bytes(key)))
        }
    finally:
        box.close()


def _export_folder(path: Path, raws: list[bytes]) -> int:
    """Append raws to the mbox, skipping any Message-ID already present.
    Returns the number of NEW messages written."""
    seen = _existing_message_ids(path)
    box = mailbox.mbox(str(path))
    written = 0
    box.lock()
    try:
        for raw in raws:
            mid = _message_id(raw)
            if mid and mid in seen:
                continue
            box.add(mailbox.mboxMessage(raw))
            if mid:
                seen.add(mid)
            written += 1
        box.flush()
    finally:
        box.unlock()
        box.close()
    return written


def _persist_batch(batch: RawBatch, emails_dir: Path) -> int:
    emails_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for folder, raws in batch.raw_by_folder.items():
        path = emails_dir / f"{folder}.mbox"
        n = _export_folder(path, raws)
        print(f"  {folder}: +{n} new ({len(raws)} fetched) → {path}")
        total += n
    return total


# ── commands ──────────────────────────────────────────────────────────────────


def _cmd_probe(args: argparse.Namespace) -> int:
    client = _make_client(keychain=args.keychain, mailbox_name=args.mailbox)
    client.connect()
    try:
        uids = client.search(["ALL"])
        print(f"Connected (read-only). {args.mailbox}: {len(uids)} message(s).")
    finally:
        client.disconnect()
    return 0


def _cmd_watermark(args: argparse.Namespace) -> int:
    wm = load_watermark(state_dir=Path(args.root))
    if wm is None:
        print("No watermark — next fetch is a cold start (full window).")
    else:
        print(f"Watermark: {wm.isoformat()}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    root = Path(args.root)
    config = FetchConfig(
        inbox_folder=args.inbox,
        sent_folder=args.sent,
        window_days=args.window_days,
        max_messages=args.max,
    )

    if args.since is not None:
        watermark: datetime | None = datetime.fromisoformat(args.since).replace(
            tzinfo=timezone.utc
        )
    else:
        watermark = load_watermark(state_dir=root)

    print(
        f"Fetching {config.folders} since "
        f"{watermark.isoformat() if watermark else 'beginning (cold start)'} "
        f"(window={config.window_days}d, max={config.max_messages})"
    )

    client = _make_client(keychain=args.keychain, mailbox_name=args.inbox)
    try:
        batch = fetch_raw_batch(
            client,
            folders=config.folders,
            watermark=watermark,
            max_messages=config.max_messages,
            window_days=config.window_days,
        )
    finally:
        client.disconnect()

    print(f"Fetched {batch.count} message(s); high-water={batch.high_water}")

    if args.dry_run:
        for folder, raws in batch.raw_by_folder.items():
            print(f"  {folder}: {len(raws)} message(s) (dry-run — not written)")
        print("Dry-run: nothing written, watermark unchanged.")
        return 0

    written = _persist_batch(batch, root / "emails")

    # Commit watermark ONLY after the batch is durably written (crash-safe).
    if args.no_advance:
        print("Watermark not advanced (--no-advance).")
    elif batch.high_water is not None and written > 0:
        commit_watermark(batch.high_water, state_dir=root)
        print(f"Watermark advanced → {batch.high_water.isoformat()}")
    else:
        print("Nothing new persisted — watermark unchanged.")
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mail_evidence.runner",
        description="Live mail-evidence runner: probe / fetch+export / watermark.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("probe", help="Connect, auth, report mailbox stats.")
    pr.add_argument("--keychain", action="store_true")
    pr.add_argument("--mailbox", default="INBOX")
    pr.set_defaults(func=_cmd_probe)

    fe = sub.add_parser(
        "fetch", help="Fetch a batch and export mboxes to <root>/emails."
    )
    fe.add_argument(
        "--root", default="fixtures", help="Fixtures root (mboxes + watermark)."
    )
    fe.add_argument("--since", default=None, help="Override watermark (YYYY-MM-DD).")
    fe.add_argument("--window-days", type=int, default=35)
    fe.add_argument(
        "--max", type=int, default=500, help="Hard cap on messages fetched."
    )
    fe.add_argument("--inbox", default="INBOX")
    fe.add_argument("--sent", default="Sent")
    fe.add_argument("--keychain", action="store_true")
    fe.add_argument(
        "--no-advance", action="store_true", help="Don't persist watermark."
    )
    fe.add_argument(
        "--dry-run", action="store_true", help="Print summary; write nothing."
    )
    fe.set_defaults(func=_cmd_fetch)

    wm = sub.add_parser("watermark", help="Print the current watermark.")
    wm.add_argument("--root", default="fixtures")
    wm.set_defaults(func=_cmd_watermark)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
