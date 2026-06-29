from datetime import datetime, timezone

import pytest

from mail_evidence.records import EvidenceRecord

from invoicing_rules.reason import (
    ManualReasoner,
    ProxyReasoner,
    ReasonPending,
    apply_proposals,
    build_prompt,
    parse_proposals,
)
from invoicing_rules.state import ClientProfile, LedgerItem


# ── fixtures ──────────────────────────────────────────────────────────────────


def _profiles():
    return {
        "SPRIG": ClientProfile(
            "SPRIG", True, True, "en", "USD", 0.0, "cid-sprig", None
        ),
        "IVORY": ClientProfile(
            "IVORY", False, True, "he", "ILS", 0.18, "cid-ivory", None
        ),
    }


def _open_item():
    return LedgerItem(
        item_id="SPRIG-RHYTHMEDIX-pocket-folder",
        bill_to="SPRIG",
        end_client="RHYTHMEDIX",
        description="Pocket Folder",
        assignee="self",
        item_kind="fixed_quote",
        billing_mode="partial",
        unit_price=1100.0,
        currency="USD",
        price_source="price_book",
        price_ref="2025-marketing-folder",
        total_qty=1.0,
        qty_billed_to_date=0.7,
        last_billed_month="2026-04",
        status_agent=None,
        completion_evidence=None,
        confidence=None,
        qty_proposed=None,
        status_confirmed=None,
        decision=None,
        qty_approved=None,
        qty_billed_actual=None,
        morning_doc_ref="2286",
        proforma_doc_ref=None,
        notes="carry-forward",
    )


# ── parse_proposals ───────────────────────────────────────────────────────────


def test_parse_bare_array():
    out = parse_proposals('[{"bill_to": "SPRIG", "description": "Logo"}]')
    assert out == [{"bill_to": "SPRIG", "description": "Logo"}]


def test_parse_fenced_json():
    text = '```json\n[{"bill_to": "SPRIG"}]\n```'
    assert parse_proposals(text) == [{"bill_to": "SPRIG"}]


def test_parse_proposals_key_wrapper():
    text = '{"proposals": [{"bill_to": "IVORY"}]}'
    assert parse_proposals(text) == [{"bill_to": "IVORY"}]


def test_parse_embedded_in_prose():
    text = 'Sure! Here are the items:\n[{"bill_to": "SPRIG"}]\nLet me know.'
    assert parse_proposals(text) == [{"bill_to": "SPRIG"}]


def test_parse_unparseable_raises():
    with pytest.raises(ValueError):
        parse_proposals("I could not find any billable work this month.")


# ── apply_proposals ───────────────────────────────────────────────────────────


def test_annotate_existing_by_item_id_preserves_pricing():
    ledger = [_open_item()]
    proposals = [
        {
            "item_id": "SPRIG-RHYTHMEDIX-pocket-folder",
            "bill_to": "SPRIG",
            "end_client": "RHYTHMEDIX",
            "description": "Pocket Folder",
            "status_agent": "complete",
            "qty_proposed": 0.3,
            "confidence": "high",
            "completion_evidence": "WT13 final file 06-05",
        }
    ]
    annotated, appended = apply_proposals(ledger, proposals, _profiles())

    assert (annotated, appended) == (1, 0)
    it = ledger[0]
    assert it.status_agent == "complete" and it.qty_proposed == 0.3
    assert it.completion_evidence == "WT13 final file 06-05"
    # pricing / identity carry-forward untouched
    assert it.unit_price == 1100.0 and it.price_ref == "2025-marketing-folder"
    assert it.qty_billed_to_date == 0.7


def test_annotate_existing_by_description_when_no_item_id():
    ledger = [_open_item()]
    proposals = [
        {
            "bill_to": "SPRIG",
            "end_client": "RHYTHMEDIX",
            "description": "pocket  folder",  # different spacing/case — normalized match
            "status_agent": "in_progress",
            "qty_proposed": 0.2,
        }
    ]
    annotated, appended = apply_proposals(ledger, proposals, _profiles())
    assert (annotated, appended) == (1, 0)
    assert ledger[0].status_agent == "in_progress"


def test_new_item_appended_with_empty_gate_columns():
    ledger = []
    proposals = [
        {
            "bill_to": "SPRIG",
            "end_client": "APREO",
            "description": "Brand book",
            "item_kind": "unit_based",
            "billing_mode": "hourly",
            "status_agent": "complete",
            "qty_proposed": None,
            "confidence": "med",
            "price_source": "price_book",
            "price_ref": "2025-general-hourly-creative-service",
        }
    ]
    annotated, appended = apply_proposals(ledger, proposals, _profiles())

    assert (annotated, appended) == (0, 1)
    it = ledger[0]
    assert it.item_id == "SPRIG-APREO-brand-book"
    assert it.bill_to == "SPRIG" and it.end_client == "APREO"
    assert it.currency == "USD"  # inherited from the SPRIG profile
    assert it.billing_mode == "hourly"
    assert it.price_ref == "2025-general-hourly-creative-service"
    # the agent identifies, never gates
    assert (
        it.status_confirmed is None and it.decision is None and it.qty_approved is None
    )
    assert it.proforma_doc_ref is None and it.morning_doc_ref is None


