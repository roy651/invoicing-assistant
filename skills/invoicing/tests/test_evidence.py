"""Unit tests for evidence.unify()."""

from datetime import datetime, timezone


from imap_fetch.fetch import Address, Message
from invoicing_rules.evidence import SOURCE_EMAIL, SOURCE_TRANSCRIPT, unify
from transcript_reader.reader import EvidenceRecord


def _make_msg(uid: str, date: datetime, *, cc=None, subject="Test subject") -> Message:
    return Message(
        id=f"INBOX/{uid}",
        thread_id=f"INBOX/{uid}",
        date=date,
        from_=Address(name="Alice", email="alice@example.com"),
        to=[Address(name="Bob", email="bob@example.com")],
        cc=cc or [],
        subject=subject,
        body_text="Some email body",
        attachments_meta=[],
    )


def _make_rec(stem: str, date: datetime) -> EvidenceRecord:
    return EvidenceRecord(
        id=f"transcripts/{stem}",
        thread_id=f"transcripts/{stem}",
        date=date,
        source="transcript",
        participants=["Alice", "Bob"],
        body_text="Some transcript text",
        filename=f"{stem}.vtt",
    )


_T1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T2 = datetime(2026, 3, 5, tzinfo=timezone.utc)
_T3 = datetime(2026, 3, 10, tzinfo=timezone.utc)


# ── basic merging and sorting ─────────────────────────────────────────────────


def test_unify_empty():
    assert unify([], []) == []


def test_unify_emails_only():
    msgs = [_make_msg("002", _T2), _make_msg("001", _T1)]
    result = unify(msgs, [])
    assert len(result) == 2
    assert result[0].date == _T1  # sorted ascending
    assert result[1].date == _T2
    assert all(r.source == SOURCE_EMAIL for r in result)


def test_unify_transcripts_only():
    recs = [_make_rec("call-b", _T3), _make_rec("call-a", _T1)]
    result = unify([], recs)
    assert len(result) == 2
    assert result[0].date == _T1
    assert result[1].date == _T3
    assert all(r.source == SOURCE_TRANSCRIPT for r in result)


def test_unify_interleaved_sorted():
    msgs = [_make_msg("001", _T1), _make_msg("003", _T3)]
    recs = [_make_rec("call", _T2)]
    result = unify(msgs, recs)
    dates = [r.date for r in result]
    assert dates == [_T1, _T2, _T3]
    assert [r.source for r in result] == [SOURCE_EMAIL, SOURCE_TRANSCRIPT, SOURCE_EMAIL]


# ── email-specific field preservation ────────────────────────────────────────


def test_email_from_cc_preserved():
    """CC must survive unification — subcontractor completion-evidence rule needs it."""
    cc_addr = Address(name="Sub Contractor", email="sub@example.com")
    msg = _make_msg("001", _T1, cc=[cc_addr])
    result = unify([msg], [])
    u = result[0]
    assert u.source == SOURCE_EMAIL
    assert u.from_ is not None
    assert u.from_.email == "alice@example.com"
    assert len(u.cc) == 1
    assert u.cc[0].email == "sub@example.com"
    assert u.to[0].email == "bob@example.com"


def test_email_subject_preserved():
    msg = _make_msg("001", _T1, subject="Re: Logo design feedback")
    result = unify([msg], [])
    assert result[0].subject == "Re: Logo design feedback"


def test_email_transcript_fields_empty():
    """Email records must have empty transcript fields."""
    msg = _make_msg("001", _T1)
    result = unify([msg], [])
    u = result[0]
    assert u.participants == []
    assert u.filename is None


# ── transcript-specific field preservation ────────────────────────────────────


def test_transcript_participants_preserved():
    rec = _make_rec("call", _T1)
    result = unify([], [rec])
    u = result[0]
    assert u.source == SOURCE_TRANSCRIPT
    assert u.participants == ["Alice", "Bob"]
    assert u.filename == "call.vtt"


def test_transcript_email_fields_empty():
    """Transcript records must have None/empty email fields."""
    rec = _make_rec("call", _T1)
    result = unify([], [rec])
    u = result[0]
    assert u.from_ is None
    assert u.to == []
    assert u.cc == []
    assert u.subject is None


# ── id and thread_id pass-through ─────────────────────────────────────────────


def test_ids_pass_through():
    msg = _make_msg("042", _T1)
    rec = _make_rec("2026-03-05_call", _T2)
    result = unify([msg], [rec])
    assert result[0].id == "INBOX/042"
    assert result[1].id == "transcripts/2026-03-05_call"
