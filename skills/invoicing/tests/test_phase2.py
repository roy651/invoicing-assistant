"""
Tests for the Phase-2 offline validation harness (task 1.11 Deliverable B).

Exercises the full harness on the committed synthetic fixture (end-to-end PASS,
settlement reconciliation, the model seam), the no-auto-bill guard, and the scorer's
ability to CATCH each metric violation.
"""

from pathlib import Path

from invoicing_rules.packet import (
    BillToGroup,
    EndClientGroup,
    ProposedLine,
    ReviewPacket,
)
from invoicing_rules.phase2 import (
    ReplayReasoner,
    discover_fixtures,
    ingest_evidence,
    run_harness,
    score,
)
from invoicing_rules.state import LedgerItem, load_ledger

_FIXTURES = Path(__file__).parent / "fixtures" / "phase2"
_MONTH = "2026-03"


def _fx():
    return discover_fixtures(_FIXTURES)


def _item(item_id, **over) -> LedgerItem:
    base = dict(
        bill_to="SPRIG",
        end_client="ACME",
        description="Scrollable website",
        assignee="self",
        item_kind="fixed_quote",
        billing_mode="partial",
        unit_price=None,
        currency="USD",
        price_source="negotiated",
        price_ref="agr-sprig-acme-web-001",
        total_qty=1.0,
        qty_billed_to_date=0.3,
        last_billed_month=None,
        status_agent=None,
        completion_evidence=None,
        confidence=None,
        qty_proposed=None,
        status_confirmed=None,
        decision=None,
        qty_approved=None,
        qty_billed_actual=None,
        morning_doc_ref=None,
        proforma_doc_ref=None,
        notes=None,
    )
    base.update(over)
    return LedgerItem(item_id=item_id, **base)


def _packet_with_line(item_id, unit_price, price_ref) -> ReviewPacket:
    line = ProposedLine(
        item_id=item_id,
        bill_to="SPRIG",
        end_client="ACME",
        description="Scrollable website",
        item_kind="fixed_quote",
        billing_mode="partial",
        qty_proposed=0.4,
        unit_price=unit_price,
        line_total=None,
        currency="USD",
        status_agent="in_progress",
        confidence="high",
        completion_evidence="e",
        price_ref=price_ref,
        price_source="negotiated",
    )
    return ReviewPacket(
        generated_at=None,  # not read by the scorer
        billing_month=_MONTH,
        groups=[
            BillToGroup(
                bill_to="SPRIG",
                is_agency=True,
                language="en",
                currency="USD",
                vat_rate=0.0,
                morning_client_id="morning-sprig-001",
                end_client_groups=[EndClientGroup(end_client="ACME", lines=[line])],
            )
        ],
    )


# ── end-to-end on the synthetic fixture ──────────────────────────────────────


def test_harness_end_to_end_passes():
    report = run_harness(_fx(), ReplayReasoner(_fx().annotations), billing_month=_MONTH)
    assert report.passed, report.render()
    names = {m.name for m in report.metrics}
    assert names == {
        "grouping",
        "price_on_resolved",
        "item_precision_recall",
        "no_false_complete",
        "no_auto_bill",
    }
    # P/R counts the agent-CREATED item (rollup-002), not just annotated rows.
    pr = next(m for m in report.metrics if m.name == "item_precision_recall")
    assert "produced=2" in pr.detail


def test_reasoner_appends_new_agent_identified_item():
    """The seam must support NEW work found in evidence, not just annotation of
    pre-existing rows — and never gate it."""
    fx = _fx()
    ledger = load_ledger(fx.opening_ledger)
    assert "SPRIG-ACME-rollup-002" not in {it.item_id for it in ledger}
    ReplayReasoner(fx.annotations).annotate(ledger, [])
    new = next(it for it in ledger if it.item_id == "SPRIG-ACME-rollup-002")
    assert new.status_agent == "complete"
    assert new.qty_proposed == 5.0
    assert new.end_client == "ACME"
    assert new.price_ref == "2025-trade-rollup"
    # The agent identifies work; it never sets gate columns.
    assert new.decision is None
    assert new.qty_approved is None
    assert new.status_confirmed is None


def test_harness_settles_pending_proforma():
    report = run_harness(_fx(), ReplayReasoner(_fx().annotations), billing_month=_MONTH)
    assert report.settlement is not None
    assert report.settlement.settled == ["SPRIG-ACME-rollup-001"]


def test_ingest_evidence_unifies_cross_folder_thread():
    evidence = ingest_evidence(_fx())
    # The INBOX parent and the Sent reply share one thread_id.
    assert len(evidence) == 2
    assert len({e.thread_id for e in evidence}) == 1
    # CC (the subcontractor signal) survived ingestion.
    assert all("sub@studio.com" in e.cc for e in evidence)


# ── the no-auto-bill guard trips on a misbehaving reasoner ───────────────────


def test_no_auto_bill_guard_detects_gate_write():
    class BadReasoner:
        def annotate(self, ledger, evidence):
            for it in ledger:
                it.status_agent = "in_progress"
                it.decision = "bill"  # ← reasoning must never write a gate column

    report = run_harness(_fx(), BadReasoner(), billing_month=_MONTH)
    no_auto = next(m for m in report.metrics if m.name == "no_auto_bill")
    assert no_auto.passed is False
    assert report.passed is False


def test_no_auto_bill_guard_detects_gated_new_item():
    """A NEW item is fine, but not one the agent already gated."""

    class GatingCreator:
        def annotate(self, ledger, evidence):
            ledger.append(
                _item(
                    "NEW-1", status_agent="complete", decision="bill", qty_approved=1.0
                )
            )

    report = run_harness(_fx(), GatingCreator(), billing_month=_MONTH)
    no_auto = next(m for m in report.metrics if m.name == "no_auto_bill")
    assert no_auto.passed is False


# ── scorer catches each violation ────────────────────────────────────────────


def test_score_catches_false_complete():
    produced = [_item("X", status_agent="complete")]
    expected = [_item("X", status_agent="in_progress")]
    report = score(produced, expected, ReviewPacket(None, _MONTH), no_auto_bill=True)
    m = next(m for m in report.metrics if m.name == "no_false_complete")
    assert m.passed is False


def test_score_catches_price_mismatch():
    produced = [_item("X", status_agent="in_progress")]
    expected = [_item("X", status_agent="in_progress", unit_price=12000.0)]
    packet = _packet_with_line(
        "X", unit_price=999.0, price_ref="agr-sprig-acme-web-001"
    )
    report = score(produced, expected, packet, no_auto_bill=True)
    m = next(m for m in report.metrics if m.name == "price_on_resolved")
    assert m.passed is False


def test_score_catches_low_recall():
    # Agent identified nothing; oracle expected one item → recall 0.
    produced = [_item("X", status_agent=None)]
    expected = [_item("X", status_agent="in_progress")]
    report = score(produced, expected, ReviewPacket(None, _MONTH), no_auto_bill=True)
    m = next(m for m in report.metrics if m.name == "item_precision_recall")
    assert m.passed is False


def test_score_catches_grouping_mismatch():
    produced = [_item("X", status_agent="in_progress", end_client="WRONG")]
    expected = [_item("X", status_agent="in_progress", end_client="ACME")]
    report = score(produced, expected, ReviewPacket(None, _MONTH), no_auto_bill=True)
    m = next(m for m in report.metrics if m.name == "grouping")
    assert m.passed is False
