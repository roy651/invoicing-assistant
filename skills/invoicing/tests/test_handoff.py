"""
Unit tests for the gate → create_proforma handoff (task 1.8).

Covers billable selection, docs/01 §5 grouping (agency per-end-client + subtitle +
heading; direct plain), qty_approved-driven line mapping + progress annotation,
blocker raises (unresolved price / missing morning_client_id), the bridge-request
shape (dry-run round-trip through create_proforma), and RECORD writing
morning_doc_ref + ledger persistence.
"""

from pathlib import Path

import pytest

from invoicing_rules.handoff import (
    apply_results,
    build_proforma_requests,
    create_and_record,
)
from invoicing_rules.state import (
    ClientProfile,
    LedgerItem,
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
    write_ledger,
)
from morning_bridge.drafts import create_proforma

_FIXTURES = Path(__file__).parent / "fixtures"
_MONTH = "2026-03"


def _refs():
    return (
        load_client_profiles(_FIXTURES / "client_profiles.csv"),
        load_price_book(_FIXTURES / "price_book.csv"),
        load_agreements(_FIXTURES / "agreements.csv"),
    )


def _item(item_id: str, bill_to: str, **over) -> LedgerItem:
    """LedgerItem factory with gate columns approved by default."""
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
        notes=None,
    )
    defaults.update(over)
    return LedgerItem(item_id=item_id, bill_to=bill_to, **defaults)


# Reusable approved items keyed to the reference fixtures.
def _web_partial():
    return _item(
        "SPRIG-ACME-web-001",
        "SPRIG",
        end_client="ACME",
        description="Scrollable website",
        item_kind="fixed_quote",
        billing_mode="partial",
        price_source="negotiated",
        price_ref="agr-sprig-acme-web-001",  # confirmed, 12000 USD
        total_qty=1.0,
        qty_billed_to_date=0.3,
        decision="partial",
        qty_approved=0.4,
    )


def _rollup_units():
    return _item(
        "SPRIG-ACME-rollup-001",
        "SPRIG",
        end_client="ACME",
        description="Roll-up banners",
        item_kind="unit_based",
        price_ref="2025-trade-rollup",  # 400 USD
        decision="bill",
        qty_approved=3.0,
    )


def _logo_direct():
    return _item(
        "DIRECT-logo-001",
        "DIRECT_IL",
        description="Logo design",
        currency="ILS",
        item_kind="fixed_quote",
        billing_mode="defer",
        price_ref="2025-logo-il",  # 8000 ILS
        total_qty=1.0,
        qty_billed_to_date=0.0,
        decision="bill",
        qty_approved=1.0,
    )


# ── grouping: agency per end-client + subtitle + heading ─────────────────────


def test_agency_one_request_per_end_client():
    profiles, pb, agrs = _refs()
    ledger = [_web_partial(), _rollup_units()]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    assert len(reqs) == 1
    req = reqs[0]
    assert req.bill_to == "SPRIG"
    assert req.end_client == "ACME"
    assert req.bill_to_client_id == "morning-sprig-001"
    assert req.language == "en"
    assert req.currency == "USD"
    assert req.vat_rate == 0.0
    # Document-level heading = end-client name (bold in morning).
    assert req.description == "ACME"
    # Both ACME items are covered by this one proforma.
    assert set(req.item_ids) == {"SPRIG-ACME-web-001", "SPRIG-ACME-rollup-001"}


def test_agency_subtitle_line_is_first_and_zero_priced():
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests([_web_partial()], profiles, pb, agrs, _MONTH)
    lines = reqs[0].lines
    subtitle = lines[0]
    assert subtitle["description"] == "------------ ACME ------------"
    assert subtitle["quantity"] == 1.0
    assert subtitle["unit_price"] == 0.0


def test_two_end_clients_two_requests():
    profiles, pb, agrs = _refs()
    other = _item(
        "SPRIG-BETA-001",
        "SPRIG",
        end_client="BETA",
        description="Banner set",
        price_ref="2025-trade-rollup",
        qty_approved=2.0,
    )
    reqs = build_proforma_requests([_rollup_units(), other], profiles, pb, agrs, _MONTH)
    ends = {r.end_client for r in reqs}
    assert ends == {"ACME", "BETA"}
    assert len(reqs) == 2


# ── grouping: direct client (no subtitle, no heading) ────────────────────────


def test_direct_client_no_subtitle_no_heading():
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests([_logo_direct()], profiles, pb, agrs, _MONTH)
    assert len(reqs) == 1
    req = reqs[0]
    assert req.end_client is None
    assert req.description is None  # omitted for direct clients
    assert req.language == "he"
    assert req.currency == "ILS"
    assert req.vat_rate == 0.18
    # No subtitle line — first line is the actual item.
    assert len(req.lines) == 1
    assert "Logo design" in req.lines[0]["description"]


# ── line mapping: qty_approved + resolved price + annotation ──────────────────


