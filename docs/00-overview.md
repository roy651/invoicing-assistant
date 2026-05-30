# 00 — Overview & Glossary

## Purpose

A freelance graphic-design studio bills monthly. Today the freelancer manually reads
a month of email and Zoom transcripts, figures out which work items were done and how
far along the multi-month ones are, matches each to a price, and creates invoices in
morning — separately per end-client for the agency she works through, and directly for
her other clients.

This system does the gathering, reasoning, and draft preparation. A human reviews and
issues. It is an assistant, not an autopilot.

## Actors & runtime

- **Cowork (on the user's Mac)** — the runtime. Runs the invoicing skill, on demand or
  on a monthly schedule. Reaches the state store via the Drive connector and morning via
  the bridge. Runs locally; only executes while the Mac is awake and Claude Desktop is open.
- **The user (freelancer)** — the gate. Reviews the proposed line items, trims/edits,
  approves. Then reviews the created drafts in morning and issues them herself.
- **The morning bridge (MCP)** — the only path to morning. Read endpoints + create-draft
  only. See `03-morning-bridge.md`.
- **The state store (Google Sheets)** — durable memory across months. See `01-data-contracts.md`.

## Data flow (one monthly cycle)

```
inputs                 engine (Cowork)                     truth
------                 ---------------                     -----
email (IMAP)  ─┐
transcripts   ─┼─►  1. SETTLE: read morning issued docs ──► reconcile ledger to reality
               │    2. SCAN:   fetch email/transcripts since watermark
price book ◄───┼──  3. MATCH:  evidence → ledger items (incl. subcontractor CC threads)
agreements ◄───┘    4. INFER:  status + confidence + proposed qty
                    5. PROPOSE: review packet, grouped by bill-to / end-client
                         │
                    [ HUMAN GATE ] ── user trims, edits qty, approves
                         │
                    6. CREATE: drafts in morning (drafts only)
                    7. UPDATE: ledger (proposed/approved recorded; billed filled next settle)
```

Note that step 1 (settlement) runs **before** new proposing every cycle, so manual edits
the user made in morning last month are absorbed before this month's reasoning begins.
This is the mechanism that keeps the ledger aligned with reality. See `02-reconciliation.md`.

## The central design principle

The ledger is an **optimistic projection**. The issued documents in morning are **truth**.
The ledger reconciles *to* morning, never the other way. Everything the agent infers is
provisional until a human confirms it and morning records it.

## Glossary (use these terms exactly, everywhere)

| Term | Meaning |
| --- | --- |
| **work item** | A single billable unit of work, tracked across months. One ledger row. |
| **bill_to** | Who the invoice is addressed to (an agency, or a direct client). Drives which invoice an item lands on. |
| **end_client** | The agency's sub-customer. Sets the invoice subtitle. Null for direct clients. |
| **agency** | A `bill_to` whose invoices carry end-client subtitles and produce one document per end-client. |
| **item_kind** | `fixed_quote` (a whole quoted job, billed in fractions to 1.0) or `unit_based` (a per-unit rate, billed by count each month). |
| **billing_mode** | For `fixed_quote` only: `defer` (bill once at completion) or `partial` (bill fractions over months). |
| **qty** | For `fixed_quote`: a fraction of the whole (e.g. 0.3). For `unit_based`: a count of units done this period. |
| **subtitle line** | A synthesized zero-priced line item `------------ {end_client} ------------` that opens an agency invoice. Not a morning field. |
| **Price Book** | Normalized, versioned price reference. Source-format-agnostic (PDF/Sheet in → one table). |
| **Agreement** | A negotiated price for a specific item, overriding the Price Book. Logged with source. |
| **Client Profile** | Per-`bill_to` settings: language, currency, VAT rate, is_agency, managed_by_agent. |
| **managed_by_agent** | If false, the agent never proposes for this client and settlement won't flag its docs as orphans (e.g. manually-created invoices with no email trail). |
| **settlement** | Start-of-cycle pass that reads morning issued docs and reconciles the ledger to them. |
| **orphan** | A line in an issued morning doc with no matching ledger item (e.g. added manually from a phone call). Settlement back-fills a ledger row. |
| **review packet** | The grouped, evidence-backed proposal presented to the human gate. |
| **watermark** | Timestamp of the last scan; bounds how far back email is freshly scanned. |
| **epoch** | One-time cutoff. Email before it is considered already accounted for via opening balances. |
