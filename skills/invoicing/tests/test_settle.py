"""
Unit tests for settlement reconciliation (task 1.9, docs/02 §C).

Covers the second-run edit cases (qty edit, deleted line, deleted invoice, orphan),
the proforma→invoice linkage (primary linkedDocumentIds + content fallback),
status recompute, silent orphans for unmanaged clients, and a create→settle
round-trip through the handoff.
"""

from pathlib import Path

import pytest

from invoicing_rules.handoff import build_proforma_requests, create_and_record
from invoicing_rules.settle import fetch_issued_invoices, settle_ledger
from invoicing_rules.state import (
    ClientProfile,
    LedgerItem,
    load_agreements,
    load_client_profiles,
    load_price_book,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _profiles():
    return load_client_profiles(_FIXTURES / "client_profiles.csv")


def _pending(item_id: str, bill_to: str, proforma_doc_ref: str, **over) -> LedgerItem:
    """A ledger item carrying a pending proforma (post-RECORD, pre-settlement)."""
    defaults = dict(
        end_client=None,
        description="Some work",
        assignee="self",
        item_kind="unit_based",
        billing_mode=None,
        unit_price=None,
        currency="USD",
        price_source="price_book",
        price_ref=None,
        total_qty=None,
        qty_billed_to_date=None,
        last_billed_month=None,
        status_agent="complete",
        completion_evidence="email-x",
        confidence="high",
        qty_proposed=None,
        status_confirmed="complete",
        decision="bill",
        qty_approved=1.0,
        qty_billed_actual=None,
        morning_doc_ref=None,
        proforma_doc_ref=proforma_doc_ref,
        notes=None,
    )
    defaults.update(over)
    return LedgerItem(item_id=item_id, bill_to=bill_to, **defaults)


def _line(desc: str, qty: float, unit_price: float) -> dict:
    # morning READ shape: `price` is the UNIT price; `amount`/`amountTotal` is the
    # line total. (The create payload differs — settlement reads, so it uses this.)
    return {
        "description": desc,
        "quantity": qty,
        "price": unit_price,
        "amount": round(qty * unit_price, 2),
        "amountTotal": round(qty * unit_price, 2),
    }


def _invoice(
    doc_id: str,
    client_id: str,
    linked: list[str],
    lines: list[dict],
    month="2026-03-15",
    status: int = 0,
) -> dict:
    # `linkedDocuments` is the morning read shape: a list of {id, type, ...} objects.
    return {
        "id": doc_id,
        "type": 305,
        "status": status,
        "client": {"id": client_id},
        "linkedDocuments": [{"id": pid, "type": 300} for pid in linked],
        "income": lines,
        "documentDate": month,
    }


# ── qty edit (issued qty differs from approved) ──────────────────────────────


def test_settle_qty_edit():
    item = _pending(
        "SPRIG-ACME-web-001",
        "SPRIG",
        "pf-1",
        end_client="ACME",
        description="Scrollable website",
        item_kind="fixed_quote",
        billing_mode="partial",
        total_qty=1.0,
        qty_billed_to_date=0.3,
        qty_approved=0.4,
    )
    ledger = [item]
    # User edited the issued quantity 0.4 → 0.35 in morning.
    inv = _invoice(
        "inv-2001",
        "morning-sprig-001",
        ["pf-1"],
        [
            _line("------------ ACME ------------", 1, 0.0),
            _line("Scrollable website - next payment (70% so far)", 0.35, 12000),
        ],
    )
    report = settle_ledger(ledger, [inv], _profiles())

    assert item.qty_billed_actual == 0.35
    assert item.qty_billed_to_date == pytest.approx(0.65)  # 0.3 + 0.35
    assert item.morning_doc_ref == "inv-2001"
    assert item.proforma_doc_ref is None  # settled
    assert item.status_confirmed == "in_progress"
    assert item.last_billed_month == "2026-03"
    assert report.settled == ["SPRIG-ACME-web-001"]
    assert len(report.qty_edits) == 1
    assert report.qty_edits[0].qty_approved == 0.4
    assert report.qty_edits[0].qty_billed_actual == 0.35


def test_settle_full_completion_marks_complete():
    item = _pending(
        "SPRIG-ACME-web-001",
        "SPRIG",
        "pf-1",
        description="Scrollable website",
        item_kind="fixed_quote",
        billing_mode="partial",
        total_qty=1.0,
        qty_billed_to_date=0.6,
        qty_approved=0.4,
    )
    inv = _invoice(
        "inv-2001",
        "morning-sprig-001",
        ["pf-1"],
        [_line("Scrollable website - 2nd and final payment", 0.4, 12000)],
    )
    settle_ledger([item], [inv], _profiles())
    assert item.qty_billed_to_date == 1.0
    assert item.status_confirmed == "complete"


def test_settle_no_qty_edit_when_matches():
    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "pf-1",
        description="Roll-up banners",
        qty_approved=3.0,
    )
    inv = _invoice(
        "inv-2001", "morning-sprig-001", ["pf-1"], [_line("Roll-up banners", 3.0, 400)]
    )
    report = settle_ledger([item], [inv], _profiles())
    assert report.settled == ["SPRIG-ACME-rollup-001"]
    assert report.qty_edits == []


