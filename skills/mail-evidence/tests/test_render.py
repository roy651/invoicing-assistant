from datetime import datetime, timezone

from mail_evidence.records import EvidenceRecord
from mail_evidence.render import render_corpus


def _email(id_, thread_id, day, frm, to, subject, body):
    return EvidenceRecord(
        id=id_,
        thread_id=thread_id,
        source="email",
        date=datetime(2026, 5, day, 9, 0, tzinfo=timezone.utc),
        body_text=body,
        from_=frm,
        to=[to],
        subject=subject,
    )


def test_groups_by_thread_and_orders_by_earliest():
    # Two threads, records given out of order; thread B opens earlier than thread A.
    records = [
        _email("a2", "A", 10, "x@a.com", "y@b.com", "Re: Logo", "second in A"),
        _email("b1", "B", 3, "p@a.com", "q@b.com", "Brochure", "first in B"),
        _email("a1", "A", 5, "y@b.com", "x@a.com", "Logo", "first in A"),
    ]
    out = render_corpus(records)

    assert "== WT 1/2" in out and "== WT 2/2" in out
    # Thread B (earliest message 05-03) renders first.
    assert out.index("== WT 1/2") < out.index("Brochure")
    assert out.index("Brochure") < out.index("Logo")
    # Records within thread A are date-sorted: "first in A" before "second in A".
    assert out.index("first in A") < out.index("second in A")


def test_record_format_and_body_truncation():
    rec = _email(
        "a1",
        "A",
        5,
        "molly@sprigconsulting.com",
        "avigail@ula.co.il",
        "FW: Headshot",
        "word " * 300,
    )
    out = render_corpus(records=[rec], body_chars=40)

    assert "[05-05] molly@sprigconsulting.co>avigail@ula.co.il | FW: Headshot" in out
    body_line = [ln for ln in out.splitlines() if ln.startswith("  ")][0]
    assert body_line.endswith("…")
    assert len(body_line) <= 2 + 40 + 2  # indent + limit + ellipsis


def test_transcript_record_renders_without_addresses():
    rec = EvidenceRecord(
        id="t1",
        thread_id="t1",
        source="transcript",
        date=datetime(2026, 5, 7, tzinfo=timezone.utc),
        body_text="call notes",
        filename="2026-05-07-call.txt",
    )
    out = render_corpus([rec])
    assert "[05-07] 2026-05-07-call.txt | (transcript)" in out
    assert "call notes" in out


def test_empty_input_is_empty_string():
    assert render_corpus([]) == ""
