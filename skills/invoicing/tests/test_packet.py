"""
Unit tests for packet.build_review_packet().

Validates grouping, flag assignment, progress annotations, exclusions,
and sorting. Does NOT test LLM reasoning — only the mechanical assembly.
"""

from pathlib import Path

import pytest

from invoicing_rules.packet import (
    FLAG_DEFER,
    FLAG_LOW_CONFIDENCE,
    FLAG_PARTIAL_FRACTION,
    FLAG_UNRESOLVED_PRICE,
    build_review_packet,
)
from invoicing_rules.state import (
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_MONTH = "2026-03"


def _load_all():
    return (
        load_ledger(_FIXTURES / "ledger.csv"),
        load_client_profiles(_FIXTURES / "client_profiles.csv"),
        load_price_book(_FIXTURES / "price_book.csv"),
        load_agreements(_FIXTURES / "agreements.csv"),
    )


# ── managed_by_agent exclusion ────────────────────────────────────────────────


def test_managed_false_client_excluded():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    bill_tos = {g.bill_to for g in packet.groups}
    assert "MANUAL_ONLY" not in bill_tos


def test_managed_true_clients_included():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    bill_tos = {g.bill_to for g in packet.groups}
    assert "SPRIG" in bill_tos
    assert "DIRECT_IL" in bill_tos


# ── grouping ──────────────────────────────────────────────────────────────────


def test_sprig_grouped_under_acme_end_client():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    assert sprig.is_agency is True
    ec_names = [ec.end_client for ec in sprig.end_client_groups]
    assert "ACME" in ec_names


def test_direct_il_has_no_end_client():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    direct = next(g for g in packet.groups if g.bill_to == "DIRECT_IL")
    assert len(direct.end_client_groups) == 1
    assert direct.end_client_groups[0].end_client is None


def test_client_profile_metadata_attached():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    assert sprig.language == "en"
    assert sprig.currency == "USD"
    assert sprig.vat_rate == 0.00
    assert sprig.morning_client_id == "morning-sprig-001"


# ── flags ─────────────────────────────────────────────────────────────────────


def test_unresolved_price_flag_on_range_item():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    unresolved_line = next(
        ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-unresolved-001"
    )
    flag_kinds = {f.kind for f in unresolved_line.flags}
    assert FLAG_UNRESOLVED_PRICE in flag_kinds


def test_partial_fraction_flag_on_partial_item():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    web_line = next(ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-web-001")
    flag_kinds = {f.kind for f in web_line.flags}
    assert FLAG_PARTIAL_FRACTION in flag_kinds


def test_deferred_item_has_defer_flag():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    unresolved = next(
        ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-unresolved-001"
    )
    flag_kinds = {f.kind for f in unresolved.flags}
    assert FLAG_DEFER in flag_kinds


def test_clean_item_has_no_critical_flags():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    direct = next(g for g in packet.groups if g.bill_to == "DIRECT_IL")
    logo_line = next(
        ln
        for ln in direct.end_client_groups[0].lines
        if ln.item_id == "DIRECT-logo-001"
    )
    flag_kinds = {f.kind for f in logo_line.flags}
    # High confidence, resolved price, defer billing_mode → no unresolved/low-conf flags
    assert FLAG_UNRESOLVED_PRICE not in flag_kinds
    assert FLAG_LOW_CONFIDENCE not in flag_kinds


# ── progress annotation ───────────────────────────────────────────────────────


def test_progress_annotation_on_partial_item():
    """SPRIG-ACME-web-001: partial fixed_quote, prior billed 30%, proposing 40%."""
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    web_line = next(ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-web-001")
    # Description should contain the cumulative percentage (30+40=70%)
    assert "70%" in web_line.description
    # And the payment label
    assert "payment" in web_line.description.lower()


def test_no_annotation_on_unit_based():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    rollup_line = next(
        ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-rollup-001"
    )
    # unit_based: no fraction annotation
    assert "payment" not in rollup_line.description.lower()
    assert "%" not in rollup_line.description


# ── line totals ───────────────────────────────────────────────────────────────


def test_line_total_computed_for_resolved_items():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    web_line = next(ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-web-001")
    # 0.4 * 12000 = 4800
    assert web_line.line_total == pytest.approx(4800.0)


def test_line_total_none_for_unresolved_price():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    unresolved_line = next(
        ln for ln in acme_group.lines if ln.item_id == "SPRIG-ACME-unresolved-001"
    )
    assert unresolved_line.line_total is None
    assert unresolved_line.unit_price is None


# ── sorting (flagged items first) ─────────────────────────────────────────────


def test_unresolved_price_sorted_before_clean_items():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    sprig = next(g for g in packet.groups if g.bill_to == "SPRIG")
    acme_group = next(ec for ec in sprig.end_client_groups if ec.end_client == "ACME")
    lines = acme_group.lines
    # The unresolved item (SPRIG-ACME-unresolved-001) must appear before the
    # fully clean rollup item.
    unresolved_idx = next(
        i for i, ln in enumerate(lines) if ln.item_id == "SPRIG-ACME-unresolved-001"
    )
    rollup_idx = next(
        i for i, ln in enumerate(lines) if ln.item_id == "SPRIG-ACME-rollup-001"
    )
    assert unresolved_idx < rollup_idx


# ── summary counts ────────────────────────────────────────────────────────────


def test_summary_counts():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    # 4 managed items with status_agent set (MANUAL_ONLY excluded):
    # SPRIG-ACME-web-001, SPRIG-ACME-rollup-001, SPRIG-ACME-unresolved-001, DIRECT-logo-001
    assert packet.total_lines == 4
    assert packet.unresolved_count == 1  # SPRIG-ACME-unresolved-001
    assert packet.deferred_count >= 1  # SPRIG-ACME-unresolved-001 is deferred


def test_billing_month_stored():
    ledger, profiles, pb, agrs = _load_all()
    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    assert packet.billing_month == _MONTH


# ── items without status_agent are excluded ───────────────────────────────────


def test_item_without_status_agent_excluded(tmp_path):
    """Items not yet assessed by LLM (status_agent blank) must not appear."""
    csv = tmp_path / "ledger.csv"
    csv.write_text(
        "item_id,bill_to,end_client,description,assignee,item_kind,billing_mode,"
        "unit_price,currency,price_source,price_ref,total_qty,qty_billed_to_date,"
        "last_billed_month,status_agent,completion_evidence,confidence,qty_proposed,"
        "status_confirmed,decision,qty_approved,qty_billed_actual,morning_doc_ref,notes\n"
        "item-pending,DIRECT_IL,,Logo,self,fixed_quote,defer,8000,ILS,price_book,"
        "2025-logo-il,1.0,0.0,,,,,,,,,,\n"
    )
    profiles = load_client_profiles(_FIXTURES / "client_profiles.csv")
    pb = load_price_book(_FIXTURES / "price_book.csv")
    agrs = load_agreements(_FIXTURES / "agreements.csv")
    ledger = load_ledger(csv)

    packet = build_review_packet(ledger, profiles, pb, agrs, _MONTH)
    assert packet.total_lines == 0