# ── deleted line / deleted invoice → revert ──────────────────────────────────


def test_settle_deleted_line_reverts():
    """Invoice issued (links the proforma) but this item's line was removed."""
    item = _pending(
        "SPRIG-ACME-web-001",
        "SPRIG",
        "pf-1",
        description="Scrollable website",
        item_kind="fixed_quote",
        billing_mode="partial",
        total_qty=1.0,
        qty_billed_to_date=0.3,
        qty_approved=0.4,
    )
    # The invoice exists and links pf-1, but only has a DIFFERENT line.
    inv = _invoice(
        "inv-2001",
        "morning-sprig-001",
        ["pf-1"],
        [_line("Some other work", 1.0, 500)],
    )
    report = settle_ledger([item], [inv], _profiles())
    assert item.proforma_doc_ref is None  # no longer pending
    assert item.qty_billed_to_date == 0.3  # unchanged — work not lost
    assert item.morning_doc_ref is None
    assert report.reverted == ["SPRIG-ACME-web-001"]
    assert report.settled == []


def test_settle_deleted_invoice_reverts():
    """Proforma confirmed gone (no invoice, not among live proformas) → revert."""
    item = _pending(
        "SPRIG-ACME-web-001",
        "SPRIG",
        "pf-1",
        description="Scrollable website",
        qty_billed_to_date=0.3,
    )
    # live_proforma_ids=set() → the proforma no longer exists in morning.
    report = settle_ledger([item], [], _profiles(), live_proforma_ids=set())
    assert item.proforma_doc_ref is None
    assert item.qty_billed_to_date == 0.3
    assert report.reverted == ["SPRIG-ACME-web-001"]


def test_settle_unconverted_proforma_stays_pending():
    """A live proforma not yet converted to an invoice must NOT be reverted — doing so
    would re-propose it and create a duplicate (morning has no API delete)."""
    item = _pending(
        "SPRIG-ACME-web-001",
        "SPRIG",
        "pf-1",
        description="Scrollable website",
        qty_billed_to_date=0.3,
    )
    report = settle_ledger([item], [], _profiles(), live_proforma_ids={"pf-1"})
    assert item.proforma_doc_ref == "pf-1"  # still pending, untouched
    assert item.qty_billed_to_date == 0.3
    assert report.still_pending == ["SPRIG-ACME-web-001"]
    assert report.reverted == []


def test_settle_unknown_liveness_is_conservative():
    """With no liveness info (live_proforma_ids=None), never revert on a missing
    invoice — stay pending rather than risk a duplicate."""
    item = _pending(
        "SPRIG-ACME-web-001", "SPRIG", "pf-1", description="Scrollable website"
    )
    report = settle_ledger(
        [item], [], _profiles()
    )  # live_proforma_ids defaults to None
    assert item.proforma_doc_ref == "pf-1"
    assert report.still_pending == ["SPRIG-ACME-web-001"]
    assert report.reverted == []


