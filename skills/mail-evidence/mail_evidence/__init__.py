"""
mail-evidence — portable mail→evidence conditioning engine.

Public API:
  EvidenceRecord, Thread, AttachmentMeta, RelevanceDecision  — data model
  RelevanceJudge, ContactStore                               — injected-dep protocols
  FetchConfig                                                — fetch configuration
  fetch_messages                                             — IMAP fetch (multi-folder)
  assemble_threads                                           — group into Threads
  dedup_in_thread                                            — in-thread quote removal
  classify_tier                                              — T1/T2/T3 classification
  condition                                                  — promote or drop T2/T3
  run                                                        — full pipeline iterator
  load_watermark, commit_watermark                           — watermark persistence
"""

from mail_evidence.assemble import assemble_threads
from mail_evidence.config import FetchConfig
from mail_evidence.dedup import dedup_in_thread
from mail_evidence.fetch.imap import ImapClient, fetch_messages
from mail_evidence.fetch.offline import ingest_email_export
from mail_evidence.fetch.watermark import commit_watermark, load_watermark
from mail_evidence.pipeline import run
from mail_evidence.promote import condition
from mail_evidence.protocols import ContactStore, RelevanceJudge
from mail_evidence.records import (
    AttachmentMeta,
    EvidenceRecord,
    RelevanceDecision,
    Thread,
)
from mail_evidence.tiering import classify_tier

__all__ = [
    # records
    "EvidenceRecord",
    "Thread",
    "AttachmentMeta",
    "RelevanceDecision",
    # protocols
    "RelevanceJudge",
    "ContactStore",
    # config
    "FetchConfig",
    # fetch
    "ImapClient",
    "fetch_messages",
    "ingest_email_export",
    # pipeline stages
    "assemble_threads",
    "dedup_in_thread",
    "classify_tier",
    "condition",
    # pipeline
    "run",
    # watermark
    "load_watermark",
    "commit_watermark",
]
