"""
Unit tests for drafts.create_draft.

All HTTP is mocked — no real network calls, no real credentials.
"""

import time
from unittest.mock import MagicMock

import httpx
import pytest

from morning_bridge import drafts
from morning_bridge.client import MorningClient
from morning_bridge.drafts import _build_payload, create_draft


# ── helpers ──────────────────────────────────────────────────────────────────


def _ok(json_data: dict) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = json_data
    r.content = b'{"ok": true}'
    r.raise_for_status = MagicMock()
    r.headers = {}
    return r


def _make_client(*, search_result=None, create_result=None) -> MorningClient:
    """
    Return a MorningClient whose HTTP is mocked.

    search_result: what POST /documents/search returns (default: empty list).
    create_result: what POST /documents returns (default: open draft stub).
    """
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.return_value = _ok({"token": "tok"})  # auth

    search_resp = _ok(search_result or {"items": []})
    draft_resp = _ok(create_result or {"id": "doc-123", "status": 0, "number": None})

    # request() is called for every non-auth call; route by path suffix.
    def _route(method, url, **_kwargs):
        if "/search" in url:
            return search_resp
        return draft_resp

    mock_http.request.side_effect = _route

    client = MorningClient("id", "sec", sandbox=True, http_client=mock_http)
    client._token = "tok"
    client._token_obtained_at = time.monotonic()
    return client


_DIRECT_REQUEST = {
    "bill_to_client_id": "client-il-001",
    "language": "he",
    "currency": "ILS",
    "vat_rate": 0.18,
    "lines": [
        {"description": "עיצוב לוגו", "quantity": 1.0, "unit_price": 5000.0},
    ],
}

_AGENCY_REQUEST = {
    "bill_to_client_id": "client-us-001",
    "language": "en",
    "currency": "USD",
    "vat_rate": 0.00,
    "lines": [
        # subtitle line (separator — price 0)
        {
            "description": "------------ Acme Corp ------------",
            "quantity": 1.0,
            "unit_price": 0.0,
        },
        # partial billable line (70%)
        {
            "description": "Scrolling website - 1st payment (70% so far)",
            "quantity": 0.7,
            "unit_price": 10000.0,
        },
        # fixed unit line
        {
            "description": "Trade show roll-up",
            "quantity": 2.0,
            "unit_price": 800.0,
        },
    ],
}


# ── surface ───────────────────────────────────────────────────────────────────


