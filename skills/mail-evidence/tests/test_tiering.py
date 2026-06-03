"""
AC#2 — Tiering inversion invariant.

Tests:
  - Allowlist absence alone NEVER yields T3.
  - T3 requires positive bulk signals on EVERY record.
  - Any known participant → T1 (explicit test for from_, to, cc each).
  - T2 is the fallback for unknown-but-human threads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from mail_evidence.protocols import ContactStore
from mail_evidence.records import EvidenceRecord, Thread
from mail_evidence.tiering import classify_tier


def _make_contact_store(known: set[str]) -> ContactStore:
    store = MagicMock(spec=ContactStore)
    store.is_known.side_effect = lambda email: email.lower() in known
    return store


def _make_thread(
    *records: EvidenceRecord,
    tier: str = "T2",
) -> Thread:
    return Thread(
        thread_id=records[0].thread_id if records else "tid",
        records=list(records),
        tier=tier,  # type: ignore[arg-type]
    )


def _email_record(
    *,
    from_: str = "unknown@stranger.com",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    is_bulk: bool = False,
) -> EvidenceRecord:
    return EvidenceRecord(
        id="<msg@host>",
        thread_id="<tid@host>",
        source="email",
        date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        body_text="body",
        from_=from_,
        to=to or [],
        cc=cc or [],
        is_bulk=is_bulk,
    )


# ── T1: known contact ─────────────────────────────────────────────────────────


def test_known_from_yields_t1():
    rec = _email_record(from_="client@agency.com")
    store = _make_contact_store({"client@agency.com"})
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T1"


def test_known_to_yields_t1():
    rec = _email_record(to=["managed@contact.com"])
    store = _make_contact_store({"managed@contact.com"})
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T1"


def test_known_cc_yields_t1():
    """A known address in CC must trigger T1."""
    rec = _email_record(cc=["subcontractor@studio.com"])
    store = _make_contact_store({"subcontractor@studio.com"})
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T1"


def test_t1_detected_across_multiple_records():
    """Known contact in any record in thread → T1."""
    r1 = _email_record(from_="nobody@unknown.com")
    r2 = _email_record(from_="known@client.com")
    store = _make_contact_store({"known@client.com"})
    thread = _make_thread(r1, r2)
    result = classify_tier(thread, store)
    assert result.tier == "T1"


# ── AC#2 inversion invariant — no T3 on allowlist absence alone ──────────────


def test_allowlist_absence_alone_never_t3():
    """
    AC#2 critical: a thread with no bulk signals must NOT become T3,
    even if no participant is in the allowlist.
    """
    rec = _email_record(from_="stranger@unknown.com", is_bulk=False)
    store = _make_contact_store(set())  # empty allowlist
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier != "T3", (
        "Allowlist absence MUST NOT produce T3 — inversion invariant violated."
    )
    assert result.tier == "T2"


def test_partially_bulk_thread_is_not_t3():
    """T3 requires ALL records to be bulk — one non-bulk record → T2."""
    r_bulk = _email_record(is_bulk=True)
    r_normal = _email_record(is_bulk=False)
    store = _make_contact_store(set())
    thread = _make_thread(r_bulk, r_normal)
    result = classify_tier(thread, store)
    assert result.tier == "T2"


# ── T3: bulk signals on every record ─────────────────────────────────────────


def test_all_bulk_records_yields_t3():
    """T3 requires positive bulk signals on ALL records."""
    r1 = _email_record(is_bulk=True)
    r2 = _email_record(is_bulk=True)
    store = _make_contact_store(set())
    thread = _make_thread(r1, r2)
    result = classify_tier(thread, store)
    assert result.tier == "T3"


def test_single_bulk_record_yields_t3():
    rec = _email_record(is_bulk=True)
    store = _make_contact_store(set())
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T3"


def test_bulk_thread_with_known_contact_yields_t1():
    """
    Even a bulk-flagged record: if sender is a known contact, T1 wins over T3.
    (The user may have subscribed to a client's newsletter.)
    """
    rec = _email_record(from_="client@agency.com", is_bulk=True)
    store = _make_contact_store({"client@agency.com"})
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T1"


# ── T2: fallback ──────────────────────────────────────────────────────────────


def test_unknown_non_bulk_yields_t2():
    rec = _email_record(from_="stranger@unknown.com", is_bulk=False)
    store = _make_contact_store(set())
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result.tier == "T2"


def test_classify_tier_returns_same_thread_object():
    """classify_tier mutates tier in place and returns the same Thread object."""
    rec = _email_record()
    store = _make_contact_store(set())
    thread = _make_thread(rec)
    result = classify_tier(thread, store)
    assert result is thread
