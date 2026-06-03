"""
Top-level pipeline: run(config, judge, store) -> Iterator[list[Thread]].

Ties together fetch → assemble → dedup → classify_tier → condition.
Yields one batch of Thread objects at a time so the consumer can durably
process and call commit_watermark() between batches.

Large-delta guard (§6.5): the window + max_messages cap bounds each run.
The consumer processes a batch, persists, then commits the watermark. A crash
re-fetches at most one batch.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime

from mail_evidence.assemble import assemble_threads
from mail_evidence.config import FetchConfig
from mail_evidence.dedup import dedup_in_thread
from mail_evidence.fetch.imap import ImapClient, fetch_messages
from mail_evidence.promote import condition
from mail_evidence.protocols import ContactStore, RelevanceJudge
from mail_evidence.records import EvidenceRecord, Thread
from mail_evidence.tiering import classify_tier

_log = logging.getLogger(__name__)


def run(
    config: FetchConfig,
    judge: RelevanceJudge,
    contact_store: ContactStore,
    client: ImapClient,
    watermark: datetime | None = None,
) -> Iterator[list[Thread]]:
    """
    Run the full mail-evidence pipeline.

    Yields batches of Thread objects (config.batch_size per batch). The consumer
    must call commit_watermark() after processing each batch to advance the
    watermark durably.

    Batch iteration over config.batch_size threads — enables processing large
    inboxes without loading all threads into memory at once.
    """
    _log.info(
        "mail-evidence: run() starting (folders=%r, watermark=%s)",
        config.folders,
        watermark,
    )

    records: list[EvidenceRecord] = fetch_messages(
        client,
        folders=config.folders,
        watermark=watermark,
        max_messages=config.max_messages,
        window_days=config.window_days,
    )
    _log.info("mail-evidence: fetched %d records", len(records))

    threads: list[Thread] = assemble_threads(records)
    _log.info("mail-evidence: assembled %d threads", len(threads))

    # Dedup, tier, condition — then batch and yield.
    processed: list[Thread] = []
    for thread in threads:
        thread = dedup_in_thread(thread)
        thread = classify_tier(thread, contact_store)
        kept = condition(thread, judge, contact_store)
        if kept is not None:
            processed.append(kept)

    _log.info(
        "mail-evidence: %d threads after tiering/conditioning (from %d)",
        len(processed),
        len(threads),
    )

    # Yield in batches.
    batch_size = max(1, config.batch_size)
    for i in range(0, max(1, len(processed)), batch_size):
        batch = processed[i : i + batch_size]
        if batch:
            yield batch
