"""
Invoicing rules skill helpers — task 1.7.

Deterministic Python helpers for the monthly forward pass:
  evidence  — unify email + transcripts into one ordered list
  state     — load ledger / client profiles / agreements / price book from CSV
  pricing   — resolve unit_price per item (never guesses; flags unresolved)
  packet    — build the grouped review packet for the human gate
"""

from invoicing_rules.evidence import unify
from invoicing_rules.packet import (
    BillToGroup,
    EndClientGroup,
    Flag,
    ProposedLine,
    ReviewPacket,
    build_review_packet,
)
from invoicing_rules.pricing import PriceResult, resolve_all, resolve_price
from invoicing_rules.state import (
    Agreement,
    ClientProfile,
    LedgerItem,
    PriceBookRow,
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
)

__all__ = [
    # evidence
    "unify",
    # state
    "ClientProfile",
    "Agreement",
    "LedgerItem",
    "PriceBookRow",
    "load_client_profiles",
    "load_agreements",
    "load_ledger",
    "load_price_book",
    # pricing
    "PriceResult",
    "resolve_price",
    "resolve_all",
    # packet
    "ReviewPacket",
    "BillToGroup",
    "EndClientGroup",
    "ProposedLine",
    "Flag",
    "build_review_packet",
]
