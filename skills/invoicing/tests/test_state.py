"""Unit tests for state loaders."""

from pathlib import Path


from invoicing_rules.state import (
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ── load_client_profiles ──────────────────────────────────────────────────────


def test_load_profiles_all_present():
    profiles = load_client_profiles(_FIXTURES / "client_profiles.csv")
    assert set(profiles.keys()) == {"SPRIG", "DIRECT_IL", "MANUAL_ONLY"}


def test_load_profiles_sprig():
    p = load_client_profiles(_FIXTURES / "client_profiles.csv")["SPRIG"]
    assert p.is_agency is True
    assert p.managed_by_agent is True
    assert p.language == "en"
    assert p.currency == "USD"
    assert p.vat_rate == 0.00
    assert p.morning_client_id == "morning-sprig-001"


def test_load_profiles_manual_not_managed():
    p = load_client_profiles(_FIXTURES / "client_profiles.csv")["MANUAL_ONLY"]
    assert p.managed_by_agent is False


def test_load_profiles_empty_csv(tmp_path):
    csv = tmp_path / "profiles.csv"
    csv.write_text(
        "bill_to,is_agency,managed_by_agent,language,currency,vat_rate,morning_client_id,notes\n"
    )
    assert load_client_profiles(csv) == {}


# ── load_agreements ───────────────────────────────────────────────────────────


def test_load_agreements_count():
    agrs = load_agreements(_FIXTURES / "agreements.csv")
    assert len(agrs) == 2


def test_load_agreements_confirmed():
    agrs = load_agreements(_FIXTURES / "agreements.csv")
    confirmed = [a for a in agrs if a.is_confirmed]
    assert len(confirmed) == 1
    assert confirmed[0].agreement_id == "agr-sprig-acme-web-001"
    assert confirmed[0].agreed_price == 12000.0
    assert confirmed[0].currency == "USD"


def test_load_agreements_detected_not_confirmed():
    agrs = load_agreements(_FIXTURES / "agreements.csv")
    detected = [a for a in agrs if not a.is_confirmed]
    assert len(detected) == 1
    assert detected[0].confidence == "detected"


def test_load_agreements_end_client_parsed():
    agrs = load_agreements(_FIXTURES / "agreements.csv")
    for a in agrs:
        assert a.end_client == "ACME"


# ── load_ledger ───────────────────────────────────────────────────────────────


def test_load_ledger_count():
    items = load_ledger(_FIXTURES / "ledger.csv")
    assert len(items) == 5


def test_load_ledger_partial_item():
    items = {i.item_id: i for i in load_ledger(_FIXTURES / "ledger.csv")}
    web = items["SPRIG-ACME-web-001"]
    assert web.bill_to == "SPRIG"
    assert web.end_client == "ACME"
    assert web.item_kind == "fixed_quote"
    assert web.billing_mode == "partial"
    assert web.unit_price == 12000.0
    assert web.total_qty == 1.0
    assert web.qty_billed_to_date == 0.3
    assert web.qty_proposed == 0.4
    assert web.status_agent == "in_progress"
    assert web.confidence == "med"


def test_load_ledger_unit_based_item():
    items = {i.item_id: i for i in load_ledger(_FIXTURES / "ledger.csv")}
    rollup = items["SPRIG-ACME-rollup-001"]
    assert rollup.item_kind == "unit_based"
    assert rollup.billing_mode is None
    assert rollup.qty_proposed == 3.0
    assert rollup.status_agent == "complete"


def test_load_ledger_unresolved_item_no_unit_price():
    items = {i.item_id: i for i in load_ledger(_FIXTURES / "ledger.csv")}
    unresolved = items["SPRIG-ACME-unresolved-001"]
    assert unresolved.unit_price is None
    assert unresolved.price_ref == "2025-web-scrollable-range"


def test_load_ledger_direct_item():
    items = {i.item_id: i for i in load_ledger(_FIXTURES / "ledger.csv")}
    logo = items["DIRECT-logo-001"]
    assert logo.bill_to == "DIRECT_IL"
    assert logo.end_client is None
    assert logo.description == "עיצוב לוגו"
    assert logo.unit_price == 8000.0
    assert logo.currency == "ILS"


def test_load_ledger_optional_fields_none():
    items = {i.item_id: i for i in load_ledger(_FIXTURES / "ledger.csv")}
    # Fields not set in the fixture should be None
    logo = items["DIRECT-logo-001"]
    assert logo.status_confirmed is None
    assert logo.decision is None
    assert logo.qty_approved is None
    assert logo.morning_doc_ref is None


# ── load_price_book ───────────────────────────────────────────────────────────


def test_load_price_book_count():
    pb = load_price_book(_FIXTURES / "price_book.csv")
    assert len(pb) == 4


def test_load_price_book_range_flagged():
    pb = load_price_book(_FIXTURES / "price_book.csv")
    row = pb["2025-web-scrollable-range"]
    assert row.is_range is True
    assert row.price_low == 11000.0
    assert row.price_high == 13200.0
    assert row.currency == "USD"


def test_load_price_book_fixed_price():
    pb = load_price_book(_FIXTURES / "price_book.csv")
    row = pb["2025-trade-rollup"]
    assert row.is_range is False
    assert row.price_low == row.price_high == 400.0


def test_load_price_book_versioned():
    pb = load_price_book(_FIXTURES / "price_book.csv")
    assert pb["2025-logo-il"].version == "2025"
    assert pb["2026-logo-il"].version == "2026"
    assert pb["2026-logo-il"].price_low == 9000.0


def test_load_price_book_keyed_by_price_id():
    pb = load_price_book(_FIXTURES / "price_book.csv")
    assert "2025-logo-il" in pb
    assert pb["2025-logo-il"].item == "Logo Design"
