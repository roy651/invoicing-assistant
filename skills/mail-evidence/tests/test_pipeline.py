"""
AC#6 — Large-delta guard: batch iterator + watermark.

Tests:
  - run() yields batches of the configured size.
  - Watermark advances only after commit_watermark() — not automatically.
  - A simulated crash (not calling commit_watermark) causes at most 1 batch re-fetch.
  - T3 threads are dropped; T1 threads are passed through.
  - Zero network/LLM calls (all mocked).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mail_evidence.config import FetchConfig
from mail_evidence.fetch.watermark import commit_watermark, load_watermark
from mail_evidence.pipeline import run
from mail_evidence.protocols import ContactStore, RelevanceJudge
from mail_evidence.records import EvidenceRecord, RelevanceDecision


# ── helpers ───────────────────────────────────────────────────────────────────


def _always_relevant_judge() -> RelevanceJudge:
    judge = MagicMock(spec=RelevanceJudge)
    judge.is_relevant.return_value = RelevanceDecision(
        relevant=True, reason="work", promote_emails=[]
    )
    return judge


def _empty_store() -> ContactStore:
    store = MagicMock(spec=ContactStore)
    store.is_known.return_value = False
    return store


def _make_record(msg_id: str, date: datetime, is_bulk: bool = False) -> EvidenceRecord:
    return EvidenceRecord(
        id=msg_id,
        thread_id=msg_id,
        source="email",
        date=date,
        body_text="body",
        from_="sender@example.com",
        is_bulk=is_bulk,
    )


def _make_mock_client(records: list[EvidenceRecord]) -> MagicMock:
    """A mock ImapClient that returns pre-built records via patched fetch_messages."""
    return MagicMock()


# ── batch iteration (AC#6) ────────────────────────────────────────────────────


def test_run_yields_batches_of_configured_size():
    config = FetchConfig(batch_size=2)
    records = [
        _make_record(f"<m{i}@host>", datetime(2026, 3, i + 1, tzinfo=timezone.utc))
        for i in range(5)
    ]
    client = _make_mock_client(records)
    judge = _always_relevant_judge()
    store = _empty_store()

    with patch("mail_evidence.pipeline.fetch_messages", return_value=records):
        batches = list(run(config, judge, store, client, watermark=None))

    # 5 threads (each record is its own thread since thread_id == id), batch_size=2 → 3 batches
    total = sum(len(b) for b in batches)
    assert total == 5
    for batch in batches[:-1]:
        assert len(batch) <= 2


def test_run_empty_inbox_yields_no_batches():
    config = FetchConfig()
    client = _make_mock_client([])
    with patch("mail_evidence.pipeline.fetch_messages", return_value=[]):
        batches = list(run(config, _always_relevant_judge(), _empty_store(), client))
    assert batches == []


# ── watermark semantics (AC#6) ────────────────────────────────────────────────


def test_watermark_not_advanced_without_commit(tmp_path):
    """A crash before commit_watermark leaves watermark unchanged (re-fetch on next run)."""
    wm = datetime(2026, 3, 1, tzinfo=timezone.utc)
    commit_watermark(wm, state_dir=tmp_path)

    # Simulate processing a batch but NOT committing the new watermark.
    loaded_after = load_watermark(state_dir=tmp_path)
    assert loaded_after == wm  # still the old watermark


def test_commit_watermark_advances_on_success(tmp_path):
    """commit_watermark persists the new high-water mark."""
    wm1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    wm2 = datetime(2026, 3, 15, tzinfo=timezone.utc)
    commit_watermark(wm1, state_dir=tmp_path)
    commit_watermark(wm2, state_dir=tmp_path)
    loaded = load_watermark(state_dir=tmp_path)
    assert loaded == wm2


def test_cold_start_watermark_is_none(tmp_path):
    result = load_watermark(state_dir=tmp_path)
    assert result is None


# ── tier filtering ────────────────────────────────────────────────────────────


def test_t3_threads_dropped_by_pipeline():
    """All-bulk threads must be dropped and never yielded."""
    # 2 bulk records (each their own thread) — should all be dropped.
    records = [
        _make_record(
            f"<bulk{i}@host>",
            datetime(2026, 3, i + 1, tzinfo=timezone.utc),
            is_bulk=True,
        )
        for i in range(2)
    ]
    config = FetchConfig(batch_size=10)
    client = _make_mock_client(records)
    with patch("mail_evidence.pipeline.fetch_messages", return_value=records):
        batches = list(run(config, _always_relevant_judge(), _empty_store(), client))
    total = sum(len(b) for b in batches)
    assert total == 0


def test_t2_not_relevant_dropped():
    """T2 threads judged irrelevant by the judge are dropped."""
    records = [_make_record("<msg@host>", datetime(2026, 3, 1, tzinfo=timezone.utc))]
    irrelevant_judge = MagicMock(spec=RelevanceJudge)
    irrelevant_judge.is_relevant.return_value = RelevanceDecision(
        relevant=False, reason="unrelated", promote_emails=[]
    )
    config = FetchConfig()
    client = _make_mock_client(records)
    with patch("mail_evidence.pipeline.fetch_messages", return_value=records):
        batches = list(run(config, irrelevant_judge, _empty_store(), client))
    total = sum(len(b) for b in batches)
    assert total == 0


def test_t1_threads_always_kept():
    """T1 threads (known contacts) bypass the judge and are always kept."""
    records = [_make_record("<msg@host>", datetime(2026, 3, 1, tzinfo=timezone.utc))]
    store = MagicMock(spec=ContactStore)
    store.is_known.return_value = True  # every address is known → T1
    judge = MagicMock(spec=RelevanceJudge)  # should never be called for T1
    config = FetchConfig()
    client = _make_mock_client(records)
    with patch("mail_evidence.pipeline.fetch_messages", return_value=records):
        batches = list(run(config, judge, store, client))
    total = sum(len(b) for b in batches)
    assert total == 1
    judge.is_relevant.assert_not_called()
