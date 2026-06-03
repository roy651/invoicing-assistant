"""Unit tests for evidence.unify() (rewired in 1.6.6 onto mail-evidence)."""

from datetime import datetime, timezone

from invoicing_rules.evidence import unify
from mail_evidence.records import EvidenceRecord, Thread


def _email(
    msg_id: str,
    date: datetime,
    *,
    thread_id: str | None = None,
    cc: list[str] | None = None,
    subject: str = "Test subject",
) -> EvidenceRecord:
    return EvidenceRecord(
        id=msg_id,
        thread_id=thread_id or msg_id,
        source="email",
        date=date,
        body_text="Some email body",
        from_="alice@example.com",
        to=["bob@example.com"],
        cc=cc or [],
        subject=subject,
        # mail-evidence populates participants as the convenience union for emails.
        participants=["alice@example.com", "bob@example.com", *(cc or [])],
    )


def _thread(*records: EvidenceRecord) -> Thread:
    tid = records[0].thread_id
    return Thread(thread_id=tid, records=list(records), tier="T1")


def _transcript(stem: str, date: datetime) -> EvidenceRecord:
    rid = f"transcripts/{stem}"
    return EvidenceRecord(
        id=rid,
        thread_id=rid,
        source="transcript",
        date=date,
        body_text="Some transcript text",
        participants=["Alice", "Bob"],
        filename=f"{stem}.vtt",
    )


_T1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T2 = datetime(2026, 3, 5, tzinfo=timezone.utc)
_T3 = datetime(2026, 3, 10, tzinfo=timezone.utc)


# ── basic merging and sorting ─────────────────────────────────────────────────


def test_unify_empty():
    assert unify([], []) == []


def test_unify_emails_only():
    threads = [_thread(_email("<m2@h>", _T2)), _thread(_email("<m1@h>", _T1))]
    result = unify(threads, [])
    assert len(result) == 2
    assert result[0].date == _T1  # sorted ascending
    assert result[1].date == _T2
    assert all(r.source == "email" for r in result)


def test_unify_transcripts_only():
    recs = [_transcript("call-b", _T3), _transcript("call-a", _T1)]
    result = unify([], recs)
    assert len(result) == 2
    assert result[0].date == _T1
    assert result[1].date == _T3
    assert all(r.source == "transcript" for r in result)


def test_unify_interleaved_sorted():
    threads = [_thread(_email("<m1@h>", _T1)), _thread(_email("<m3@h>", _T3))]
    recs = [_transcript("call", _T2)]
    result = unify(threads, recs)
    dates = [r.date for r in result]
    assert dates == [_T1, _T2, _T3]
    assert [r.source for r in result] == ["email", "transcript", "email"]


def test_unify_flattens_multi_record_thread():
    """A single thread with several records contributes all of them, date-sorted."""
    early = _email("<early@h>", _T1, thread_id="<root@h>")
    late = _email("<late@h>", _T3, thread_id="<root@h>")
    mid_transcript = _transcript("call", _T2)
    result = unify([_thread(early, late)], [mid_transcript])
    assert [r.id for r in result] == ["<early@h>", "transcripts/call", "<late@h>"]


# ── email-specific field preservation ────────────────────────────────────────


def test_email_from_cc_preserved():
    """CC must survive unification — subcontractor completion-evidence rule needs it."""
    msg = _email("<m1@h>", _T1, cc=["sub@example.com"])
    result = unify([_thread(msg)], [])
    u = result[0]
    assert u.source == "email"
    assert u.from_ == "alice@example.com"
    assert u.cc == ["sub@example.com"]
    assert u.to == ["bob@example.com"]


def test_email_subject_preserved():
    msg = _email("<m1@h>", _T1, subject="Re: Logo design feedback")
    result = unify([_thread(msg)], [])
    assert result[0].subject == "Re: Logo design feedback"


# ── transcript-specific field isolation ───────────────────────────────────────


def test_transcript_participants_preserved():
    result = unify([], [_transcript("call", _T1)])
    u = result[0]
    assert u.source == "transcript"
    assert u.participants == ["Alice", "Bob"]
    assert u.filename == "call.vtt"


def test_transcript_email_fields_empty():
    """Transcript records must have None/empty email fields."""
    result = unify([], [_transcript("call", _T1)])
    u = result[0]
    assert u.from_ is None
    assert u.to == []
    assert u.cc == []
    assert u.subject is None


# ── id and thread_id pass-through ─────────────────────────────────────────────


def test_ids_pass_through():
    msg = _email("<042@h>", _T1)
    rec = _transcript("2026-03-05_call", _T2)
    result = unify([_thread(msg)], [rec])
    assert result[0].id == "<042@h>"
    assert result[1].id == "transcripts/2026-03-05_call"


def test_thread_id_preserved_for_grouping():
    """Records keep their thread_id so the reasoning step can still group by thread."""
    a = _email("<a@h>", _T1, thread_id="<root@h>")
    b = _email("<b@h>", _T2, thread_id="<root@h>")
    result = unify([_thread(a, b)], [])
    assert {r.thread_id for r in result} == {"<root@h>"}
