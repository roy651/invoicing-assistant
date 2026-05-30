# 01 — Data Contracts

The authoritative shapes. All other docs and all code defer to this file. If an
implementer is unsure of a field, it is defined here or it does not exist.

The Sheets store holds the human-editable state (ClientProfiles, Agreements, Ledger,
OpeningBalances, Config). Code reads/writes via the Drive connector. IDs are config
(`.env` / Keychain).

The Price Book is derived reference data, not human-edited state. It lives on disk at
`data/price_book.csv` (gitignored), produced by running `sheets/normalize_price_list.py`.
The agent reads it directly from disk at runtime.

---

## 1. Client Profile

One row per `bill_to`. Pure configuration — never inferred. The user maintains it.

| Column | Type | Notes |
| --- | --- | --- |
| `bill_to` | string (PK) | Invoice recipient, e.g. `SPRIG`, `<Direct Client>`. |
| `is_agency` | bool | If true, invoices use end-client subtitles and split one doc per end-client. |
| `managed_by_agent` | bool | If false, agent never proposes for it; settlement won't flag its docs as orphans. |
| `language` | enum | `en` \| `he`. Drives invoice language. |
| `currency` | enum | `USD` \| `ILS`. |
| `vat_rate` | decimal | e.g. `0.00` for export (agency), `0.18` domestic. |
| `morning_client_id` | string | The client's ID in morning, resolved once via the bridge. |
| `notes` | string | Free text. |

Observed examples (for orientation, not committed): the agency profile is
`is_agency=true, managed_by_agent=true, language=en, currency=USD, vat_rate=0.00`.
The electric-company-style manual client is
`is_agency=false, managed_by_agent=false, language=he, currency=ILS, vat_rate=0.18`.

---

## 2. Price Book

Normalized, **versioned**, **source-format-agnostic**. The raw 2026 list is a Google
Sheet; the 2025 list (still in effect for older quotes) is a PDF; future lists may be
anything. `sheets/normalize_price_list.py` converts each source into rows of this shape
and writes `data/price_book.csv`. **At runtime the agent reads `data/price_book.csv`
directly from disk — never the raw PDF/Sheet, and never a Sheets tab.**

| Column | Type | Notes |
| --- | --- | --- |
| `price_id` | string (PK) | Stable, e.g. `2025-web-scrollable`. Encodes version. |
| `version` | string | Price-list version, e.g. `2025`, `2026`. |
| `effective_from` | date | When this version takes effect. |
| `effective_to` | date \| null | Null = currently in effect. Multiple versions can be live (old quotes honored). |
| `category` | string | e.g. `Web Design`, `Marketing`, `Trade Shows`. |
| `item` | string | e.g. `Scrollable Single Page`, `Roll-up`, `Hourly Creative Service`. |
| `price_low` | decimal | For a fixed price, equals `price_high`. |
| `price_high` | decimal | For a range item, the top of the range. |
| `currency` | enum | `USD` \| `ILS`. |
| `is_range` | bool | True when `price_low != price_high`. |
| `notes` | string | From source (e.g. rounding note). |

### Critical: ranges are not auto-resolvable

Many items are ranges (e.g. web `Scrollable Single Page` `11,000–13,200`). The agent
**must not** pick a number inside a range. The concrete price for a ranged job is set
when the job is quoted and is stored on the **work item** (`unit_price`), with `price_ref`
pointing back to the range row. If a work item references a range and has no `unit_price`,
that is an **unresolved price → flag at the gate, never guess**.

### Version selection rule

A work item is priced at the version **in effect when the job was quoted**, captured at
item creation (`price_ref` pins the `price_id`, which pins the version). A 2025-quoted job
billed in 2026 keeps its 2025 price. Do not re-price by billing date.

---

## 3. Agreements Log

Negotiated prices that override the Price Book. Append-only; the user (or the agent,
on confirmation) adds rows. Negotiated prices are common and frequently differ from book.

| Column | Type | Notes |
| --- | --- | --- |
| `agreement_id` | string (PK) | |
| `bill_to` | string | |
| `end_client` | string \| null | |
| `item_desc` | string | What was agreed. |
| `agreed_price` | decimal | |
| `currency` | enum | |
| `agreed_on` | date | |
| `source_ref` | string | Email/transcript id where it was agreed (traceability). |
| `confidence` | enum | `confirmed` (user-entered) vs `detected` (agent-proposed, awaiting confirm). |

Only `confirmed` agreements may be used as a price source without flagging.

---

## 4. Work Item Ledger — the core

One row per work item, persistent across months. Columns are grouped by *who owns the
truth* of each: configuration, the agent's provisional read, and the human/morning truth.

### Identity & classification

| Column | Type | Notes |
| --- | --- | --- |
| `item_id` | string (PK) | Stable, e.g. `APREO-scroll-001`. Continuity across months hinges on this. |
| `bill_to` | string | FK → Client Profile. Drives which invoice. |
| `end_client` | string \| null | Subtitle for agency invoices; null for direct. |
| `description` | string | The work, in the invoice's language. |
| `assignee` | enum/string | `self` or a subcontractor name. Tells the agent whose CC'd client threads count as completion evidence. |
| `item_kind` | enum | `fixed_quote` \| `unit_based`. |
| `billing_mode` | enum \| null | `defer` \| `partial`. Required for `fixed_quote`; null for `unit_based`. |

