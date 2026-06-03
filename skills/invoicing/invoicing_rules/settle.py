"""
Settlement reconciliation — task 1.9 (docs/02 §C).

Settlement is step 1 of the monthly cycle: it reconciles the ledger to the
documents actually ISSUED in morning since the last run, BEFORE any new proposing.
morning is truth — `qty_billed_to_date` accumulates only from what was issued, read
back here, never from a proposal or a gate approval.

The system creates Proformas (type 300); the human converts approved ones to Tax
Invoices (type 305) in morning, which links each invoice back to its source proforma
via `linkedDocumentIds`. Settlement reads the issued INVOICES as truth and matches
each ledger item to one (docs/02 "Proforma → Invoice linkage"):

  1. Primary — the item's `proforma_doc_ref` (set at RECORD) appears in an issued
     invoice's `linkedDocumentIds`.
  2. Content fallback — same morning client + a line whose description matches the
     item (proforma deleted before linking, or linkedDocumentIds absent).
  3. No match — the item's proforma was never issued (deleted draft / deleted line /
     deleted invoice) → revert to open: clear `proforma_doc_ref`, leave
     `qty_billed_to_date` unchanged. Work is not lost; it re-enters the open set.

Issued-invoice lines not consumed by any ledger item are ORPHANS — work the user
added directly in morning. Managed clients get a back-filled ledger row (flagged);
unmanaged clients (e.g. the manually-billed utilities) are recorded silently.

Settlement uses only read endpoints; the drafts-only contract holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from invoicing_rules.state import ClientProfile, LedgerItem

_EPS = 1e-9


@dataclass
class QtyEdit:
    """A settled item whose issued quantity differs from what was approved."""

    item_id: str
    qty_approved: float | None
    qty_billed_actual: float


@dataclass
class SettlementReport:
    settled: list[str] = field(default_factory=list)
    qty_edits: list[QtyEdit] = field(default_factory=list)
    still_pending: list[str] = field(default_factory=list)  # proforma not yet issued
    reverted: list[str] = field(default_factory=list)
    orphans_flagged: list[str] = field(default_factory=list)  # new ledger item_ids
    orphans_silent: list[str] = field(default_factory=list)  # "<doc_id>#<line_idx>"

    def summary(self) -> str:
        """The brief diff report shown to the user before new proposing starts."""
        return (
            f"Since last time: {len(self.settled)} settled "
            f"({len(self.qty_edits)} with qty edits), "
            f"{len(self.still_pending)} still pending (awaiting issuance), "
            f"{len(self.reverted)} reverted to open, "
            f"{len(self.orphans_flagged)} orphan(s) back-filled and flagged, "
            f"{len(self.orphans_silent)} silent orphan(s) on unmanaged clients. "
            f"Ledger updated."
        )


def settle_ledger(
    ledger: list[LedgerItem],
    issued_docs: list[dict],
    profiles: dict[str, ClientProfile],
    *,
    live_proforma_ids: set[str] | None = None,
) -> SettlementReport:
    """
    Reconcile the ledger to the issued invoices. Mutates `ledger` in place
    (updates pending items, appends orphan rows) and returns a SettlementReport.

    `live_proforma_ids` are the ids of proformas (type 300) that STILL EXIST,
    unconverted, in morning — fetch them with `fetch_open_proformas`. They are how a
    not-yet-issued proforma (leave pending) is told apart from a deleted one (revert).
    Issuance is human-paced and asynchronous, so a pending proforma usually has no
    issued invoice yet; reverting it would re-propose the work and create a DUPLICATE
    proforma (morning has no API delete). When `live_proforma_ids` is None the liveness
    is unknown, so we are conservative and never revert on a missing invoice — the item
    stays pending until a run can confirm the proforma is actually gone.
    """
    client_to_bill_to = {
        p.morning_client_id: p.bill_to for p in profiles.values() if p.morning_client_id
    }
    consumed: set[tuple[str, int]] = set()
    report = SettlementReport()

    # 1. Reconcile every item that carries a pending proforma.
    for item in [it for it in ledger if it.proforma_doc_ref]:
        profile = profiles.get(item.bill_to)
        client_id = profile.morning_client_id if profile else None
        doc, idx = _find_settlement(item, issued_docs, client_id, consumed)

        if doc is not None and idx is not None:
            line = doc["income"][idx]
            qty = _opt_float(line.get("quantity")) or 0.0
            item.qty_billed_actual = qty
            item.qty_billed_to_date = (item.qty_billed_to_date or 0.0) + qty
            item.morning_doc_ref = str(doc["id"])
            item.last_billed_month = _doc_month(doc)
            _recompute_status(item)
            item.proforma_doc_ref = None  # settled — no longer pending
            consumed.add((str(doc["id"]), idx))
            report.settled.append(item.item_id)
            if item.qty_approved is not None and abs(qty - item.qty_approved) > _EPS:
                report.qty_edits.append(QtyEdit(item.item_id, item.qty_approved, qty))
        elif doc is not None:
            # The proforma WAS issued as this invoice (linked), but its line was
            # deleted → revert this item to open. qty_billed_to_date unchanged.
            item.proforma_doc_ref = None
            report.reverted.append(item.item_id)
        elif live_proforma_ids is None or item.proforma_doc_ref in live_proforma_ids:
            # No issued invoice yet, but the proforma still exists (or liveness is
            # unknown) → still pending. Do NOT revert: that would duplicate it.
            report.still_pending.append(item.item_id)
        else:
            # No invoice and the proforma is gone (deleted draft / deleted invoice) →
            # revert to open; work re-enters the open set, evidence intact.
            item.proforma_doc_ref = None
            report.reverted.append(item.item_id)

    # 2. Orphans: issued lines no ledger item consumed.
    for doc in issued_docs:
        client_id = doc.get("client", {}).get("id")
        bill_to = client_to_bill_to.get(client_id)
        profile = profiles.get(bill_to) if bill_to else None
        for idx, line in enumerate(doc.get("income", [])):
            if (str(doc["id"]), idx) in consumed:
                continue
            if _is_zero_priced(line):
                continue  # subtitle / header line — never a billable orphan
            if profile is not None and not profile.managed_by_agent:
                report.orphans_silent.append(f"{doc['id']}#{idx}")
                continue
            new_item = _orphan_row(line, doc, bill_to, idx)
            ledger.append(new_item)
            report.orphans_flagged.append(new_item.item_id)

    return report


def fetch_issued_invoices(
    client: object,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """
    Thin read-only wrapper: issued Tax Invoices (type 305, closed) in the window.

    Settlement reads issued invoices, not proformas. Returns the raw morning doc
    dicts (each carries `linkedDocumentIds` back to its source proforma).

    IMPORTANT: `from_date` must reach back to the OLDEST unsettled proforma, not just
    last month. Issuance is human-paced — a proforma created months ago may be
    converted to an invoice only now. If the window misses that late invoice, the
    item looks neither-issued-nor-live and gets falsely reverted (→ duplicate). The
    runner (task 1.10) owns choosing this date from the oldest pending row.
    """
    from morning_bridge.reads import (
        DOC_STATUS_CLOSED,
        DOC_TYPE_TAX_INVOICE,
        search_documents,
    )

    resp = search_documents(
        client,
        doc_type=[DOC_TYPE_TAX_INVOICE],
        status=[DOC_STATUS_CLOSED],
        from_date=from_date,
        to_date=to_date,
    )
    return resp.get("items", [])


def fetch_open_proformas(client: object) -> set[str]:
    """
    Read-only: ids of Proformas (type 300) still OPEN (unconverted) in morning.

    Pass the result as `settle_ledger(..., live_proforma_ids=...)` so a not-yet-issued
    proforma is left pending instead of being reverted into a duplicate.
    """
    from morning_bridge.reads import DOC_TYPE_PROFORMA, search_documents

    resp = search_documents(client, doc_type=[DOC_TYPE_PROFORMA])
    return {str(d["id"]) for d in resp.get("items", []) if d.get("id")}


# ── internal ──────────────────────────────────────────────────────────────────


def _find_settlement(
    item: LedgerItem,
    issued_docs: list[dict],
    client_id: str | None,
    consumed: set[tuple[str, int]],
) -> tuple[dict | None, int | None]:
    # Primary: the invoice whose linkedDocumentIds references this proforma. If the
    # invoice exists but the item's line was deleted, idx is None → caller reverts
    # (we do NOT then hunt other invoices: this proforma's fate is decided).
    for doc in issued_docs:
        if item.proforma_doc_ref in doc.get("linkedDocumentIds", []):
            return doc, _match_line_index(item, doc, consumed)
    # Content fallback: same client + a matching line.
    for doc in issued_docs:
        if client_id and doc.get("client", {}).get("id") == client_id:
            idx = _match_line_index(item, doc, consumed)
            if idx is not None:
                return doc, idx
    return None, None


def _match_line_index(
    item: LedgerItem, doc: dict, consumed: set[tuple[str, int]]
) -> int | None:
    """
    First unconsumed, non-zero-priced income line whose description matches.

    Limitation: startswith is first-match-wins. If two items share a description
    prefix on the same invoice, the first in iteration order claims the first line.
    Revisit against real fixtures (Phase 2) if same-prefix collisions occur.
    """
    for idx, line in enumerate(doc.get("income", [])):
        if (str(doc["id"]), idx) in consumed:
            continue
        if _is_zero_priced(line):
            continue
        desc = str(line.get("description", ""))
        if item.description and desc.startswith(item.description):
            return idx
    return None


def _recompute_status(item: LedgerItem) -> None:
    """Set status_confirmed from accumulated actuals (docs/02: complete if billed >= total)."""
    if item.item_kind == "fixed_quote" and item.total_qty:
        billed = item.qty_billed_to_date or 0.0
        item.status_confirmed = (
            "complete" if billed >= item.total_qty - _EPS else "in_progress"
        )
    # unit_based items recur; leave status_confirmed as the gate set it.


def _orphan_row(line: dict, doc: dict, bill_to: str | None, idx: int) -> LedgerItem:
    qty = _opt_float(line.get("quantity")) or 0.0
    return LedgerItem(
        item_id=f"ORPHAN-{doc['id']}-{idx}",
        bill_to=bill_to or str(doc.get("client", {}).get("id", "")),
        end_client=None,
        description=str(line.get("description", "")),
        assignee=None,
        item_kind="unit_based",
        billing_mode=None,
        unit_price=_opt_float(line.get("unitPrice")),
        currency=str(line.get("currency", "")) or "ILS",
        price_source=None,
        price_ref=None,
        total_qty=None,
        qty_billed_to_date=qty,
        last_billed_month=_doc_month(doc),
        status_agent=None,
        completion_evidence=None,
        confidence=None,
        qty_proposed=None,
        status_confirmed="complete",
        decision=None,
        qty_approved=None,
        qty_billed_actual=qty,
        morning_doc_ref=str(doc["id"]),
        proforma_doc_ref=None,
        notes="added manually in morning",
    )


def _doc_month(doc: dict) -> str | None:
    """YYYY-MM from the doc's date, when it is an ISO string."""
    raw = doc.get("documentDate") or doc.get("date")
    if isinstance(raw, str) and len(raw) >= 7:
        return raw[:7]
    return None


def _is_zero_priced(line: dict) -> bool:
    # Zero-priced line = the synthesized agency subtitle/header, never an orphan.
    # TODO(Phase 2): confirm morning's actual issued-line field names ("price" /
    # "unitPrice") against real invoices so a subtitle is never mistaken for an orphan.
    val = _opt_float(line.get("price"))
    if val is None:
        val = _opt_float(line.get("unitPrice"))
    return val is not None and abs(val) < _EPS


def _opt_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
