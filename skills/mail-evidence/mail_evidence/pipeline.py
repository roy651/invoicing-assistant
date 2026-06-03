"""
Top-level pipeline: run(config, judge, store) -> Iterator[list[Thread]].

Ties together fetch → assemble → dedup → classify_tier → condition, then yields
the surviving threads in config.batch_size-sized slices for ergonomic downstream
processing.

Scope / honest limits (§6.5 — large-delta guard):
  - Each run is BOUNDED, not streamed: fetch pulls up to max_messages (within the
    window) in one pass, and all threads are conditioned before the first slice is
    yielded. The batch_size slicing is a convenience for the consumer, not
    memory-bounded streaming. At a freelancer's mail volume (cap 500/run) this is
    fine; true incremental streaming is the 1.10 runner's job.
  - run() does NOT advance the watermark and does not surface a per-batch
    high-water timestamp. Cross-run resumption works via the IMAP SINCE search
    (so a re-run does not re-pull already-windowed mail); the caller persists the
    watermark by committing the max date of durably-processed records. The 1.10
    runner owns that real (batch, high_water) contract — see build-plan 1.10.
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
    Run the full mail-evidence pipeline for one bounded fetch.

    Fetches up to config.max_messages within the window, conditions every thread,
    then yields the survivors in config.batch_size-sized slices. The slicing is for
    downstream ergonomics — it is not memory-bounded streaming, and this function
    does not advance the watermark (see module docstring and build-plan 1.10).
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
