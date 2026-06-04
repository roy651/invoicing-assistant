"""
Read-only operations against the morning API (docs/03 whitelist).

These eleven functions are the complete public read surface of the bridge.
No write, issue, send, payment, close, or delete operations exist here.
create_draft lives in drafts.py (task 1.4 stub).

API field names follow the morning spec (camelCase in the wire format):
  type / status / clientId / fromDate / toDate / pageSize
"""

from __future__ import annotations

from morning_bridge.client import MorningClient

# ── Account & Business ───────────────────────────────────────────────────────


def get_account(client: MorningClient) -> dict:
    """GET /account/me — account info for the authenticated user."""
    return client.get("/account/me")


def get_account_settings(client: MorningClient) -> dict:
    """GET /account/settings — account-level settings."""
    return client.get("/account/settings")


def list_businesses(client: MorningClient) -> dict:
    """GET /businesses — all businesses on the account."""
    return client.get("/businesses")


def get_business(client: MorningClient, business_id: str | None = None) -> dict:
    """
    GET /businesses/me or /businesses/{id}.
    Omit business_id to get the current (active) business.
    """
    path = f"/businesses/{business_id}" if business_id else "/businesses/me"
    return client.get(path)


# ── Clients ──────────────────────────────────────────────────────────────────


def get_client(client: MorningClient, client_id: str) -> dict:
    """GET /clients/{id}."""
    return client.get(f"/clients/{client_id}")


def search_clients(
    client: MorningClient,
    *,
    name: str | None = None,
    email: str | None = None,
    tax_id: str | None = None,
    active: bool | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """POST /clients/search."""
    body: dict = {"page": page, "pageSize": page_size}
    if name is not None:
        body["name"] = name
    if email is not None:
        body["email"] = email
    if tax_id is not None:
        body["taxId"] = tax_id
    if active is not None:
        body["active"] = active
    return client.post("/clients/search", body)


# ── Items ────────────────────────────────────────────────────────────────────


def get_item(client: MorningClient, item_id: str) -> dict:
    """GET /items/{id}."""
    return client.get(f"/items/{item_id}")


def search_items(
    client: MorningClient,
    *,
    name: str | None = None,
    active: bool | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """POST /items/search."""
    body: dict = {"page": page, "pageSize": page_size}
    if name is not None:
        body["name"] = name
    if active is not None:
        body["active"] = active
    return client.post("/items/search", body)


# ── Documents ────────────────────────────────────────────────────────────────

# Document type codes (from morning API enum)
DOC_TYPE_PROFORMA = 300  # Proforma / חשבון עסקה — non-fiscal review artifact
DOC_TYPE_TAX_INVOICE = 305  # Tax Invoice — fiscal; issuance is human-only
DOC_TYPE_RECEIPT = 400
DOC_TYPE_PRICE_QUOTE = 10

# Document status codes
DOC_STATUS_OPEN = 0  # unpaid / in progress (a type-305 is still fiscally ISSUED)
DOC_STATUS_CLOSED = 1  # paid / closed
DOC_STATUS_CANCELLED = 4  # cancelled (carries a linked cancellation doc, type 330)


def get_document(client: MorningClient, document_id: str) -> dict:
    """GET /documents/{id}."""
    return client.get(f"/documents/{document_id}")


def search_documents(
    client: MorningClient,
    *,
    doc_type: list[int] | None = None,
    status: list[int] | None = None,
    client_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """
    POST /documents/search.

    doc_type: list of type codes, e.g. [305] for Tax Invoice.
    status:   list of status codes, e.g. [0] for open/draft, [1] for closed/issued.
    from_date / to_date: ISO date strings "YYYY-MM-DD".
    """
    body: dict = {"page": page, "pageSize": page_size}
    if doc_type is not None:
        body["type"] = doc_type
    if status is not None:
        body["status"] = status
    if client_id is not None:
        body["clientId"] = client_id
    if from_date is not None:
        body["fromDate"] = from_date
    if to_date is not None:
        body["toDate"] = to_date
    return client.post("/documents/search", body)


def get_document_download_links(client: MorningClient, document_id: str) -> dict:
    """GET /documents/{id}/download/links — PDF download URLs (he, en, origin)."""
    return client.get(f"/documents/{document_id}/download/links")
