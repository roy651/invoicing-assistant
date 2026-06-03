"""
IMAP fetch — read-only, multi-folder, References-chain threading.

Migrated and extended from skills/imap-fetch/imap_fetch/fetch.py + client.py.

Key differences from the original imap-fetch:
  - Fetches from INBOX and Sent (two folders).
  - Thread IDs are assigned via References-chain union-find across both folders,
    so a Sent reply and its INBOX parent share a thread_id.
  - Produces EvidenceRecord (mail_evidence.records) instead of Message.
  - ImapClient.select_folder() allows folder switching on one connection.

Safety:
  - EXAMINE only (read-only SELECT). The \\Seen flag is never set.
  - No STORE / COPY / MOVE / EXPUNGE. The write-command ban is asserted in tests.

CC preservation:
  cc is always parsed and included as first-class field (never dropped).
  CC-only messages (user in cc, not to) are returned — they carry subcontractor
  completion evidence.

Bulk signals:
  is_bulk=True is set when List-Unsubscribe, Precedence: bulk, or a no-reply
  sender pattern is detected. Used by tiering.py to classify T3 threads.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message as EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mail_evidence.records import AttachmentMeta, EvidenceRecord

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)
_FETCH_PARTS = ["RFC822", "UID"]

_DEFAULT_PORT = 993
_DEFAULT_MAILBOX = "INBOX"


# ── ImapClient ────────────────────────────────────────────────────────────────


class ImapClient:
    """
    Thin, read-only IMAP session.

    Uses EXAMINE (read-only SELECT) so the \\Seen flag is never set.
    No write commands (STORE, COPY, MOVE, EXPUNGE) are ever issued.

    Usage:
        with ImapClient.from_env() as client:
            client.select_folder("INBOX")
            records = fetch_folder(client, "INBOX", watermark)
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        mailbox: str = _DEFAULT_MAILBOX,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._mailbox = mailbox
        self._imap: object | None = None

    @classmethod
    def from_env(cls, *, mailbox: str = _DEFAULT_MAILBOX) -> "ImapClient":
        _load_dotenv()
        host = os.environ.get("IMAP_HOST", "")
        port = int(os.environ.get("IMAP_PORT", str(_DEFAULT_PORT)))
        user = os.environ.get("IMAP_USER", "")
        password = os.environ.get("IMAP_APP_PASSWORD", "")
        if not host or not user or not password:
            raise RuntimeError(
                "IMAP_HOST, IMAP_USER, and IMAP_APP_PASSWORD must be set."
            )
        return cls(host, port, user, password, mailbox=mailbox)

    @classmethod
    def from_keychain(
        cls,
        service_name: str = "mail-evidence",
        *,
        mailbox: str = _DEFAULT_MAILBOX,
    ) -> "ImapClient":
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("keyring required: uv add keyring") from exc

        def _get(account: str) -> str:
            val = keyring.get_password(service_name, account)
            if not val:
                raise RuntimeError(
                    f"Keychain missing: service={service_name!r} account={account!r}"
                )
            return val

        host = _get("host")
        user = _get("user")
        password = _get("password")
        port_str = keyring.get_password(service_name, "port")
        port = int(port_str) if port_str else _DEFAULT_PORT
        return cls(host, port, user, password, mailbox=mailbox)

    def connect(self) -> None:
        if self._imap is not None:
            return
        try:
            import imapclient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError("imapclient required: uv add imapclient") from exc
        server = imapclient.IMAPClient(self._host, port=self._port, ssl=True)
        server.login(self._user, self._password)
        server.select_folder(self._mailbox, readonly=True)
        self._imap = server

    def select_folder(self, folder: str) -> None:
        """Switch to a different folder (read-only). Connects if needed."""
        if self._imap is None:
            self.connect()
        self._mailbox = folder
        self._imap.select_folder(folder, readonly=True)  # type: ignore[union-attr]

    def disconnect(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()  # type: ignore[union-attr]
            except Exception:
                pass
            self._imap = None

    def search(self, criteria: list) -> list[int]:
        if self._imap is None:
            raise RuntimeError("Call connect() before search()")
        return self._imap.search(criteria)  # type: ignore[union-attr]

    def fetch(self, uids: list[int], parts: list[str]) -> dict:
        if self._imap is None:
            raise RuntimeError("Call connect() before fetch()")
        return self._imap.fetch(uids, parts)  # type: ignore[union-attr]

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()


# ── internal raw message type ─────────────────────────────────────────────────


@dataclass
class _RawMail:
    """
    Pre-threading internal representation. Carries the References/In-Reply-To
    headers needed for cross-folder union-find. Converted to EvidenceRecord
    after thread_ids are assigned.
    """

    uid: int
    folder: str
    msg_id: str
    references: list[str]  # from References + In-Reply-To headers
    date: datetime
    from_: str | None
    to: list[str]
    cc: list[str]
    subject: str
    body_text: str
    attachments_meta: list[AttachmentMeta] = field(default_factory=list)
    is_bulk: bool = False
    thread_id: str = ""  # set by _assign_thread_ids()


# ── public API ────────────────────────────────────────────────────────────────


def fetch_messages(
    client: ImapClient,
    *,
    folders: list[str] | None = None,
    watermark: datetime | None = None,
    max_messages: int = 500,
    window_days: int | None = None,
) -> list[EvidenceRecord]:
    """
    Fetch from one or more IMAP folders and return cross-threaded EvidenceRecords.

    Thread IDs are References-chain based (cross-folder): a Sent reply and its
    INBOX parent share the same thread_id.

    Returns records sorted by date ascending.
    """
    if folders is None:
        folders = ["INBOX"]

    effective_watermark = watermark
    if window_days is not None:
        window_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
        if effective_watermark is None or window_cutoff > effective_watermark:
            effective_watermark = window_cutoff

    client.connect()

    all_raws: list[_RawMail] = []
    seen_msg_ids: set[str] = set()

    for folder in folders:
        try:
            client.select_folder(folder)
        except Exception:
            _log.warning("mail-evidence: cannot select folder %r — skipping", folder)
            continue

        raws = _fetch_folder_raws(
            client,
            folder,
            effective_watermark,
            max_messages - len(all_raws),
        )
        for raw in raws:
            if raw.msg_id not in seen_msg_ids:
                seen_msg_ids.add(raw.msg_id)
                all_raws.append(raw)
            else:
                _log.debug(
                    "mail-evidence: dedup skipped duplicate Message-ID %r", raw.msg_id
                )

        if len(all_raws) >= max_messages:
            _log.info(
                "mail-evidence: reached max_messages=%d cap after folder %r",
                max_messages,
                folder,
            )
            break

    _assign_thread_ids(all_raws)

    records = [_raw_to_record(r) for r in all_raws]
    records.sort(key=lambda r: r.date)
    return records


# ── internal fetch ────────────────────────────────────────────────────────────


def _fetch_folder_raws(
    client: ImapClient,
    folder: str,
    watermark: datetime | None,
    limit: int,
) -> list[_RawMail]:
    criteria = (
        ["ALL"] if watermark is None else ["SINCE", watermark.strftime("%d-%b-%Y")]
    )
    _log.info("mail-evidence: searching %r with %r", folder, criteria)

    uids = client.search(criteria)
    _log.info("mail-evidence: %d UID(s) in %r", len(uids), folder)

    if not uids:
        return []

    uids_to_fetch = uids[:limit]
    raw_map = client.fetch(uids_to_fetch, _FETCH_PARTS)

    raws = []
    for uid, data in raw_map.items():
        try:
            raws.append(_parse_raw(uid, data, folder=folder))
        except Exception:
            _log.warning(
                "mail-evidence: failed to parse UID %s in %r — skipping",
                uid,
                folder,
                exc_info=True,
            )

    return raws


def _parse_raw(uid: int, data: dict, *, folder: str) -> _RawMail:
    raw_bytes = data.get(b"RFC822") or data.get("RFC822") or b""
    msg: EmailMessage = email.message_from_bytes(
        raw_bytes, policy=email.policy.compat32
    )

    msg_id = (
        _decode_header_value(msg.get("Message-ID", "")).strip() or f"{folder}/{uid}"
    )
    subject = _decode_header_value(msg.get("Subject", ""))
    date = _parse_date(msg.get("Date", ""))
    from_addrs = _parse_addresses(msg.get("From", ""))
    to_addrs = _parse_addresses(msg.get("To", ""))
    cc_addrs = _parse_addresses(msg.get("CC", ""))
    references = _get_references(msg)
    body_text, attachments = _extract_body_and_attachments(msg)
    is_bulk = _detect_bulk(msg, from_addrs)

    from_str = from_addrs[0] if from_addrs else None

    return _RawMail(
        uid=uid,
        folder=folder,
        msg_id=msg_id,
        references=references,
        date=date,
        from_=from_str,
        to=to_addrs,
        cc=cc_addrs,
        subject=subject,
        body_text=body_text,
        attachments_meta=attachments,
        is_bulk=is_bulk,
    )


def _raw_to_record(raw: _RawMail) -> EvidenceRecord:
    all_addrs = []
    if raw.from_:
        all_addrs.append(raw.from_)
    all_addrs.extend(raw.to)
    all_addrs.extend(raw.cc)

    return EvidenceRecord(
        id=raw.msg_id,
        thread_id=raw.thread_id or raw.msg_id,
        source="email",
        date=raw.date,
        body_text=raw.body_text,
        from_=raw.from_,
        to=raw.to,
        cc=raw.cc,
        subject=raw.subject,
        participants=all_addrs,
        filename=None,
        attachments_meta=raw.attachments_meta,
        is_bulk=raw.is_bulk,
    )


# ── threading (References-chain union-find) ────────────────────────────────────


def _assign_thread_ids(mails: list[_RawMail]) -> None:
    """
    Assign thread_id to each _RawMail using References-chain union-find.

    Algorithm:
    1. Build a map from Message-ID → mail.
    2. Union each mail with every id in its References list.
    3. thread_id = Message-ID of the union-find root (earliest by date within cluster).

    This handles cross-folder threading: a Sent reply and its INBOX parent share
    a thread_id because the Sent message has the INBOX message's ID in References.
    """
    if not mails:
        return

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for mail in mails:
        find(mail.msg_id)
        for ref in mail.references:
            ref = ref.strip()
            if ref:
                union(mail.msg_id, ref)

    # For each cluster root, pick the actual mail with the earliest date as canonical root.
    root_to_earliest: dict[str, _RawMail] = {}
    for mail in mails:
        root = find(mail.msg_id)
        if root not in root_to_earliest or mail.date < root_to_earliest[root].date:
            root_to_earliest[root] = mail

    for mail in mails:
        root = find(mail.msg_id)
        canonical = root_to_earliest[root]
        mail.thread_id = canonical.msg_id


# ── header parsing helpers ─────────────────────────────────────────────────────


def _decode_header_value(raw: str) -> str:
    parts = email.header.decode_header(raw)
    decoded = []
    for part_bytes, charset in parts:
        if isinstance(part_bytes, str):
            decoded.append(part_bytes)
        else:
            try:
                decoded.append(part_bytes.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part_bytes.decode("utf-8", errors="replace"))
    return "".join(decoded).strip()


def _parse_date(raw: str) -> datetime:
    if not raw:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc)
    except Exception:
        _log.warning("mail-evidence: could not parse date %r", raw)
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_addresses(raw: str) -> list[str]:
    if not raw:
        return []
    decoded = _decode_header_value(raw)
    pairs = getaddresses([decoded])
    return [e.strip().lower() for _, e in pairs if e.strip()]