# ── orphans ──────────────────────────────────────────────────────────────────


def test_settle_orphan_managed_backfilled():
    """An issued line with no pending ledger item, on a managed client → flagged row."""
    inv = _invoice(
        "inv-2002",
        "morning-sprig-001",
        [],
        [_line("Extra banner added by phone", 2.0, 400)],
    )
    ledger: list[LedgerItem] = []
    report = settle_ledger(ledger, [inv], _profiles())

    assert len(report.orphans_flagged) == 1
    assert len(ledger) == 1
    orphan = ledger[0]
    assert orphan.item_id == "ORPHAN-inv-2002-0"
    assert orphan.bill_to == "SPRIG"
    assert orphan.description == "Extra banner added by phone"
    assert orphan.status_confirmed == "complete"
    assert orphan.qty_billed_actual == 2.0
    assert orphan.qty_billed_to_date == 2.0
    assert orphan.unit_price == 400  # read from the income line's `price` field
    assert orphan.morning_doc_ref == "inv-2002"
    assert orphan.notes == "added manually in morning"


def test_settle_orphan_unmanaged_silent():
    """Unmanaged client (e.g. utilities) → recorded silently, no ledger row, no flag."""
    profiles = {
        "UTILITY": ClientProfile(
            bill_to="UTILITY",
            is_agency=False,
            managed_by_agent=False,
            language="he",
            currency="ILS",
            vat_rate=0.18,
            morning_client_id="morning-util-001",
            notes=None,
        )
    }
    inv = _invoice(
        "inv-3000", "morning-util-001", [], [_line("Electricity July", 1.0, 250)]
    )
    ledger: list[LedgerItem] = []
    report = settle_ledger(ledger, [inv], profiles)
    assert report.orphans_flagged == []
    assert report.orphans_silent == ["inv-3000#0"]
    assert ledger == []  # nothing back-filled


def test_settle_subtitle_line_not_treated_as_orphan():
    """The zero-priced agency subtitle line must never become an orphan row."""
    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "pf-1",
        description="Roll-up banners",
        qty_approved=3.0,
    )
    inv = _invoice(
        "inv-2001",
        "morning-sprig-001",
        ["pf-1"],
        [
            _line("------------ ACME ------------", 1, 0.0),
            _line("Roll-up banners", 3.0, 400),
        ],
    )
    ledger = [item]
    report = settle_ledger(ledger, [inv], _profiles())
    assert report.orphans_flagged == []
    assert len(ledger) == 1  # no orphan appended for the subtitle


def test_settle_content_match_tolerates_typo_and_affixes():
    """Real invoice lines carry end-client prefixes, payment-stage suffixes, and typos
    ('Landind' vs 'Landing'). Token-overlap content-matches them where startswith could
    not. linkedDocuments empty (as on every real invoice) → forces the content path."""
    item = _pending(
        "SPRIG-VERGE-rovo-landing",
        "SPRIG",
        "pf-1",
        end_client="Verge",
        description="RoVo - Landing page",
        item_kind="fixed_quote",
        billing_mode="partial",
        total_qty=1.0,
        qty_billed_to_date=0.6,
        qty_approved=0.4,
    )
    inv = _invoice(
        "inv-1",
        "morning-sprig-001",
        [],  # no proforma link → only content-matching can settle it
        [_line("RoVo - Landind page* - 2nd & last payment", 0.4, 3500)],
    )
    report = settle_ledger([item], [inv], _profiles())
    assert report.settled == ["SPRIG-VERGE-rovo-landing"]
    assert item.qty_billed_to_date == 1.0


# ── content fallback (linkedDocumentIds absent) ──────────────────────────────


