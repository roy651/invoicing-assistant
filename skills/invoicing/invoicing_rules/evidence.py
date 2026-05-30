"""
Evidence unification — task 1.7.

Merges email Messages (imap-fetch) and transcript EvidenceRecords into a single
ordered list of UnifiedEvidence records.

CRITICAL design note: from_, to, and cc are preserved as first-class fields on
UnifiedEvidence (not collapsed into participants). The subcontractor completion-evidence
rule in SKILL.md depends on knowing exactly who was CC'd on a message — a thread where
a subcontractor reports directly to the end client with Avigail CC'd is first-class
completion evidence for an assignee != self item. Collapsing to a flat participants
list would destroy this signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from imap_fetch.fetch import Address, Message
from transcript_reader.reader import EvidenceRecord

SOURCE_EMAIL = "email"
SOURCE_TRANSCRIPT = "transcript"


@dataclass
class UnifiedEvidence:
    """
    Single evidence record consumed by the invoicing rules skill.

    Source-agnostic for body_text / id / thread_id / date; source-specific
    fields are optional and populated only when relevant.

    Email-sourced records populate: from_, to, cc, subject.
    Transcript-sourced records populate: participants, filename.
    """

    id: str
    thread_id: str
    date: datetime
    source: str  # SOURCE_EMAIL | SOURCE_TRANSCRIPT
    body_text: str

    # email-only (None / empty for transcripts)
    from_: Address | None = None
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    subject: str | None = None

    # transcript-only (empty for emails)
    participants: list[str] = field(default_factory=list)
    filename: str | None = None


def unify(
    messages: list[Message],
    transcripts: list[EvidenceRecord],
) -> list[UnifiedEvidence]:
    """
    Merge email messages and transcript records into one list sorted by date.

    Does not deduplicate — caller is responsible for not passing the same
    source records twice (the watermark in imap-fetch + transcripts handles this).
    """
    unified: list[UnifiedEvidence] = []

    for msg in messages:
        unified.append(
            UnifiedEvidence(
                id=msg.id,
                thread_id=msg.thread_id,
                date=msg.date,
                source=SOURCE_EMAIL,
                body_text=msg.body_text,
                from_=msg.from_,
                to=list(msg.to),
                cc=list(msg.cc),
                subject=msg.subject,
            )
        )

    for rec in transcripts:
        unified.append(
            UnifiedEvidence(
                id=rec.id,
                thread_id=rec.thread_id,
                date=rec.date,
                source=SOURCE_TRANSCRIPT,
                body_text=rec.body_text,
                participants=list(rec.participants),
                filename=rec.filename,
            )
        )

    unified.sort(key=lambda e: e.date)
    return unified
