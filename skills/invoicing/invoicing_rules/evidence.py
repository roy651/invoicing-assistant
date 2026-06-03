"""
Evidence unification — task 1.7, rewired in 1.6.6.

Flattens conditioned mail-evidence Threads and transcript EvidenceRecords into a
single chronologically ordered evidence list for the monthly reasoning pass.

Email and transcript records are both `mail_evidence.EvidenceRecord` now (the
package owns the schema), so unification is a merge + chronological sort — there
is no second evidence type to convert into.

CRITICAL: email records keep first-class `from_`/`to`/`cc` (flat lowercased
address strings), never collapsed into `participants`. The subcontractor
completion-evidence rule in SKILL.md reads `cc` directly — a thread where a
subcontractor reports to the end client with the user CC'd is first-class
completion evidence for an `assignee != self` item.
"""

from __future__ import annotations

from mail_evidence.records import EvidenceRecord, Thread


def unify(
    threads: list[Thread],
    transcripts: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    """
    Merge conditioned mail threads + transcript records into one date-sorted list.

    threads:     mail-evidence output — already fetched, deduped, tiered, and
                 conditioned. Their records are the email evidence (source="email").
    transcripts: transcript_reader output (source="transcript").

    Returns every record sorted by date ascending. Each record keeps its
    `thread_id` and `source`, so the reasoning step can still group by thread.

    Does not deduplicate across sources — the per-source watermarks (mail-evidence
    state/ and the transcripts `since` filter) prevent re-ingesting the same record.
    """
    records: list[EvidenceRecord] = []
    for thread in threads:
        records.extend(thread.records)
    records.extend(transcripts)
    records.sort(key=lambda r: r.date)
    return records