def test_line_uses_qty_approved_and_resolved_price():
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests(
        [_web_partial(), _rollup_units()], profiles, pb, agrs, _MONTH
    )
    lines = reqs[0].lines
    by_desc = {
        ln["description"].split(" - ")[0]: ln for ln in lines if ln["unit_price"]
    }
    web = by_desc["Scrollable website"]
    rollup = by_desc["Roll-up banners"]
    assert web["quantity"] == 0.4
    assert web["unit_price"] == 12000.0  # confirmed agreement
    assert rollup["quantity"] == 3.0
    assert rollup["unit_price"] == 400.0  # price-book non-range


def test_partial_annotation_reflects_qty_approved():
    """qty_billed_to_date 0.3 + qty_approved 0.4 = 70% so far."""
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests([_web_partial()], profiles, pb, agrs, _MONTH)
    web_line = next(ln for ln in reqs[0].lines if "Scrollable" in ln["description"])
    assert "70%" in web_line["description"]
    assert "payment" in web_line["description"].lower()


def test_unit_based_line_has_no_annotation():
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests([_rollup_units()], profiles, pb, agrs, _MONTH)
    rollup_line = next(ln for ln in reqs[0].lines if "Roll-up" in ln["description"])
    assert "%" not in rollup_line["description"]
    assert "payment" not in rollup_line["description"].lower()


# ── selection: exclude non-billable rows ──────────────────────────────────────


def test_unmanaged_client_excluded():
    profiles, pb, agrs = _refs()
    manual = _item(
        "MANUAL-001", "MANUAL_ONLY", currency="ILS", price_ref="2025-logo-il"
    )
    reqs = build_proforma_requests([manual], profiles, pb, agrs, _MONTH)
    assert reqs == []


@pytest.mark.parametrize("decision", ["defer", "hold", None])
def test_non_billable_decision_excluded(decision):
    profiles, pb, agrs = _refs()
    item = _rollup_units()
    item.decision = decision
    reqs = build_proforma_requests([item], profiles, pb, agrs, _MONTH)
    assert reqs == []


@pytest.mark.parametrize("qty", [0.0, None])
def test_zero_or_missing_qty_approved_excluded(qty):
    profiles, pb, agrs = _refs()
    item = _rollup_units()
    item.qty_approved = qty
    reqs = build_proforma_requests([item], profiles, pb, agrs, _MONTH)
    assert reqs == []


@pytest.mark.parametrize("status", [None, ""])
def test_missing_status_confirmed_excluded(status):
    """Defensive: a billable decision without status_confirmed is a half-written gate
    row and must not be billed."""
    profiles, pb, agrs = _refs()
    item = _rollup_units()
    item.status_confirmed = status
    reqs = build_proforma_requests([item], profiles, pb, agrs, _MONTH)
    assert reqs == []


# ── blockers: never invent a price; never bill without a morning client ──────


def test_unresolved_price_raises():
    profiles, pb, agrs = _refs()
    ranged = _item(
        "SPRIG-ACME-range-001",
        "SPRIG",
        end_client="ACME",
        description="Scrollable site (range)",
        price_ref="2025-web-scrollable-range",  # is_range, no unit_price → unresolved
        qty_approved=1.0,
    )
    with pytest.raises(ValueError, match="SPRIG-ACME-range-001"):
        build_proforma_requests([ranged], profiles, pb, agrs, _MONTH)


def test_missing_morning_client_id_raises():
    # A managed profile that has no morning_client_id yet.
    profiles = {
        "NEWCO": ClientProfile(
            bill_to="NEWCO",
            is_agency=False,
            managed_by_agent=True,
            language="he",
            currency="ILS",
            vat_rate=0.18,
            morning_client_id=None,
            notes=None,
        )
    }
    _, pb, agrs = _refs()
    item = _item(
        "NEWCO-001",
        "NEWCO",
        currency="ILS",
        price_ref="2025-logo-il",
        qty_approved=1.0,
    )
    with pytest.raises(ValueError, match="morning_client_id"):
        build_proforma_requests([item], profiles, pb, agrs, _MONTH)


# ── bridge-request shape + dry-run round-trip ─────────────────────────────────


def test_to_bridge_request_has_no_type_key():
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests([_logo_direct()], profiles, pb, agrs, _MONTH)
    bridge_req = reqs[0].to_bridge_request()
    assert "type" not in bridge_req
    assert set(bridge_req) >= {
        "bill_to_client_id",
        "language",
        "currency",
        "vat_rate",
        "lines",
    }
    # Direct client → no document-level description.
    assert "description" not in bridge_req


def test_build_request_accepted_by_create_proforma_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    profiles, pb, agrs = _refs()
    reqs = build_proforma_requests(
        [_web_partial(), _rollup_units()], profiles, pb, agrs, _MONTH
    )
    result = create_proforma(None, reqs[0].to_bridge_request())
    assert result["dry_run"] is True
    payload = result["payload"]
    assert payload["type"] == 300  # bridge hard-locks Proforma
    assert payload["lang"] == "en"
    assert payload["client"]["id"] == "morning-sprig-001"
    # Subtitle (0.00) + two billed lines.
    assert len(payload["income"]) == 3
    subtitle = payload["income"][0]
    assert subtitle["unitPrice"] == 0.0
    assert subtitle["price"] == 0.0


