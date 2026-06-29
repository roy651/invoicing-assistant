#!/usr/bin/env python
"""
run_month.py — the turnkey monthly invoicing pass.

One command drives the whole cycle over a month's fetched mail:

  CONDITION  exported mbox(es) → conditioned work-thread corpus (`_work.txt`)
  OPENING    carry-forward ledger from prior issued invoices (project_ledger)
  SETTLE     reconcile any pending agent proformas vs issued invoices (only if any exist)
  REASON     ── THE MODEL SEAM ── a Reasoner reads the corpus + open ledger and proposes
             status_agent / qty_proposed / new items (reason.py: manual or proxy backend)
  PRICE      resolve_all inside build_proforma_requests (unresolved → 0 + marker)
  PREVIEW    print the exact draft proformas, grouped per (bill_to, end_client)
  CREATE     only with --create: POST type-300 draft proformas to morning

Safety (CLAUDE.md): drafts only; the human gate is morning's proforma→invoice conversion.
WRITES NOTHING unless --create is passed. Without --create, DRY_RUN is forced true and the
create step prints payloads only. --create flips DRY_RUN false and performs the real
type-300 write (the bridge is structurally incapable of issuing a fiscal document).

Usage:
  uv run python scripts/run_month.py --month 2026-06            # validate (dry-run preview)
  uv run python scripts/run_month.py --month 2026-06 --create   # the real type-300 write
  Reasoning backend: --reason-mode manual (default; file-based, supervised)
                     --reason-mode proxy  (plain LLM via hermes claude-proxy)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
for _p in (
    "skills/invoicing",
    "skills/mail-evidence",
    "skills/transcripts",
    "morning-bridge",
    "scripts",
):
    sys.path.insert(0, str(_ROOT / _p))

# Load credentials from .env (morning keys, IMAP) but DO NOT let .env decide DRY_RUN —
# the --create flag is the single authoritative write switch (set in main()).
load_dotenv(_ROOT / ".env")

import project_ledger  # noqa: E402  (scripts/project_ledger.py)
from invoicing_rules.handoff import build_proforma_requests, create_and_record  # noqa: E402
from invoicing_rules.phase2 import condition_corpus, is_billing_artifact  # noqa: E402,F401
from invoicing_rules.reason import (  # noqa: E402
    ManualReasoner,
    ProxyReasoner,
    ReasonPending,
)
from invoicing_rules.settle import settle_ledger  # noqa: E402
from invoicing_rules.state import (  # noqa: E402
    LedgerItem,
    load_agreements,
    load_client_profiles,
    load_ledger,
    load_price_book,
    write_ledger,
)
from mail_evidence.render import render_corpus  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def _prior_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    d = date(y, m, 1)
    p = date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)
    return f"{p.year:04d}-{p.month:02d}"


def _months_in(invoices_dir: Path) -> list[str]:
    import glob
    import json

    months: set[str] = set()
    for path in glob.glob(str(invoices_dir / "*.json")):
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        m = (doc.get("documentDate") or "")[:7]
        if m:
            months.add(m)
    return sorted(months)


def build_opening_ledger(
    invoices_dir: Path, profiles_path: Path, upto_month: str
) -> list[LedgerItem]:
    """Carry-forward opening ledger = every issued invoice line through `upto_month`,
    collapsed per item with cumulative qty_billed_to_date (project_ledger). Accumulates
    across all available prior months so multi-month partials carry the right
    billed-to-date, not just the last month's increment."""
    per_line: list[LedgerItem] = []
    for m in _months_in(invoices_dir):
        if m <= upto_month:
            per_line.extend(project_ledger.project(invoices_dir, profiles_path, m))
    return project_ledger.to_opening(per_line, upto_month)


def make_reasoner(mode: str, root: Path, common: dict):
    if mode == "proxy":
        return ProxyReasoner.from_env(**common)
    return ManualReasoner(
        prompt_path=root / "_reason_prompt.txt",
        response_path=root / "_reason_response.json",
        **common,
    )


def _print_preview(requests, results) -> None:
    print(f"\n=== {len(requests)} DRAFT proforma(s) ===")
    for req, res in zip(requests, results):
        live = "DRY-RUN" if res.get("dry_run") else f"created id={res.get('id')}"
        head = f"\n▸ {req.bill_to}" + (f" / {req.end_client}" if req.end_client else "")
        print(f"{head}  ({req.currency}, vat {req.vat_rate:g})  [{live}]")
        payload = res.get("payload", req.to_bridge_request())
        for ln in payload.get("lines", req.lines):
            print(
                f"    {ln.get('quantity'):>6} × {ln.get('unit_price'):>9g}  "
                f"{ln.get('description')}"
            )


