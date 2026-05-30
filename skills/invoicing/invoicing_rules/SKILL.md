---
name: invoicing-rules
description: >-
  Run the monthly forward pass for a freelancer's billing: read email +
  transcripts, match them to open ledger items, infer what was completed and how
  much to bill, resolve prices, and emit a grouped review packet for a human to
  approve. Proposes only — never issues, never bills. Use when starting the
  monthly invoicing review for a billing month.
---

# Invoicing rules — monthly forward pass

You run the reasoning core of the invoicing system. You read the freelancer's
evidence for a billing month, decide which work items advanced and by how much,
price them, and present a **review packet** for the human gate.

You **stop at the packet.** You never create a draft, never settle, never write
to the ledger. Those are downstream steps (gate → create → record) owned by
other tasks. Your output is a proposal a human reviews.

The cycle is deterministic in order: **SCAN → MATCH → INFER → PRICE → PROPOSE.**
At each mechanical step you call the existing Python helpers in
`invoicing_rules`; the *reasoning* (MATCH, INFER) is yours, but it must obey the
rules below. Do not re-derive schema or algorithm from memory — the helpers and
`docs/01`/`docs/02` are the source of truth.

> Settlement (reading issued morning docs back) runs *before* SCAN in the full
> monthly machine (`docs/02 §B`), but it is a separate skill. Assume it has
> already run: the ledger you load is reconciled to morning. You reason forward
> only.

## Invariants (hold at every step — enforce in your output, not just intent)

1. **No evidence → no line.** Every proposed line cites concrete evidence ids
   (and short quotes) in `completion_evidence`. If you cannot cite evidence for a
   status, do not assert it.
2. **Never invent a price or an item.** Prices come only from the Price Book
   (`price_ref`) or a **confirmed** Agreement, via the pricing helper. Unresolved
   ranges and unconfirmed agreements are *flagged*, never guessed. Never fabricate
   an item_id or a description that has no basis in evidence.
3. **You propose; the gate decides.** Never set `status_confirmed`, `decision`,
   or `qty_approved`. Those columns belong to the human gate. You only fill
   `status_agent`, `completion_evidence`, `confidence`, `qty_proposed`.
4. **`managed_by_agent=False` clients are never proposed.** They are excluded by
   the packet builder; do not attempt to surface them. Their billing flows only
   through settlement's silent orphan path.
5. **Bias toward under-claiming.** Completion inference is the weakest link. When
   unsure whether work is done or how far along it is, propose less / defer and
   flag it, never more.

---

## Step 1 — SCAN

Gather the period's evidence and the open ledger.

1. Fetch email (IMAP skill) and transcripts (transcripts reader) for the billing
   month.
2. Unify them into one chronologically ordered list:

   ```python
   from invoicing_rules import unify
   evidence = unify(messages, transcripts)   # list[UnifiedEvidence], sorted by date
   ```

   `unify` preserves email `from_`/`to`/`cc`/`subject` as first-class fields and
   transcript `participants`/`filename` separately — it does **not** collapse them.
   You will read `cc` directly in MATCH/INFER for the subcontractor signal, so do
   not flatten it yourself.
3. Load state from the CSV exports:

   ```python
   from invoicing_rules import (
       load_ledger, load_client_profiles, load_price_book, load_agreements,
   )
   ledger    = load_ledger(ledger_csv)
   profiles  = load_client_profiles(profiles_csv)
   price_book = load_price_book(price_book_csv)
   agreements = load_agreements(agreements_csv)
   ```

Always load **all open ledger items**, not just ones touched by new email — open
items carry forward and are re-evaluated every cycle (`docs/02 §D`).

## Step 2 — MATCH (reasoning)

Link each piece of evidence to an existing `item_id`, or propose a genuinely new
item. This is your judgment, constrained by:

- **Never silently merge or duplicate.** If evidence could plausibly belong to two
  items, or to none, surface it as uncertain (`completion_evidence` notes the
  ambiguity, `confidence` reflects it) — do not pick silently.
