"""
Unit tests for pricing.resolve_price().

Covers every resolution path and the NEVER-GUESS invariant for ranges.
"""

from pathlib import Path


from invoicing_rules.pricing import (
    SOURCE_AGREEMENT,
    SOURCE_DIRECT,
    SOURCE_PRICE_BOOK,
    resolve_all,
    resolve_price,
)
from invoicing_rules.state import (
    LedgerItem,
    load_agreements,
    load_ledger,
    load_price_book,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ── helpers ───────────────────────────────────────────────────────────────────


def _item(**kwargs) -> LedgerItem:
    """Build a minimal LedgerItem with sensible defaults."""
    defaults = dict(
        item_id="test-item",
        bill_to="SPRIG",
        end_client="ACME",
        description="Test item",
        assignee="self",
        item_kind="fixed_quote",
        billing_mode="partial",
        unit_price=None,
        currency="USD",
        price_source=None,
        price_ref=None,
        total_qty=1.0,
        qty_billed_to_date=0.0,
        last_billed_month=None,
        status_agent="in_progress",
        completion_evidence="email-001",
        confidence="high",
        qty_proposed=0.5,
        status_confirmed=None,
        decision=None,
        qty_approved=None,
        qty_billed_actual=None,
        morning_doc_ref=None,
        notes=None,
    )
    defaults.update(kwargs)
    return LedgerItem(**defaults)


def _pb():
    return load_price_book(_FIXTURES / "price_book.csv")


def _agrs():
    return load_agreements(_FIXTURES / "agreements.csv")


# ── path 1: direct (unit_price already on item) ───────────────────────────────


def test_direct_resolved_when_unit_price_and_ref_set():
    item = _item(
        unit_price=12000.0,
        price_ref="agr-sprig-acme-web-001",
        price_source="negotiated",
    )
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is True
    assert result.unit_price == 12000.0
    assert result.source == SOURCE_DIRECT
    assert result.flag is None


def test_direct_requires_price_ref_to_be_truthy():
    """unit_price without price_ref is still unresolved (no audit trail)."""
    item = _item(unit_price=12000.0, price_ref=None)
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "no price_ref" in result.flag


# ── path 2: negotiated (Agreement lookup) ─────────────────────────────────────


def test_negotiated_confirmed_resolves():
    item = _item(price_source="negotiated", price_ref="agr-sprig-acme-web-001")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is True
    assert result.unit_price == 12000.0
    assert result.source == SOURCE_AGREEMENT


def test_negotiated_detected_not_confirmed_unresolved():
    item = _item(price_source="negotiated", price_ref="agr-sprig-acme-web-002-pending")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "detected" in result.flag
    assert "not confirmed" in result.flag


def test_negotiated_missing_agreement_unresolved():
    item = _item(price_source="negotiated", price_ref="agr-does-not-exist")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "not found" in result.flag


# ── path 3: price book ────────────────────────────────────────────────────────


def test_price_book_fixed_resolves():
    item = _item(
        price_source="price_book", price_ref="2025-trade-rollup", item_kind="unit_based"
    )
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is True
    assert result.unit_price == 400.0
    assert result.source == SOURCE_PRICE_BOOK


def test_price_book_range_without_unit_price_unresolved():
    """INVARIANT: never auto-guess a price inside a range."""
    item = _item(price_source="price_book", price_ref="2025-web-scrollable-range")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "range" in result.flag
    assert "11000" in result.flag  # low shown in message
    assert "13200" in result.flag  # high shown in message


def test_price_book_missing_entry_unresolved():
    item = _item(price_source="price_book", price_ref="9999-does-not-exist")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "not found" in result.flag


# ── path 4: unknown source ────────────────────────────────────────────────────


def test_unknown_price_source_unresolved():
    item = _item(price_source="custom", price_ref="something")
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert result.flag is not None


def test_no_price_ref_unresolved():
    item = _item(price_source="price_book", price_ref=None)
    result = resolve_price(item, _pb(), _agrs())
    assert result.resolved is False
    assert "no price_ref" in result.flag


# ── resolve_all ───────────────────────────────────────────────────────────────


def test_resolve_all_returns_keyed_dict():
    items = load_ledger(_FIXTURES / "ledger.csv")
    pb = _pb()
    agrs = _agrs()
    results = resolve_all(items, pb, agrs)
    assert set(results.keys()) == {i.item_id for i in items}


def test_resolve_all_from_fixtures():
    items = load_ledger(_FIXTURES / "ledger.csv")
    pb = _pb()
    agrs = _agrs()
    results = resolve_all(items, pb, agrs)

    # SPRIG-ACME-web-001: unit_price=12000 already set + price_ref → direct
    assert results["SPRIG-ACME-web-001"].resolved is True
    assert results["SPRIG-ACME-web-001"].unit_price == 12000.0
    assert results["SPRIG-ACME-web-001"].source == SOURCE_DIRECT

    # SPRIG-ACME-rollup-001: unit_price=400 + price_ref → direct
    assert results["SPRIG-ACME-rollup-001"].resolved is True

    # SPRIG-ACME-unresolved-001: no unit_price, range ref → unresolved
    assert results["SPRIG-ACME-unresolved-001"].resolved is False

    # DIRECT-logo-001: unit_price=8000 + price_ref → direct
    assert results["DIRECT-logo-001"].resolved is True
    assert results["DIRECT-logo-001"].unit_price == 8000.0