# ── RECORD: write morning_doc_ref + persist ──────────────────────────────────


def test_apply_results_records_proforma_id():
    profiles, pb, agrs = _refs()
    ledger = [_web_partial(), _rollup_units()]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    result = {"id": "doc-40001", "type": 300, "number": 40001}
    updated = apply_results(ledger, reqs[0], result)
    assert set(updated) == {"SPRIG-ACME-web-001", "SPRIG-ACME-rollup-001"}
    for item in ledger:
        assert item.morning_doc_ref == "doc-40001"
        # qty_billed_actual stays empty until settlement reads the issued doc.
        assert item.qty_billed_actual is None


def test_apply_results_dry_run_records_nothing():
    profiles, pb, agrs = _refs()
    ledger = [_logo_direct()]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    updated = apply_results(ledger, reqs[0], {"dry_run": True, "payload": {}})
    assert updated == []
    assert ledger[0].morning_doc_ref is None


def test_write_ledger_persists_morning_doc_ref(tmp_path):
    profiles, pb, agrs = _refs()
    ledger = [_web_partial(), _logo_direct()]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    for req in reqs:
        apply_results(ledger, req, {"id": f"doc-{req.bill_to}", "type": 300})

    out = tmp_path / "ledger_out.csv"
    write_ledger(ledger, out)
    reloaded = {it.item_id: it for it in load_ledger(out)}

    assert reloaded["SPRIG-ACME-web-001"].morning_doc_ref == "doc-SPRIG"
    assert reloaded["DIRECT-logo-001"].morning_doc_ref == "doc-DIRECT_IL"
    # Untouched fields survive the round-trip.
    assert reloaded["SPRIG-ACME-web-001"].qty_approved == 0.4
    assert reloaded["SPRIG-ACME-web-001"].price_ref == "agr-sprig-acme-web-001"
    assert reloaded["DIRECT-logo-001"].currency == "ILS"


# ── create_and_record: interleaved create → record → persist ─────────────────


def test_create_and_record_persists_each_proforma(tmp_path):
    profiles, pb, agrs = _refs()
    # Two end-clients → two proformas (ACME, BETA), sorted by end_client.
    beta = _item(
        "SPRIG-BETA-001",
        "SPRIG",
        end_client="BETA",
        description="Banner set",
        price_ref="2025-trade-rollup",
        qty_approved=2.0,
    )
    ledger = [_web_partial(), beta]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    out = tmp_path / "ledger.csv"

    def fake_create(_client, bridge_req):
        # id derived from the document heading (end_client) for traceability.
        return {"id": f"doc-{bridge_req['description']}", "type": 300}

    results = create_and_record(None, reqs, ledger, out, create_fn=fake_create)

    assert len(results) == 2
    reloaded = {it.item_id: it for it in load_ledger(out)}
    assert reloaded["SPRIG-ACME-web-001"].morning_doc_ref == "doc-ACME"
    assert reloaded["SPRIG-BETA-001"].morning_doc_ref == "doc-BETA"


def test_create_and_record_persists_before_next_request(tmp_path):
    """The first proforma's id must be on disk before the second is created —
    proving the dual-write window is one proforma, not the whole batch."""
    profiles, pb, agrs = _refs()
    beta = _item(
        "SPRIG-BETA-001",
        "SPRIG",
        end_client="BETA",
        description="Banner set",
        price_ref="2025-trade-rollup",
        qty_approved=2.0,
    )
    ledger = [_web_partial(), beta]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    out = tmp_path / "ledger.csv"

    calls = []

    def spy_create(_client, bridge_req):
        calls.append(bridge_req["description"])
        if bridge_req["description"] == "BETA":
            # By the time BETA is created, ACME must already be persisted.
            persisted = {it.item_id: it for it in load_ledger(out)}
            assert persisted["SPRIG-ACME-web-001"].morning_doc_ref == "doc-ACME"
        return {"id": f"doc-{bridge_req['description']}", "type": 300}

    create_and_record(None, reqs, ledger, out, create_fn=spy_create)
    assert calls == ["ACME", "BETA"]  # ordered, ACME first


def test_create_and_record_dry_run_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    profiles, pb, agrs = _refs()
    ledger = [_logo_direct()]
    reqs = build_proforma_requests(ledger, profiles, pb, agrs, _MONTH)
    out = tmp_path / "ledger.csv"

    results = create_and_record(None, reqs, ledger, out, create_fn=create_proforma)

    assert results[0]["dry_run"] is True
    # Nothing real was created → no morning_doc_ref recorded, on disk or in memory.
    assert ledger[0].morning_doc_ref is None
    assert load_ledger(out)[0].morning_doc_ref is None
