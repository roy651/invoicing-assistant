"""
Injected-dependency protocols for mail-evidence.

RelevanceJudge and ContactStore are domain concerns — they live in the
consuming application (e.g., invoicing-assistant), not in this package.
This module defines only the structural contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mail_evidence.records import RelevanceDecision, Thread


class RelevanceJudge(Protocol):
    """Decides whether a T2 (unknown-but-human) thread is work-related."""

    def is_relevant(self, thread: "Thread") -> "RelevanceDecision": ...


class ContactStore(Protocol):
    """Persistence layer for known contacts (managed by the consumer)."""

    def is_known(self, email: str) -> bool: ...

    def role_of(self, email: str) -> str | None: ...

    def add_auto(self, email: str, reason: str) -> None:
        """Add a contact with role=other, source=auto."""
        ...
