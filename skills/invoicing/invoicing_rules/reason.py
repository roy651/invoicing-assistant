"""
The MODEL SEAM, productized — task: turnkey reasoning pass.

In production the monthly cycle's MATCH/INFER step is performed by a model that reads the
conditioned work-thread corpus + the open ledger and proposes, per (bill_to, end_client),
which work was done and how complete it is. This module turns that into a callable seam so
`run_month.py` can drive it without a human assembling the prompt by hand.

Two interchangeable backends, both implementing the phase2 `Reasoner` protocol
(`annotate(ledger, evidence)`), so they plug into the existing harness AND the runner:

  - ProxyReasoner  — a PLAIN LLM completion through the hermes claude-proxy (mirrors the
                     `wyckoff` skill: deterministic engine + one proxy call + tolerant parse).
                     This is reason-over-provided-text → structured-output; no agentic loop.
  - ManualReasoner — file-based supervised mode: writes the fully-assembled prompt to disk
                     and HALTS (raises ReasonPending). A human (or an in-session model)
                     drops the proposals JSON at the response path; a re-run parses it and
                     applies it. This is how the first supervised runs are done.

The reasoning NEVER touches the gate columns (status_confirmed / decision / qty_approved):
it only writes the agent columns on existing items and APPENDS new items it found, with
their gate columns empty. The agent identifies work; it never bills it (CLAUDE.md inv. 2).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from mail_evidence.records import EvidenceRecord
from mail_evidence.render import render_corpus

from invoicing_rules.state import (
    Agreement,
    ClientProfile,
    LedgerItem,
    PriceBookRow,
)

# ── the proposal contract (what the model returns) ────────────────────────────

# One proposal = one billable (or surfaced) work item. Recall-biased: the model surfaces
# anything plausibly billable; Avigail prunes at the conversion gate. Fields:
#   item_id     echo the OPEN-LEDGER id for a carry-forward item; OMIT for newly-found work
#   bill_to     the morning client (e.g. "SPRIG", "IVORY") — must match a client profile
#   end_client  the end client for an agency, else null (== bill_to for direct)
#   description bare item name (no payment-phase suffix; the packet adds that)
#   item_kind   "fixed_quote" (a quoted deliverable, billed in fractions) | "unit_based"
#   billing_mode "partial" | "defer" | "hourly" | null
#   status_agent "not_started" | "in_progress" | "complete"
#   qty_proposed fraction (fixed_quote) or count/hours (unit_based) to bill THIS month
#   confidence   "high" | "med" | "low"
#   completion_evidence  short citation: which WT(s) + what was delivered/approved
#   price_source "price_book" | "negotiated" | null   (new items only; carry-forward keeps its own)
#   price_ref    a price_book price_id or an agreement id            (new items only)

_SYSTEM = """You are the invoicing reasoner for a freelance design studio (ula / Avigail).
You read a month of work-thread email and decide which billable work was done and how
complete each item is. You produce DRAFT proposals only — a human reviews and prices every
line before anything is billed. You never issue or finalize anything.

Your output drives draft proformas, so RECALL IS THE GOAL: surface every item that was
plausibly worked on this month, even if you are unsure. A missed item is worse than an
extra one — the human prunes extras at review. Do not omit a suspicious-but-plausible item.

Rules:
- Group every item by (bill_to, end_client). bill_to is the morning client who pays
  (the agency, or the direct client). end_client is the agency's underlying client
  (null for direct clients). Use ONLY bill_to values that appear in the client profiles.
- For a CARRY-FORWARD item shown in the OPEN LEDGER, echo its item_id and report this
  month's progress: status_agent + qty_proposed (the fraction to bill THIS month, on top
  of qty_billed_to_date). Do NOT re-bill what is already billed.
- For NEW work found only in the email, omit item_id (a new one is assigned), and set
  item_kind / billing_mode and, when you can, price_source + price_ref from the price book
  or a logged agreement. If you cannot map a price, leave them null — the human prices it.
- NEVER invent a price or a quantity you don't have evidence for. When unsure, surface the
  item with your best-effort status and let the human set the number.
