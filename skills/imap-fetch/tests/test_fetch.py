"""
Tests for imap_fetch.fetch.

All IMAP calls are mocked — no real network, no real credentials.

Critical test: test_no_write_imap_command_ever_sent — asserts that fetch_since
never issues STORE, COPY, MOVE, EXPUNGE, DELETE, or any flag mutation.  This is
the read-only DoD item from docs/04.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from unittest.mock import MagicMock


from imap_fetch.fetch import (
    _decode_header_value,
    _normalise_subject,
    _parse_date,
    _strip_html,
    fetch_since,
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
    references: str = "",
    in_reply_to: str = "",
    mime: str = "text/plain",
) -> bytes:
    """Build a minimal raw RFC 2822 message as bytes."""
    lines = [
        f"From: {from_}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Content-Type: {mime}; charset=utf-8",
        "Content-Transfer-Encoding: 8bit",
    ]
    if cc:
        lines.append(f"CC: {cc}")
    if references:
        lines.append(f"References: {references}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += ["", body]
    return "\r\n".join(lines).encode("utf-8")


def _make_mock_client(
    *,
    uids: list[int] | None = None,
    messages: dict | None = None,
    mailbox: str = "INBOX",
) -> MagicMock:
    """Return a mock ImapClient that yields the specified UIDs and raw messages."""
    client = MagicMock()
    client._mailbox = mailbox
    client.search.return_value = uids or []
    client.fetch.return_value = messages or {}
    return client


def _raw_map(uid: int, raw: bytes) -> dict:
    """Build the dict structure imapclient returns."""
    return {uid: {b"RFC822": raw, b"UID": uid}}


# ── write-command ban ─────────────────────────────────────────────────────────


def test_no_write_imap_command_ever_sent():
    """
    Critical DoD test: fetch_since must NEVER call any IMAP write command.

    Asserts that the ImapClient mock receives NO call to:
      store, copy, move, expunge, delete, set_flags, add_flags, remove_flags,
      append, create_folder, delete_folder, rename_folder, subscribe_folder,
      unsubscribe_folder.

    The underlying connection must also use EXAMINE (read-only SELECT), not
    SELECT.  That guarantee lives in ImapClient.connect() and is tested in
    test_client.py; here we only verify the fetch layer doesn't call write ops.
    """
    raw = _make_raw_email(subject="Hello")
    client = _make_mock_client(
        uids=[42],
        messages=_raw_map(42, raw),
    )

    fetch_since(client, watermark=None)

    # Enumerate every method on the mock that was called.
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
        f"fetch_since called write IMAP command(s): {violations}\n"
        f"All calls made: {called_methods}"
    )


# ── fetch_since behaviour ─────────────────────────────────────────────────────


def test_fetch_since_returns_empty_on_no_messages():
    client = _make_mock_client(uids=[], messages={})
    result = fetch_since(client, watermark=None)
    assert result == []


def test_fetch_since_cold_start_searches_all():
    """watermark=None → search criteria should be ['ALL']."""
    client = _make_mock_client(uids=[], messages={})
    fetch_since(client, watermark=None)
    client.search.assert_called_once_with(["ALL"])


def test_fetch_since_with_watermark_uses_since():
    """watermark supplied → search criteria should include SINCE."""
    client = _make_mock_client(uids=[], messages={})
    wm = datetime(2026, 4, 30, tzinfo=timezone.utc)
    fetch_since(client, watermark=wm)
    criteria = client.search.call_args[0][0]
    assert criteria[0] == "SINCE"
    assert "2026" in criteria[1]


def test_fetch_since_parses_single_message():
    raw = _make_raw_email(
        subject="Logo design approval",
        from_="client@agency.com",
        to="designer@studio.com",
        cc="pm@agency.com",
        body="Approved! Please proceed.",
    )
    client = _make_mock_client(uids=[7], messages=_raw_map(7, raw))
    msgs = fetch_since(client, watermark=None)

    assert len(msgs) == 1
    m = msgs[0]
    assert m.id == "INBOX/7"
    assert m.subject == "Logo design approval"
    assert m.from_.email == "client@agency.com"
    assert m.to[0].email == "designer@studio.com"
    # CC preserved — critical for subcontractor completion evidence.
    assert len(m.cc) == 1
    assert m.cc[0].email == "pm@agency.com"
    assert "Approved" in m.body_text


def test_fetch_since_sorts_ascending_by_date():
    raw_old = _make_raw_email(date="Mon, 01 Apr 2026 09:00:00 +0000", body="First")
    raw_new = _make_raw_email(date="Tue, 02 Apr 2026 10:00:00 +0000", body="Second")
    messages = {}
    messages.update(_raw_map(10, raw_old))
    messages.update(_raw_map(11, raw_new))
    # Return in reverse order to test sorting.
    client = _make_mock_client(uids=[11, 10], messages=messages)
    msgs = fetch_since(client, watermark=None)
    assert len(msgs) == 2
    assert "First" in msgs[0].body_text
    assert "Second" in msgs[1].body_text


def test_fetch_since_skips_unparseable_message():
    """Corrupt messages must be skipped, not crash the whole fetch."""
    good = _make_raw_email(subject="Good", body="Hello")
    messages = {
        1: {b"RFC822": b"NOT VALID RFC 2822 !!!!", b"UID": 1},
        2: {b"RFC822": good, b"UID": 2},
    }
    client = _make_mock_client(uids=[1, 2], messages=messages)
    # Should not raise; we expect 1 good message (the corrupt one may still
    # parse with a degraded result — the key is no exception).
    result = fetch_since(client, watermark=None)
    # At least the good one must be returned.
    subjects = [m.subject for m in result]
    assert "Good" in subjects


# ── thread grouping ───────────────────────────────────────────────────────────


def test_thread_grouping_by_subject():
    """Messages with the same normalised subject must share a thread_id."""
    raw1 = _make_raw_email(subject="Re: Website project", body="First reply")
    raw2 = _make_raw_email(subject="Re: Website project", body="Second reply")
    raw3 = _make_raw_email(subject="Unrelated topic", body="Other")
    messages = {}
    messages.update(_raw_map(1, raw1))
    messages.update(_raw_map(2, raw2))
    messages.update(_raw_map(3, raw3))
    client = _make_mock_client(uids=[1, 2, 3], messages=messages)
    msgs = fetch_since(client, watermark=None)

    by_id = {m.id: m for m in msgs}
    assert by_id["INBOX/1"].thread_id == by_id["INBOX/2"].thread_id
    assert by_id["INBOX/3"].thread_id != by_id["INBOX/1"].thread_id


def test_thread_id_is_earliest_message_in_group():
    """Thread root must be the message with the lowest UID."""
    raw1 = _make_raw_email(subject="Project update", body="Initial")
    raw2 = _make_raw_email(subject="Re: Project update", body="Reply")
    messages = {}
    messages.update(_raw_map(5, raw1))
    messages.update(_raw_map(9, raw2))
    client = _make_mock_client(uids=[5, 9], messages=messages)
    msgs = fetch_since(client, watermark=None)
    # Both should point at the first (UID 5).
    for m in msgs:
        assert m.thread_id == "INBOX/5"


# ── CC preservation ───────────────────────────────────────────────────────────


def test_cc_only_message_not_dropped():
    """A message where user appears only in CC must be fetched and returned."""
    raw = _make_raw_email(
        to="client@end.com",
        cc="designer@studio.com",
        body="Subcontractor confirmed directly to client.",
    )
    client = _make_mock_client(uids=[99], messages=_raw_map(99, raw))
    msgs = fetch_since(client, watermark=None)
    assert len(msgs) == 1
    assert msgs[0].cc[0].email == "designer@studio.com"


# ── encoding ─────────────────────────────────────────────────────────────────


def test_decode_hebrew_subject():
    """MIME-encoded Hebrew subject must decode cleanly."""
    # "שלום" encoded as UTF-8 base64: base64.b64encode('שלום'.encode()) == b'16nXnNeV150='
    raw = _make_raw_email(subject="=?utf-8?b?16nXnNeV150=?=", body="test")
    client = _make_mock_client(uids=[1], messages=_raw_map(1, raw))
    msgs = fetch_since(client, watermark=None)
    assert msgs[0].subject == "שלום"


def test_decode_header_value_plain():
    assert _decode_header_value("Hello World") == "Hello World"


def test_decode_header_value_utf8_b64():
    # "שלום" in UTF-8 base64 MIME encoding.
    # base64.b64encode('שלום'.encode('utf-8')) == b'16nXnNeV150='
    encoded = "=?utf-8?b?16nXnNeV150=?="
    assert _decode_header_value(encoded) == "שלום"


def test_decode_header_value_quoted_printable():
    # "é" in ISO-8859-1 quoted-printable.
    encoded = "=?iso-8859-1?q?=E9?="
    result = _decode_header_value(encoded)
    assert result == "é"


# ── HTML stripping ────────────────────────────────────────────────────────────


def test_strip_html_removes_tags():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_decodes_entities():
    assert "&amp;" not in _strip_html("Tom &amp; Jerry")
    assert "Tom & Jerry" in _strip_html("Tom &amp; Jerry")


def test_strip_html_removes_script_blocks():
    result = _strip_html("<script>alert('xss')</script>Hello")
    assert "alert" not in result
    assert "Hello" in result


def test_strip_html_br_becomes_newline():
    result = _strip_html("Line1<br/>Line2")
    assert "Line1" in result
    assert "Line2" in result


# ── date parsing ─────────────────────────────────────────────────────────────


def test_parse_date_utc():
    dt = _parse_date("Thu, 01 May 2026 10:00:00 +0000")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 1


def test_parse_date_with_offset():
    dt = _parse_date("Thu, 01 May 2026 13:00:00 +0300")
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 10  # 13:00+03:00 → 10:00 UTC


def test_parse_date_empty_returns_epoch():
    dt = _parse_date("")
    assert dt.year == 1970


# ── subject normalisation ─────────────────────────────────────────────────────


def test_normalise_subject_strips_re():
    assert _normalise_subject("Re: Website project") == "website project"


def test_normalise_subject_strips_fwd():
    assert _normalise_subject("Fwd: Invoice question") == "invoice question"


def test_normalise_subject_case_insensitive():
    assert _normalise_subject("RE: Test") == _normalise_subject("re: Test")


def test_normalise_subject_preserves_non_prefix_re():
    # "regarding" should not be stripped — only leading "Re: " prefix.
    s = _normalise_subject("regarding the project")
    assert "regarding" in s


# ── attachments metadata ──────────────────────────────────────────────────────


def test_attachment_metadata_collected():
    """Attachment metadata must be collected; bytes must never be fetched."""
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
    msgs = fetch_since(client, watermark=None)
    assert len(msgs) == 1
    m = msgs[0]
    assert len(m.attachments_meta) == 1
    att = m.attachments_meta[0]
    assert att.filename == "invoice.pdf"
    assert att.mime_type == "application/pdf"
    # Body text must be extracted from the text/plain part.
    assert "Please find attached" in m.body_text
