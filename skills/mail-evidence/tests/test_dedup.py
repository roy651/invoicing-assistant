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
    """A substantial >-prefixed quote matching a sibling body is removed."""
    original = (
        "The scrollable website design is approved and ready for invoicing this month."
    )
    t = _make_thread(
        original,  # record 0
        f"> {original}\n\nI agree, sending it over.",  # record 1 quotes record 0
    )
    result = dedup_in_thread(t)
    assert result.records[0].body_text == original  # record 0 unchanged
    reply_body = result.records[1].body_text
    assert "I agree, sending it over." in reply_body
    assert original not in reply_body


def test_short_quote_kept_even_if_in_sibling():
    """A short quoted block (< min tokens) is never stripped, even when it matches a
    sibling — it could coincide by chance, and losing it from a forward is evidence
    loss (§6.4). Bias is toward keeping."""
    t = _make_thread(
        "Thanks, approved.",  # record 0
        "> Thanks, approved.\n\nForwarding this for your records.",  # record 1
    )
    result = dedup_in_thread(t)
    assert "> Thanks, approved." in result.records[1].body_text


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
    original = "The logo design is approved and the final files are attached here."
    t = _make_thread(
        original,
        f"On Mon, 1 Apr 2026, Alice wrote:\n> {original}\n\nGreat news!",
    )
    result = dedup_in_thread(t)
    reply_body = result.records[1].body_text
    assert "Great news!" in reply_body
    assert original not in reply_body


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
    in_thread = (
        "Here is the project brief with the full scope and timeline for your review."
    )
    external = "Some forwarded context from a different thread that no sibling record contains."
    t = _make_thread(
        in_thread,
        f"> {in_thread}\n\nMy response to the brief.\n\n> {external}\n\nEnd.",
    )
    result = dedup_in_thread(t)
    body = result.records[1].body_text
    assert "My response to the brief." in body
    assert "End." in body
    assert in_thread not in body  # in-thread repeat stripped
    # External context is kept because it doesn't match any sibling
    assert external in body


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
