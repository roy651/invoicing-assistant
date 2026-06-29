"""
Corpus renderer — turn conditioned evidence into the work-thread text a reasoner reads.

The reasoning seam (a human in a session, or a plain LLM call) reads ONE block of text:
the surviving work threads after conditioning. This module produces that text — the
`_work.txt` shape that was assembled by hand for the May/March runs — so the turnkey
flow can build it deterministically.

It is intentionally free of any invoicing dependency (it renders only EvidenceRecords),
so the daily-summary spin-off can reuse it. Billing-artifact dropping and unification
with transcripts happen upstream; this just formats whatever records it is given.

Format (one block per thread, threads ordered by their earliest message):

    == WT 1/106
    [04-30] molly@sprigconsulting.co>avigail@ula.co.il | FW: Headshot
      Here is Adam's headshot! From: Adam Tschida ...
    [05-04] avigail@ula.co.il>molly@sprigconsulting.co | Re: Headshot
      Sounds good ...
"""

from __future__ import annotations

from mail_evidence.records import EvidenceRecord

_ADDR_WIDTH = (
    24  # truncate each address like the May corpus ("molly@sprigconsulting.co")
)
_BODY_CHARS = 500  # per-message body excerpt; bounds the corpus to ~one prompt


def _addr(value: str | None) -> str:
    """One address, truncated. Email uses from_/to[0]; transcripts have neither."""
    if not value:
        return "?"
    return value.strip()[:_ADDR_WIDTH]


def _excerpt(body: str, limit: int) -> str:
    """Whitespace-collapsed body excerpt, truncated to `limit` chars."""
    collapsed = " ".join((body or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " …"


def _header(rec: EvidenceRecord) -> str:
    date = rec.date.strftime("%m-%d") if rec.date else "??-??"
    if rec.source == "transcript":
        who = rec.filename or _addr(None)
        subject = "(transcript)"
    else:
        to = rec.to[0] if rec.to else None
        who = f"{_addr(rec.from_)}>{_addr(to)}"
        subject = rec.subject or "(no subject)"
    return f"[{date}] {who} | {subject}"


def _group_by_thread(records: list[EvidenceRecord]) -> list[list[EvidenceRecord]]:
    """Group records into threads (preserving each thread's first-seen order), then
    order threads by their earliest message so the corpus reads chronologically while
    keeping each conversation contiguous."""
    threads: dict[str, list[EvidenceRecord]] = {}
    for rec in records:
        threads.setdefault(rec.thread_id, []).append(rec)
    for recs in threads.values():
        recs.sort(key=lambda r: r.date)
    return sorted(threads.values(), key=lambda recs: recs[0].date)


def render_corpus(
    records: list[EvidenceRecord], *, body_chars: int = _BODY_CHARS
) -> str:
    """Render conditioned evidence as the work-thread corpus text.

    `records` is the already-conditioned, billing-artifact-free evidence list (e.g. the
    output of the invoicing pipeline's ingest step). Records are grouped by thread;
    threads are numbered and ordered by earliest message.
    """
    threads = _group_by_thread(records)
    total = len(threads)
    blocks: list[str] = []
    for i, recs in enumerate(threads, start=1):
        lines = [f"== WT {i}/{total}"]
        for rec in recs:
            lines.append(_header(rec))
            excerpt = _excerpt(rec.body_text, body_chars)
            if excerpt:
                lines.append(f"  {excerpt}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
