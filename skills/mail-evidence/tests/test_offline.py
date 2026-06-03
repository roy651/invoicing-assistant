"""
Tests for offline email ingestion (task 1.11 Deliverable A).

Verifies the offline path produces EvidenceRecords identical to the live
fetch_messages path (same parser reused), cross-folder References-chain threading,
.mbox support, CC preservation, and Message-ID dedup.
"""

from __future__ import annotations

import mailbox
from unittest.mock import MagicMock

from mail_evidence.fetch.imap import ImapClient, fetch_messages
from mail_evidence.fetch.offline import ingest_email_export


def _raw(
    *,
    message_id: str,
    subject: str = "Subject",
    from_: str = "alice@example.com",
    to: str = "bob@example.com",
    cc: str = "",
    body: str = "Body text",
    date: str = "Mon, 01 Apr 2026 09:00:00 +0000",
    references: str = "",
    in_reply_to: str = "",
) -> bytes:
    lines = [
        f"From: {from_}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: 8bit",
        f"Message-ID: {message_id}",
    ]
    if cc:
        lines.append(f"CC: {cc}")
    if references:
        lines.append(f"References: {references}")
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += ["", body]
    return "\r\n".join(lines).encode("utf-8")


def _write_eml(folder_dir, name: str, raw: bytes) -> None:
    folder_dir.mkdir(parents=True, exist_ok=True)
    (folder_dir / name).write_bytes(raw)


def _fields(rec):
    return (
        rec.id,
        rec.thread_id,
        rec.source,
        rec.from_,
        tuple(rec.to),
        tuple(rec.cc),
        rec.subject,
        rec.body_text,
        rec.date,
    )


def _fetch_via_live(folder_msgs: dict[str, list[bytes]]):
    """Run the live fetch_messages over an in-memory mock of the same raw bytes."""
    client = MagicMock(spec=ImapClient)
    state = {"folder": None}
    folder_maps: dict[str, dict] = {}
    uid = 0
    for folder, msgs in folder_msgs.items():
        m = {}
        for raw in msgs:
            uid += 1
            m[uid] = {b"RFC822": raw, b"UID": uid}
        folder_maps[folder] = m

    client.select_folder.side_effect = lambda f: state.__setitem__("folder", f)
    client.search.side_effect = lambda _crit: list(folder_maps[state["folder"]].keys())
    client.fetch.side_effect = lambda _uids, _parts: folder_maps[state["folder"]]
    return fetch_messages(client, folders=list(folder_msgs.keys()))


# ── basic ingestion ───────────────────────────────────────────────────────────


def test_ingest_single_eml(tmp_path):
    _write_eml(
        tmp_path / "INBOX",
        "m1.eml",
        _raw(
            message_id="<m1@host>",
            subject="Logo approval",
            from_="client@agency.com",
            to="designer@studio.com",
            cc="pm@agency.com",
            body="Approved!",
        ),
    )
    records = ingest_email_export(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec.id == "<m1@host>"
    assert rec.source == "email"
    assert rec.from_ == "client@agency.com"
    assert rec.to == ["designer@studio.com"]
    assert rec.cc == ["pm@agency.com"]
    assert rec.subject == "Logo approval"
    assert "Approved!" in rec.body_text


def test_cc_preserved_as_first_class(tmp_path):
    _write_eml(
        tmp_path,
        "m.eml",
        _raw(message_id="<m@host>", cc="sub@studio.com,manager@agency.com"),
    )
    rec = ingest_email_export(tmp_path)[0]
    assert "sub@studio.com" in rec.cc
    assert "manager@agency.com" in rec.cc


# ── cross-folder threading (the key acceptance) ──────────────────────────────


def test_sent_reply_threads_with_inbox_parent(tmp_path):
    parent = _raw(
        message_id="<parent@host>",
        subject="Project brief",
        body="Please review.",
        date="Mon, 01 Apr 2026 09:00:00 +0000",
    )
    reply = _raw(
        message_id="<reply@host>",
        subject="Re: Project brief",
        from_="designer@studio.com",
        to="client@agency.com",
        body="On it.",
        date="Mon, 01 Apr 2026 10:00:00 +0000",
        in_reply_to="<parent@host>",
    )
    _write_eml(tmp_path / "INBOX", "parent.eml", parent)
    _write_eml(tmp_path / "Sent", "reply.eml", reply)

    records = ingest_email_export(tmp_path)
    assert len(records) == 2
    thread_ids = {r.thread_id for r in records}
    assert thread_ids == {"<parent@host>"}  # both unified onto the parent


# ── fidelity: offline output == live fetch_messages output ───────────────────


def test_matches_fetch_messages(tmp_path):
    parent = _raw(
        message_id="<parent@host>",
        subject="Brief",
        body="Please review.",
        date="Mon, 01 Apr 2026 09:00:00 +0000",
    )
    reply = _raw(
        message_id="<reply@host>",
        subject="Re: Brief",
        from_="designer@studio.com",
        to="client@agency.com",
        cc="pm@agency.com",
        body="Done.",
        date="Mon, 01 Apr 2026 10:00:00 +0000",
        in_reply_to="<parent@host>",
    )
    _write_eml(tmp_path / "INBOX", "parent.eml", parent)
    _write_eml(tmp_path / "Sent", "reply.eml", reply)

    offline = ingest_email_export(tmp_path)
    live = _fetch_via_live({"INBOX": [parent], "Sent": [reply]})

    assert [_fields(r) for r in offline] == [_fields(r) for r in live]


# ── .mbox support ─────────────────────────────────────────────────────────────


def test_mbox_ingestion(tmp_path):
    box_path = tmp_path / "Archive.mbox"
    box = mailbox.mbox(str(box_path))
    box.lock()
    box.add(_raw(message_id="<a@host>", subject="One", body="First"))
    box.add(_raw(message_id="<b@host>", subject="Two", body="Second"))
    box.flush()
    box.unlock()
    box.close()

    records = ingest_email_export(tmp_path)
    assert {r.id for r in records} == {"<a@host>", "<b@host>"}


def test_mbox_non_ascii_postmark_line(tmp_path):
    """Real exports carry non-ASCII (e.g. Hebrew) sender names on the mbox 'From '
    postmark line. Constructing a message object decodes that line as strict ASCII
    and raises; ingestion must read past it via get_bytes instead."""
    msg = _raw(message_id="<heb@host>", subject="One", body="First")
    postmark = "From דנה@acme.co.il Mon May 04 09:00:00 2026\n".encode()
    (tmp_path / "Export.mbox").write_bytes(postmark + msg + b"\n")

    records = ingest_email_export(tmp_path)
    assert {r.id for r in records} == {"<heb@host>"}


# ── dedup by Message-ID across folders ───────────────────────────────────────


def test_dedup_same_message_id_across_folders(tmp_path):
    same = _raw(message_id="<dup@host>", subject="Sent to self")
    _write_eml(tmp_path / "INBOX", "x.eml", same)
    _write_eml(tmp_path / "Sent", "x.eml", same)
    records = ingest_email_export(tmp_path)
    assert len(records) == 1
