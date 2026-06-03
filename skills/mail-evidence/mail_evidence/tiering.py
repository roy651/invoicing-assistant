"""
Thread tiering — deterministic, headers only.

Tier assignment:
  T1 — any participant (from_/to/cc across all records) is in ContactStore.
       → full evidence, keep as-is.
  T3 — ONLY when EVERY record in the thread carries a positive bulk-mail signal
       (is_bulk=True). Allowlist absence NEVER causes T3.
       → dropped.
  T2 — everything else (unknown-but-human).
       → passed to RelevanceJudge.

Inversion invariant (§6.1): a stale allowlist costs extra T2 judgments, never a
dropped thread. Bulk-drop requires positive signals on every record; never on
allowlist absence alone.
"""

from __future__ import annotations

from mail_evidence.protocols import ContactStore
from mail_evidence.records import Thread


def classify_tier(thread: Thread, contact_store: ContactStore) -> Thread:
    """
    Assign tier to a Thread. Mutates thread.tier in place and returns the thread.

    T1 check: any email address across all records is in contact_store.
    T3 check: ALL records have is_bulk=True.
    T2: fallback.
    """
    # Collect all email addresses from all records in the thread.
    all_addresses: set[str] = set()
    for rec in thread.records:
        if rec.from_:
            all_addresses.add(rec.from_.lower())
        for addr in rec.to:
            all_addresses.add(addr.lower())
        for addr in rec.cc:
            all_addresses.add(addr.lower())

    # T1: any known contact.
    for addr in all_addresses:
        if contact_store.is_known(addr):
            thread.tier = "T1"
            return thread

    # T3: ONLY if every record has a bulk signal — never on allowlist absence.
    if thread.records and all(rec.is_bulk for rec in thread.records):
        thread.tier = "T3"
        return thread

    # T2: unknown-but-human.
    thread.tier = "T2"
    return thread
