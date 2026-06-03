"""
State loaders — task 1.7.

Loads the four state sources consumed by the invoicing rules skill:
  - Client Profiles   (CSV or Sheets export)
  - Agreements        (CSV or Sheets export)
  - Work Item Ledger  (CSV or Sheets export)
  - Price Book        (data/price_book.csv, gitignored, produced by normalize_price_list.py)

All loaders accept a filesystem path so they are testable with synthetic fixtures.
The Google Sheets connector (task future) will write the same CSV format before
handing off to these loaders — no loader change needed at that point.

Column names follow sheets/schema.py exactly. Empty CSV cells become None.
Boolean columns (is_agency, managed_by_agent, is_range) accept TRUE/FALSE
(Google Sheets export) or true/false or 1/0.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────


def _str(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s if s else None


def _bool(v: str | None) -> bool:
    if not v:
        return False
    return v.strip().upper() in {"TRUE", "1", "YES"}


def _float(v: str | None) -> float | None:
    if not v:
        return None
    s = v.strip()
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ── ClientProfile ─────────────────────────────────────────────────────────────


@dataclass
class ClientProfile:
    bill_to: str
    is_agency: bool
    managed_by_agent: bool
    language: str
    currency: str
    vat_rate: float
    morning_client_id: str | None
    notes: str | None


def load_client_profiles(path: str | Path) -> dict[str, ClientProfile]:
    """Return dict keyed by bill_to."""
    rows = _read_csv(Path(path))
    profiles: dict[str, ClientProfile] = {}
    for r in rows:
        bill_to = r["bill_to"].strip()
        if not bill_to:
            continue
        profiles[bill_to] = ClientProfile(
            bill_to=bill_to,
            is_agency=_bool(r.get("is_agency", "")),
            managed_by_agent=_bool(r.get("managed_by_agent", "")),
            language=r.get("language", "he").strip() or "he",
            currency=r.get("currency", "ILS").strip() or "ILS",
            vat_rate=_float(r.get("vat_rate", "0")) or 0.0,
            morning_client_id=_str(r.get("morning_client_id", "")),
            notes=_str(r.get("notes", "")),
        )
    return profiles


# ── Agreement ─────────────────────────────────────────────────────────────────


@dataclass
class Agreement:
    agreement_id: str
    bill_to: str
    end_client: str | None
    item_desc: str
    agreed_price: float
    currency: str
    agreed_on: str | None
    source_ref: str | None
    confidence: str  # "confirmed" | "detected"

    @property
    def is_confirmed(self) -> bool:
        return self.confidence.strip().lower() == "confirmed"


def load_agreements(path: str | Path) -> list[Agreement]:
    """Returns all agreements. Callers filter to .is_confirmed as needed."""
    rows = _read_csv(Path(path))
    out: list[Agreement] = []
    for r in rows:
        aid = r.get("agreement_id", "").strip()
        if not aid:
            continue
        price = _float(r.get("agreed_price", ""))
        if price is None:
            continue
        out.append(
            Agreement(
                agreement_id=aid,
                bill_to=r.get("bill_to", "").strip(),
                end_client=_str(r.get("end_client", "")),
                item_desc=r.get("item_desc", "").strip(),
                agreed_price=price,
                currency=r.get("currency", "USD").strip() or "USD",
                agreed_on=_str(r.get("agreed_on", "")),
                source_ref=_str(r.get("source_ref", "")),
                confidence=r.get("confidence", "detected").strip() or "detected",
            )
        )
    return out


# ── LedgerItem ────────────────────────────────────────────────────────────────


@dataclass
class LedgerItem:
    # Identity & classification
    item_id: str
    bill_to: str
    end_client: str | None
    description: str
    assignee: str | None
    item_kind: str  # "fixed_quote" | "unit_based"
    billing_mode: str | None  # "defer" | "partial" | None (unit_based)

    # Pricing
    unit_price: float | None
    currency: str
    price_source: str | None  # "price_book" | "negotiated"
    price_ref: str | None

    # Progress (fixed_quote only)
    total_qty: float | None
    qty_billed_to_date: float | None
    last_billed_month: str | None

    # Agent provisional read (filled each cycle by LLM)
    status_agent: str | None  # "not_started" | "in_progress" | "complete"
    completion_evidence: str | None
    confidence: str | None  # "high" | "med" | "low"
    qty_proposed: float | None

    # Human gate + morning truth
    status_confirmed: str | None
    decision: str | None  # "bill" | "partial" | "defer" | "hold"
    qty_approved: float | None
    qty_billed_actual: float | None
    morning_doc_ref: str | None  # the issued-invoice id (set at settlement)
    # The pending proforma id (set at RECORD, cleared at settlement). Its presence
    # means "a proforma exists for this item this cycle" — CREATE skips such items
    # (idempotency), and settlement reconciles them against issued invoices.
    proforma_doc_ref: str | None = field(default=None)
    notes: str | None = field(default=None)


# Ledger CSV column order — mirrors sheets/schema.py LEDGER exactly so a written
# ledger round-trips through load_ledger and the future Sheets connector.
LEDGER_COLUMNS = [
    "item_id",
    "bill_to",
    "end_client",
    "description",
    "assignee",
    "item_kind",
    "billing_mode",
    "unit_price",
    "currency",
    "price_source",
    "price_ref",
    "total_qty",
    "qty_billed_to_date",
    "last_billed_month",
    "status_agent",
    "completion_evidence",
    "confidence",
    "qty_proposed",
    "status_confirmed",
    "decision",
    "qty_approved",
    "qty_billed_actual",
    "morning_doc_ref",
    "proforma_doc_ref",
    "notes",
]


def load_ledger(path: str | Path) -> list[LedgerItem]:
    rows = _read_csv(Path(path))
    out: list[LedgerItem] = []
    for r in rows:
        iid = r.get("item_id", "").strip()
        if not iid:
            continue
        out.append(
            LedgerItem(
                item_id=iid,
                bill_to=r.get("bill_to", "").strip(),
                end_client=_str(r.get("end_client", "")),
                description=r.get("description", "").strip(),
                assignee=_str(r.get("assignee", "")),
                item_kind=r.get("item_kind", "fixed_quote").strip() or "fixed_quote",
                billing_mode=_str(r.get("billing_mode", "")),
                unit_price=_float(r.get("unit_price", "")),
                currency=r.get("currency", "ILS").strip() or "ILS",
                price_source=_str(r.get("price_source", "")),
                price_ref=_str(r.get("price_ref", "")),
                total_qty=_float(r.get("total_qty", "")),
                qty_billed_to_date=_float(r.get("qty_billed_to_date", "")),
                last_billed_month=_str(r.get("last_billed_month", "")),
                status_agent=_str(r.get("status_agent", "")),
                completion_evidence=_str(r.get("completion_evidence", "")),
                confidence=_str(r.get("confidence", "")),
                qty_proposed=_float(r.get("qty_proposed", "")),
                status_confirmed=_str(r.get("status_confirmed", "")),
                decision=_str(r.get("decision", "")),
                qty_approved=_float(r.get("qty_approved", "")),
                qty_billed_actual=_float(r.get("qty_billed_actual", "")),
                morning_doc_ref=_str(r.get("morning_doc_ref", "")),
                proforma_doc_ref=_str(r.get("proforma_doc_ref", "")),
                notes=_str(r.get("notes", "")),
            )
        )
    return out


def write_ledger(ledger: list[LedgerItem], path: str | Path) -> None:
    """
    Persist the ledger back to CSV in the canonical column order.

    Counterpart to load_ledger; the future Sheets connector writes the same shape.
    Empty/None cells are written as "" so a re-load yields None again.
    """

    def _cell(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        return str(v)

    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        writer.writeheader()
        for item in ledger:
            writer.writerow(
                {
                    "item_id": _cell(item.item_id),
                    "bill_to": _cell(item.bill_to),
                    "end_client": _cell(item.end_client),
                    "description": _cell(item.description),
                    "assignee": _cell(item.assignee),
                    "item_kind": _cell(item.item_kind),
                    "billing_mode": _cell(item.billing_mode),
                    "unit_price": _cell(item.unit_price),
                    "currency": _cell(item.currency),
                    "price_source": _cell(item.price_source),
                    "price_ref": _cell(item.price_ref),
                    "total_qty": _cell(item.total_qty),
                    "qty_billed_to_date": _cell(item.qty_billed_to_date),
                    "last_billed_month": _cell(item.last_billed_month),
                    "status_agent": _cell(item.status_agent),
                    "completion_evidence": _cell(item.completion_evidence),
                    "confidence": _cell(item.confidence),
                    "qty_proposed": _cell(item.qty_proposed),
                    "status_confirmed": _cell(item.status_confirmed),
                    "decision": _cell(item.decision),
                    "qty_approved": _cell(item.qty_approved),
                    "qty_billed_actual": _cell(item.qty_billed_actual),
                    "morning_doc_ref": _cell(item.morning_doc_ref),
                    "proforma_doc_ref": _cell(item.proforma_doc_ref),
                    "notes": _cell(item.notes),
                }
            )


# ── PriceBookRow ──────────────────────────────────────────────────────────────


@dataclass
class PriceBookRow:
    price_id: str
    version: str
    effective_from: str | None
    effective_to: str | None
    category: str
    item: str
    price_low: float
    price_high: float
    currency: str
    is_range: bool
    notes: str | None


def load_price_book(path: str | Path) -> dict[str, PriceBookRow]:
    """Return dict keyed by price_id."""
    rows = _read_csv(Path(path))
    out: dict[str, PriceBookRow] = {}
    for r in rows:
        pid = r.get("price_id", "").strip()
        if not pid:
            continue
        price_low = _float(r.get("price_low", "")) or 0.0
        price_high = _float(r.get("price_high", "")) or price_low
        out[pid] = PriceBookRow(
            price_id=pid,
            version=r.get("version", "").strip(),
            effective_from=_str(r.get("effective_from", "")),
            effective_to=_str(r.get("effective_to", "")),
            category=r.get("category", "").strip(),
            item=r.get("item", "").strip(),
            price_low=price_low,
            price_high=price_high,
            currency=r.get("currency", "ILS").strip() or "ILS",
            is_range=_bool(r.get("is_range", "FALSE")),
            notes=_str(r.get("notes", "")),
        )
    return out
