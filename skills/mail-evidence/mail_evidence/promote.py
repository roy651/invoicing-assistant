"""
T2 promotion — call RelevanceJudge, update ContactStore, keep or drop.
"""

from __future__ import annotations

import logging

from mail_evidence.protocols import ContactStore, RelevanceJudge
from mail_evidence.records import Thread

_log = logging.getLogger(__name__)


def condition(
    thread: Thread,
    judge: RelevanceJudge,
    contact_store: ContactStore,
) -> Thread | None:
    """
    Condition a single thread.

    T1 → return as-is.
    T2 → call judge; if relevant, promote contacts and return thread (with
         relevance set); if not relevant, return None (dropped).
    T3 → return None (dropped, logged).
    """
    if thread.tier == "T1":
        return thread

    if thread.tier == "T3":
        _log.debug(
            "mail-evidence: dropping T3 thread %r (bulk signals on all records)",
            thread.thread_id,
        )
        return None

    # T2 — call the injected judge.
    decision = judge.is_relevant(thread)
    thread.relevance = decision

    if decision.relevant:
        for email_addr in decision.promote_emails:
            contact_store.add_auto(
                email_addr,
                reason=f"auto-promoted from T2 thread {thread.thread_id!r}",
            )
            _log.info(
                "mail-evidence: promoted %r to contact store (thread %r)",
                email_addr,
                thread.thread_id,
            )
        return thread

    _log.debug(
        "mail-evidence: dropping T2 thread %r — judge: %s",
        thread.thread_id,
        decision.reason,
    )
    return None
