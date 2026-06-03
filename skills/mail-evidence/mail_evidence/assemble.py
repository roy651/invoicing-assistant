"""
Thread assembly.

Groups EvidenceRecords (already carrying thread_id from fetch layer) into Thread
objects, sorted chronologically within each thread. Each Thread is initialised
with tier="T2" as a placeholder; classify_tier() assigns the real tier.
"""

from __future__ import annotations

from mail_evidence.records import EvidenceRecord, Thread


def assemble_threads(records: list[EvidenceRecord]) -> list[Thread]:
    """
    Group records by thread_id into Thread objects.

    Thread ID assignment (cross-folder References-chain) is handled upstream by
    the fetch layer. This function only groups and sorts.

    Returns a list of Threads sorted by the date of their earliest record.
    """
    thread_map: dict[str, list[EvidenceRecord]] = {}
    for rec in records:
        thread_map.setdefault(rec.thread_id, []).append(rec)

    threads = []
    for thread_id, recs in thread_map.items():
        recs_sorted = sorted(recs, key=lambda r: r.date)
        threads.append(Thread(thread_id=thread_id, records=recs_sorted, tier="T2"))

    threads.sort(key=lambda t: t.records[0].date)
    return threads
