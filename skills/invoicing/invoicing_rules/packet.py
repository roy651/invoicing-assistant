"""
Review-packet builder — task 1.7.

Takes a ledger already annotated by the LLM (status_agent, completion_evidence,
confidence, qty_proposed all filled) plus client profiles and pricing data, and
assembles the structured review packet the human gate consumes.

The packet is pure data: no formatting, no presentation concerns. SKILL.md
formats it for the user; the gate stores decisions back to the ledger.

Grouping: bill_to → end_client (None for direct clients).
Sorting within each group: flagged items (low confidence, unresolved price,
partial-fraction judgment calls) appear before clean items.

This function does NOT set status_confirmed, decision, or qty_approved.
Those are the gate's job.

managed_by_agent=False clients are excluded entirely — their docs flow only
through settlement's silent orphan path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from invoicing_rules.pricing import PriceResult, resolve_all
from invoicing_rules.state import Agreement, ClientProfile, LedgerItem, PriceBookRow

# ── flag kinds (stable string constants for SKILL.md and tests) ──────────────

FLAG_UNRESOLVED_PRICE = "unresolved_price"
FLAG_LOW_CONFIDENCE = "low_confidence"
FLAG_PARTIAL_FRACTION = "partial_fraction"  # suggested fraction; user must confirm
FLAG_UNCERTAIN_MATCH = "uncertain_match"  # LLM set this in completion_evidence
FLAG_NEW_ITEM = "new_item"  # not yet in ledger; surfaced by LLM
FLAG_DEFER = "defer"  # qty_proposed == 0; included for visibility


@dataclass
class Flag:
    kind: str
    message: str


@dataclass
class ProposedLine:
    item_id: str
    bill_to: str
    end_client: str | None
    description: str  # with progress annotation if partial fixed_quote
    item_kind: str
    billing_mode: str | None
    qty_proposed: float
    unit_price: float | None
    line_total: float | None  # qty_proposed * unit_price, or None if unresolved
    currency: str
    status_agent: str | None
    confidence: str | None
    completion_evidence: str | None
    price_ref: str | None
    price_source: str | None
    flags: list[Flag] = field(default_factory=list)


@dataclass
class EndClientGroup:
    """One group of proposed lines sharing the same end_client (or None for direct)."""

    end_client: str | None  # None for direct clients
    lines: list[ProposedLine] = field(default_factory=list)


@dataclass
class BillToGroup:
    """All proposed lines for a single bill_to, split by end_client."""

    bill_to: str
    is_agency: bool
    language: str
    currency: str
    vat_rate: float
    morning_client_id: str | None
    end_client_groups: list[EndClientGroup] = field(default_factory=list)


@dataclass
class ReviewPacket:
    generated_at: datetime
    billing_month: str  # YYYY-MM
    groups: list[BillToGroup] = field(default_factory=list)
    # Summary counts for quick triage
    total_lines: int = 0
    unresolved_count: int = 0
    low_confidence_count: int = 0
    deferred_count: int = 0


# ── public API ────────────────────────────────────────────────────────────────


def build_review_packet(
    ledger: list[LedgerItem],
    profiles: dict[str, ClientProfile],
    price_book: dict[str, PriceBookRow],
    agreements: list[Agreement],
    billing_month: str,
) -> ReviewPacket:
    """
    Build the review packet for the given billing_month.

    Only processes items whose bill_to has a profile with managed_by_agent=True.
    Items without status_agent set are skipped (LLM has not yet assessed them).
    """
    # Resolve prices for all managed items first.
    managed = [
        item
        for item in ledger
        if _is_managed(item, profiles) and item.status_agent is not None
    ]
    price_results = resolve_all(managed, price_book, agreements)

    # Group by bill_to → end_client.
    bt_map: dict[str, dict[str | None, list[ProposedLine]]] = {}
    for item in managed:
        pr = price_results[item.item_id]
        line = _build_line(item, pr)
        bt_map.setdefault(item.bill_to, {}).setdefault(item.end_client, []).append(line)

    packet = ReviewPacket(
        generated_at=datetime.now(tz=timezone.utc),
        billing_month=billing_month,
    )

    for bill_to, ec_map in sorted(bt_map.items()):
        profile = profiles[bill_to]
        bt_group = BillToGroup(
            bill_to=bill_to,
            is_agency=profile.is_agency,
            language=profile.language,
            currency=profile.currency,
            vat_rate=profile.vat_rate,
            morning_client_id=profile.morning_client_id,
        )
        for end_client, lines in sorted(ec_map.items(), key=lambda kv: (kv[0] or "")):
            sorted_lines = _sort_lines(lines)
            bt_group.end_client_groups.append(
                EndClientGroup(end_client=end_client, lines=sorted_lines)
            )
        packet.groups.append(bt_group)

    # Compute summary counts.
    all_lines = [
        line for bt in packet.groups for ec in bt.end_client_groups for line in ec.lines
    ]
    packet.total_lines = len(all_lines)
    packet.unresolved_count = sum(
        1 for ln in all_lines if any(f.kind == FLAG_UNRESOLVED_PRICE for f in ln.flags)
    )
    packet.low_confidence_count = sum(
        1 for ln in all_lines if any(f.kind == FLAG_LOW_CONFIDENCE for f in ln.flags)
    )
    packet.deferred_count = sum(
        1 for ln in all_lines if any(f.kind == FLAG_DEFER for f in ln.flags)
    )

    return packet


# ── internal ──────────────────────────────────────────────────────────────────


def _is_managed(item: LedgerItem, profiles: dict[str, ClientProfile]) -> bool:
    profile = profiles.get(item.bill_to)
    return profile is not None and profile.managed_by_agent


def _build_line(item: LedgerItem, pr: PriceResult) -> ProposedLine:
    qty = item.qty_proposed or 0.0
    description = annotate_description(item, qty)
    unit_price = pr.unit_price
    line_total = round(qty * unit_price, 2) if unit_price is not None else None

    flags: list[Flag] = []

    if not pr.resolved:
        flags.append(
            Flag(
                kind=FLAG_UNRESOLVED_PRICE,
                message=pr.flag or "unresolved price",
            )
        )

    if item.confidence == "low":
        flags.append(
            Flag(
                kind=FLAG_LOW_CONFIDENCE,
                message=f"confidence=low: {item.completion_evidence or 'no evidence cited'}",
            )
        )

    if item.item_kind == "fixed_quote" and item.billing_mode == "partial" and qty > 0:
        flags.append(
            Flag(
                kind=FLAG_PARTIAL_FRACTION,
                message=f"suggested fraction {qty:.0%} — user should confirm the exact amount",
            )
        )

    if qty == 0.0:
        flags.append(
            Flag(
                kind=FLAG_DEFER,
                message="qty_proposed=0 (deferred); included for visibility",
            )
        )

    return ProposedLine(
        item_id=item.item_id,
        bill_to=item.bill_to,
        end_client=item.end_client,
        description=description,
        item_kind=item.item_kind,
        billing_mode=item.billing_mode,
        qty_proposed=qty,
        unit_price=unit_price,
        line_total=line_total,
        currency=item.currency,
        status_agent=item.status_agent,
        confidence=item.confidence,
        completion_evidence=item.completion_evidence,
        price_ref=pr.price_ref,
        price_source=pr.source,
        flags=flags,
    )


def annotate_description(item: LedgerItem, this_qty: float) -> str:
    """
    Append a progress annotation for partial fixed_quote items, using `this_qty`
    as the amount billed/proposed in THIS document.

    Format: "<description> - <ordinal> payment (<cumulative>% so far)"
    or      "<description> - <ordinal> and final payment"

    Shared by the review packet (this_qty = qty_proposed) and the create handoff
    (this_qty = qty_approved), so the proforma's progress note reflects the
    quantity actually approved, not the original proposal.

    Ordinal: "1st" if nothing billed yet, otherwise generic "next" — we cannot
    determine the exact payment number from qty_billed_to_date alone (it is a
    cumulative sum, not a payment count). The LLM can override this annotation
    in the description field before the packet builder is called if it has
    richer context.
    """
    if item.item_kind != "fixed_quote" or item.billing_mode != "partial":
        return item.description

    total = item.total_qty or 1.0
    prior = item.qty_billed_to_date or 0.0

    cumulative_pct = math.floor((prior + this_qty) / total * 100)
    is_final = (prior + this_qty) >= total

    ordinal = "1st" if _is_approx_zero(prior) else "next"

    if is_final:
        suffix = f" - {ordinal} and final payment"
    else:
        suffix = f" - {ordinal} payment ({cumulative_pct}% so far)"

    return item.description + suffix


def _is_approx_zero(v: float) -> bool:
    return abs(v) < 1e-9


def _sort_lines(lines: list[ProposedLine]) -> list[ProposedLine]:
    """
    Sort lines so those needing human judgment appear first.

    Priority (lower = shown first):
      0 — unresolved price
      1 — low confidence
      2 — partial fraction suggestion
      3 — deferred (qty=0)
      4 — clean proposed
    """

    def _priority(line: ProposedLine) -> int:
        kinds = {f.kind for f in line.flags}
        if FLAG_UNRESOLVED_PRICE in kinds:
            return 0
        if FLAG_LOW_CONFIDENCE in kinds:
            return 1
        if FLAG_PARTIAL_FRACTION in kinds:
            return 2
        if FLAG_DEFER in kinds:
            return 3
        return 4

    return sorted(lines, key=_priority)
