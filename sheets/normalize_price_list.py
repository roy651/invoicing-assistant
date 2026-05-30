#!/usr/bin/env python3
"""
Normalize the price-list CSV into Price Book rows (docs/01-data-contracts.md §2).

Source
------
  fixtures/2026PriceList.csv
  Columns: Category, Item, Old Price (2025), New Price (2026), Notes
  Every source row produces TWO output rows — one for each version.

Output (gitignored — contains real prices)
------------------------------------------
  fixtures/generated/price_book.csv

price_id algorithm  (CONTRACT — ledger.price_ref must equal price_book.price_id exactly)
---------------------------------------------------------------------------
  {version}-{category_slug}-{item_slug}

  slug(text):
    1. Lowercase.
    2. Replace every run of non-alphanumeric characters with a single '-'.
    3. Strip leading/trailing '-'.

  Examples:
    ("2025", "Web Design",    "Scrollable Single Page")  → "2025-web-design-scrollable-single-page"
    ("2026", "3D & Animation","3D Modeling")              → "2026-3d-animation-3d-modeling"
    ("2025", "Marketing",     "Booklet (8 Pages)")        → "2025-marketing-booklet-8-pages"

  The same algorithm is applied to both 2025 and 2026 rows; only the version
  prefix differs.  Never alter this function without a migration plan for all
  existing ledger.price_ref values.

Effective dates / version selection rule (01-data-contracts.md §2)
------------------------------------------------------------------
  2025  → effective_from=2025-01-01, effective_to="" (open; currently in effect).
  2026  → effective_from="" (blank), notes prefixed "DRAFT - not yet in effect."
          A blank effective_from signals that this version must NOT be auto-selected
          for new quotes until explicitly activated by the user.

Range parsing
-------------
  Separator: en dash (U+2013), em dash (U+2014), or ASCII hyphen-minus.
  Both price tokens must start with '$' — prevents false positives on other hyphens.
  "$11,000–$13,200" → price_low=11000, price_high=13200, is_range=TRUE
  "$130"            → price_low=130,   price_high=130,   is_range=FALSE

Optional PDF spot-check
-----------------------
  Call spot_check(rows, known) with a dict of {price_id: expected_price_low}
  spot-read from fixtures/2025PriceList.pdf.  See the function docstring.
"""

import csv
import re
import sys
from pathlib import Path

# Resolve paths relative to this file so the script runs from any cwd.
_HERE = Path(__file__).parent
_REPO = _HERE.parent

SOURCE_CSV = _REPO / "fixtures" / "2026PriceList.csv"
OUTPUT_CSV = _REPO / "fixtures" / "generated" / "price_book.csv"

# Import column order from the authoritative schema definition.
sys.path.insert(0, str(_HERE))
from schema import PRICE_BOOK as _COLUMNS  # noqa: E402

EFFECTIVE_FROM_2025 = "2025-01-01"

# Range separator: en dash, em dash, or hyphen-minus; flanked by $ price tokens.
_RANGE_RE = re.compile(r"^\$([0-9,]+)\s*[–—\-]\s*\$([0-9,]+)$")
_SINGLE_RE = re.compile(r"^\$([0-9,]+)$")