### Pricing

| Column | Type | Notes |
| --- | --- | --- |
| `unit_price` | decimal | Full quote (`fixed_quote`) or per-unit rate (`unit_based`). Required when a range was resolved. |
| `currency` | enum | From Client Profile, stored for clarity. |
| `price_source` | enum | `price_book` \| `negotiated`. Never `inferred`. |
| `price_ref` | string | `price_id` or `agreement_id`. Makes price traceable. |

### Progress (fixed_quote only; unit_based ignores these)

| Column | Type | Notes |
| --- | --- | --- |
| `total_qty` | decimal | Usually `1.0`. The whole job. |
| `qty_billed_to_date` | decimal | Cumulative fraction **actually issued** in prior months. Accumulates from `qty_billed_actual`. |
| `last_billed_month` | string | `YYYY-MM`. |

### Agent's provisional read (overwritten each cycle)

| Column | Type | Notes |
| --- | --- | --- |
| `status_agent` | enum | `not_started` \| `in_progress` \| `complete`. The agent's inference only. |
| `completion_evidence` | string | Email/transcript ids + short quotes (incl. subcontractor-CC confirmations) justifying the status. The user verifies against this. |
| `confidence` | enum | `high` \| `med` \| `low`. Low is always surfaced regardless of proposal. |
| `qty_proposed` | decimal | Suggested qty to bill now. 0 = defer; a fraction = partial; remainder = final. |

### Human gate + morning truth

| Column | Type | Notes |
| --- | --- | --- |
| `status_confirmed` | enum \| null | The user's ground truth. **Overrides `status_agent`** and feeds next month's reasoning. |
| `decision` | enum \| null | `bill` \| `partial` \| `defer` \| `hold`. |
| `qty_approved` | decimal \| null | What the user approved at the gate, pre-draft. |
| `qty_billed_actual` | decimal \| null | Read back from the **issued** morning doc at next settlement. The only input to `qty_billed_to_date`. |
| `morning_doc_ref` | string \| null | The issued document id, set at settlement. |
| `notes` | string | Free text. |

### The three-stage quantity (do not collapse these)

```
qty_proposed     (agent infers)
   │  human gate
qty_approved     (user approves, pre-draft)
   │  user may edit the DRAFT in morning, then issues
qty_billed_actual (read back from issued doc — TRUTH)
   │
qty_billed_to_date += qty_billed_actual     ← accumulation rule
```

Because the user can change quantities, delete lines, or delete whole invoices after the
gate, **only `qty_billed_actual` is trusted** for accumulation. "Remaining" is always
`total_qty - qty_billed_to_date`, and self-corrects (a 0.7→0.6 edit leaves 0.4 remaining,
not 0.3). See `02-reconciliation.md`.

---

## 5. morning proforma payload mapping

How a ready-to-bill ledger row becomes a `create_proforma` call. The bridge owns the
exact API field names (`03-morning-bridge.md`); this defines the *semantic* mapping.

**Review artifact is a Proforma (type 300 / חשבון עסקה)**, not a Tax Invoice (305).
`POST /documents` with type 305 issues a real fiscal document immediately — there is no
draft/unsigned mode for invoices. The Proforma is non-fiscal, deletable, and converted
to a real invoice by the human in morning when they approve the review packet. That
conversion is the issuance step and is **out of this system's automated scope**.

### Document grouping

- Group approved items by `bill_to`.
- If `is_agency`: emit **one draft per `end_client`** (the agency's stated preference).
  Each opens with a **subtitle line**.
- If direct: one draft, no subtitle line.

### Document-level description (agencies only)

morning's top-level `description` field renders as a **bold heading** on the
proforma/invoice — this is how the end-client name appeared as a heading on the
reference invoices, distinct from the zero-priced subtitle line.

- `is_agency`: the invoicing skill sets `request.description = end_client` (e.g.
  `"Acme Corp"`) when calling `create_proforma`.
- Direct clients: `description` is **omitted** from the request; `_build_payload`
  omits it from the API body.

### Subtitle line (synthesized — agencies only)

The agency invoices open with a zero-priced header line, observed as:

```
QTY 1 | description: "------------ {end_client} ------------" | price 0.00 | total 0.00
```

The bridge generates this as the first line item. It is not a morning subtitle field.

### Line item mapping

| morning line field | Source |
| --- | --- |
| `quantity` | `qty_approved` (`fixed_quote`: a fraction; `unit_based`: a count). |
| `description` | `description` + generated progress annotation (below). |
| `unitPrice` | `unit_price`. |
| `currency` / `vat` | From Client Profile. |

### Progress annotation (generated, keeps invoices self-documenting)

For `fixed_quote` `partial` items, append a human-readable note to the description,
derived from the ledger — matching the observed style:

- nth payment ordinal from prior `qty_billed_to_date` history, and cumulative %:
  e.g. `"Scrolling website - 2nd payment (70% so far)"`.
- final installment: append `"- 2nd and Last Payment"` (or nth) when this draft brings
  `qty_billed_to_date` to `total_qty`.

`unit_based` items get no fraction annotation.

### Language & currency

Driven entirely by Client Profile: agency → English/USD/0% VAT; domestic direct →
Hebrew/ILS/18% VAT. The bridge selects per document; nothing is global.
