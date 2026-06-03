"""
Tests for assemble.assemble_threads().

Verifies grouping by thread_id, chronological ordering within threads,
and that transcripts with their own thread_id remain isolated.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mail_evidence.assemble import assemble_threads
from mail_evidence.records import EvidenceRecord


def _email(
    uid: str, thread_id: str, date: datetime, body: str = "body"
) -> EvidenceRecord:
    return EvidenceRecord(
        id=uid,
        thread_id=thread_id,
        source="email",
        date=date,
        body_text=body,
        from_="sender@example.com",
    )


def _transcript(stem: str, date: datetime) -> EvidenceRecord:
    tid = f"transcripts/{stem}"
    return EvidenceRecord(
        id=tid,
        thread_id=tid,
        source="transcript",
        date=date,
        body_text="Transcript content",
        participants=["Alice", "Bob"],
        filename=f"{stem}.vtt",
    )


_T1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
_T2 = datetime(2026, 3, 5, tzinfo=timezone.utc)
_T3 = datetime(2026, 3, 10, tzinfo=timezone.utc)


def test_empty_records_returns_empty():
    assert assemble_threads([]) == []


def test_single_record_single_thread():
    rec = _email("<msg1@host>", "<msg1@host>", _T1)
    threads = assemble_threads([rec])
    assert len(threads) == 1
    assert threads[0].thread_id == "<msg1@host>"
    assert len(threads[0].records) == 1


def test_two_records_same_thread_id():
    r1 = _email("<msg1@host>", "<root@host>", _T1)
    r2 = _email("<msg2@host>", "<root@host>", _T2)
    threads = assemble_threads([r1, r2])
    assert len(threads) == 1
    assert threads[0].thread_id == "<root@host>"
    assert len(threads[0].records) == 2


def test_records_sorted_chronologically_within_thread():
    r_late = _email("<late@host>", "<root@host>", _T3)
    r_early = _email("<early@host>", "<root@host>", _T1)
    threads = assemble_threads([r_late, r_early])
    assert threads[0].records[0].date == _T1
    assert threads[0].records[1].date == _T3


def test_threads_sorted_by_earliest_record():
    r_old = _email("<old@host>", "<old@host>", _T1)
    r_new = _email("<new@host>", "<new@host>", _T3)
    # Pass in reverse order.
    threads = assemble_threads([r_new, r_old])
    assert threads[0].thread_id == "<old@host>"
    assert threads[1].thread_id == "<new@host>"


def test_three_separate_threads():
    r1 = _email("<a@host>", "<a@host>", _T1)
    r2 = _email("<b@host>", "<b@host>", _T2)
    r3 = _email("<c@host>", "<c@host>", _T3)
    threads = assemble_threads([r1, r2, r3])
    assert len(threads) == 3


def test_transcript_stays_in_own_thread():
    email_rec = _email("<msg@host>", "<msg@host>", _T1)
    trans_rec = _transcript("2026-03-05_call", _T2)
    threads = assemble_threads([email_rec, trans_rec])
    assert len(threads) == 2
    thread_ids = {t.thread_id for t in threads}
    assert "<msg@host>" in thread_ids
    assert "transcripts/2026-03-05_call" in thread_ids


def test_initial_tier_is_t2():
    """assemble_threads assigns tier='T2' as placeholder — tiering.py overrides."""
    rec = _email("<m@host>", "<m@host>", _T1)
    threads = assemble_threads([rec])
    assert threads[0].tier == "T2"


def test_mixed_email_and_transcript_grouped_correctly():
    r1 = _email("<parent@host>", "<parent@host>", _T1, "Project brief")
    r2 = _email("<reply@host>", "<parent@host>", _T2, "Looks good")
    trans = _transcript("2026-03-10_call", _T3)
    threads = assemble_threads([r1, r2, trans])
    assert len(threads) == 2
    email_thread = next(t for t in threads if t.thread_id == "<parent@host>")
    assert len(email_thread.records) == 2