- **Respect `assignee`.** For items whose `assignee` is a subcontractor (not
  `self`), the relevant completion signal is the **end client confirming on a
  thread the subcontractor is CC'd on**. Read `UnifiedEvidence.cc` to detect this.
  A subcontractor's own claim of completion is weaker than the client's CC'd
  acknowledgement.
- **New items** are allowed only with evidence behind them (invariant 1) and a
  basis for their `bill_to`/`description`. A new item with no resolvable price is
  still valid — it surfaces flagged, not suppressed.

## Step 3 — INFER (reasoning)

For each matched/open item, fill the four agent columns following `docs/02 §E`:

- `fixed_quote` + `defer`, you judge complete → `qty_proposed = total_qty - qty_billed_to_date`.
- `fixed_quote` + `partial`, in progress → `qty_proposed` = a **suggested**
  fraction from progress signals. It is a suggestion; the exact fraction is the
  user's call (the packet flags it `partial_fraction`).
- `fixed_quote` + not complete + `defer` → `qty_proposed = 0` (deferred; still
  surfaced for visibility).
- `unit_based` → `qty_proposed` = count of units evidenced this period. No
  fraction logic.

**Hard rule — confidence is mandatory (review note N1).** Every item you assess
(i.e. every item you give a `status_agent`) MUST also get a `confidence` of
`high`, `medium`, or `low`. An item with `status_agent` set but `confidence`
unset is a spec violation: the packet builder only raises the low-confidence flag
when `confidence == "low"`, so an unset confidence would sort as a *clean* line
and risk a false "looks fine" signal to the human. Never emit an assessed item
without a confidence. If you are not confident enough to set one, you are not
confident enough to assess the item — leave `status_agent` unset and it is
correctly excluded.

`completion_evidence` must carry evidence ids + short quotes (including
subcontractor-CC confirmations) for whatever status you assert.

Never touch `status_confirmed` / `decision` / `qty_approved` (invariant 3).

## Step 4 — PRICE

Resolve each assessed item's price mechanically — do not price by hand:

```python
from invoicing_rules import resolve_all
price_results = resolve_all(assessed_items, price_book, agreements)
```

`resolve_all` applies the resolution hierarchy and flags everything it cannot
resolve. Trust its `resolved` flag; never substitute a number for an unresolved
result.

- A **range** Price Book entry with no `unit_price` on the row resolves to
  *unresolved* — it never auto-picks a number inside the range.
- An agreement that is **not confirmed** resolves to *unresolved* — needs the
  user.

**Price-resolution contract (review note N2).** The DIRECT path — an item that
already has `unit_price` *and* `price_ref` set — is trusted as **carry-forward**
and is **not re-validated** against the Price Book or agreements on this run. This
is correct and intended for multi-month fixed quotes: the price was authoritative
when the item was created and must stay stable across installments. The guarantee
this path makes is **"`price_ref` is traceable"** (a human can look it up), *not*
"this number was re-checked against the book today." State it that way to the user
if asked; do not describe a carried-forward price as freshly validated.

## Step 5 — PROPOSE

Hand the assessed items + their price results to the packet builder:

```python
from invoicing_rules import build_review_packet
packet = build_review_packet(
    ledger, profiles, price_book, agreements, billing_month,
)
```

`build_review_packet` re-resolves prices internally, excludes
`managed_by_agent=False` clients and items without `status_agent`, groups by
`bill_to → end_client`, attaches flags
(`unresolved_price` / `low_confidence` / `partial_fraction` / `defer`), annotates
partial-fixed-quote descriptions with progress, and sorts so the lines needing
human judgment come first.

Present the packet to the user grouped as built, leading with the flagged lines.
Make clear it is a **proposal**: their edits are the authority. Then **stop.**

---

## Stop condition

You are done when the review packet is presented. Do **not**:

- create proformas or drafts (downstream: gate → create),
- write `status_confirmed` / `decision` / `qty_approved` or any approval,
- settle against morning or modify the ledger on disk.

Wait for the human gate.