def test_same_normalized_description_merges_into_one_item():
    # Two proposals naming the same item (punctuation/case differ) collapse onto one row.
    ledger = []
    proposals = [
        {
            "bill_to": "SPRIG",
            "end_client": "APREO",
            "description": "Logo",
            "status_agent": "in_progress",
        },
        {
            "bill_to": "SPRIG",
            "end_client": "APREO",
            "description": "Logo!!",
            "status_agent": "complete",
        },
    ]
    annotated, appended = apply_proposals(ledger, proposals, _profiles())
    assert (annotated, appended) == (1, 1)  # first appended, second annotates it
    assert len(ledger) == 1 and ledger[0].status_agent == "complete"


def test_new_id_disambiguated_against_unconventional_existing_id():
    # An open item whose id matches the slug but whose DESCRIPTION differs → the new
    # item can't merge by description, so its colliding id is disambiguated.
    open_item = _open_item()
    open_item.item_id = "SPRIG-APREO-logo"
    open_item.end_client = "APREO"
    open_item.description = "Brand Logo Design"  # norm != "logo"
    ledger = [open_item]
    proposals = [{"bill_to": "SPRIG", "end_client": "APREO", "description": "Logo"}]
    annotated, appended = apply_proposals(ledger, proposals, _profiles())
    assert (annotated, appended) == (0, 1)
    assert ledger[-1].item_id == "SPRIG-APREO-logo-2"


# ── build_prompt ──────────────────────────────────────────────────────────────


def test_build_prompt_includes_key_sections():
    system, user = build_prompt(
        corpus="== WT 1/1\n[06-01] a>b | hi\n  body",
        ledger=[_open_item()],
        profiles=_profiles(),
        price_book={},
        agreements=[],
        month="2026-06",
    )
    assert "RECALL IS THE GOAL" in system
    assert "BILLING MONTH: 2026-06" in user
    assert "SPRIG-RHYTHMEDIX-pocket-folder" in user  # open ledger shown with ids
    assert "WORK THREADS" in user and "== WT 1/1" in user


# ── ManualReasoner (the supervised seam) ──────────────────────────────────────


def _evidence():
    return [
        EvidenceRecord(
            id="m1",
            thread_id="T",
            source="email",
            date=datetime(2026, 6, 5, tzinfo=timezone.utc),
            body_text="final pocket folder file sent",
            from_="molly@sprig.com",
            to=["avigail@ula.co.il"],
            subject="RMX Pocket Folder",
        )
    ]


def test_manual_reasoner_writes_prompt_then_halts(tmp_path):
    prompt_p = tmp_path / "_reason_prompt.txt"
    resp_p = tmp_path / "_reason_response.json"
    r = ManualReasoner(
        _profiles(), {}, [], "2026-06", prompt_path=prompt_p, response_path=resp_p
    )
    ledger = [_open_item()]

    with pytest.raises(ReasonPending):
        r.annotate(ledger, _evidence())
    assert prompt_p.exists()
    assert "WORK THREADS" in prompt_p.read_text()
    # nothing applied yet
    assert ledger[0].status_agent is None


def test_manual_reasoner_applies_response_on_rerun(tmp_path):
    prompt_p = tmp_path / "_reason_prompt.txt"
    resp_p = tmp_path / "_reason_response.json"
    resp_p.write_text(
        '[{"item_id": "SPRIG-RHYTHMEDIX-pocket-folder", "bill_to": "SPRIG",'
        ' "description": "Pocket Folder", "status_agent": "complete", "qty_proposed": 0.3}]'
    )
    r = ManualReasoner(
        _profiles(), {}, [], "2026-06", prompt_path=prompt_p, response_path=resp_p
    )
    ledger = [_open_item()]
    r.annotate(ledger, _evidence())  # no raise
    assert ledger[0].status_agent == "complete" and ledger[0].qty_proposed == 0.3


# ── ProxyReasoner (call mocked — network not exercised) ───────────────────────


def test_proxy_reasoner_parses_and_applies(monkeypatch):
    r = ProxyReasoner(_profiles(), {}, [], "2026-06")
    monkeypatch.setattr(
        r,
        "_call",
        lambda system, user: '[{"item_id": "SPRIG-RHYTHMEDIX-pocket-folder",'
        ' "bill_to": "SPRIG", "description": "Pocket Folder",'
        ' "status_agent": "complete", "qty_proposed": 0.5}]',
    )
    ledger = [_open_item()]
    r.annotate(ledger, _evidence())
    assert ledger[0].status_agent == "complete" and ledger[0].qty_proposed == 0.5
