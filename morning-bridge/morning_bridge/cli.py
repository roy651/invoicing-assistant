"""
Minimal CLI for manual sandbox testing.

Usage (run from repo root):
  uv run python -m morning_bridge.cli get-account
  uv run python -m morning_bridge.cli get-business
  uv run python -m morning_bridge.cli search-clients --name "Acme"
  uv run python -m morning_bridge.cli search-items
  uv run python -m morning_bridge.cli search-documents --type 305 --status 0
  uv run python -m morning_bridge.cli get-document --id <id>

Reads MORNING_API_KEY_ID / MORNING_API_SECRET from .env (sandbox only).
"""

from __future__ import annotations

import argparse
import json
import sys

from morning_bridge.client import client_from_env
from morning_bridge import reads
from morning_bridge.drafts import create_draft


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m morning_bridge.cli",
        description="morning API sandbox tester",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("get-account", help="GET /account/me")
    sub.add_parser("get-business", help="GET /businesses/me")
    sub.add_parser("list-businesses", help="GET /businesses")

    p = sub.add_parser("search-clients", help="POST /clients/search")
    p.add_argument("--name", default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--active", action="store_true", default=None)

    p = sub.add_parser("get-client", help="GET /clients/{id}")
    p.add_argument("--id", required=True)

    p = sub.add_parser("search-items", help="POST /items/search")
    p.add_argument("--name", default=None)

    p = sub.add_parser("get-item", help="GET /items/{id}")
    p.add_argument("--id", required=True)

    p = sub.add_parser("search-documents", help="POST /documents/search")
    p.add_argument(
        "--type",
        type=int,
        nargs="+",
        dest="doc_type",
        metavar="CODE",
        help="document type code(s), e.g. 305",
    )
    p.add_argument(
        "--status",
        type=int,
        nargs="+",
        metavar="CODE",
        help="status code(s): 0=open/draft 1=closed/issued",
    )
    p.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--client-id", default=None)

    p = sub.add_parser("get-document", help="GET /documents/{id}")
    p.add_argument("--id", required=True)

    p = sub.add_parser("download-links", help="GET /documents/{id}/download/links")
    p.add_argument("--id", required=True)

    p = sub.add_parser(
        "create-draft",
        help="POST /documents — create a persisted draft (sandbox only)",
    )
    p.add_argument("--client-id", required=True, dest="bill_to_client_id")
    p.add_argument("--lang", required=True, choices=["en", "he"])
    p.add_argument("--currency", required=True, choices=["USD", "ILS"])
    p.add_argument("--vat", required=True, type=float, dest="vat_rate")
    p.add_argument(
        "--line",
        action="append",
        dest="lines",
        metavar="DESC:QTY:UNIT_PRICE",
        required=True,
        help="Repeat for each line. Example: --line 'Logo design:1:5000'",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payload without creating (sets DRY_RUN=true for this call)",
    )

    args = parser.parse_args(argv)

    try:
        client = client_from_env()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    with client:
        match args.cmd:
            case "get-account":
                _print(reads.get_account(client))
            case "get-business":
                _print(reads.get_business(client))
            case "list-businesses":
                _print(reads.list_businesses(client))
            case "search-clients":
                _print(
                    reads.search_clients(
                        client,
                        name=args.name,
                        email=args.email,
                        active=args.active if args.active else None,
                    )
                )
            case "get-client":
                _print(reads.get_client(client, args.id))
            case "search-items":
                _print(reads.search_items(client, name=args.name))
            case "get-item":
                _print(reads.get_item(client, args.id))
            case "search-documents":
                _print(
                    reads.search_documents(
                        client,
                        doc_type=args.doc_type,
                        status=args.status,
                        client_id=args.client_id,
                        from_date=args.from_date,
                        to_date=args.to_date,
                    )
                )
            case "get-document":
                _print(reads.get_document(client, args.id))
            case "download-links":
                _print(reads.get_document_download_links(client, args.id))
            case "create-draft":
                lines = []
                for raw in args.lines:
                    parts = raw.rsplit(":", 2)
                    if len(parts) != 3:
                        print(
                            f"Error: --line must be DESC:QTY:UNIT_PRICE, got {raw!r}",
                            file=sys.stderr,
                        )
                        return 1
                    desc, qty, unit_price = parts
                    lines.append(
                        {
                            "description": desc,
                            "quantity": float(qty),
                            "unit_price": float(unit_price),
                        }
                    )
                request = {
                    "bill_to_client_id": args.bill_to_client_id,
                    "language": args.lang,
                    "currency": args.currency,
                    "vat_rate": args.vat_rate,
                    "lines": lines,
                }
                if args.dry_run:
                    import os as _os

                    _os.environ["DRY_RUN"] = "true"
                _print(create_draft(client, request))

    return 0


if __name__ == "__main__":
    sys.exit(main())
