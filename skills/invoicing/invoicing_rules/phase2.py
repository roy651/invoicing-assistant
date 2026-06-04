"""
Phase-2 OFFLINE validation harness + scorer — task 1.11 Deliverable B.

This is the go/no-go runnable: it drives the deterministic parts of the monthly
cycle over an exported fixture corpus and scores the result against the oracle
`expected-ledger.csv`. It is NOT push-button — the reasoning pass (MATCH/INFER) is
performed by the MODEL in Cowork, not by Python (see "THE MODEL SEAM" below). The
harness scaffolds everything around that one step and scores the outcome.

Pipeline:
  1. INGEST   fixtures/emails (offline .eml/.mbox adapter) conditioned exactly as
              production (dedup → tier → drop bulk/irrelevant) + transcripts → unify()
  2. LOAD     profiles / agreements / price_book / opening ledger from fixtures
  3. SETTLE   reconcile fixtures/invoices into the opening ledger (cycle is settle-first)
  4. REASON   ── THE MODEL SEAM ── a Reasoner fills status_agent/completion_evidence/
              confidence/qty_proposed. In production the model does this in Cowork; a
              fixture-backed ReplayReasoner stands in so the harness runs standalone.
              The harness asserts the reasoning step never touches gate columns.
  5. PRICE+PROPOSE  resolve_all (inside) + build_review_packet
  6. SCORE    project produced ledger + packet against expected-ledger on the §07
              metrics → pass/fail report.

Fixture layout (docs §9), all git-ignored; the committed synthetic set under
tests/fixtures/phase2/ mirrors it:
  <root>/emails/{INBOX,Sent}/*.eml   <root>/transcripts/*       (optional)
  <root>/invoices/*.json             <root>/client_profiles.csv
  <root>/agreements.csv              <root>/price_book.csv
  <root>/opening_ledger.csv          <root>/expected_ledger.csv
  <root>/agent_annotations.csv       (ReplayReasoner input — "what the model produced")
  <root>/open_proformas.json         (optional: ids of still-live proformas)

Run:  uv run python -m invoicing_rules.phase2 <fixtures_root> [YYYY-MM]
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mail_evidence import (
    assemble_threads,
    classify_tier,
    condition,
    dedup_in_thread,
    ingest_email_export,
)
from mail_evidence.records import EvidenceRecord, RelevanceDecision, Thread

from invoicing_rules.evidence import unify
from invoicing_rules.packet import ReviewPacket, build_review_packet
from invoicing_rules.settle import SettlementReport, settle_ledger
from invoicing_rules.state import (
    LedgerItem,
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
)

try:  # transcripts is a sibling skill; optional for the harness
    from transcript_reader.reader import read_folder as _read_transcripts
except ImportError:  # pragma: no cover
    _read_transcripts = None

_GATE_COLS = ("status_confirmed", "decision", "qty_approved")


# ── fixture discovery + loading ──────────────────────────────────────────────


@dataclass
class FixtureSet:
    root: Path
    emails: Path
    invoices: Path
    transcripts: Path | None
    client_profiles: Path
    agreements: Path | None
    price_book: Path
    opening_ledger: Path | None
    expected_ledger: Path
    annotations: Path
    open_proformas: Path | None


# Inputs a real run can't proceed without: the mailbox to read, who to bill,
# and how to price. Everything else may be absent on a cold start (no prior
# agreements, a blank opening ledger, no live proformas yet).
_REQUIRED_INPUTS = ("emails", "client_profiles.csv", "price_book.csv")


def discover_fixtures(root: str | Path) -> FixtureSet:
    root = Path(root)

    missing = [name for name in _REQUIRED_INPUTS if not (root / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"fixture root {root} is missing required input(s): {', '.join(missing)}"
        )

    def opt(p: Path) -> Path | None:
        return p if p.exists() else None

    return FixtureSet(
        root=root,
        emails=root / "emails",
        invoices=root / "invoices",
        transcripts=opt(root / "transcripts"),
        client_profiles=root / "client_profiles.csv",
        agreements=opt(root / "agreements.csv"),
        price_book=root / "price_book.csv",
        opening_ledger=opt(root / "opening_ledger.csv"),
        expected_ledger=root / "expected_ledger.csv",
        annotations=root / "agent_annotations.csv",
        open_proformas=opt(root / "open_proformas.json"),
    )


class _KeepAllJudge:
    """
    Harness default RelevanceJudge: keep every T2 (unknown-but-human) thread, so the
    reasoning seam — not a heuristic — decides what is work. This is the conservative
    direction for a go/no-go: it never drops a thread the oracle might expect to bill.
    Bulk (T3) threads are already dropped by tiering before this runs. Injectable —
    production swaps in the model-backed judge.
    """

    def is_relevant(self, thread: Thread) -> RelevanceDecision:
        return RelevanceDecision(
            relevant=True,
            reason="harness default: keep all human threads",
            promote_emails=[],
        )


class _FixtureContactStore:
    """
    Harness default ContactStore: empty allowlist. By the tiering inversion invariant
    (§6.1) an empty store only ever costs an extra T2 judgment — never a dropped
    thread — so no real client thread is lost; pure-bulk threads still drop as T3.
    `add_auto` records promotions in memory only; the harness persists nothing.
    """

    def __init__(self) -> None:
        self._known: dict[str, str] = {}

    def is_known(self, email: str) -> bool:
        return email.lower() in self._known

    def role_of(self, email: str) -> str | None:
        return self._known.get(email.lower())

    def add_auto(self, email: str, reason: str) -> None:
        self._known.setdefault(email.lower(), "other")


def ingest_evidence(
    fx: FixtureSet,
    *,
    judge: object | None = None,
    contact_store: object | None = None,
) -> list[EvidenceRecord]:
    """
    Deliverable A + transcripts → one unified, date-sorted evidence list.

    Mirrors the production pipeline (mail_evidence.run): after assembly, each thread is
    deduped → tiered → conditioned, so bulk/marketing (T3) and judged-irrelevant (T2)
    threads are dropped here instead of reaching the reasoning seam. Judge and contact
    store are injectable; the fixture defaults keep all human threads and drop only
    deterministic bulk.
    """
    judge = judge or _KeepAllJudge()
    contact_store = contact_store or _FixtureContactStore()

    records = ingest_email_export(fx.emails) if fx.emails.exists() else []
    conditioned: list[Thread] = []
    for thread in assemble_threads(records):
        thread = dedup_in_thread(thread)
        thread = classify_tier(thread, contact_store)
        kept = condition(thread, judge, contact_store)
        if kept is not None:
            conditioned.append(kept)

    transcripts: list[EvidenceRecord] = []
    if fx.transcripts and _read_transcripts is not None:
        transcripts = _read_transcripts(fx.transcripts)
    return unify(conditioned, transcripts)


def load_invoices(fx: FixtureSet) -> list[dict]:
    """Load issued morning docs from fixtures/invoices/*.json (doc, list, or {items:[]})."""
    docs: list[dict] = []
    if not fx.invoices.exists():
        return docs
    for path in sorted(fx.invoices.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "items" in data:
            docs.extend(data["items"])
        elif isinstance(data, dict):
            docs.append(data)
        elif isinstance(data, list):
            docs.extend(data)
    return docs


def load_open_proformas(fx: FixtureSet) -> set[str] | None:
    if not fx.open_proformas:
        return None
    return set(json.loads(fx.open_proformas.read_text(encoding="utf-8-sig")))


# ── THE MODEL SEAM ───────────────────────────────────────────────────────────


class Reasoner(Protocol):
    """
    The MATCH/INFER reasoning pass. In production the model performs this in Cowork,
    reading the unified evidence and the open ledger and mutating the ledger in place:

      - for an existing open item, write its agent columns (`status_agent`,
        `completion_evidence`, `confidence`, `qty_proposed`);
      - for NEW work found in the evidence but not yet on the ledger, APPEND a new
        LedgerItem (identity / classification / pricing + agent columns) — SKILL.md's
        INFER allows this, and item precision/recall is meant to measure exactly it.

    It MUST NOT touch the gate columns (`status_confirmed` / `decision` /
    `qty_approved`) on any item, new or existing — those are the human gate's. The
    harness enforces this around the call.
    """

    def annotate(
        self, ledger: list[LedgerItem], evidence: list[EvidenceRecord]
    ) -> None: ...


@dataclass
class ReplayReasoner:
    """
    Fixture-backed stand-in for the model: applies "what the model produced" from a
    ledger-shaped CSV (same columns as the ledger; loaded via load_ledger).

      - A row whose item_id already exists is an ANNOTATION: only the agent columns
        are copied onto the open item (identity/pricing/gate of the open row are left
        as they are).
      - A row with a NEW item_id is an agent-IDENTIFIED item: it is appended in full
        (identity/classification/pricing + agent columns), with its gate and
        morning-truth columns forced empty — the agent never bills.
    """

    annotations_path: Path

    def annotate(
        self, ledger: list[LedgerItem], evidence: list[EvidenceRecord]
    ) -> None:
        if not self.annotations_path.exists():
            return
        by_id = {it.item_id: it for it in ledger}
        for row in load_ledger(self.annotations_path):
            existing = by_id.get(row.item_id)
            if existing is not None:
                existing.status_agent = row.status_agent
                existing.completion_evidence = row.completion_evidence
                existing.confidence = row.confidence
                existing.qty_proposed = row.qty_proposed
            else:
                # New item found in the evidence — the agent never gates or settles it.
                row.status_confirmed = None
                row.decision = None
                row.qty_approved = None
                row.qty_billed_actual = None
                row.morning_doc_ref = None
                row.proforma_doc_ref = None
                ledger.append(row)


# ── scoring ──────────────────────────────────────────────────────────────────


@dataclass
class Metric:
    name: str
    passed: bool
    detail: str


@dataclass
class Phase2Report:
    metrics: list[Metric] = field(default_factory=list)
    settlement: SettlementReport | None = None

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.metrics)

    def render(self) -> str:
        lines = [f"Phase-2 validation: {'PASS' if self.passed else 'FAIL'}"]
        for m in self.metrics:
            lines.append(f"  [{'PASS' if m.passed else 'FAIL'}] {m.name}: {m.detail}")
        if self.settlement is not None:
            lines.append("  " + self.settlement.summary())
        return "\n".join(lines)


_NONALNUM = re.compile(r"[^a-z0-9]+")


def _norm(desc: str | None) -> str:
    """
    Semantic match key for a work item: lowercase, non-alphanumeric runs → single
    space, trimmed. Parentheticals are kept as words, so "Roll-up banners" and
    "Roll-up banners (trade show)" stay distinct.

    Matching produced↔expected on this (instead of `item_id`) is required because the
    model assigns its own ids to NEW items it finds in email — an exact-id match would
    miss every agent-identified item regardless of correctness. Grouping is deliberately
    NOT part of the key (it compares bill_to/end_client of matched items), so a
    misassigned client surfaces as a grouping failure, not a silent miss.
    """
    return _NONALNUM.sub(" ", (desc or "").lower()).strip()


def score(
    produced: list[LedgerItem],
    expected: list[LedgerItem],
    packet: ReviewPacket,
    *,
    no_auto_bill: bool,
    settlement: SettlementReport | None = None,
) -> Phase2Report:
    """
    Compute the §07 metrics as projections over the relevant columns, so extra or
    missing columns in a reduced real `expected-ledger.csv` never break scoring.
    Produced and expected rows are matched by normalized description (see `_norm`).
    """
    prod = {_norm(it.description): it for it in produced if _norm(it.description)}
    exp = {_norm(it.description): it for it in expected if _norm(it.description)}
    both = [k for k in prod if k in exp]
    metrics: list[Metric] = []

    # 1. Grouping 100% — bill_to / end_client of semantically-matched items.
    gmis = [
        k
        for k in both
        if (prod[k].bill_to, prod[k].end_client) != (exp[k].bill_to, exp[k].end_client)
    ]
    metrics.append(
        Metric(
            "grouping",
            not gmis,
            f"{len(both) - len(gmis)}/{len(both)} match"
            + (f"; mismatch={sorted(gmis)}" if gmis else ""),
        )
    )

    # 2. Price 100% on resolved — packet lines with a real (non-zero) unit_price. A
    #    0-priced line is the unresolved-price marker (see docs/08 §5a), not a resolved
    #    price, so it is excluded here while still counting as an identified item below.
    packet_lines = {
        _norm(ln.description): ln
        for bt in packet.groups
        for ec in bt.end_client_groups
        for ln in ec.lines
    }
    resolved = [k for k, ln in packet_lines.items() if ln.unit_price]
    pmis = [
        k
        for k in resolved
        if k in exp
        and (packet_lines[k].unit_price, packet_lines[k].price_ref)
        != (exp[k].unit_price, exp[k].price_ref)
    ]
    metrics.append(
        Metric(
            "price_on_resolved",
            not pmis,
            f"{len(resolved) - len(pmis)}/{len(resolved)} resolved match"
            + (f"; mismatch={sorted(pmis)}" if pmis else ""),
        )
    )

    # 3. Item precision / recall ≥ 0.90 — over agent-identified items (status_agent set).
    prod_items = {k for k, it in prod.items() if it.status_agent}
    exp_items = {k for k, it in exp.items() if it.status_agent}
    inter = prod_items & exp_items
    precision = len(inter) / len(prod_items) if prod_items else 1.0
    recall = len(inter) / len(exp_items) if exp_items else 1.0
    metrics.append(
        Metric(
            "item_precision_recall",
            precision >= 0.90 and recall >= 0.90,
            f"P={precision:.2f} R={recall:.2f} "
            f"(produced={len(prod_items)}, expected={len(exp_items)})",
        )
    )

    # 4. Zero false "complete" — agent never marks complete what the oracle says isn't.
    false_complete = [
        k
        for k in both
        if prod[k].status_agent == "complete" and exp[k].status_agent != "complete"
    ]
    metrics.append(
        Metric(
            "no_false_complete",
            not false_complete,
            "none"
            if not false_complete
            else f"{len(false_complete)}: {sorted(false_complete)}",
        )
    )

    # 5. Zero auto-bill — the reasoning step left the gate columns untouched.
    metrics.append(
        Metric(
            "no_auto_bill",
            no_auto_bill,
            "reasoning left gate columns untouched"
            if no_auto_bill
            else "reasoning step wrote a gate column (status_confirmed/decision/qty_approved)",
        )
    )

    return Phase2Report(metrics=metrics, settlement=settlement)


# ── orchestration ─────────────────────────────────────────────────────────────


def run_harness(
    fx: FixtureSet,
    reasoner: Reasoner,
    *,
    billing_month: str,
    judge: object | None = None,
    contact_store: object | None = None,
) -> Phase2Report:
    profiles = load_client_profiles(fx.client_profiles)
    price_book = load_price_book(fx.price_book)
    agreements = load_agreements(fx.agreements) if fx.agreements else []
    ledger = load_ledger(fx.opening_ledger) if fx.opening_ledger else []
    expected = load_ledger(fx.expected_ledger)

    evidence = ingest_evidence(fx, judge=judge, contact_store=contact_store)
    invoices = load_invoices(fx)
    live = load_open_proformas(fx)

    # SETTLE first (cycle order): reconcile issued invoices into the opening ledger.
    settlement = settle_ledger(ledger, invoices, profiles, live_proforma_ids=live)

    # ── THE MODEL SEAM ── reasoning pass; assert it never auto-bills.
    gate_before = _snapshot_gate(ledger)
    reasoner.annotate(ledger, evidence)
    no_auto_bill = not _gate_columns_violated(gate_before, ledger)

    packet = build_review_packet(
        ledger, profiles, price_book, agreements, billing_month
    )

    return score(
        ledger, expected, packet, no_auto_bill=no_auto_bill, settlement=settlement
    )


def _snapshot_gate(ledger: list[LedgerItem]) -> dict[str, tuple]:
    return {it.item_id: tuple(getattr(it, c) for c in _GATE_COLS) for it in ledger}


def _gate_columns_violated(before: dict[str, tuple], ledger: list[LedgerItem]) -> bool:
    """
    True if the reasoning step touched any gate column: changed an existing item's
    gate columns, or created a NEW item carrying any of them. New items with empty
    gate columns are fine (the agent identifies work, it does not bill it).
    """
    for it in ledger:
        gate = tuple(getattr(it, c) for c in _GATE_COLS)
        prior = before.get(it.item_id)
        if prior is None:
            if any(g is not None for g in gate):
                return True
        elif gate != prior:
            return True
    return False


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    root = argv[0]
    billing_month = argv[1] if len(argv) > 1 else "2026-03"
    fx = discover_fixtures(root)
    report = run_harness(
        fx, ReplayReasoner(fx.annotations), billing_month=billing_month
    )
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
