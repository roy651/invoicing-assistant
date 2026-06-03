"""
Offline email ingestion — parse exported .eml/.mbox files into EvidenceRecords.

For the Phase-2 fixture harness: ingest a freelancer's exported mailbox
(`fixtures/emails/`) with NO live IMAP connection. This reuses the live parser
(`fetch.imap`) end to end — the same header/body decoders, the same
References-chain `_assign_thread_ids`, the same `_raw_to_record` — so the offline
path yields the SAME `EvidenceRecord`s and threading that `fetch_messages` produces
from a live mailbox. The Phase-2 validation therefore runs on production-identical
shapes; only the byte source differs (a file instead of an IMAP FETCH).

Folder assignment (provenance + INBOX/Sent separation): a message's folder is the
name of its containing subdirectory under the export root, or an `.mbox` file's stem;
files directly under the root default to INBOX. Threading still unifies ACROSS
folders exactly as live — a Sent reply shares its INBOX parent's `thread_id`, because
`_assign_thread_ids` runs over every message regardless of folder.

This module is import-clean (stdlib + `mail_evidence` only) — no Google, no invoicing,
no domain judgment. It stays inside the package portability boundary.
"""

from __future__ import annotations

import logging
import mailbox
from collections.abc import Iterator
from pathlib import Path

from mail_evidence.fetch.imap import (
    _RawMail,
    _assign_thread_ids,
    _parse_raw,
    _raw_to_record,
)
from mail_evidence.records import EvidenceRecord

_log = logging.getLogger(__name__)
_DEFAULT_FOLDER = "INBOX"
_SUFFIXES = {".eml", ".mbox"}


def ingest_email_export(root: str | Path) -> list[EvidenceRecord]:
    """
    Parse all `.eml` and `.mbox` files under `root` into EvidenceRecords.

    Output mirrors `fetch_messages`: identical field mapping, cross-folder
    References-chain `thread_id`s, deduped by Message-ID, sorted by date ascending.
    """
    root = Path(root)
    raws: list[_RawMail] = []
    seen_msg_ids: set[str] = set()
    uid = 0

    for path, folder in _iter_sources(root):
        for raw_bytes in _messages_in(path):
            uid += 1
            try:
                raw = _parse_raw(uid, {b"RFC822": raw_bytes}, folder=folder)
            except Exception:
                _log.warning("mail-evidence: failed to parse %s — skipping", path)
                continue
            # Dedup by Message-ID, same as the live cross-folder pass.
            if raw.msg_id in seen_msg_ids:
                continue
            seen_msg_ids.add(raw.msg_id)
            raws.append(raw)

    _assign_thread_ids(raws)
    records = [_raw_to_record(r) for r in raws]
    records.sort(key=lambda r: r.date)
    return records


# ── internal ──────────────────────────────────────────────────────────────────


def _iter_sources(root: Path) -> Iterator[tuple[Path, str]]:
    """Yield (file_path, folder) for every .eml/.mbox under root, sorted for determinism."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SUFFIXES:
            yield path, _folder_for(path, root)


def _folder_for(path: Path, root: Path) -> str:
    """Folder = .mbox stem, or the first subdirectory under root, else INBOX."""
    if path.suffix.lower() == ".mbox":
        return path.stem
    rel = path.relative_to(root)
    return rel.parts[0] if len(rel.parts) > 1 else _DEFAULT_FOLDER


def _messages_in(path: Path) -> Iterator[bytes]:
    """Yield the raw RFC822 bytes of each message in an .eml (one) or .mbox (many).

    For .mbox we use mailbox's message-boundary detection (which correctly handles
    `From ` escaping) but read each message via ``get_bytes`` rather than iterating
    message objects: real exports carry non-ASCII (e.g. Hebrew) sender names on the
    ``From `` postmark line, and constructing a message object decodes that line as
    strict ASCII and raises. ``get_bytes`` skips the postmark line without decoding it.
    """
    if path.suffix.lower() == ".mbox":
        box = mailbox.mbox(str(path))
        try:
            for key in box.iterkeys():
                yield box.get_bytes(key)
        finally:
            box.close()
    else:
        yield path.read_bytes()
