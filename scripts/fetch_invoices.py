#!/usr/bin/env python
"""
Download issued invoices from morning as JSON → fixtures/invoices/*.json.

READ-ONLY. Uses only the bridge's read surface (search_documents + get_document);
it never imports drafts and never issues, creates, or mutates a document. The output
JSON is the authoritative input for the Phase-2 harness: the SETTLE step loads it, and
it is the basis for the expected-ledger oracle and client_profiles.

It also pins the real read shape (income field names, documentDate type) that settle.py
was anchored against by guess — see docs/06 Phase-2 go/no-go.

Credentials (production is required for real invoices — these live in your morning
account, not the sandbox):
  - Keychain (preferred): --keychain morning-live  → keyring service holding
    account "api_key_id" and "api_secret".
  - Env / .env fallback: MORNING_API_KEY_ID + MORNING_API_SECRET.

Usage:
  uv run python scripts/fetch_invoices.py --production --from 2026-04-01 --to 2026-05-31
  uv run python scripts/fetch_invoices.py --production --from 2026-04-01 --to 2026-05-31 \
      --types 305 320 --keychain morning-live
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "morning-bridge"))

from morning_bridge.client import MorningClient, client_from_env  # noqa: E402
from morning_bridge.reads import get_document, search_documents  # noqa: E402

# morning document type codes (fiscal issued invoices).
_DEFAULT_TYPES = [305, 320]  # 305 Tax Invoice, 320 Tax Invoice/Receipt


def _build_client(args: argparse.Namespace) -> MorningClient:
    sandbox = not args.production
    if args.keychain:
        import keyring  # type: ignore[import-untyped]

        api_id = keyring.get_password(args.keychain, "api_key_id")
        api_secret = keyring.get_password(args.keychain, "api_secret")
        if not api_id or not api_secret:
            raise SystemExit(
                f"Keychain service {args.keychain!r} is missing api_key_id / api_secret."
            )
        return MorningClient(api_id, api_secret, sandbox=sandbox)
    return client_from_env(sandbox=sandbox)


def _search_ids(client: MorningClient, types, from_date, to_date):
    """Page through search_documents; yield (id, number) for each issued doc."""
    page = 1
    while True:
        resp = search_documents(
            client,
            doc_type=types,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=50,
        )
        items = resp.get("items") or resp.get("data") or []
        if not items:
            break
        for it in items:
            yield it.get("id"), it.get("number") or it.get("documentNumber")
        if len(items) < 50:
            break
        page += 1


def _safe_name(doc_id: str, number) -> str:
    base = str(number) if number else str(doc_id)
    return re.sub(r"[^0-9A-Za-z._-]", "_", base)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Download issued invoices as JSON (read-only)."
    )
    ap.add_argument(
        "--from", dest="from_date", required=True, help="ISO date YYYY-MM-DD"
    )
    ap.add_argument("--to", dest="to_date", required=True, help="ISO date YYYY-MM-DD")
    ap.add_argument("--types", type=int, nargs="+", default=_DEFAULT_TYPES)
    ap.add_argument("--out", default=str(_ROOT / "fixtures" / "invoices"))
    ap.add_argument(
        "--production", action="store_true", help="hit live morning (default: sandbox)"
    )
    ap.add_argument(
        "--keychain", metavar="SERVICE", help="keyring service for live creds"
    )
    args = ap.parse_args(argv)

    client = _build_client(args)
    env = "⚠ PRODUCTION (live)" if args.production else "sandbox"
    print(
        f"morning: {env}  |  types={args.types}  |  {args.from_date} → {args.to_date}"
    )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pairs = list(_search_ids(client, args.types, args.from_date, args.to_date))
    print(f"found {len(pairs)} document(s)")

    first_shape_printed = False
    written = 0
    for doc_id, number in pairs:
        if not doc_id:
            continue
        doc = get_document(client, doc_id)
        if not first_shape_printed:
            print(f"read shape — top-level keys: {sorted(doc.keys())}")
            first_shape_printed = True
        (out / f"{_safe_name(doc_id, number)}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written += 1

    print(f"wrote {written} JSON file(s) → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