def test_settle_content_fallback_matches_by_client_and_desc():
    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "pf-unlinked",
        description="Roll-up banners",
        qty_approved=3.0,
    )
    # Invoice does NOT link the proforma (deleted before linking), but same client +
    # matching description line.
    inv = _invoice(
        "inv-2009", "morning-sprig-001", [], [_line("Roll-up banners", 3.0, 400)]
    )
    report = settle_ledger([item], [inv], _profiles())
    assert report.settled == ["SPRIG-ACME-rollup-001"]
    assert item.morning_doc_ref == "inv-2009"
    assert item.proforma_doc_ref is None


# ── report + idempotency ─────────────────────────────────────────────────────


def test_settle_summary_text():
    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "pf-1",
        description="Roll-up banners",
        qty_approved=3.0,
    )
    inv = _invoice(
        "inv-2001", "morning-sprig-001", ["pf-1"], [_line("Roll-up banners", 3.0, 400)]
    )
    report = settle_ledger([item], [inv], _profiles())
    s = report.summary()
    assert "1 settled" in s
    assert "Ledger updated" in s


def test_settle_second_run_is_noop():
    """After settlement clears proforma_doc_ref, a re-run touches nothing."""
    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "pf-1",
        description="Roll-up banners",
        qty_approved=3.0,
    )
    inv = _invoice(
        "inv-2001", "morning-sprig-001", ["pf-1"], [_line("Roll-up banners", 3.0, 400)]
    )
    settle_ledger([item], [inv], _profiles())
    billed_after_first = item.qty_billed_to_date
    report2 = settle_ledger([item], [inv], _profiles())
    assert report2.settled == []
    assert item.qty_billed_to_date == billed_after_first  # no double-accumulation


# ── create → settle round-trip ───────────────────────────────────────────────


def test_create_then_settle_roundtrip(tmp_path):
    profiles = _profiles()
    pb = load_price_book(_FIXTURES / "price_book.csv")
    agrs = load_agreements(_FIXTURES / "agreements.csv")

    item = _pending(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        "",  # not yet created
        end_client="ACME",
        description="Roll-up banners",
        price_ref="2025-trade-rollup",
        qty_approved=3.0,
    )
    item.proforma_doc_ref = None
    ledger = [item]

    # CREATE + RECORD.
    requests = build_proforma_requests(ledger, profiles, pb, agrs, "2026-03")
    out = tmp_path / "ledger.csv"
    create_and_record(
        None,
        requests,
        ledger,
        out,
        create_fn=lambda _c, _r: {"id": "pf-99", "type": 300},
    )
    assert item.proforma_doc_ref == "pf-99"

    # Human converts proforma pf-99 → invoice inv-5001 in morning, then SETTLE.
    inv = _invoice(
        "inv-5001", "morning-sprig-001", ["pf-99"], [_line("Roll-up banners", 3.0, 400)]
    )
    report = settle_ledger(ledger, [inv], profiles)

    assert report.settled == ["SPRIG-ACME-rollup-001"]
    assert item.morning_doc_ref == "inv-5001"
    assert item.proforma_doc_ref is None
    assert item.qty_billed_to_date == 3.0


# ── fetch_issued_invoices: status semantics (real read shape) ────────────────


def test_fetch_issued_invoices_keeps_status_0_drops_cancelled(monkeypatch):
    """A type-305 is fiscally issued at status 0 (unpaid); only CANCELLED (4) is
    excluded. The previous status=[closed] filter would have returned nothing."""
    captured = {}

    def fake_search(client, **kwargs):
        captured.update(kwargs)
        return {
            "items": [
                {"id": "inv-a", "type": 305, "status": 0},  # issued, unpaid
                {"id": "inv-b", "type": 305, "status": 1},  # issued, paid
                {"id": "inv-c", "type": 305, "status": 4},  # cancelled → drop
            ]
        }

    monkeypatch.setattr("morning_bridge.reads.search_documents", fake_search)
    out = fetch_issued_invoices(object(), from_date="2026-04-01")

    assert {d["id"] for d in out} == {"inv-a", "inv-b"}
    assert "status" not in captured  # we do NOT filter by status in the query
    assert captured["doc_type"] == [305]
