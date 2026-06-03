"""
AC#3 — In-thread deduplication.

Tests:
  1. In-thread reply quote is stripped.
  2. Forwarded / external quoted history is preserved verbatim.
  3. Original (non-quoted) content is never stripped.
  4. Single-record threads are unchanged.
  5. Attribution-line quotes ("On ... wrote:") are handled.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mail_evidence.dedup import dedup_in_thread
from mail_evidence.records import EvidenceRecord, Thread


def _make_thread(*bodies: str) -> Thread:
    """Build a Thread from a list of body strings (one record per body)."""
    records = []
    for i, body in enumerate(bodies):
        records.append(
            EvidenceRecord(
                id=f"<msg{i}@host>",
                thread_id="<root@host>",
                source="email",
                date=datetime(2026, 3, i + 1, tzinfo=timezone.utc),
                body_text=body,
                from_=f"sender{i}@example.com",
            )
        )
    return Thread(thread_id="<root@host>", records=records, tier="T1")


# ── AC#3 test 1 — in-thread reply quote stripped ─────────────────────────────


def test_in_thread_reply_quote_stripped():
    """A >-prefixed quote matching a sibling body is removed."""
    t = _make_thread(
        "Hello world",  # record 0
        "> Hello world\n\nI agree",  # record 1 quotes record 0
    )
    result = dedup_in_thread(t)
    assert result.records[0].body_text == "Hello world"  # record 0 unchanged
    reply_body = result.records[1].body_text
    assert "I agree" in reply_body
    assert "> Hello world" not in reply_body


# ── AC#3 test 2 — forwarded external quote preserved ─────────────────────────


def test_forwarded_external_quote_preserved_verbatim():
    """A >-quoted block that does NOT match any sibling is kept verbatim."""
    t = _make_thread(
        "Project update: done",  # record 0
        "> John told me something completely unrelated\n\nSee above",  # record 1 — external
    )
    result = dedup_in_thread(t)
    assert (
        "> John told me something completely unrelated" in result.records[1].body_text
    )


# ── AC#3 test 3 — original content never stripped ────────────────────────────


def test_original_content_never_stripped():
    """Record 0's original body is never touched even when record 1 quotes it."""
    t = _make_thread(
        "Original message content",
        "> Original message content\n\nReply here",
    )
    result = dedup_in_thread(t)
    assert result.records[0].body_text == "Original message content"


def test_non_quoted_lines_always_kept():
    """Non->-prefixed lines are never removed."""
    t = _make_thread(
        "First message",
        "> First message\n\nSecond message — my actual reply",
    )
    result = dedup_in_thread(t)
    assert "Second message" in result.records[1].body_text
    assert "my actual reply" in result.records[1].body_text


# ── single-record thread unchanged ────────────────────────────────────────────


def test_single_record_thread_unchanged():
    t = _make_thread("Only message — no siblings")
    result = dedup_in_thread(t)
    assert result.records[0].body_text == "Only message — no siblings"


# ── attribution-line quotes ───────────────────────────────────────────────────


def test_attribution_line_quote_stripped():
    """'On <date>, X wrote:' + >-lines matching a sibling is stripped."""
    t = _make_thread(
        "The logo is approved.",
        "On Mon, 1 Apr 2026, Alice wrote:\n> The logo is approved.\n\nGreat news!",
    )
    result = dedup_in_thread(t)
    reply_body = result.records[1].body_text
    assert "Great news!" in reply_body
    assert "> The logo is approved." not in reply_body


def test_attribution_line_external_kept():
    """Attribution + quote that doesn't match any sibling is preserved."""
    t = _make_thread(
        "My message",
        "On Mon, 1 Apr 2026, External Person wrote:\n> Forwarded context from elsewhere\n\nSee above.",
    )
    result = dedup_in_thread(t)
    assert "Forwarded context from elsewhere" in result.records[1].body_text


# ── multiple quotes in one record ─────────────────────────────────────────────


def test_only_matching_quotes_stripped_in_mixed_record():
    """A record with both an in-thread quote and external history: only in-thread stripped."""
    t = _make_thread(
        "Project brief.",
        "> Project brief.\n\nMy response.\n\n> External context not in thread\n\nEnd.",
    )
    result = dedup_in_thread(t)
    body = result.records[1].body_text
    assert "My response." in body
    assert "End." in body
    assert "> Project brief." not in body
    # External context is kept because it doesn't match any sibling
    assert "> External context not in thread" in body


# ── tier and relevance preserved ─────────────────────────────────────────────


def test_thread_metadata_preserved():
    from mail_evidence.records import RelevanceDecision

    t = _make_thread("Hello", "> Hello\n\nReply")
    t.tier = "T1"
    t.relevance = RelevanceDecision(relevant=True, reason="work", promote_emails=[])
    result = dedup_in_thread(t)
    assert result.tier == "T1"
    assert result.relevance is not None
    assert result.relevance.relevant is True
