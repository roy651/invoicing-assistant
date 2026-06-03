"""
AC#5 — Read-only IMAP guarantee.

Asserts that fetch_messages never calls any IMAP write command.
Migrated from skills/imap-fetch/tests/test_fetch.py.

Also verifies basic fetch behaviour: CC preservation, multi-folder support,
and the cross-folder threading precondition (Message-IDs are parsed and used
for thread_id assignment).
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from unittest.mock import MagicMock

from mail_evidence.fetch.imap import (
    ImapClient,
    _decode_header_value,
    _parse_date,
    _strip_html,
    fetch_messages,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_raw_email(
    *,
    subject: str = "Test subject",
    from_: str = "sender@example.com",
    to: str = "recipient@example.com",
    cc: str = "",
    date: str = "Thu, 01 May 2026 10:00:00 +0000",
    body: str = "Hello, world!",
    message_id: str = "",
    references: str = "",
    in_reply_to: str = "",
    mime: str = "text/plain",
    list_unsubscribe: str = "",
    precedence: str = "",
) -> bytes:
    lines = [
        f"From: {from_}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Content-Type: {mime}; charset=utf-8",
        "Content-Transfer-Encoding: 8bit",
    ]
    if message_id:
        lines.append(f"Message-ID: {message_id}")
    if cc:
        lines.append(f"CC: {cc}")
    if references:
        lines.append(f"References: {references}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if list_unsubscribe:
        lines.append(f"List-Unsubscribe: {list_unsubscribe}")
    if precedence:
        lines.append(f"Precedence: {precedence}")
    lines += ["", body]
    return "\r\n".join(lines).encode("utf-8")


def _make_mock_client(
    *,
    uids: list[int] | None = None,
    messages: dict | None = None,
    mailbox: str = "INBOX",
) -> MagicMock:
    client = MagicMock(spec=ImapClient)
    client._mailbox = mailbox
    client.search.return_value = uids or []
    client.fetch.return_value = messages or {}
    return client


def _raw_map(uid: int, raw: bytes) -> dict:
    return {uid: {b"RFC822": raw, b"UID": uid}}


# ── write-command ban (AC#5) ──────────────────────────────────────────────────


def test_no_write_imap_command_ever_sent():
    """
    Critical AC#5: fetch_messages must NEVER call any IMAP write command.

    Checks that the ImapClient mock receives NO call to STORE, COPY, MOVE,
    EXPUNGE, DELETE, or any flag mutation.
    """
    raw = _make_raw_email(subject="Hello")
    client = _make_mock_client(uids=[42], messages=_raw_map(42, raw))

    fetch_messages(client, folders=["INBOX"], watermark=None)

    called_methods = {c[0] for c in client.method_calls}
    _WRITE_COMMANDS = {
        "store",
        "copy",
        "move",
        "expunge",
        "delete",
        "delete_messages",
        "set_flags",
        "add_flags",
        "remove_flags",
        "append",
        "create_folder",
        "delete_folder",
        "rename_folder",
        "subscribe_folder",
        "unsubscribe_folder",
        "setacl",
    }
    violations = called_methods & _WRITE_COMMANDS
    assert not violations, (
        f"fetch_messages called write IMAP command(s): {violations}\n"
        f"All calls made: {called_methods}"
    )


# ── fetch behaviour ───────────────────────────────────────────────────────────


def test_fetch_returns_empty_on_no_messages():
    client = _make_mock_client(uids=[], messages={})
    result = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert result == []


def test_fetch_cold_start_searches_all():
    client = _make_mock_client(uids=[], messages={})
    fetch_messages(client, folders=["INBOX"], watermark=None)
    client.search.assert_called_with(["ALL"])


def test_fetch_with_watermark_uses_since():
    client = _make_mock_client(uids=[], messages={})
    wm = datetime(2026, 4, 30, tzinfo=timezone.utc)
    fetch_messages(client, folders=["INBOX"], watermark=wm)
    criteria = client.search.call_args[0][0]
    assert criteria[0] == "SINCE"
    assert "2026" in criteria[1]


def test_fetch_parses_single_message():
    raw = _make_raw_email(
        subject="Logo design approval",
        from_="client@agency.com",
        to="designer@studio.com",
        cc="pm@agency.com",
        body="Approved! Please proceed.",
        message_id="<abc@example.com>",
    )
    client = _make_mock_client(uids=[7], messages=_raw_map(7, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)

    assert len(records) == 1
    rec = records[0]
    assert rec.id == "<abc@example.com>"
    assert rec.subject == "Logo design approval"
    assert rec.from_ == "client@agency.com"
    assert "designer@studio.com" in rec.to
    assert "pm@agency.com" in rec.cc
    assert "Approved" in rec.body_text
    assert rec.source == "email"


def test_fetch_sorts_ascending_by_date():
    raw_old = _make_raw_email(date="Mon, 01 Apr 2026 09:00:00 +0000", body="First")
    raw_new = _make_raw_email(date="Tue, 02 Apr 2026 10:00:00 +0000", body="Second")
    messages = {}
    messages.update(_raw_map(10, raw_old))
    messages.update(_raw_map(11, raw_new))
    client = _make_mock_client(uids=[11, 10], messages=messages)
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert len(records) == 2
    assert "First" in records[0].body_text
    assert "Second" in records[1].body_text


def test_fetch_skips_unparseable_message():
    good = _make_raw_email(subject="Good", body="Hello")
    messages = {
        1: {b"RFC822": b"NOT VALID RFC 2822 !!!!", b"UID": 1},
        2: {b"RFC822": good, b"UID": 2},
    }
    client = _make_mock_client(uids=[1, 2], messages=messages)
    result = fetch_messages(client, folders=["INBOX"], watermark=None)
    subjects = [r.subject for r in result]
    assert "Good" in subjects


# ── CC preservation (AC#4) ───────────────────────────────────────────────────


def test_cc_preserved_as_first_class_field():
    """CC must be a first-class list field, never folded into participants."""
    raw = _make_raw_email(
        to="client@end.com",
        cc="designer@studio.com,pm@agency.com",
        body="Subcontractor confirmed directly to client.",
    )
    client = _make_mock_client(uids=[99], messages=_raw_map(99, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert len(records) == 1
    rec = records[0]
    assert "designer@studio.com" in rec.cc
    assert "pm@agency.com" in rec.cc
    # CC addresses must NOT be in the from_ field
    assert rec.from_ != "designer@studio.com"


def test_cc_only_record_not_dropped():
    """A message where the user appears only in CC must be returned."""
    raw = _make_raw_email(
        to="client@end.com",
        cc="user@freelancer.com",
        body="Subcontractor confirmed directly to client.",
    )
    client = _make_mock_client(uids=[99], messages=_raw_map(99, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert len(records) == 1


def test_transcript_record_has_empty_cc():
    """Transcript EvidenceRecords must have cc=[] and from_=None."""
    from mail_evidence.records import EvidenceRecord
    from datetime import datetime, timezone

    rec = EvidenceRecord(
        id="transcripts/2026-03-01_call",
        thread_id="transcripts/2026-03-01_call",
        source="transcript",
        date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        body_text="Meeting notes",
        participants=["Alice", "Bob"],
        filename="2026-03-01_call.vtt",
    )
    assert rec.cc == []
    assert rec.from_ is None
    assert rec.to == []
    assert rec.subject is None


# ── bulk signal detection ─────────────────────────────────────────────────────


def test_list_unsubscribe_sets_is_bulk():
    raw = _make_raw_email(
        body="Newsletter content",
        list_unsubscribe="<mailto:unsub@newsletter.com>",
    )
    client = _make_mock_client(uids=[1], messages=_raw_map(1, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert records[0].is_bulk is True


def test_precedence_bulk_sets_is_bulk():
    raw = _make_raw_email(body="Bulk mail", precedence="bulk")
    client = _make_mock_client(uids=[1], messages=_raw_map(1, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert records[0].is_bulk is True


def test_no_bulk_signal_is_bulk_false():
    raw = _make_raw_email(
        from_="alice@client.com",
        body="Regular email from a client.",
    )
    client = _make_mock_client(uids=[1], messages=_raw_map(1, raw))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert records[0].is_bulk is False


# ── cross-folder threading (AC#1) ────────────────────────────────────────────


def test_cross_folder_threading_sent_reply_shares_thread_id():
    """
    AC#1: a Sent reply and its INBOX parent must share thread_id.

    INBOX message A has Message-ID <parent@example.com>.
    Sent message B has In-Reply-To: <parent@example.com>.
    Both must receive thread_id == <parent@example.com> (the root).
    """
    inbox_raw = _make_raw_email(
        message_id="<parent@example.com>",
        subject="Project brief",
        body="Please review the attached brief.",
        date="Mon, 01 Apr 2026 09:00:00 +0000",
    )
    sent_raw = _make_raw_email(
        message_id="<reply@example.com>",
        in_reply_to="<parent@example.com>",
        subject="Re: Project brief",
        body="Looks good, will start Monday.",
        date="Mon, 01 Apr 2026 10:00:00 +0000",
    )

    # Mock a client that serves different content per folder.
    client = MagicMock(spec=ImapClient)
    client._mailbox = "INBOX"

    inbox_messages = _raw_map(1, inbox_raw)
    sent_messages = _raw_map(2, sent_raw)

    def search_side_effect(criteria):
        if client._mailbox == "INBOX":
            return [1]
        return [2]

    def fetch_side_effect(uids, parts):
        if client._mailbox == "INBOX":
            return inbox_messages
        return sent_messages

    def select_folder_side_effect(folder):
        client._mailbox = folder

    client.search.side_effect = search_side_effect
    client.fetch.side_effect = fetch_side_effect
    client.select_folder.side_effect = select_folder_side_effect

    records = fetch_messages(client, folders=["INBOX", "Sent"], watermark=None)

    assert len(records) == 2
    thread_ids = {r.thread_id for r in records}
    assert len(thread_ids) == 1, (
        f"Expected both records to share one thread_id, got: {thread_ids}"
    )
    shared_tid = thread_ids.pop()
    assert shared_tid == "<parent@example.com>"


def test_unrelated_messages_have_different_thread_ids():
    """Messages with no shared references must be in separate threads."""
    raw_a = _make_raw_email(
        message_id="<a@example.com>",
        subject="Topic A",
        body="Message A",
        date="Mon, 01 Apr 2026 09:00:00 +0000",
    )
    raw_b = _make_raw_email(
        message_id="<b@example.com>",
        subject="Topic B",
        body="Message B",
        date="Mon, 01 Apr 2026 10:00:00 +0000",
    )
    messages = {}
    messages.update(_raw_map(1, raw_a))
    messages.update(_raw_map(2, raw_b))
    client = _make_mock_client(uids=[1, 2], messages=messages)
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert len(records) == 2
    assert records[0].thread_id != records[1].thread_id


# ── attachments ───────────────────────────────────────────────────────────────


def test_attachment_metadata_collected():
    raw_email = textwrap.dedent("""\
        From: sender@example.com
        To: rcv@example.com
        Subject: Invoice attached
        Date: Thu, 01 May 2026 10:00:00 +0000
        MIME-Version: 1.0
        Content-Type: multipart/mixed; boundary="BOUNDARY"

        --BOUNDARY
        Content-Type: text/plain; charset=utf-8

        Please find attached.

        --BOUNDARY
        Content-Type: application/pdf; name="invoice.pdf"
        Content-Disposition: attachment; filename="invoice.pdf"
        Content-Transfer-Encoding: base64

        JVBERi0xLjQK

        --BOUNDARY--
    """).encode("utf-8")

    client = _make_mock_client(uids=[1], messages=_raw_map(1, raw_email))
    records = fetch_messages(client, folders=["INBOX"], watermark=None)
    assert len(records) == 1
    rec = records[0]
    assert len(rec.attachments_meta) == 1
    att = rec.attachments_meta[0]
    assert att.filename == "invoice.pdf"
    assert att.mime_type == "application/pdf"
    assert "Please find attached" in rec.body_text


# ── header decoding helpers ───────────────────────────────────────────────────


def test_decode_header_value_plain():
    assert _decode_header_value("Hello World") == "Hello World"


def test_decode_header_value_hebrew_base64():
    encoded = "=?utf-8?b?16nXnNeV150=?="
    assert _decode_header_value(encoded) == "שלום"


def test_decode_header_value_quoted_printable():
    encoded = "=?iso-8859-1?q?=E9?="
    assert _decode_header_value(encoded) == "é"


def test_parse_date_utc():
    dt = _parse_date("Thu, 01 May 2026 10:00:00 +0000")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.month == 5 and dt.day == 1


def test_parse_date_with_offset():
    dt = _parse_date("Thu, 01 May 2026 13:00:00 +0300")
    assert dt.hour == 10  # 13:00+03:00 → 10:00 UTC


def test_parse_date_empty_returns_epoch():
    assert _parse_date("").year == 1970


def test_strip_html_removes_tags():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_decodes_entities():
    assert "Tom & Jerry" in _strip_html("Tom &amp; Jerry")


def test_strip_html_removes_script_blocks():
    result = _strip_html("<script>alert('xss')</script>Hello")
    assert "alert" not in result
    assert "Hello" in result
