#!/usr/bin/env python3
"""
Bootstrap the invoicing-assistant Google Sheet.

Creates the six tabs (ClientProfiles, PriceBook, Agreements, Ledger,
OpeningBalances, Config) with exact column headers and placeholder seed rows.
Idempotent: existing tabs are left untouched.

Setup (one-time):
  pip install -r sheets/requirements.txt
  Place OAuth client-secrets at sheets/credentials.json  (gitignored).
  First run opens a browser for Google consent; token is cached in
  sheets/token.json (also gitignored).

Env vars (in .env or shell):
  SHEET_ID    — if set, opens the existing sheet; otherwise creates a new one.
  SHEET_NAME  — name for a newly created sheet (default: "Invoicing Assistant").

After running, set SHEET_ID=<id> in your .env so subsequent calls reuse it.
"""

import csv
import os
import re
import sys
from pathlib import Path

import gspread

HERE = Path(__file__).parent
SEED_DIR = HERE / "seed"
CREDS_FILE = HERE / "credentials.json"
TOKEN_FILE = HERE / "token.json"


def _tab_to_filename(tab_name: str) -> str:
    """Convert CamelCase tab name to snake_case CSV filename."""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", tab_name).lower()
    return s + ".csv"


def load_seed(tab_name: str) -> list[list[str]]:
    path = SEED_DIR / _tab_to_filename(tab_name)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    return rows


def main() -> None:
    if not CREDS_FILE.exists():
        sys.exit(
            f"Missing {CREDS_FILE}.\n"
            "Download OAuth client-secrets from Google Cloud Console and place them there.\n"
            "See: https://docs.gspread.org/en/latest/oauth2.html"
        )

    # Import here so the error above fires before gspread import issues.
    from schema import TABS

    gc = gspread.oauth(
        credentials_filename=str(CREDS_FILE),
        authorized_user_filename=str(TOKEN_FILE),
    )

    sheet_id = os.environ.get("SHEET_ID")
    if sheet_id:
        sh = gc.open_by_key(sheet_id)
        print(f"Opened existing sheet: {sh.title}")
    else:
        name = os.environ.get("SHEET_NAME", "Invoicing Assistant")
        sh = gc.create(name)
        print(f"Created new sheet: {sh.title}")
        print(f"  URL : https://docs.google.com/spreadsheets/d/{sh.id}")
        print(f"  → Add SHEET_ID={sh.id} to your .env\n")

    existing = {ws.title for ws in sh.worksheets()}

    for tab_name, columns in TABS.items():
        if tab_name in existing:
            print(f"  {tab_name}: already exists — skipped")
            continue

        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(columns))
        seed_rows = load_seed(tab_name)
        if seed_rows:
            ws.update(seed_rows, value_input_option="USER_ENTERED")
        else:
            ws.update([columns], value_input_option="USER_ENTERED")
        row_count = len(seed_rows) - 1 if seed_rows else 0
        print(
            f"  {tab_name}: created ({row_count} seed row{'s' if row_count != 1 else ''})"
        )

    # Remove the blank default sheet Google creates on a fresh spreadsheet.
    try:
        sh.del_worksheet(sh.worksheet("Sheet1"))
    except gspread.WorksheetNotFound:
        pass

    print("\nDone.")


if __name__ == "__main__":
    main()
