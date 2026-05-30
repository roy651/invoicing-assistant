"""
Price resolution — task 1.7.

Given a LedgerItem, determines the authoritative unit_price with a traceable source.

Resolution order:
  1. unit_price already set on the item AND price_ref present → trust it (was
     resolved at item-creation time; price_ref provides the audit trail).
  2. price_source == "negotiated" → look up price_ref in confirmed Agreements.
     Unconfirmed ("detected") agreements are NOT used — flag as unresolved.
  3. price_source == "price_book" → look up price_ref in Price Book.
     Non-range entry → use price_low (== price_high).
     Range entry without unit_price on the item → UNRESOLVED (never auto-guess).
  4. Any other case (missing price_ref, unknown source) → UNRESOLVED.

INVARIANT: this function never guesses or interpolates a price. Every resolved
result has a price_ref that a human can verify. Every unresolved result has a
human-readable flag message explaining what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from invoicing_rules.state import Agreement, LedgerItem, PriceBookRow

SOURCE_DIRECT = "direct"  # unit_price was already on the ledger row
SOURCE_AGREEMENT = "negotiated"  # resolved from a confirmed Agreement
SOURCE_PRICE_BOOK = "price_book"  # resolved from a non-range Price Book entry


@dataclass
class PriceResult:
    unit_price: float | None
    price_ref: str | None
    source: str | None  # SOURCE_DIRECT | SOURCE_AGREEMENT | SOURCE_PRICE_BOOK | None
    resolved: bool
    flag: str | None  # human-readable explanation when not resolved


def resolve_price(
    item: LedgerItem,
    price_book: dict[str, PriceBookRow],
    agreements: list[Agreement],
) -> PriceResult:
    """
    Resolve the unit_price for a single LedgerItem.

    Returns a PriceResult. Callers must check .resolved; never assume a price
    is valid without checking this flag.
    """

    # 1. Direct: unit_price already set and price_ref present → already resolved.
    #    (price_ref provides the audit trail so the price is traceable.)
    if item.unit_price is not None and item.price_ref:
        return PriceResult(
            unit_price=item.unit_price,
            price_ref=item.price_ref,
            source=SOURCE_DIRECT,
            resolved=True,
            flag=None,
        )

    # No price_ref → cannot resolve.
    if not item.price_ref:
        return PriceResult(
            unit_price=None,
            price_ref=None,
            source=None,
            resolved=False,
            flag=f"item {item.item_id!r}: no price_ref set — cannot resolve price",
        )

    # 2. Negotiated: look up in confirmed Agreements.
    if item.price_source == "negotiated":
        agreement = _find_agreement(item.price_ref, agreements)
        if agreement is None:
            return PriceResult(
                unit_price=None,
                price_ref=item.price_ref,
                source=SOURCE_AGREEMENT,
                resolved=False,
                flag=(f"item {item.item_id!r}: agreement {item.price_ref!r} not found"),
            )
        if not agreement.is_confirmed:
            return PriceResult(
                unit_price=None,
                price_ref=item.price_ref,
                source=SOURCE_AGREEMENT,
                resolved=False,
                flag=(
                    f"item {item.item_id!r}: agreement {item.price_ref!r} is "
                    f"{agreement.confidence!r} (not confirmed) — needs user confirmation"
                ),
            )
        return PriceResult(
            unit_price=agreement.agreed_price,
            price_ref=item.price_ref,
            source=SOURCE_AGREEMENT,
            resolved=True,
            flag=None,
        )

    # 3. Price Book.
    if item.price_source == "price_book":
        pb_row = price_book.get(item.price_ref)
        if pb_row is None:
            return PriceResult(
                unit_price=None,
                price_ref=item.price_ref,
                source=SOURCE_PRICE_BOOK,
                resolved=False,
                flag=(
                    f"item {item.item_id!r}: price_book entry {item.price_ref!r} "
                    f"not found in price book"
                ),
            )
        if pb_row.is_range:
            # Range with no resolved unit_price on the item → unresolved.
            # Never auto-pick a number inside a range (spec invariant).
            return PriceResult(
                unit_price=None,
                price_ref=item.price_ref,
                source=SOURCE_PRICE_BOOK,
                resolved=False,
                flag=(
                    f"item {item.item_id!r}: {item.price_ref!r} is a range "
                    f"({pb_row.price_low}–{pb_row.price_high} {pb_row.currency}) "
                    f"— set unit_price on the ledger row to resolve"
                ),
            )
        return PriceResult(
            unit_price=pb_row.price_low,
            price_ref=item.price_ref,
            source=SOURCE_PRICE_BOOK,
            resolved=True,
            flag=None,
        )

    # 4. Unknown / missing price_source.
    return PriceResult(
        unit_price=None,
        price_ref=item.price_ref,
        source=None,
        resolved=False,
        flag=(
            f"item {item.item_id!r}: price_source {item.price_source!r} not "
            f"recognised — expected 'price_book' or 'negotiated'"
        ),
    )


def resolve_all(
    items: list[LedgerItem],
    price_book: dict[str, PriceBookRow],
    agreements: list[Agreement],
) -> dict[str, PriceResult]:
    """Resolve prices for all items. Returns dict keyed by item_id."""
    return {item.item_id: resolve_price(item, price_book, agreements) for item in items}


# ── internal ──────────────────────────────────────────────────────────────────


def _find_agreement(agreement_id: str, agreements: list[Agreement]) -> Agreement | None:
    for a in agreements:
        if a.agreement_id == agreement_id:
            return a
    return None