- HOURLY work: this month several projects were billed hourly. Hours almost never appear in
  email. For an hourly item set billing_mode="hourly", give your best-effort status, and
  leave price_ref null and qty_proposed null (or your rough guess) — the human sets
  hours × rate at conversion. Surface it; do not try to nail the amount.
- status_agent="complete" means the deliverable was finished/approved/sent this period.
  Use "in_progress" for partial progress, "not_started" for items only discussed.

Return ONLY a JSON array of proposal objects — no markdown, no prose, no explanation:
[
  {"item_id": "SPRIG-RHYTHMEDIX-pocket-folder", "bill_to": "SPRIG",
   "end_client": "RHYTHMEDIX", "description": "Pocket Folder", "item_kind": "fixed_quote",
   "billing_mode": "partial", "status_agent": "complete", "qty_proposed": 0.3,
   "confidence": "high", "completion_evidence": "WT13/40 final press file sent 05-25",
   "price_source": null, "price_ref": null}
]"""


# ── prompt assembly ───────────────────────────────────────────────────────────


def _render_profiles(profiles: dict[str, ClientProfile]) -> str:
    lines = ["CLIENT PROFILES (bill_to | agency? | currency | end-client billing):"]
    for bt, p in sorted(profiles.items()):
        kind = "agency" if p.is_agency else "direct"
        lines.append(f"  {bt} | {kind} | {p.currency} | managed={p.managed_by_agent}")
    return "\n".join(lines)


def _render_price_book(price_book: dict[str, PriceBookRow]) -> str:
    """Condensed price list so the model can set price_ref on new items. Full normalized
    book is large; the model only needs id → category/item → price to choose a ref."""
    lines = ["PRICE BOOK (price_ref | category > item | price | currency):"]
    for pid, row in sorted(price_book.items()):
        price = (
            f"{row.price_low:g}-{row.price_high:g}"
            if row.is_range
            else f"{row.price_low:g}"
        )
        lines.append(
            f"  {pid} | {row.category} > {row.item} | {price} | {row.currency}"
        )
    return "\n".join(lines)


def _render_agreements(agreements: list[Agreement]) -> str:
    if not agreements:
        return "AGREEMENTS: (none logged)"
    lines = [
        "AGREEMENTS (agreement_id | bill_to/end_client | item | price | confidence):"
    ]
    for a in agreements:
        lines.append(
            f"  {a.agreement_id} | {a.bill_to}/{a.end_client or '-'} | {a.item_desc} | "
            f"{a.agreed_price:g} {a.currency} | {a.confidence}"
        )
    return "\n".join(lines)


def _render_open_ledger(ledger: list[LedgerItem]) -> str:
    if not ledger:
        return "OPEN LEDGER: (empty — cold start, no carry-forward items)"
    lines = [
        "OPEN LEDGER (carry-forward items — echo item_id to report this month's progress):",
        "  item_id | bill_to/end_client | description | unit_price | billed_to_date/total | notes",
    ]
    for it in ledger:
        price = f"{it.unit_price:g} {it.currency}" if it.unit_price is not None else "—"
        progress = f"{it.qty_billed_to_date or 0:g}/{it.total_qty or 1:g}"
        lines.append(
            f"  {it.item_id} | {it.bill_to}/{it.end_client or '-'} | {it.description} | "
            f"{price} | {progress} | {it.notes or ''}"
        )
    return "\n".join(lines)


def build_prompt(
    corpus: str,
    ledger: list[LedgerItem],
    profiles: dict[str, ClientProfile],
    price_book: dict[str, PriceBookRow],
    agreements: list[Agreement],
    month: str,
) -> tuple[str, str]:
    """Return (system, user) for the reasoning call. `corpus` is the rendered work-thread
    text; `ledger` is the OPEN (carry-forward) ledger the model annotates."""
    user = "\n\n".join(
        [
            f"BILLING MONTH: {month}",
            _render_profiles(profiles),
            _render_open_ledger(ledger),
            _render_agreements(agreements),
            _render_price_book(price_book),
            "WORK THREADS (the month's conditioned email — your evidence):",
            corpus,
            "Now return the JSON array of proposals for this month.",
        ]
    )
    return _SYSTEM, user


# ── tolerant parse (same lesson as wyckoff: models answer in prose) ───────────


def parse_proposals(text: str) -> list[dict]:
    """Parse the model's reply into a list of proposal dicts, tolerantly:
    whole-text JSON → {"proposals": [...]} → fenced ```json block → first embedded
    [...] array. Raises ValueError if nothing parses (a silent [] would drop a month)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
        t = t.strip()

    candidates = [t]
    # an embedded array anywhere in the reply
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        candidates.append(m.group(0))

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("proposals"), list):
            return obj["proposals"]
        if isinstance(obj, list):
            return obj

    raise ValueError(
        "could not parse any proposals from the reasoning reply (expected a JSON array)"
    )


# ── merge proposals onto the ledger (the actual reasoning effect) ─────────────

_NONALNUM = re.compile(r"[^a-z0-9]+")
_AGENT_COLS = ("status_agent", "completion_evidence", "confidence", "qty_proposed")


def _norm(desc: str | None) -> str:
    return _NONALNUM.sub(" ", (desc or "").lower()).strip()


def _slug(s: str) -> str:
    return _NONALNUM.sub("-", (s or "").lower()).strip("-")


def _as_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def apply_proposals(
    ledger: list[LedgerItem],
    proposals: list[dict],
    profiles: dict[str, ClientProfile],
) -> tuple[int, int]:
    """Apply the model's proposals to the ledger IN PLACE. Returns (annotated, appended).

    - A proposal matching an existing open item (by echoed item_id, else by
      (bill_to, normalized description)) writes ONLY the agent columns onto it — its
      identity / pricing / progress carry-forward state is preserved.
    - A proposal with no match is APPENDED as a new item: identity + classification +
      (optional) pricing from the proposal, agent columns set, gate + morning-truth
      columns left empty. The agent never gates or settles what it identifies.
    """
    by_id = {it.item_id: it for it in ledger}
    by_key = {(it.bill_to, _norm(it.description)): it for it in ledger}
    existing_ids = set(by_id)

    annotated = appended = 0
    for p in proposals:
        item_id = (p.get("item_id") or "").strip() or None
        bill_to = (p.get("bill_to") or "").strip()
        end_client = (p.get("end_client") or "").strip() or None
        description = (p.get("description") or "").strip()

        target = None
        if item_id and item_id in by_id:
            target = by_id[item_id]
        elif (bill_to, _norm(description)) in by_key:
            target = by_key[(bill_to, _norm(description))]

        if target is not None:
            target.status_agent = (p.get("status_agent") or "").strip() or None
            target.completion_evidence = (
                p.get("completion_evidence") or ""
            ).strip() or None
            target.confidence = (p.get("confidence") or "").strip() or None
            target.qty_proposed = _as_float(p.get("qty_proposed"))
            annotated += 1
            continue

        # New work — assign an id and append, gate/truth columns empty.
        new_id = (
            item_id or f"{bill_to}-{(end_client or '').upper()}-{_slug(description)}"
        )
        base, n = new_id, 1
        while new_id in existing_ids:
            n += 1
            new_id = f"{base}-{n}"
        existing_ids.add(new_id)

        currency = profiles[bill_to].currency if bill_to in profiles else "USD"
        item = LedgerItem(
            item_id=new_id,
            bill_to=bill_to,
            end_client=end_client,
            description=description,
            assignee="self",
            item_kind=(p.get("item_kind") or "fixed_quote").strip(),
            billing_mode=(p.get("billing_mode") or "").strip() or None,
            unit_price=None,
            currency=currency,
            price_source=(p.get("price_source") or "").strip() or None,
            price_ref=(p.get("price_ref") or "").strip() or None,
            total_qty=None,
            qty_billed_to_date=None,
            last_billed_month=None,
            status_agent=(p.get("status_agent") or "").strip() or None,
            completion_evidence=(p.get("completion_evidence") or "").strip() or None,
            confidence=(p.get("confidence") or "").strip() or None,
            qty_proposed=_as_float(p.get("qty_proposed")),
            status_confirmed=None,
            decision=None,
            qty_approved=None,
            qty_billed_actual=None,
            morning_doc_ref=None,
            proforma_doc_ref=None,
            notes=None,
        )
        ledger.append(item)
        by_id[new_id] = item
        by_key[(bill_to, _norm(description))] = item
        appended += 1

    return annotated, appended


# ── backends (both implement the phase2 Reasoner protocol) ────────────────────


class ReasonPending(Exception):
    """Raised by ManualReasoner when the prompt has been written but no response exists
    yet. Not an error — the orchestrator catches it, tells the operator where the prompt
    is, and stops cleanly so the operator can supply the proposals and re-run."""

    def __init__(self, prompt_path: Path, response_path: Path) -> None:
        self.prompt_path = prompt_path
        self.response_path = response_path
        super().__init__(
            f"reasoning prompt written to {prompt_path}; provide proposals at "
            f"{response_path} and re-run"
        )


@dataclass
class _BaseReasoner:
    profiles: dict[str, ClientProfile]
    price_book: dict[str, PriceBookRow]
    agreements: list[Agreement]
    month: str
    body_chars: int = 500

    def _build(
        self, ledger: list[LedgerItem], evidence: list[EvidenceRecord]
    ) -> tuple[str, str]:
        corpus = render_corpus(evidence, body_chars=self.body_chars)
        return build_prompt(
            corpus, ledger, self.profiles, self.price_book, self.agreements, self.month
        )


@dataclass
class ManualReasoner(_BaseReasoner):
    """Supervised, file-based seam. First call writes the prompt and raises ReasonPending.
    Once `response_path` exists, the call parses it and applies the proposals."""

    prompt_path: Path = Path("_reason_prompt.txt")
    response_path: Path = Path("_reason_response.json")

    def annotate(
        self, ledger: list[LedgerItem], evidence: list[EvidenceRecord]
    ) -> None:
        system, user = self._build(ledger, evidence)
        self.prompt_path.write_text(
            f"<<<SYSTEM>>>\n{system}\n\n<<<USER>>>\n{user}\n", encoding="utf-8"
        )
        if not self.response_path.exists():
            raise ReasonPending(self.prompt_path, self.response_path)
        proposals = parse_proposals(self.response_path.read_text(encoding="utf-8-sig"))
        apply_proposals(ledger, proposals, self.profiles)


@dataclass
class ProxyReasoner(_BaseReasoner):
    """Automated seam: one plain LLM completion through the hermes claude-proxy. Mirrors
    wyckoff/scripts/analysis._call_llm — POST, retry, record any non-claude fallback."""

    api_url: str = "http://localhost:8765/v1/chat/completions"
    model: str = "claude-opus-4-6"
    api_key: str = "local"
    timeout: int = 310
    fallback_backends: tuple = ()

    def annotate(
        self, ledger: list[LedgerItem], evidence: list[EvidenceRecord]
    ) -> None:
        system, user = self._build(ledger, evidence)
        text = self._call(system, user)
        proposals = parse_proposals(text)
        apply_proposals(ledger, proposals, self.profiles)

    def _call(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 4096,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    self.api_url, headers=headers, json=payload, timeout=self.timeout
                )
                resp.raise_for_status()
                backend = resp.headers.get("X-Proxy-Backend", "")
                if backend and not backend.lower().startswith("claude"):
                    # surface a degraded run rather than ship a fallback as if it were Claude
                    self.fallback_backends = (*self.fallback_backends, backend)
                text = resp.json()["choices"][0]["message"]["content"].strip()
                if not text:
                    raise ValueError("empty LLM response")
                return text
            except Exception as e:  # noqa: BLE001 — retry any transient proxy failure
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise last_err

    @classmethod
    def from_env(cls, **kwargs) -> ProxyReasoner:
        return cls(
            api_url=os.environ.get("LLM_API_URL", cls.api_url),
            model=os.environ.get("REASON_LLM_MODEL", cls.model),
            api_key=os.environ.get("LLM_API_KEY", cls.api_key),
            **kwargs,
        )