# ── orchestration ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turnkey monthly invoicing pass")
    ap.add_argument("--month", required=True, help="Billing month YYYY-MM")
    ap.add_argument(
        "--root", default=None, help="Run dir (default fixtures/runs/<month>)"
    )
    ap.add_argument(
        "--invoices",
        default=str(_ROOT / "fixtures" / "invoices"),
        help="Issued-invoice JSON dir (prior months, for carry-forward + settle)",
    )
    ap.add_argument(
        "--profiles", default=str(_ROOT / "fixtures" / "client_profiles.csv")
    )
    ap.add_argument("--price-book", default=str(_ROOT / "fixtures" / "price_book.csv"))
    ap.add_argument("--agreements", default=str(_ROOT / "fixtures" / "agreements.csv"))
    ap.add_argument(
        "--opening",
        default=None,
        help="Pre-built opening ledger CSV (else derived from --invoices)",
    )
    ap.add_argument("--reason-mode", choices=["manual", "proxy"], default="manual")
    ap.add_argument(
        "--create",
        action="store_true",
        help="Perform the real type-300 write to morning (default: dry-run preview)",
    )
    args = ap.parse_args(argv)

    month = args.month
    root = Path(args.root) if args.root else _ROOT / "fixtures" / "runs" / month
    root.mkdir(parents=True, exist_ok=True)

    # The single authoritative write switch. Without --create nothing can reach morning.
    os.environ["DRY_RUN"] = "false" if args.create else "true"

    profiles = load_client_profiles(args.profiles)
    price_book = load_price_book(args.price_book)
    agreements = (
        load_agreements(args.agreements) if Path(args.agreements).exists() else []
    )

    # 1. CONDITION ─────────────────────────────────────────────────────────────
    emails_dir = root / "emails"
    transcripts_dir = root / "transcripts"
    evidence = condition_corpus(
        emails_dir, transcripts_dir if transcripts_dir.exists() else None
    )
    corpus = render_corpus(evidence)
    (root / "_work.txt").write_text(corpus, encoding="utf-8")
    n_threads = corpus.count("== WT ")
    print(
        f"CONDITION: {len(evidence)} records → {n_threads} work threads "
        f"(~{len(corpus) // 4} tokens) → {root / '_work.txt'}"
    )

    # 2. OPENING LEDGER ─────────────────────────────────────────────────────────
    if args.opening:
        ledger = load_ledger(args.opening)
        print(f"OPENING: {len(ledger)} carry-forward item(s) from {args.opening}")
    else:
        upto = _prior_month(month)
        ledger = build_opening_ledger(Path(args.invoices), Path(args.profiles), upto)
        opening_path = root / "opening_ledger.csv"
        write_ledger(ledger, opening_path)
        print(
            f"OPENING: {len(ledger)} carry-forward item(s) through {upto} "
            f"(derived from {args.invoices}) → {opening_path}"
        )

    # 3. SETTLE (only if there are pending agent proformas to reconcile) ─────────
    pending = [it for it in ledger if it.proforma_doc_ref]
    if pending:
        import glob
        import json as _json

        invoices = [
            _json.loads(Path(p).read_text(encoding="utf-8-sig"))
            for p in glob.glob(str(Path(args.invoices) / "*.json"))
        ]
        report = settle_ledger(ledger, invoices, profiles)
        print(f"SETTLE: {report.summary()}")
    else:
        print("SETTLE: skipped (no pending agent proformas to reconcile)")

    # 4. REASON ── THE MODEL SEAM ───────────────────────────────────────────────
    common = dict(
        profiles=profiles, price_book=price_book, agreements=agreements, month=month
    )
    reasoner = make_reasoner(args.reason_mode, root, common)
    try:
        reasoner.annotate(ledger, evidence)
    except ReasonPending as pending_exc:
        print(
            f"\nREASON ({args.reason_mode}): prompt written to {pending_exc.prompt_path}\n"
            f"  → produce the proposals JSON, save it to {pending_exc.response_path},\n"
            f"  → then re-run the same command to continue."
        )
        return 0

    annotated = [it for it in ledger if it.status_agent]
    print(
        f"REASON ({args.reason_mode}): {len(annotated)} item(s) proposed "
        f"({len(ledger)} on ledger)"
    )
    write_ledger(ledger, root / "agent_annotations.csv")

    # 5+6. PRICE + PROPOSE + PREVIEW/CREATE ─────────────────────────────────────
    requests = build_proforma_requests(ledger, profiles, price_book, agreements, month)
    if not requests:
        print(
            "\nNothing billable this month (no items with a billable agent proposal)."
        )
        write_ledger(ledger, root / "ledger.csv")
        return 0

    if args.create:
        from morning_bridge.client import client_from_env

        env = os.environ.get("MORNING_ENV", "sandbox")
        print("\n" + "=" * 64)
        print(
            f"  CREATE: writing {len(requests)} type-300 DRAFT proforma(s) to "
            f"morning [{env.upper()}]"
        )
        print("  These are non-fiscal drafts. Avigail reviews + converts in morning.")
        print("=" * 64)
        client = client_from_env()
        results = create_and_record(client, requests, ledger, root / "ledger.csv")
    else:
        results = create_and_record(None, requests, ledger, root / "ledger.csv")

    _print_preview(requests, results)
    print(f"\nLedger written → {root / 'ledger.csv'}")
    if not args.create:
        print(
            "Preview only — nothing was written to morning. Re-run with --create to write."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
