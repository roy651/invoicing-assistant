"""
Live mail-evidence runner / CLI (task 1.10).

The thin entry point that drives real IMAP mailboxes: probe the connection,
fetch a bounded batch, and EXPORT it to disk as mbox files the offline harness
re-ingests byte-identically. Reasoning + proforma creation stay a separate
phase2 step — this runner only does fetch → persist → advance watermark.

Multi-account: each command loops over the accounts from `load_imap_accounts()`
(`IMAP_ACCOUNTS=ula,gmail` + per-account `IMAP_<NAME>_HOST/PORT/USER/APP_PASSWORD`
and optional `_INBOX`/`_SENT` folder overrides). A freelancer's inbound mail may
live in Gmail while sent mail stays on a custom domain — fetch both, merge by
Message-ID. `--account <name>` limits to one.

Usage:
  uv run python -m mail_evidence.runner probe        [--account NAME]
  uv run python -m mail_evidence.runner fetch        [--root fixtures] [--account NAME]
                  [--since YYYY-MM-DD] [--window-days N] [--max N] [--no-advance] [--dry-run]
  uv run python -m mail_evidence.runner watermark    [--root fixtures] [--account NAME]

Batch/watermark contract (build-plan 1.10):
  Messages export to <root>/emails/<account>_<folder>.mbox; a per-account watermark
  (mail_evidence_watermark_<account>.json) lives at <root> and is committed ONLY
  after that account's batch is durably written. A crash before commit re-fetches at
  most one batch. Re-export is idempotent: messages already present (by Message-ID)
  are skipped, so the day-granular IMAP SINCE overlap never duplicates mail.

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

from mail_evidence.config import ImapAccount, load_imap_accounts
from mail_evidence.fetch.imap import ImapClient, RawBatch, fetch_raw_batch
from mail_evidence.fetch.watermark import commit_watermark, load_watermark

_log = logging.getLogger(__name__)


# ── account selection ─────────────────────────────────────────────────────────


def _select_accounts(name: str | None) -> list[ImapAccount]:
    accounts = load_imap_accounts()
    if not accounts:
        raise RuntimeError(
            "No IMAP accounts configured. Set IMAP_ACCOUNTS=<names> with per-account "
            "IMAP_<NAME>_HOST/PORT/USER/APP_PASSWORD, or a legacy IMAP_HOST/USER/APP_PASSWORD."
        )
    if name:
        accounts = [a for a in accounts if a.name == name]
        if not accounts:
            raise RuntimeError(f"No IMAP account named {name!r}.")
    return accounts


def _client_for(account: ImapAccount) -> ImapClient:
    """Test seam — build the live client for one account."""
    return ImapClient.for_account(account)


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


def _persist_batch(batch: RawBatch, emails_dir: Path, account: str) -> int:
    """Export each folder to emails/<account>_<folder>.mbox (provenance per source;
    cross-account duplicates collapse by Message-ID at ingest)."""
    emails_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for folder, raws in batch.raw_by_folder.items():
        safe = folder.replace("/", "_").replace(" ", "_")
        path = emails_dir / f"{account}_{safe}.mbox"
        n = _export_folder(path, raws)
        print(f"    {account}/{folder}: +{n} new ({len(raws)} fetched) → {path.name}")
        total += n
    return total


# ── commands (each loops over the selected accounts) ──────────────────────────


def _cmd_probe(args: argparse.Namespace) -> int:
    for acct in _select_accounts(args.account):
        client = _client_for(acct)
        client.connect()
        try:
            print(f"[{acct.name}] connected (read-only) — {acct.host}")
            for folder in acct.folders:
                try:
                    client.select_folder(folder)
                    print(f"    {folder}: {len(client.search(['ALL']))} message(s)")
                except Exception as exc:  # noqa: BLE001 — report, don't abort other folders
                    print(f"    {folder}: <cannot select: {exc}>")
        finally:
            client.disconnect()
    return 0


def _cmd_watermark(args: argparse.Namespace) -> int:
    for acct in _select_accounts(args.account):
        wm = load_watermark(state_dir=Path(args.root), name=acct.name)
        print(
            f"[{acct.name}] {'cold start (no watermark)' if wm is None else wm.isoformat()}"
        )
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    root = Path(args.root)
    for acct in _select_accounts(args.account):
        if args.since is not None:
            watermark: datetime | None = datetime.fromisoformat(args.since).replace(
                tzinfo=timezone.utc
            )
        else:
            watermark = load_watermark(state_dir=root, name=acct.name)

        print(
            f"[{acct.name}] fetching {acct.folders} since "
            f"{watermark.isoformat() if watermark else 'beginning (cold start)'} "
            f"(window={args.window_days}d, max={args.max})"
        )
        client = _client_for(acct)
        try:
            batch = fetch_raw_batch(
                client,
                folders=acct.folders,
                watermark=watermark,
                max_messages=args.max,
                window_days=args.window_days,
            )
        finally:
            client.disconnect()
        print(
            f"[{acct.name}] fetched {batch.count} message(s); high-water={batch.high_water}"
        )

        if args.dry_run:
            for folder, raws in batch.raw_by_folder.items():
                print(f"    {acct.name}/{folder}: {len(raws)} message(s) (dry-run)")
            continue

        written = _persist_batch(batch, root / "emails", acct.name)
        # Commit per-account watermark ONLY after the batch is durably written.
        if args.no_advance:
            print(f"[{acct.name}] watermark not advanced (--no-advance).")
        elif batch.high_water is not None and written > 0:
            commit_watermark(batch.high_water, state_dir=root, name=acct.name)
            print(f"[{acct.name}] watermark advanced → {batch.high_water.isoformat()}")
        else:
            print(f"[{acct.name}] nothing new persisted — watermark unchanged.")

    if args.dry_run:
        print("Dry-run: nothing written, watermarks unchanged.")
    return 0


# ── entry point ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mail_evidence.runner",
        description="Live mail-evidence runner: probe / fetch+export / watermark.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("probe", help="Connect, auth, report per-account folder stats.")
    pr.add_argument(
        "--account", default=None, help="Limit to one account name (default: all)."
    )
    pr.set_defaults(func=_cmd_probe)

    fe = sub.add_parser(
        "fetch", help="Fetch each account and export mboxes to <root>/emails."
    )
    fe.add_argument(
        "--root", default="fixtures", help="Fixtures root (mboxes + watermarks)."
    )
    fe.add_argument(
        "--account", default=None, help="Limit to one account name (default: all)."
    )
    fe.add_argument("--since", default=None, help="Override watermark (YYYY-MM-DD).")
    fe.add_argument("--window-days", type=int, default=35)
    fe.add_argument(
        "--max", type=int, default=500, help="Hard cap on messages fetched per account."
    )
    fe.add_argument(
        "--no-advance", action="store_true", help="Don't persist watermark."
    )
    fe.add_argument(
        "--dry-run", action="store_true", help="Print summary; write nothing."
    )
    fe.set_defaults(func=_cmd_fetch)

    wm = sub.add_parser("watermark", help="Print each account's current watermark.")
    wm.add_argument("--root", default="fixtures")
    wm.add_argument(
        "--account", default=None, help="Limit to one account name (default: all)."
    )
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
