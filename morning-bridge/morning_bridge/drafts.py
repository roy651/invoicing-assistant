"""
Draft creation — Task 1.4 stub.

create_draft will POST to /documents with type=305 and no payment array,
which morning persists as an Open (status=0) draft with no invoice number.
The double-bill guard, dry-run mode, and payload validation live here.

See docs/03 §Create-draft input contract and docs/01 §5 for the semantic mapping.
"""

from __future__ import annotations

from morning_bridge.client import MorningClient


def create_draft(client: MorningClient, request: dict) -> dict:
    """
    Create a draft invoice in morning (task 1.4 — not yet implemented).

    request follows the CreateDraftRequest shape from docs/03:
      bill_to_client_id, language, currency, vat_rate, lines[]

    When DRY_RUN=true the function returns the payload it WOULD send
    without creating anything.
    """
    raise NotImplementedError("create_draft is task 1.4 — not yet implemented")
