"""
Draft creation — Task 1.4.

create_draft is the ONLY public function. It posts to /documents via the
restricted client._create path (write allowlist = {"/documents"}).

Safety controls:
  DRY_RUN=true  — return the payload that would be sent; create nothing.
  Double-bill guard — search open invoices for the same client before creating;
    raise on any description match, requiring human resolution.

Input shape (CreateDraftRequest):
  bill_to_client_id: str      morning client id
  language:          str      "en" | "he"
  currency:          str      "USD" | "ILS"
  vat_rate:          float    e.g. 0.18 for domestic, 0.00 for export
  lines:             list of:
    description: str
    quantity:    float
    unit_price:  float

A subtitle line (agency end-client separator) is quantity=1, unit_price=0.00 —
the invoicing skill builds it; the bridge passes it through unchanged.

See docs/03 §Create-draft input contract and docs/01 §5 for the semantic mapping.
"""

from __future__ import annotations

import os

from morning_bridge.client import MorningClient

_DOC_TYPE_TAX_INVOICE = 305


def _build_payload(request: dict) -> dict:
    """Map CreateDraftRequest → morning POST /documents body."""
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
    return {
        "type": _DOC_TYPE_TAX_INVOICE,
        "currency": request["currency"],
        "lang": request["language"],
        "clientId": request["bill_to_client_id"],
        "income": income,
        # Omitting payment array keeps the document as an open draft (status=0,
        # no invoice number) — morning does not finalise it until the user issues it.
    }


def _check_double_bill(client: MorningClient, request: dict) -> None:
    """
    Search the client's open tax-invoice drafts for description matches.

    Raises RuntimeError naming the conflicting descriptions so the caller
    (the human gate) can resolve before retrying.  Zero false-negatives is more
    important than zero false-positives here — an unexpected match is cheap to
    dismiss, a missed double-bill is a client-facing error.
    """
    from morning_bridge.reads import (
        DOC_STATUS_OPEN,
        DOC_TYPE_TAX_INVOICE,
        search_documents,
    )

    # Non-zero unit_price lines are the billable lines; subtitle (price=0) lines
    # are separators and are excluded from the guard.
    billable_descs = {
        line["description"]
        for line in request["lines"]
        if float(line.get("unit_price", 0)) > 0
    }
    if not billable_descs:
        return  # nothing billable to guard against

    recent = search_documents(
        client,
        doc_type=[DOC_TYPE_TAX_INVOICE],
        status=[DOC_STATUS_OPEN],
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
        raise RuntimeError(
            "Double-bill guard: open draft(s) already contain matching descriptions. "
            "Resolve manually before retrying.\n"
            f"  Conflicting: {sorted(conflicts)}"
        )


def create_draft(client: MorningClient, request: dict) -> dict:
    """
    Create a persisted draft invoice in morning (status=Open, no invoice number).

    Returns the morning API response dict.  In dry-run mode (DRY_RUN=true env var)
    returns {"dry_run": True, "payload": <the body that would be sent>} and makes
    no HTTP call.

    Raises RuntimeError when the double-bill guard fires.
    Raises ValueError when required request fields are missing.
    """
    _validate_request(request)
    payload = _build_payload(request)

    if os.environ.get("DRY_RUN", "").lower() == "true":
        return {"dry_run": True, "payload": payload}

    _check_double_bill(client, request)

    result = client._create("/documents", payload)

    # Verify the API returned a draft, not an issued document.
    # status=0 → Open/draft; a missing invoiceNumber confirms it was not finalized.
    status = result.get("status")
    invoice_num = result.get("number") or result.get("invoiceNumber")
    if status != 0 or invoice_num:
        raise RuntimeError(
            f"morning returned an unexpected document state after create: "
            f"status={status!r}, invoiceNumber={invoice_num!r}. "
            "Expected status=0 and no invoice number (draft)."
        )

    return result


def _validate_request(request: dict) -> None:
    required = {"bill_to_client_id", "language", "currency", "vat_rate", "lines"}
    missing = required - request.keys()
    if missing:
        raise ValueError(
            f"CreateDraftRequest missing required fields: {sorted(missing)}"
        )
    if not request["lines"]:
        raise ValueError("CreateDraftRequest.lines must not be empty")
    for i, line in enumerate(request["lines"]):
        for field in ("description", "quantity", "unit_price"):
            if field not in line:
                raise ValueError(f"lines[{i}] missing field {field!r}")
