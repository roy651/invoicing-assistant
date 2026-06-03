"""
Data model for mail-evidence.

EvidenceRecord is the atomic evidence unit: one email or one transcript file.
Thread is the evidence unit for billing reasoning: one thread = one conversation.

The from_/to/cc fields are first-class on EvidenceRecord (never collapsed into
participants). The subcontractor-CC completion signal in the invoicing skill reads
cc directly — collapsing it into participants would destroy that signal.

Schema invariant: transcript records have from_=None, to=[], cc=[], subject=None;
email records have participants=[] (convenience union only), filename=None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class AttachmentMeta:
    filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class RelevanceDecision:
    relevant: bool
    reason: str
    promote_emails: list[str]

    def __hash__(self) -> int:
        return hash((self.relevant, self.reason, tuple(self.promote_emails)))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RelevanceDecision):
            return NotImplemented
        return (
            self.relevant == other.relevant
            and self.reason == other.reason
            and self.promote_emails == other.promote_emails
        )


@dataclass
class EvidenceRecord:
    """
    Atomic evidence unit: one email or one transcript file.

    id:              Stable identifier. For email: Message-ID header value, or
                     <folder>/<uid> fallback. For transcripts: "transcripts/<stem>".
    thread_id:       References-chain root id (email) or same as id (transcript).
    source:          "email" | "transcript"
    date:            UTC-aware datetime.
    body_text:       Full body text, never quote-stripped (dedup handles that).
    from_:           Sender address string. None for transcripts.
    to:              Recipient addresses. [] for transcripts.
    cc:              CC addresses (first-class — drives subcontractor-CC signal). [] for transcripts.
    subject:         Decoded subject. None for transcripts.
    participants:    Speakers (transcript) or convenience union of all addresses (email).
    filename:        Source filename. None for emails.
    attachments_meta: Attachment metadata. [] for transcripts.
    is_bulk:         True when bulk-mail signals were present in headers (fetch layer sets this).
    """

    id: str
    thread_id: str
    source: Literal["email", "transcript"]
    date: datetime
    body_text: str
    from_: str | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str | None = None
    participants: list[str] = field(default_factory=list)
    filename: str | None = None
    attachments_meta: list[AttachmentMeta] = field(default_factory=list)
    is_bulk: bool = False


@dataclass
class Thread:
    """
    Evidence unit for billing reasoning: one conversation thread.

    thread_id: The root EvidenceRecord.id for the thread.
    records:   Chronologically sorted EvidenceRecords in this thread.
    tier:      T1 (known contact), T2 (judge required), T3 (bulk/drop).
    relevance: Set only after a T2 thread is judged by RelevanceJudge.
    """

    thread_id: str
    records: list[EvidenceRecord]
    tier: Literal["T1", "T2", "T3"]
    relevance: RelevanceDecision | None = None