def _get_references(msg: EmailMessage) -> list[str]:
    refs: list[str] = []
    for header in ("References", "In-Reply-To"):
        raw = msg.get(header, "")
        refs.extend(re.findall(r"<[^>]+>", raw))
    return refs


def _detect_bulk(msg: EmailMessage, from_addrs: list[str]) -> bool:
    if msg.get("List-Unsubscribe"):
        return True
    precedence = msg.get("Precedence", "").lower()
    if "bulk" in precedence or "list" in precedence:
        return True
    if from_addrs:
        sender = from_addrs[0].lower()
        if re.search(r"no.?reply|noreply|do.not.reply|donotreply", sender):
            return True
    return False


# ── body extraction ───────────────────────────────────────────────────────────


def _extract_body_and_attachments(
    msg: EmailMessage,
) -> tuple[str, list[AttachmentMeta]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentMeta] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = part.get_content_disposition() or ""
            mime_type = part.get_content_type()
            filename = part.get_filename()

            if "attachment" in disposition or (
                filename and "inline" not in disposition
            ):
                size = (
                    len(part.get_payload(decode=False) or "")
                    if part.get_payload()
                    else 0
                )
                attachments.append(
                    AttachmentMeta(
                        filename=_decode_header_value(filename or ""),
                        mime_type=mime_type,
                        size_bytes=size,
                    )
                )
            elif mime_type == "text/plain":
                plain_parts.append(_decode_part(part))
            elif mime_type == "text/html":
                html_parts.append(_decode_part(part))
    else:
        mime_type = msg.get_content_type()
        if mime_type == "text/plain":
            plain_parts.append(_decode_part(msg))
        elif mime_type == "text/html":
            html_parts.append(_decode_part(msg))

    if plain_parts:
        body = "\n\n".join(plain_parts).strip()
    elif html_parts:
        body = _strip_html("\n\n".join(html_parts)).strip()
    else:
        body = ""

    return body, attachments


def _decode_part(part: EmailMessage) -> str:
    charset = part.get_content_charset() or "utf-8"
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _strip_html(html_text: str) -> str:
    text = re.sub(
        r"<(style|script)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.I
    )
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── dotenv helper ─────────────────────────────────────────────────────────────


def _load_dotenv(_here: Path | None = None) -> None:
    from dotenv import load_dotenv

    here = (_here or Path(__file__)).resolve()
    for candidate in [
        here.parent,
        here.parent.parent,
        here.parent.parent.parent,
        here.parent.parent.parent.parent,
        here.parent.parent.parent.parent.parent,
    ]:
        env_file = candidate / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return
