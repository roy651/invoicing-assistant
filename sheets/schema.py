# Authoritative column definitions for all Google Sheets tabs.
# Source of truth: docs/01-data-contracts.md and docs/02-reconciliation.md §A.
# setup.py reads this to create / validate the live sheet.

CLIENT_PROFILES = [
    "bill_to",
    "is_agency",
    "managed_by_agent",
    "language",
    "currency",
    "vat_rate",
    "morning_client_id",
    "notes",
]

PRICE_BOOK = [
    "price_id",
    "version",
    "effective_from",
    "effective_to",
    "category",
    "item",
    "price_low",
    "price_high",
    "currency",
    "is_range",
    "notes",
]

AGREEMENTS = [
    "agreement_id",
    "bill_to",
    "end_client",
    "item_desc",
    "agreed_price",
    "currency",
    "agreed_on",
    "source_ref",
    "confidence",
]

# Columns follow the spec grouping order (01-data-contracts.md §4).
LEDGER = [
    # Identity & classification
    "item_id",
    "bill_to",
    "end_client",
    "description",
    "assignee",
    "item_kind",
    "billing_mode",
    # Pricing
    "unit_price",
    "currency",
    "price_source",
    "price_ref",
    # Progress (fixed_quote only; unit_based leaves these blank)
    "total_qty",
    "qty_billed_to_date",
    "last_billed_month",
    # Agent provisional read (overwritten each cycle)
    "status_agent",
    "completion_evidence",
    "confidence",
    "qty_proposed",
    # Human gate + morning truth
    "status_confirmed",
    "decision",
    "qty_approved",
    "qty_billed_actual",
    "morning_doc_ref",
    "notes",
]

# Columns pinned from docs/02-reconciliation.md §A (cold-start declaration).
# These are merged into the Ledger on first run; the tab is never written again.
# currency is derived from ClientProfiles at merge time, so it is not stored here.
OPENING_BALANCES = [
    "item_id",
    "bill_to",
    "end_client",
    "description",
    "unit_price",
    "total_qty",
    "qty_billed_to_date",
]

# Scalar runtime configuration. epoch is the one-time cutoff: email before this
# date is considered already accounted for via opening balances (02-reconciliation §A).
CONFIG = ["key", "value", "notes"]

# Ordered mapping used by setup.py to create tabs in display order.
# PriceBook is intentionally absent: it is derived reference data read from
# data/price_book.csv at runtime, not a human-edited Sheet tab.
TABS: dict[str, list[str]] = {
    "ClientProfiles": CLIENT_PROFILES,
    "Agreements": AGREEMENTS,
    "Ledger": LEDGER,
    "OpeningBalances": OPENING_BALANCES,
    "Config": CONFIG,
}