def test_drafts_exact_surface():
    """drafts.py exposes exactly {create_draft} — no accidental additions."""
    import inspect

    public = {
        name
        for name, obj in inspect.getmembers(drafts, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == drafts.__name__
    }
    assert public == {"create_draft"}, (
        f"Unexpected public functions in drafts.py: {public}"
    )


# ── write allowlist ───────────────────────────────────────────────────────────


def test_write_allowlist_blocks_bad_path():
    client = _make_client()
    with pytest.raises(ValueError, match="not in allowlist"):
        client._create("/documents/123/send", {})


def test_write_allowlist_passes_documents():
    client = _make_client()
    result = client._create("/documents", {"type": 305})
    assert result["id"] == "doc-123"


# ── dry-run ───────────────────────────────────────────────────────────────────


def test_dry_run_returns_payload_makes_no_request(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    client = _make_client()

    result = create_draft(client, _DIRECT_REQUEST)

    assert result["dry_run"] is True
    assert result["payload"]["type"] == 305
    assert result["payload"]["clientId"] == "client-il-001"
    # No write call — only the search call should be absent too (dry-run exits early).
    assert client._http.request.call_count == 0


def test_dry_run_false_proceeds(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    client = _make_client()
    result = create_draft(client, _DIRECT_REQUEST)
    assert result.get("id") == "doc-123"


# ── payload shape ─────────────────────────────────────────────────────────────


def test_build_payload_direct_invoice():
    payload = _build_payload(_DIRECT_REQUEST)
    assert payload["type"] == 305
    assert payload["lang"] == "he"
    assert payload["currency"] == "ILS"
    assert payload["clientId"] == "client-il-001"
    assert len(payload["income"]) == 1
    line = payload["income"][0]
    assert line["quantity"] == 1.0
    assert line["unitPrice"] == 5000.0
    assert line["price"] == 5000.0
    assert line["vat"] == 0.18
    assert "payment" not in payload  # no payment → stays draft


def test_build_payload_agency_invoice():
    payload = _build_payload(_AGENCY_REQUEST)
    assert payload["lang"] == "en"
    assert payload["currency"] == "USD"
    assert len(payload["income"]) == 3

    subtitle = payload["income"][0]
    assert subtitle["unitPrice"] == 0.0
    assert subtitle["price"] == 0.0
    assert subtitle["vat"] == 0.00

    partial = payload["income"][1]
    assert partial["quantity"] == 0.7
    assert partial["unitPrice"] == 10000.0
    assert abs(partial["price"] - 7000.0) < 0.01

    unit = payload["income"][2]
    assert unit["quantity"] == 2.0
    assert unit["price"] == 1600.0


# ── double-bill guard ─────────────────────────────────────────────────────────


def test_double_bill_guard_raises_on_matching_description(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    existing_doc = {
        "items": [
            {
                "status": 0,
                "income": [
                    {"description": "עיצוב לוגו", "unitPrice": 5000.0},
                ],
            }
        ]
    }
    client = _make_client(search_result=existing_doc)

    with pytest.raises(RuntimeError, match="Double-bill guard"):
        create_draft(client, _DIRECT_REQUEST)


def test_double_bill_guard_passes_when_no_overlap(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    existing_doc = {
        "items": [
            {
                "status": 0,
                "income": [
                    {"description": "Different work item", "unitPrice": 500.0},
                ],
            }
        ]
    }
    client = _make_client(search_result=existing_doc)
    result = create_draft(client, _DIRECT_REQUEST)
    assert result["id"] == "doc-123"


def test_double_bill_guard_ignores_subtitle_lines(monkeypatch):
    """Subtitle lines (unit_price=0) must not trigger the guard."""
    monkeypatch.setenv("DRY_RUN", "false")
    existing_doc = {
        "items": [
            {
                "status": 0,
                "income": [
                    # same text as the subtitle in _AGENCY_REQUEST, but it's zero-priced
                    {
                        "description": "------------ Acme Corp ------------",
                        "unitPrice": 0.0,
                    }
                ],
            }
        ]
    }
    client = _make_client(search_result=existing_doc)
    result = create_draft(client, _AGENCY_REQUEST)
    assert result["id"] == "doc-123"


# ── post-create validation ────────────────────────────────────────────────────


def test_create_raises_if_morning_returns_issued_doc(monkeypatch):
    """
    If morning somehow returns a closed doc (status=1 or an invoice number),
    raise so the user knows something unexpected happened.
    """
    monkeypatch.setenv("DRY_RUN", "false")
    issued = {"id": "doc-999", "status": 1, "number": "INV-2026-001"}
    client = _make_client(create_result=issued)

    with pytest.raises(RuntimeError, match="unexpected document state"):
        create_draft(client, _DIRECT_REQUEST)


def test_create_raises_if_doc_has_invoice_number(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    finalized = {"id": "doc-999", "status": 0, "number": "INV-2026-002"}
    client = _make_client(create_result=finalized)

    with pytest.raises(RuntimeError, match="unexpected document state"):
        create_draft(client, _DIRECT_REQUEST)


# ── validation ────────────────────────────────────────────────────────────────


def test_create_raises_on_missing_fields(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    with pytest.raises(ValueError, match="missing required fields"):
        create_draft(MagicMock(), {"lines": []})


def test_create_raises_on_empty_lines(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    req = {**_DIRECT_REQUEST, "lines": []}
    with pytest.raises(ValueError, match="lines must not be empty"):
        create_draft(MagicMock(), req)
