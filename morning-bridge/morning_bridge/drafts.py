"""
Proforma creation — Task 1.4.

create_proforma is the ONLY public function.  It posts to /documents with
type=300 (Proforma / חשבון עסקה) via the restricted client._create path.

Why Proforma and NOT Tax Invoice (305):
  morning's POST /documents always issues a real fiscal document when type=305.
  signed=false does not prevent issuance; /preview returns only an ephemeral
  base64 render.  Proforma (type 300) is non-fiscal, has its own series
  (40001+), is not reported to the tax authority, is deletable, and is
  converted to a real invoice by the human in morning when they choose to issue.
  That conversion — and ONLY that conversion — is the issuance step.  It is
  OUT of this system's automated scope.

Safety controls:
  Type lock — create_proforma hard-codes type=300 and RAISES if the caller
    attempts to inject a different type.  The bridge is physically incapable
    of creating type-305 or any other fiscal document.
  DRY_RUN=true  — return the payload that would be sent; create nothing.
  Double-bill guard — search existing proformas for the same client before
    creating; surface a warning if descriptions match (soft signal, not hard
    block — recurring items bill identically month-to-month).

Input shape (CreateProformaRequest):
  bill_to_client_id: str      morning client id
  language:          str      "en" | "he"
  currency:          str      "USD" | "ILS"
  vat_rate:          float    e.g. 0.18 for domestic, 0.00 for export
  description:       str | absent  OPTIONAL.  Document-level heading rendered
                               on the invoice (morning's top-level "description"
                               field).  For agency invoices the invoicing skill
                               sets this to the end_client name; for direct
                               clients it is omitted entirely.
  lines:             list of:
    description: str
    quantity:    float
    unit_price:  float

A subtitle line (agency end-client separator) is quantity=1, unit_price=0.00 —
the invoicing skill builds it; the bridge passes it through unchanged.

See docs/03 and docs/01 §5 for the semantic mapping.
"""

from __future__ import annotations

import os

from morning_bridge.client import MorningClient

_DOC_TYPE_PROFORMA = 300


def _build_payload(request: dict) -> dict:
    """Map CreateProformaRequest → morning POST /documents body."""
    income = []
    for line in request["lines"]:
        qty = float(line["quantity"])
        unit_price = float(line["unit_price"])
        income.append(
            {
                "description": line["description"],
                "quantity": qty,
                "unitPrice": unit_price,
                "price": round(qty * unit_price, 2),
                "currency": request["currency"],
                "vat": float(request["vat_rate"]),
            }
        )
    payload: dict = {
        "type": _DOC_TYPE_PROFORMA,
        "currency": request["currency"],
        "lang": request["language"],
        "client": {"id": request["bill_to_client_id"]},
        "income": income,
    }
    # Optional document-level heading (renders bold on the invoice).
    # For agency invoices this is set to the end_client name by the invoicing
    # skill (1.7); for direct clients it is omitted.
    if "description" in request:
        payload["description"] = request["description"]
    return payload


def _check_double_bill(client: MorningClient, request: dict) -> list[str]:
    """
    Search existing proformas for this client for description matches.

    Returns a list of warning strings (empty = no conflicts).  Soft signal:
    description matching cannot distinguish a real duplicate from a recurring
    unit_based item billed identically each month.  Authoritative dedup lives
    in the ledger (morning_doc_ref + qty_billed_to_date) at settlement.
    """
    from morning_bridge.reads import DOC_TYPE_PROFORMA, search_documents

    billable_descs = {
        line["description"]
        for line in request["lines"]
        if float(line.get("unit_price", 0)) > 0
    }
    if not billable_descs:
        return []

    recent = search_documents(
        client,
        doc_type=[DOC_TYPE_PROFORMA],
        client_id=request["bill_to_client_id"],
    )

    existing_descs: set[str] = set()
    for doc in recent.get("items", []):
        for income_line in doc.get("income", []):
            desc = income_line.get("description", "")
            if desc:
                existing_descs.add(desc)

    conflicts = billable_descs & existing_descs
    if conflicts:
        return [
            "Possible duplicate: existing proforma(s) contain matching line descriptions. "
            "Verify this is not a double-bill before the human issues the invoice.\n"
            f"  Matching: {sorted(conflicts)}"
        ]
    return []


def create_proforma(client: MorningClient, request: dict) -> dict:
    """
    Create a Proforma invoice (type 300, חשבון עסקה) in morning.

    Type is hard-coded to 300.  Passing 'type' in the request raises ValueError
    — this function is structurally incapable of creating a fiscal document.

    Returns the morning API response dict.  In dry-run mode (DRY_RUN=true env
    var) returns {"dry_run": True, "payload": ...} and makes no HTTP call.

    Raises ValueError when the request contains a type override or is missing
    required fields.
    """
    if "type" in request:
        raise ValueError(
            "create_proforma hard-codes type=300 (Proforma / חשבון עסקה). "
            "Do not pass 'type' in the request — this function is structurally "
            "incapable of creating tax invoices or any other fiscal document type."
        )
    _validate_request(request)
    payload = _build_payload(request)

    if os.environ.get("DRY_RUN", "").lower() == "true":
        return {"dry_run": True, "payload": payload}

    guard_warnings = _check_double_bill(client, request)

    result = client._create("/documents", payload)

    doc_type = result.get("type")
    if doc_type != _DOC_TYPE_PROFORMA:
        raise RuntimeError(
            f"morning returned type={doc_type!r} — expected {_DOC_TYPE_PROFORMA} (Proforma). "
            "Something unexpected happened; do not proceed."
        )

    if guard_warnings:
        result["guard_warnings"] = guard_warnings

    return result


def _validate_request(request: dict) -> None:
    required = {"bill_to_client_id", "language", "currency", "vat_rate", "lines"}
    missing = required - request.keys()
    if missing:
        raise ValueError(
            f"CreateProformaRequest missing required fields: {sorted(missing)}"
        )
    if not request["lines"]:
        raise ValueError("CreateProformaRequest.lines must not be empty")
    for i, line in enumerate(request["lines"]):
        for field in ("description", "quantity", "unit_price"):
            if field not in line:
                raise ValueError(f"lines[{i}] missing field {field!r}")
