# 05 — Invoicing Rules Skill (Cowork SKILL.md)

The orchestration skill that runs inside Cowork. It ties together the IMAP skill, the
transcripts reader, the state-store Sheets, and the morning bridge, and runs the monthly
cycle from `02-reconciliation.md`. This doc is the spec; the deliverable is the skill's
`SKILL.md` plus any helper scripts.

## Trigger

- On demand (user starts the monthly task), or a monthly Cowork scheduled task.
- Reasoning-heavy and money-adjacent → it pauses at the gate; it never runs end-to-end
  unattended to a created draft without explicit user approval in the session.

## Inputs it reads

- Email (via the IMAP skill) and transcripts (folder reader) since the watermark.
- State Sheets: Client Profiles, Agreements Log, Work Item Ledger.
- Price Book: `data/price_book.csv` (local disk, produced by `sheets/normalize_price_list.py`).
- morning (via the bridge, read endpoints) for settlement + history.

## The cycle (authoritative steps — see `02` for the state machine)

1. **Settle.** Read issued morning docs since last run; reconcile the ledger to them
   (qty edits, deletions, orphans). Produce the diff report.
2. **Scan.** Fetch new email/transcripts; load all open ledger items.
3. **Match.** Link evidence to existing `item_id`s or propose new items. Surface uncertain
   matches; never silently merge or duplicate. Respect `assignee` (subcontractor CC threads).
4. **Infer.** Fill `status_agent`, `completion_evidence` (ids + short quotes), `confidence`,
   `qty_proposed` per the inference rules in `02 §E`.
5. **Price.** Resolve each item's price from Price Book (`price_ref` + version) or a
   `confirmed` Agreement. Ranges/unknowns with no `unit_price` → mark unresolved (flag).
6. **Propose.** Emit the review packet (below) — the agent's proposal, for transparency.
7. **Create.** Create **draft proformas** autonomously from the agent's proposal
   (`status_agent` ∈ {in_progress, complete} and `qty_proposed > 0`), grouped per `01 §5`
   (one per end-client for agencies; subtitle line; progress annotations; language/
   currency/VAT from Client Profile). Drafts only (type 300 — non-fiscal, deletable).
8. **Record.** Write the proforma ids (`proforma_doc_ref`) to the ledger. Leave
   `qty_billed_actual` empty — next cycle's settlement fills it from the issued doc.
9. **Gate (human).** The conversion IS the gate: Avigail reviews/trims/edits/prices every
   line at **proforma → invoice conversion** in morning. The agent never issues or converts.

## Review packet (what the gate sees)

Grouped by `bill_to`, then `end_client`. Per line:

| Shown | From |
| --- | --- |
| end_client / bill_to | ledger |
| description (+ proposed progress annotation) | ledger + generated |
| item_kind, billing_mode | ledger |
| qty_proposed | inference |
| unit_price, line total, currency | pricing |
| status_agent + **confidence** | inference |
| evidence (ids + short quotes, incl. CC confirmations) | completion_evidence |
| flags | unresolved price / low confidence / uncertain match / new item |

Sort so the items needing judgment (low confidence, unresolved price, partial-fraction
suggestions) are front and center. The packet is a proposal; the user's edits are authority.

## Hard rules (restate, enforce in skill logic)

- Drafts only; the gate decides; morning is truth; never invent prices; per-item evidence.
  (Full list in `CLAUDE.md`.)
- Drafts only — the agent creates proformas but **never issues or converts**; the human
  gate is the conversion. Create only items with a billable `status_agent` + `qty_proposed > 0`.
- `managed_by_agent=false` clients are never proposed (their docs are handled only by
  settlement's silent orphan path).
- The partial *fraction* is the user's call; the agent suggests and flags, never asserts.

## Definition of done

- Given a fixtures month of email + a seeded ledger, produces a review packet that a human
  judges correct (precision/recall + price accuracy targets in `07-acceptance.md`).
- On approval, drives the bridge (dry-run) to emit correct draft payloads.
- A second run after "issuing" (simulated) settles cleanly: qty edits, a deleted line, and
  an injected orphan are all reconciled and reported.
