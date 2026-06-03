"""
Gate → create_proforma handoff + ledger record — task 1.8.

This is steps 8 (CREATE) and 9 (RECORD) of the monthly cycle in docs/05. It runs
AFTER the human gate has written `status_confirmed`, `decision`, and `qty_approved`
onto the ledger. It does NOT decide what to bill — it mechanically turns
gate-approved ledger rows into morning `create_proforma` requests, and records the
returned proforma ids back onto the ledger.

Grouping and payload mapping follow docs/01 §5:
  - Group approved items by `bill_to`.
  - Agency (`is_agency`): one proforma per `end_client`. Each opens with a
    zero-priced subtitle line and sets the document-level `description` to the
    end-client name (renders as a bold heading in morning).
  - Direct: one proforma, no subtitle line, no document-level `description`.
  - Line: quantity = `qty_approved`, unit_price = resolved price, description =
    `annotate_description(item, qty_approved)`, currency/VAT from Client Profile.

Safety invariants enforced here:
  - Never invent a price. An approved item whose price will not resolve is a
    blocker — the whole batch raises rather than guessing or silently dropping it.
  - Drafts only. This builds `create_proforma` requests; the bridge hard-locks
    type=300 (Proforma). Nothing here issues or sends.
  - `managed_by_agent=False` clients are never billed by the agent (settlement
    handles their docs via the orphan path); they are excluded even if approved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from invoicing_rules.packet import annotate_description
from invoicing_rules.pricing import resolve_all
from invoicing_rules.state import (
    Agreement,
    ClientProfile,
    LedgerItem,
    PriceBookRow,
    write_ledger,
)
from morning_bridge.drafts import create_proforma

# Decisions that result in a billed line. "defer"/"hold" are not billed.
_BILLABLE_DECISIONS = {"bill", "partial"}

# Subtitle separator format (agency end-client header), per docs/01 §5.
_SUBTITLE_DASHES = "------------"


@dataclass
class ProformaRequest:
    """
    One morning proforma to create, plus the ledger items it covers.

    `lines` is the wire-ready line list (the agency subtitle line, if any, is
    already the first element). `item_ids` records which ledger rows this proforma
    bills, so apply_results can write the returned proforma id back onto them.
    `end_client` is carried for traceability/logging only.
    """

    bill_to: str
    bill_to_client_id: str
    language: str
    currency: str
    vat_rate: float
    lines: list[dict]
    item_ids: list[str]
    end_client: str | None = None
    description: str | None = None  # document-level heading (agencies only)

    def to_bridge_request(self) -> dict:
        """Return the exact dict create_proforma accepts (never includes 'type')."""
        request: dict = {
            "bill_to_client_id": self.bill_to_client_id,
            "language": self.language,
            "currency": self.currency,
            "vat_rate": self.vat_rate,
            "lines": self.lines,
        }
        if self.description is not None:
            request["description"] = self.description
        return request


# ── CREATE (step 8) ───────────────────────────────────────────────────────────


def build_proforma_requests(
    ledger: list[LedgerItem],
    profiles: dict[str, ClientProfile],
    price_book: dict[str, PriceBookRow],
    agreements: list[Agreement],
    billing_month: str,
) -> list[ProformaRequest]:
    """
    Build the grouped proforma requests for all gate-approved items.

    Selects items that are managed, gate-approved (`decision` in {bill, partial})
    and have `qty_approved > 0`. Raises ValueError listing every blocker (missing
    profile / morning_client_id / unresolved price) — all-or-nothing per run, so a
    bad row never produces a half-built or mispriced draft.
    """
    billable = [item for item in ledger if _is_billable(item, profiles)]
    if not billable:
        return []

    price_results = resolve_all(billable, price_book, agreements)

    blockers: list[str] = []
    for item in billable:
        profile = profiles[item.bill_to]
        if not profile.morning_client_id:
            blockers.append(
                f"{item.item_id}: client {item.bill_to!r} has no morning_client_id"
            )
        pr = price_results[item.item_id]
        if not pr.resolved or pr.unit_price is None:
            blockers.append(f"{item.item_id}: {pr.flag or 'price did not resolve'}")
    if blockers:
        raise ValueError(
            "Cannot build proformas — resolve these before the gate hands off:\n  "
            + "\n  ".join(blockers)
        )

    # Group by bill_to → end_client. For direct clients end_client is None and all
    # items collapse into one group.
    grouped: dict[tuple[str, str | None], list[LedgerItem]] = {}
    for item in billable:
        profile = profiles[item.bill_to]
        key = (item.bill_to, item.end_client if profile.is_agency else None)
        grouped.setdefault(key, []).append(item)

    requests: list[ProformaRequest] = []
    for (bill_to, end_client), items in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
    ):
        profile = profiles[bill_to]
        items_sorted = sorted(items, key=lambda it: it.item_id)

        lines: list[dict] = []
        if profile.is_agency and end_client:
            lines.append(_subtitle_line(end_client))
        for item in items_sorted:
            pr = price_results[item.item_id]
            qty = item.qty_approved or 0.0
            lines.append(
                {
                    "description": annotate_description(item, qty),
                    "quantity": qty,
                    "unit_price": pr.unit_price,
                }
            )

        requests.append(
            ProformaRequest(
                bill_to=bill_to,
                bill_to_client_id=profile.morning_client_id,
                language=profile.language,
                currency=profile.currency,
                vat_rate=profile.vat_rate,
                lines=lines,
                item_ids=[it.item_id for it in items_sorted],
                end_client=end_client,
                description=end_client if (profile.is_agency and end_client) else None,
            )
        )

    return requests


# ── RECORD (step 9) ───────────────────────────────────────────────────────────


def apply_results(
    ledger: list[LedgerItem],
    request: ProformaRequest,
    result: dict,
) -> list[str]:
    """
    Record the created proforma id onto the ledger rows it covers.

    Writes the proforma's morning id to `morning_doc_ref` for every item in
    `request.item_ids` (docs/02 linkage: settlement later overwrites this with the
    issued-invoice id and keeps the proforma id in proforma_doc_ref, added at 1.9).

    Dry-run results ({"dry_run": True, ...}) carry no id, so nothing is recorded —
    returns []. Otherwise returns the list of item_ids updated.

    Leaves `qty_billed_actual` empty — only next cycle's settlement fills it from
    the issued document.
    """
    if result.get("dry_run"):
        return []

    doc_id = result.get("id")
    if not doc_id:
        raise ValueError(
            "create_proforma result has no 'id' — cannot record morning_doc_ref"
        )

    by_id = {item.item_id: item for item in ledger}
    updated: list[str] = []
    for item_id in request.item_ids:
        item = by_id.get(item_id)
        if item is None:
            continue
        item.morning_doc_ref = str(doc_id)
        updated.append(item_id)
    return updated


def create_and_record(
    client: object,
    requests: list[ProformaRequest],
    ledger: list[LedgerItem],
    ledger_path: str | Path,
    *,
    create_fn: Callable[[object, dict], dict] = create_proforma,
) -> list[dict]:
    """
    Create each proforma, record its id, and persist the ledger — one request at a
    time, in that order — BEFORE moving to the next request.

    Interleaving create → record → write_ledger per request (instead of creating the
    whole batch then recording it) shrinks the dual-write crash window from the whole
    batch to a single proforma: if the process dies mid-run, at most one created
    proforma is missing its `morning_doc_ref` on disk (recoverable by checking
    morning), rather than several.

    NOT idempotent across separate CREATE runs. Re-running this on the same approved
    ledger creates DUPLICATE proformas — morning has no API delete (dashboard cleanup
    only). Full within-cycle idempotency (skip items already carrying this cycle's
    proforma_doc_ref) lands in 1.9; until then, never re-run CREATE within a cycle.

    `create_fn` is injectable for testing; it defaults to the bridge's create_proforma
    (which honours DRY_RUN). Returns the per-request results in order.
    """
    results: list[dict] = []
    for request in requests:
        result = create_fn(client, request.to_bridge_request())
        apply_results(ledger, request, result)
        write_ledger(ledger, ledger_path)
        results.append(result)
    return results


# ── internal ──────────────────────────────────────────────────────────────────


def _is_billable(item: LedgerItem, profiles: dict[str, ClientProfile]) -> bool:
    profile = profiles.get(item.bill_to)
    if profile is None or not profile.managed_by_agent:
        return False
    # The gate writes status_confirmed + decision + qty_approved together; require
    # status_confirmed so a half-written gate row is never billed.
    if not item.status_confirmed:
        return False
    if item.decision not in _BILLABLE_DECISIONS:
        return False
    return item.qty_approved is not None and item.qty_approved > 0


def _subtitle_line(end_client: str) -> dict:
    """Zero-priced agency end-client header line (docs/01 §5)."""
    return {
        "description": f"{_SUBTITLE_DASHES} {end_client} {_SUBTITLE_DASHES}",
        "quantity": 1.0,
        "unit_price": 0.0,
    }
