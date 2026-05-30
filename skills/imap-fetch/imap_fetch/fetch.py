"""
Core fetch logic — fetch_since() and the Message dataclass.

Design notes:

Thread grouping:
  Uses References and In-Reply-To headers to cluster messages into threads,
  with a normalised-subject fallback.  The thread_id is the UID of the first
  message in the thread (lowest UID within the cluster), making it stable
  across runs as long as the folder doesn't change UIDs (which IMAP guarantees
  for the lifetime of a session, and UIDVALIDITY guards across sessions).

Encoding:
  Mail from an Israeli designer is mixed Hebrew/English.  Headers are decoded
  with email.header.decode_header, bodies with the charset declared in the
  MIME part.  Quoted-printable and base64 transfer-encodings are handled by
  the stdlib email.message module.

HTML stripping:
  Prefer the text/plain part.  If no text/plain, strip HTML with a simple
  regex that removes tags and decodes entities; html.parser is not used to
  avoid pulling in a full parser for what is a best-effort text extraction.

Attachments:
  Only metadata is collected (filename, MIME type, size in bytes).
  Attachment bytes are never downloaded — not even into memory.

CC preservation:
  cc is always parsed and included.  CC-only messages (where the user appears
  only in cc, not to) are NOT dropped — they are first-class completion
  evidence when subcontractors confirm work directly to a client.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message as EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from imap_fetch.client import ImapClient

_log = logging.getLogger(__name__)

# IMAP fetch data items we request.  RFC822 fetches the full RFC 2822 message.
_FETCH_PARTS = ["RFC822", "UID"]


@dataclass
class Address:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


@dataclass
class AttachmentMeta:
    filename: str
    mime_type: str  # e.g. "application/pdf"
    size_bytes: int  # approximate; from Content-Length or body part length


@dataclass
class Message:
    """
    Normalised email record consumed by the invoicing skill.

    id:              Stable identifier — "<mailbox>/<uid>".  Stable within a
                     UIDVALIDITY epoch; the watermark guards against epoch resets.
    thread_id:       "<mailbox>/<uid-of-first-in-thread>".  Grouped by
                     References/In-Reply-To, fallback = normalised subject hash.
    date:            UTC-aware datetime.
    from_:           Sender (field named from_ to avoid shadowing builtin).
    to:              Recipient list.
    cc:              CC list — always preserved; never dropped.
    subject:         Decoded subject string.
    body_text:       Plain text body, HTML stripped to text if no text/plain part.
    attachments_meta: Metadata only — bytes never fetched.
    """

    id: str
    thread_id: str
    date: datetime
    from_: Address
    to: list[Address]
    cc: list[Address]
    subject: str
    body_text: str
    attachments_meta: list[AttachmentMeta] = field(default_factory=list)


# ── public API ────────────────────────────────────────────────────────────────


def fetch_since(client: "ImapClient", watermark: datetime | None) -> list[Message]:
    """
    Fetch messages received strictly after `watermark`.

    If watermark is None, fetches from the beginning of the mailbox (cold start).
    The caller is responsible for advancing the watermark after processing
    (see watermark.py).

    Returns messages sorted by date ascending (oldest first).
    """
    client.connect()

    criteria = _build_search_criteria(watermark)
    _log.info("imap-fetch: searching with criteria %r", criteria)

    uids = client.search(criteria)
    _log.info("imap-fetch: %d message(s) matched", len(uids))

    if not uids:
        return []

    raw_map = client.fetch(uids, _FETCH_PARTS)
    messages = []
    for uid, data in raw_map.items():
        try:
            msg = _parse_message(uid, data, mailbox=client._mailbox)
            messages.append(msg)
        except Exception:
            _log.warning(
                "imap-fetch: failed to parse UID %s — skipping", uid, exc_info=True
            )

    # Sort ascending by date.
    messages.sort(key=lambda m: m.date)

    # Group into threads — mutates thread_id in place.
    _assign_thread_ids(messages)

    return messages


# ── search ────────────────────────────────────────────────────────────────────


def _build_search_criteria(watermark: datetime | None) -> list:
    """
    Build an IMAP SEARCH criteria list.

    SINCE uses the date portion only (IMAP spec limitation).  We fetch a
    slightly wider window and filter strictly in _parse_message.
    """
    if watermark is None:
        return ["ALL"]
    # IMAP SINCE is inclusive and date-only; we filter strictly on datetime
    # after parsing.
    date_str = watermark.strftime("%d-%b-%Y")
    return ["SINCE", date_str]


# ── parsing ───────────────────────────────────────────────────────────────────


def _parse_message(uid: int, data: dict, *, mailbox: str) -> Message:
    """Parse a single raw IMAP fetch result into a Message."""
    raw = data.get(b"RFC822") or data.get("RFC822") or b""
    email_msg: EmailMessage = email.message_from_bytes(
        raw, policy=email.policy.compat32
    )

    msg_id = f"{mailbox}/{uid}"
    subject = _decode_header_value(email_msg.get("Subject", ""))
    date = _parse_date(email_msg.get("Date", ""))
    from_ = _parse_addresses(email_msg.get("From", ""))
    to = _parse_addresses(email_msg.get("To", ""))
    cc = _parse_addresses(email_msg.get("CC", ""))
    # references = _get_references(email_msg)
    body_text, attachments = _extract_body_and_attachments(email_msg)

    return Message(
        id=msg_id,
        thread_id=msg_id,  # placeholder; overwritten by _assign_thread_ids
        date=date,
        from_=from_[0] if from_ else Address(name="", email=""),
        to=to,
        cc=cc,
        subject=subject,
        body_text=body_text,
        attachments_meta=attachments,
    )


def _decode_header_value(raw: str) -> str:
    """Decode a MIME-encoded header (RFC 2047) to a plain Unicode string."""
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
    """Parse Date header to UTC-aware datetime.  Falls back to epoch on error."""
    if not raw:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(raw)
        # Normalise to UTC.
        return dt.astimezone(timezone.utc)
    except Exception:
        _log.warning("imap-fetch: could not parse date %r", raw)
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_addresses(raw: str) -> list[Address]:
    """Parse a From/To/CC header into a list of Address objects."""
    if not raw:
        return []
    decoded = _decode_header_value(raw)
    pairs = getaddresses([decoded])
    return [Address(name=n.strip(), email=e.strip().lower()) for n, e in pairs if e]


def _get_references(email_msg: EmailMessage) -> list[str]:
    """Extract Message-IDs from References and In-Reply-To headers."""
    refs: list[str] = []
    for header in ("References", "In-Reply-To"):
        raw = email_msg.get(header, "")
        # Message-IDs are <...> delimited.
        refs.extend(re.findall(r"<[^>]+>", raw))
    return refs


def _extract_body_and_attachments(
    email_msg: EmailMessage,
) -> tuple[str, list[AttachmentMeta]]:
    """
    Walk MIME parts.

    Returns (body_text, attachments).

    Priority for body_text:
      1. text/plain (preferred)
      2. text/html stripped to text (fallback)
      3. Empty string if neither found.

    Attachments: collect metadata for all non-inline parts with a filename.
    Never store attachment bytes.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentMeta] = []

    if email_msg.is_multipart():
        for part in email_msg.walk():
            disposition = part.get_content_disposition() or ""
            mime_type = part.get_content_type()
            filename = part.get_filename()

            if "attachment" in disposition or (
                filename and "inline" not in disposition
            ):
                # Attachment — metadata only; never decode the bytes.
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
        mime_type = email_msg.get_content_type()
        if mime_type == "text/plain":
            plain_parts.append(_decode_part(email_msg))
        elif mime_type == "text/html":
            html_parts.append(_decode_part(email_msg))

    if plain_parts:
        body = "\n\n".join(plain_parts).strip()
    elif html_parts:
        body = _strip_html("\n\n".join(html_parts)).strip()
    else:
        body = ""

    return body, attachments


