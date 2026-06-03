"""
In-thread deduplication.

Rule: remove a quoted block iff it is an in-thread repeat — i.e., its
normalised content is found in a sibling record's body. Preserve everything
else verbatim, including forwarded/external quoted history.

§6.4 invariant: original (non-quoted) content is never stripped. Dedup only
operates across thread-internal quote relationships; it never strips across
threads.

Algorithm per record in a thread:
  1. Find quoted segments: >-prefixed line runs, and attribution+quote blocks
     ("On <date>, X wrote:" followed by >-lines).
  2. For each quoted segment, normalise (strip > prefixes, collapse whitespace).
  3. Check if the normalised content is a substring of any sibling record's
     normalised body — but only for blocks of at least _MIN_QUOTE_TOKENS tokens.
     Shorter blocks are never stripped (a short generic confirmation can coincide
     with a sibling by chance; losing it from a forward would be evidence loss).
  4. If match → strip (in-thread repeat). If no match → keep (forwarded/external).
  5. Original (non-quoted) lines are always kept.

Note: the spec (§3.3) describes identity-first anchoring (resolve the attribution
to a sibling Message-ID / sender+date) with content as the fallback. This module
implements the content match plus the length floor, which achieves §6.4's
protective goal (never strip external-only evidence); full identity anchoring is a
possible refinement if real fixtures show coincidental long-block collisions.
"""

from __future__ import annotations

import re

from mail_evidence.records import EvidenceRecord, Thread

# Attribution pattern: "On Thu, 1 Jan 2026 at 10:00, Alice wrote:" (various date formats)
_ATTRIBUTION_RE = re.compile(
    r"^On\s+.{5,80},\s*.{2,80}\s+wrote:$",
    re.IGNORECASE,
)

# A quoted block must carry at least this many content tokens before a sibling-body
# match is trusted as a real in-thread repeat. Short generic blocks ("thanks,
# approved — invoice to follow") can appear in a sibling body by coincidence;
# stripping such a block out of a forwarded/external quote would be silent evidence
# loss — the exact case §6.4 protects. So we bias toward KEEPING anything shorter.
# This is a pragmatic floor in lieu of full identity-first anchoring (spec §3.3).
_MIN_QUOTE_TOKENS = 8


def dedup_in_thread(thread: Thread) -> Thread:
    """
    Remove in-thread reply quotes from all records in the thread.

    Returns a new Thread with deduped records. If no record changed, the same
    records list is reused (no allocation).
    """
    if len(thread.records) <= 1:
        return thread

    # Build normalised bodies for all records in the thread (used for matching).
    sibling_bodies = [_normalise(rec.body_text) for rec in thread.records]

    new_records: list[EvidenceRecord] = []
    changed = False

    for idx, rec in enumerate(thread.records):
        # Each record's sibling set = all other records' normalised bodies.
        other_bodies = [b for i, b in enumerate(sibling_bodies) if i != idx]
        cleaned = _strip_in_thread_quotes(rec.body_text, other_bodies)
        if cleaned != rec.body_text:
            changed = True
            new_records.append(_replace_body(rec, cleaned))
        else:
            new_records.append(rec)

    if not changed:
        return thread

    return Thread(
        thread_id=thread.thread_id,
        records=new_records,
        tier=thread.tier,
        relevance=thread.relevance,
    )


# ── internal ──────────────────────────────────────────────────────────────────


def _strip_in_thread_quotes(body: str, sibling_bodies: list[str]) -> str:
    """
    Strip quoted blocks that are in-thread repeats; keep everything else.

    Never touches non-quoted lines.
    """
    lines = body.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check for attribution line ("On ..., ... wrote:") followed by quotes.
        if _ATTRIBUTION_RE.match(line.rstrip()):
            j = i + 1
            # Collect the quoted block after the attribution.
            quote_lines = []
            while j < len(lines) and (
                lines[j].startswith(">")
                or (
                    not lines[j].strip()
                    and j + 1 < len(lines)
                    and lines[j + 1].startswith(">")
                )
            ):
                if lines[j].startswith(">"):
                    quote_lines.append(lines[j])
                j += 1

            if quote_lines and _is_in_thread_quote(quote_lines, sibling_bodies):
                # Skip attribution + quoted block.
                i = j
                continue
            # Not a sibling quote (external/forwarded) — keep as-is.
            result.append(line)
            i += 1
            continue

        # Check for a bare >-prefixed block (no attribution line preceding it).
        if line.startswith(">"):
            j = i
            quote_lines = []
            while j < len(lines) and lines[j].startswith(">"):
                quote_lines.append(lines[j])
                j += 1

            if _is_in_thread_quote(quote_lines, sibling_bodies):
                i = j
                continue
            # External/forwarded quote — keep.
            result.extend(lines[i:j])
            i = j
            continue

        result.append(line)
        i += 1

    # Strip trailing blank lines introduced by removed blocks.
    cleaned = "\n".join(result)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _is_in_thread_quote(quote_lines: list[str], sibling_bodies: list[str]) -> bool:
    """
    Return True only when the quoted block is long enough to safely attribute AND
    its content appears verbatim in a sibling record's body.

    The length floor (_MIN_QUOTE_TOKENS) keeps short generic confirmations that
    coincide with a sibling from being stripped out of forwarded/external context
    (§6.4): over-stripping is silent evidence loss, so short blocks are always kept.
    """
    normalised_quote = _normalise("\n".join(quote_lines))
    if len(normalised_quote.split()) < _MIN_QUOTE_TOKENS:
        return False
    for sibling in sibling_bodies:
        if normalised_quote in sibling:
            return True
    return False


def _normalise(text: str) -> str:
    """Strip > prefixes, collapse whitespace, lowercase — for substring matching."""
    words: list[str] = []
    for line in text.split("\n"):
        clean = re.sub(r"^[>\s]+", "", line).strip()
        if clean:
            words.extend(clean.lower().split())
    return " ".join(words)


def _replace_body(rec: EvidenceRecord, new_body: str) -> EvidenceRecord:
    """Return a copy of rec with body_text replaced."""
    return EvidenceRecord(
        id=rec.id,
        thread_id=rec.thread_id,
        source=rec.source,
        date=rec.date,
        body_text=new_body,
        from_=rec.from_,
        to=list(rec.to),
        cc=list(rec.cc),
        subject=rec.subject,
        participants=list(rec.participants),
        filename=rec.filename,
        attachments_meta=list(rec.attachments_meta),
        is_bulk=rec.is_bulk,
    )