def _slug(text: str) -> str:
    """
    Produce a stable URL-safe slug for use in price_id.
    Lowercases; replaces any run of non-alphanumeric characters with '-';
    strips leading/trailing '-'.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _price_id(version: str, category: str, item: str) -> str:
    """See module docstring for the full algorithm and examples."""
    return f"{version}-{_slug(category)}-{_slug(item)}"


def _parse_price(raw: str) -> tuple[float, float, bool]:
    """
    Parse a price cell into (price_low, price_high, is_range).
    Raises ValueError if the cell cannot be parsed.
    """
    raw = raw.strip()

    m = _RANGE_RE.match(raw)
    if m:
        low = float(m.group(1).replace(",", ""))
        high = float(m.group(2).replace(",", ""))
        return low, high, True

    m = _SINGLE_RE.match(raw)
    if m:
        val = float(m.group(1).replace(",", ""))
        return val, val, False

    raise ValueError(f"Unrecognised price format: {raw!r}")


def normalize(source: Path = SOURCE_CSV) -> list[dict]:
    """
    Read the source CSV and return normalized Price Book dicts.
    Every source row produces one 2025 row and one 2026 row.
    """
    rows: list[dict] = []

    with source.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for lineno, record in enumerate(reader, start=2):
            category = record["Category"].strip()
            item = record["Item"].strip()
            orig_notes = record.get("Notes", "").strip()

            for version, col in [
                ("2025", "Old Price (2025)"),
                ("2026", "New Price (2026)"),
            ]:
                raw = record[col].strip()
                try:
                    price_low, price_high, is_range = _parse_price(raw)
                except ValueError as exc:
                    print(f"  WARNING line {lineno} [{version}]: {exc}", file=sys.stderr)
                    continue

                if version == "2025":
                    effective_from = EFFECTIVE_FROM_2025
                    effective_to = ""
                    # The CSV notes describe 2026 rounding, not 2025 prices. Leave blank.
                    notes = ""
                else:
                    # 2026 is a draft; effective_from left blank so version-selection
                    # (01 §2) cannot pick it for current quotes.
                    effective_from = ""
                    effective_to = ""
                    draft_prefix = "DRAFT - not yet in effect."
                    notes = f"{draft_prefix} {orig_notes}".strip() if orig_notes else draft_prefix

                rows.append({
                    "price_id":       _price_id(version, category, item),
                    "version":        version,
                    "effective_from": effective_from,
                    "effective_to":   effective_to,
                    "category":       category,
                    "item":           item,
                    "price_low":      price_low,
                    "price_high":     price_high,
                    "currency":       "USD",
                    "is_range":       "TRUE" if is_range else "FALSE",
                    "notes":          notes,
                })

    return rows


def spot_check(rows: list[dict], known_values: dict[str, float]) -> None:
    """
    Compare a subset of rows against values spot-read from the 2025 PDF.
    Pass {price_id: expected_price_low}; mismatches print to stderr.

    Usage example (run interactively after calling normalize()):
        rows = normalize()
        spot_check(rows, {
            "2025-web-design-scrollable-single-page": 10000,
            "2025-trade-shows-roll-up":                  850,
            "2025-general-hourly-creative-service":      130,
        })
    """
    index = {r["price_id"]: r for r in rows}
    all_ok = True
    for pid, expected_low in known_values.items():
        row = index.get(pid)
        if row is None:
            print(f"  SPOT-CHECK MISS: {pid} not found", file=sys.stderr)
            all_ok = False
            continue
        actual = row["price_low"]
        if actual != expected_low:
            print(
                f"  SPOT-CHECK MISMATCH: {pid}  expected={expected_low}  got={actual}",
                file=sys.stderr,
            )
            all_ok = False
        else:
            print(f"  SPOT-CHECK OK: {pid} = {actual}")
    if all_ok:
        print("  All spot-checks passed.")


def main() -> None:
    if not SOURCE_CSV.exists():
        sys.exit(f"Source not found: {SOURCE_CSV}")

    print(f"Reading {SOURCE_CSV} ...")
    rows = normalize()

    rows_2025 = [r for r in rows if r["version"] == "2025"]
    rows_2026 = [r for r in rows if r["version"] == "2026"]
    ranges_2025 = sum(1 for r in rows_2025 if r["is_range"] == "TRUE")
    ranges_2026 = sum(1 for r in rows_2026 if r["is_range"] == "TRUE")

    print(f"  2025: {len(rows_2025)} rows, {ranges_2025} range items  (effective_from={EFFECTIVE_FROM_2025}, open)")
    print(f"  2026: {len(rows_2026)} rows, {ranges_2026} range items  (DRAFT — effective_from blank)")

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows → {OUTPUT_CSV}")
    print("Load into the live PriceBook tab via sheets/setup.py or Google Sheets import.")
    print("To spot-check 2025 values against the PDF, call spot_check() — see docstring.")


if __name__ == "__main__":
    main()