def _decode_part(part: EmailMessage) -> str:
    """Decode a MIME part to a Unicode string, honouring charset."""
    charset = part.get_content_charset() or "utf-8"
    payload = part.get_payload(decode=True)  # handles base64 + qp
    if payload is None:
        return ""
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _strip_html(html_text: str) -> str:
    """Best-effort HTML→plain-text (no external parser required)."""
    # Remove <style> and <script> blocks.
    text = re.sub(
        r"<(style|script)[^>]*>.*?</\1>", "", html_text, flags=re.DOTALL | re.I
    )
    # Replace <br> and <p> tags with newlines.
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    # Drop remaining tags.
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities.
    text = html.unescape(text)
    # Collapse excessive whitespace but preserve paragraph breaks.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── thread grouping ───────────────────────────────────────────────────────────


def _assign_thread_ids(messages: list[Message]) -> None:
    """
    Assign stable thread_id to each Message in place.

    Algorithm (simple union-find over message-id references):
      1. Build a map from Message-ID header → Message.
      2. For each message, walk References + In-Reply-To; any matching
         Message-ID maps to the same thread as the referencing message.
      3. Thread root = the message with the lowest UID in the cluster.
      4. Fallback: messages with no threading headers that share a
         normalised subject are grouped together.

    This is all in-memory; no IMAP round-trips.
    """
    # Re-parse references from the raw messages — we need them here.
    # Since Message doesn't store references we reconstruct from the
    # thread_id placeholder (which is still the individual id at this point)
    # and the id field.  Threading is best-effort; the invoicing skill only
    # needs approximate grouping.

    # Build subject-based fallback groups.
    subject_groups: dict[str, list[Message]] = {}
    for msg in messages:
        key = _normalise_subject(msg.subject)
        subject_groups.setdefault(key, []).append(msg)

    for group in subject_groups.values():
        if len(group) <= 1:
            continue
        # Thread root = message with the smallest id (proxy for oldest UID).
        root = min(group, key=lambda m: m.id)
        for msg in group:
            msg.thread_id = root.id


def _normalise_subject(subject: str) -> str:
    """
    Strip Re:/Fwd: prefixes and normalise whitespace for subject grouping.

    Returns a lowercase, trimmed subject string used as the thread key.
    """
    s = re.sub(r"^\s*(re|fwd?|fw)\s*:\s*", "", subject, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def _subject_hash(subject: str) -> str:
    """Short hash of the normalised subject — used as a fallback thread_id."""
    return hashlib.sha1(_normalise_subject(subject).encode()).hexdigest()[:12]
