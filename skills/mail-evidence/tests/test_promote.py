"""
Tests for promote.condition().

AC#7 — RelevanceJudge and ContactStore are mockable; zero network/LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock

from mail_evidence.promote import condition
from mail_evidence.protocols import ContactStore, RelevanceJudge
from mail_evidence.records import EvidenceRecord, RelevanceDecision, Thread


def _make_thread(tier: str) -> Thread:
    rec = EvidenceRecord(
        id="<msg@host>",
        thread_id="<tid@host>",
        source="email",
        date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        body_text="body",
    )
    return Thread(thread_id="<tid@host>", records=[rec], tier=tier)  # type: ignore[arg-type]


def _judge(relevant: bool, promote: list[str] | None = None) -> RelevanceJudge:
    j = MagicMock(spec=RelevanceJudge)
    j.is_relevant.return_value = RelevanceDecision(
        relevant=relevant,
        reason="test reason",
        promote_emails=promote or [],
    )
    return j


def _store() -> MagicMock:
    return MagicMock(spec=ContactStore)


# ── T1 ────────────────────────────────────────────────────────────────────────


def test_t1_returned_as_is():
    thread = _make_thread("T1")
    judge = _judge(relevant=False)  # should never be called
    result = condition(thread, judge, _store())
    assert result is thread
    judge.is_relevant.assert_not_called()


# ── T3 ────────────────────────────────────────────────────────────────────────


def test_t3_dropped():
    thread = _make_thread("T3")
    judge = _judge(relevant=True)  # should never be called
    result = condition(thread, judge, _store())
    assert result is None
    judge.is_relevant.assert_not_called()


# ── T2 relevant ───────────────────────────────────────────────────────────────


def test_t2_relevant_returned_with_relevance_set():
    thread = _make_thread("T2")
    judge = _judge(relevant=True, promote=["new@contact.com"])
    result = condition(thread, judge, _store())
    assert result is thread
    assert result.relevance is not None
    assert result.relevance.relevant is True


def test_t2_relevant_promotes_contacts():
    thread = _make_thread("T2")
    judge = _judge(relevant=True, promote=["new@contact.com", "other@contact.com"])
    store = _store()
    condition(thread, judge, store)
    store.add_auto.assert_any_call("new@contact.com", reason=ANY)
    store.add_auto.assert_any_call("other@contact.com", reason=ANY)
    assert store.add_auto.call_count == 2


def test_t2_relevant_no_promote_emails_no_add_auto():
    thread = _make_thread("T2")
    judge = _judge(relevant=True, promote=[])
    store = _store()
    condition(thread, judge, store)
    store.add_auto.assert_not_called()


# ── T2 not relevant ───────────────────────────────────────────────────────────


def test_t2_not_relevant_dropped():
    thread = _make_thread("T2")
    judge = _judge(relevant=False)
    result = condition(thread, judge, _store())
    assert result is None


def test_t2_not_relevant_does_not_promote():
    thread = _make_thread("T2")
    judge = _judge(relevant=False, promote=["someone@example.com"])
    store = _store()
    condition(thread, judge, store)
    store.add_auto.assert_not_called()


# ── AC#7 injection — no network/LLM calls ────────────────────────────────────


def test_condition_uses_only_injected_judge():
    """
    AC#7: condition() must be fully driven by the injected judge.
    No hidden LLM/network calls — verified by using a mock judge.
    """
    thread = _make_thread("T2")
    judge = MagicMock(spec=RelevanceJudge)
    judge.is_relevant.return_value = RelevanceDecision(
        relevant=True, reason="ok", promote_emails=[]
    )
    store = _store()
    condition(thread, judge, store)
    judge.is_relevant.assert_called_once_with(thread)
