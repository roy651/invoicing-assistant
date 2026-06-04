"""
Mechanical invoice → expected_ledger projector (the validation oracle builder).

Reads issued Tax Invoices (morning JSON) and emits a ledger-shaped
`expected_ledger.csv` by FIXED RULES — no email is read, no judgment is applied.
This is the answer key for the Phase-2 go/no-go: because it is derived only from
the invoices (and the reasoning pass is derived only from the emails), a high
score is not circular. It replaces the hand-authored oracle.

Projection rules (deterministic):
  - Keep type-305 invoices in the target month, excluding status-4 (cancelled).
  - bill_to   ← client.id mapped through client_profiles.csv.
  - end_client← the most recent `-------- NAME --------` separator line (a single
                agency invoice stacks several end-clients, each under a header).
  - description← line text, canonicalised: parentheticals, payment-phase suffixes
                ("- 2nd payment", "(so far 0.7)") and trailing `*` stripped to the
                bare item name. Typos are kept verbatim (faithful to the invoice).
  - unit_price← line `price` (the per-unit price; amount = price × quantity).
  - qty_proposed ← line `quantity` (the increment billed on this invoice).
  - status_agent ← payment-phase markers: "last/final/completion/balance" → complete;
                an ordinal/"payment"/"so far"/"deposit" with no final marker →
                in_progress; no marker (one-shot full billing) → complete.

Fields the invoice cannot supply (price_ref provenance, cumulative qty) are left
empty; the scorer compares unit_price for resolved lines and treats an empty
oracle price_ref as a wildcard.

Run:
  uv run python scripts/project_ledger.py --month 2026-05 \
      [--invoices fixtures/morning_invoices_raw] [--profiles fixtures/client_profiles.csv] \
      [--out fixtures/expected_ledger.generated.csv]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (
    "skills/invoicing",
    "skills/mail-evidence",
    "skills/transcripts",
    "morning-bridge",
):
    sys.path.insert(0, str(_ROOT / _p))

from invoicing_rules.state import (  # noqa: E402
    LedgerItem,
    load_client_profiles,
    write_ledger,
)
from morning_bridge.reads import DOC_TYPE_TAX_INVOICE  # noqa: E402

_DOC_STATUS_CANCELLED = 4

# A separator header line, e.g. "-------- RHYTHMEDIX --------".
_SEPARATOR = re.compile(r"^-{2,}.*-{2,}$")
# Payment-phase vocabulary used for both desc-stripping and status inference.
_PAY = re.compile(
    r"\b(payment|deposit|advance|installment|balance|completion|so far)\b", re.I
)
_ORDINAL = re.compile(r"\b\d+\s*(st|nd|rd|th)\b", re.I)
_FINAL = re.compile(r"\b(last|final|completion|balance)\b", re.I)
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _end_client_from_header(desc: str) -> str:
    return re.sub(r"^-+\s*|\s*-+$", "", desc.strip()).strip()


def _canon_desc(desc: str) -> str:
    """Strip parentheticals + trailing payment-phase segments to the bare item name."""
    s = re.sub(r"\([^)]*\)", "", desc)
    parts = [p.strip() for p in s.split(" - ")]
    kept: list[str] = []
    for i, p in enumerate(parts):
        is_payment_tail = i > 0 and (
            _PAY.search(p) or _ORDINAL.search(p) or _FINAL.search(p)
        )
        if is_payment_tail:
            continue
        kept.append(p)
    s = " - ".join(kept) if kept else (parts[0] if parts else desc)
    s = s.replace("*", " ")
    return re.sub(r"\s+", " ", s).strip()


def _status_from(desc: str) -> str:
    s = desc.lower()
    if _FINAL.search(s):
        return "complete"
    if _PAY.search(s) or _ORDINAL.search(s):
        return "in_progress"
    return "complete"


def _slug(s: str) -> str:
    return _NONALNUM.sub("-", s.lower()).strip("-")


def _is_separator(desc: str) -> bool:
    return bool(_SEPARATOR.match(desc.strip()))


def project(invoices_dir: Path, profiles_path: Path, month: str) -> list[LedgerItem]:
    profiles = load_client_profiles(profiles_path)
    id_to_bill_to = {
        p.morning_client_id: bt for bt, p in profiles.items() if p.morning_client_id
    }

    items: list[LedgerItem] = []
    seen_ids: set[str] = set()
    for path in sorted(glob.glob(str(invoices_dir / "*.json"))):
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if doc.get("type") != DOC_TYPE_TAX_INVOICE:
            continue
        if doc.get("status") == _DOC_STATUS_CANCELLED:
            continue
        if (doc.get("documentDate") or "")[:7] != month:
            continue

        client_id = (doc.get("client") or {}).get("id", "")
        bill_to = id_to_bill_to.get(client_id, client_id or "UNKNOWN")
        currency = doc.get("currency") or "USD"
        end_client = doc.get("description") or None

        for line in doc.get("income", []):
            raw_desc = str(line.get("description", "")).strip()
            if not raw_desc:
                continue
            if _is_separator(raw_desc):
                end_client = _end_client_from_header(raw_desc)
                continue
            price = line.get("price") or 0
            amount = line.get("amount") or 0
            if not price and not amount:
                continue  # zero-value, non-separator → skip (no billable signal)
            if price < 0 or amount < 0:
                continue  # reversal/credit correction — not new billable work

            desc = _canon_desc(raw_desc)
            item_id = f"{bill_to}-{(end_client or '').upper()}-{_slug(desc)}"
            base_id = item_id
            n = 1
            while item_id in seen_ids:
                n += 1
                item_id = f"{base_id}-{n}"
            seen_ids.add(item_id)

            status = _status_from(raw_desc)
            items.append(
                LedgerItem(
                    item_id=item_id,
                    bill_to=bill_to,
                    end_client=end_client,
                    description=desc,
                    assignee="self",
                    item_kind="fixed_quote",
                    billing_mode="partial" if status == "in_progress" else None,
                    unit_price=float(price) if price else None,
                    currency=currency,
                    price_source=None,
                    price_ref=None,
                    total_qty=None,
                    qty_billed_to_date=None,
                    last_billed_month=month,
                    status_agent=status,
                    completion_evidence=f"invoice line: {raw_desc}",
                    confidence="high",
                    qty_proposed=float(line.get("quantity") or 0) or None,
                    status_confirmed=None,
                    decision=None,
                    qty_approved=None,
                    qty_billed_actual=None,
                    morning_doc_ref=str(doc.get("id") or "") or None,
                    proforma_doc_ref=None,
                    notes=None,
                )
            )
    return items


def to_opening(items: list[LedgerItem], month: str) -> list[LedgerItem]:
    """Collapse projected billed-lines into an OPENING-ledger shape for the next month:
    group by (bill_to, end_client, normalised description), sum the billed quantity into
    qty_billed_to_date, and CLEAR the agent columns (status_agent/qty_proposed) so the
    next month's reasoning re-evaluates each carry-forward item rather than inheriting a
    verdict. Identity + cumulative-billed is what carries forward; the judgement does not."""
    groups: dict[tuple, LedgerItem] = {}
    qty: dict[tuple, float] = {}
    for it in items:
        key = (it.bill_to, (it.end_client or "").lower(), _slug(it.description))
        qty[key] = qty.get(key, 0.0) + (it.qty_proposed or 0.0)
        if key not in groups:
            groups[key] = it
    opening: list[LedgerItem] = []
    for key, it in groups.items():
        it.qty_billed_to_date = round(qty[key], 4) or None
        it.last_billed_month = month
        it.status_agent = None
        it.qty_proposed = None
        it.completion_evidence = None
        it.confidence = None
        opening.append(it)
    return opening


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Project issued invoices → expected_ledger.csv"
    )
    ap.add_argument("--month", required=True, help="Target month YYYY-MM")
    ap.add_argument(
        "--invoices", default=str(_ROOT / "fixtures" / "morning_invoices_raw")
    )
    ap.add_argument(
        "--profiles", default=str(_ROOT / "fixtures" / "client_profiles.csv")
    )
    ap.add_argument(
        "--out", default=str(_ROOT / "fixtures" / "expected_ledger.generated.csv")
    )
    ap.add_argument(
        "--as-opening",
        action="store_true",
        help="Emit an OPENING ledger (collapsed carry-forward) instead of a per-line oracle.",
    )
    args = ap.parse_args(argv)

    items = project(Path(args.invoices), Path(args.profiles), args.month)
    if args.as_opening:
        items = to_opening(items, args.month)
    write_ledger(items, Path(args.out))
    kind = "opening item" if args.as_opening else "oracle line"
    print(f"Projected {len(items)} {kind}(s) for {args.month} → {args.out}")
    for it in items:
        print(
            f"  {it.bill_to}/{it.end_client}: {it.description!r} @ {it.unit_price} [{it.status_agent}]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
