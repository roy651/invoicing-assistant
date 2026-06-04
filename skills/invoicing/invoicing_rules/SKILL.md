---
name: invoicing-rules
description: >-
  Run the monthly forward pass for a freelancer's billing: read email +
  transcripts, match them to open ledger items, infer what was completed and how
  much to bill, resolve prices, and emit a grouped review packet for a human to
  approve. Only on explicit approval does it prepare draft proformas (never tax
  invoices) and record them. Never issues, never bills. Use when starting the
  monthly invoicing review for a billing month.
---

# Invoicing rules — monthly forward pass

You run the reasoning core of the invoicing system. You read the freelancer's
evidence for a billing month, decide which work items advanced and by how much,
price them, and present a **review packet** for the human gate.

You **stop at the packet for the human to decide.** Only after their explicit
approval do you prepare **draft proformas** (the morning bridge can only create
non-fiscal Proformas, never tax invoices) and record the result. You never issue
a tax invoice, never bill — issuance is the human converting a proforma in morning.

The cycle is deterministic in order: **SETTLE → SCAN → MATCH → INFER → PRICE →
PROPOSE.** Then the human gate decides; on approval you run **CREATE → RECORD.**
At each mechanical step you call the existing Python helpers in
`invoicing_rules`; the *reasoning* (MATCH, INFER) is yours, but it must obey the
rules below. Do not re-derive schema or algorithm from memory — the helpers and
`docs/01`/`docs/02` are the source of truth.

> Settle FIRST (`docs/02 §B`): last month's manual edits in morning are absorbed
> before this month's reasoning starts, so you reason forward from a ledger that
> already matches reality.

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

## Step 0 — SETTLE (first, before anything else)

Reconcile the ledger to what was actually ISSUED in morning since the last run,
so this month reasons from reality. **morning is truth** — `qty_billed_to_date`
accumulates only from issued invoices read back here, never from a proposal or a
gate approval.

```python
from invoicing_rules import (
    fetch_issued_invoices, fetch_open_proformas, settle_ledger, write_ledger,
)

# from_date must reach back to the OLDEST unsettled proforma, not just last month —
# issuance is human-paced, so a proforma may be converted long after creation.
issued = fetch_issued_invoices(client, from_date=oldest_unsettled_date)
live = fetch_open_proformas(client)   # type-300 docs still unconverted in morning
report = settle_ledger(ledger, issued, profiles, live_proforma_ids=live)
write_ledger(ledger, ledger_csv)
# Show report.summary() to the user before any new proposing.
```

`settle_ledger` matches each item carrying a pending `proforma_doc_ref` to its
issued invoice (via `linkedDocumentIds`, content fallback otherwise) and records the
truth: `qty_billed_actual` = the issued line quantity, accumulates
`qty_billed_to_date`, sets `morning_doc_ref` to the invoice id, recomputes status,
and clears `proforma_doc_ref`. A proforma **not yet converted** (still a live type-300
doc) is left **still pending** — never reverted, or it would re-propose into a
duplicate. Only a proforma that is gone (deleted draft / deleted line / deleted
invoice, and not among `live_proforma_ids`) **reverts to open** — `qty_billed_to_date`
unchanged, work not lost. Issued lines with no ledger item are **orphans**:
back-filled + flagged for managed clients, recorded silently for unmanaged ones.

> Always pass `live_proforma_ids`. Without it settlement cannot distinguish a
> not-yet-issued proforma from a deleted one and stays conservative (never reverts),
> leaving items pending until liveness is known.

Read-only: settlement uses only morning read endpoints. Present
`report.summary()` so the user can correct the reconciliation before proposing.

## Step 1 — SCAN

Gather the period's evidence and the open ledger.

1. Fetch and condition email with **mail-evidence** (`run()` →
   fetch INBOX+Sent → assemble References-chain threads → dedup → tier T1/T2/T3 →
   drop bulk/irrelevant) and read transcripts with the transcripts reader. Both
   yield `mail_evidence.EvidenceRecord`s — email grouped in `Thread`s, transcripts
   as a flat list.
2. Flatten them into one chronologically ordered evidence list:

   ```python
   from invoicing_rules import unify
   evidence = unify(threads, transcripts)   # list[EvidenceRecord], sorted by date
   ```

   Every record keeps its `source`, `thread_id`, and first-class
   `from_`/`to`/`cc`/`subject` (email) or `participants`/`filename` (transcript) —
   `unify` does **not** collapse them. You will read `cc` directly in MATCH/INFER
   for the subcontractor signal, so do not flatten it yourself.
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
  thread the subcontractor is CC'd on**. Read `EvidenceRecord.cc` to detect this.
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

## Step 3b — AGREEMENTS from email (price overrides)

The Price Book is the default price (`price_source = price_book`). Override it ONLY when
**this batch's** evidence contains an explicit price agreement — Avigail names a price,
discount, or surcharge AND the client agrees on the thread. When you find one:

- Append an Agreement (the `agreements` input) with the agreed price, the item/scope it
  covers, and `source_ref` citing the evidence id(s) + a short quote.
- `confidence = confirmed` ONLY when **both sides** clearly agree in the thread; a
  one-sided or ambiguous mention is `confidence = detected`, which `resolve_all` treats
  as unresolved (the human prices it).
- Set the item's `price_source = negotiated` and `price_ref` = that agreement's id.

Do NOT fabricate an agreement to force a price to resolve, and do NOT carry one from
memory: if the agreement was made in a prior period and is not in this batch's evidence,
the item resolves **unresolved** and is surfaced flagged. A confirmed agreement is still
only a draft input — the gate reviews it, and its price is traceable to the cited email,
not invented (invariant 4).

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
Make clear it is a **proposal**: their edits are the authority. Then **stop and
wait for the gate.**

---

## The human gate — the hard stop for *deciding*

You do not decide what bills. The human reviews the packet, trims, edits
quantities, adds items, and writes the gate columns on the ledger:
`status_confirmed`, `decision` (`bill` | `partial` | `defer` | `hold`), and
`qty_approved`. **Never write those yourself** (invariant 3). Nothing past this
point runs without the user's explicit go-ahead in the session.

## Steps 6–7 — CREATE + RECORD (only after explicit approval)

Build the requests, then create-and-record them in one interleaved call that also
performs RECORD (step 9): for each proforma it creates, writes the returned id onto
the ledger, and persists — before creating the next one.

```python
from invoicing_rules import build_proforma_requests, create_and_record
from morning_bridge.drafts import create_proforma

requests = build_proforma_requests(
    ledger, profiles, price_book, agreements, billing_month
)
results = create_and_record(
    client, requests, ledger, ledger_csv, create_fn=create_proforma
)
```

`build_proforma_requests` selects only managed, gate-approved items
(`status_confirmed` set, `decision` in {`bill`, `partial`}, `qty_approved > 0`),
groups them per `docs/01 §5` — one proforma per end-client for agencies (each with
the zero-priced subtitle line and the end-client name as the document heading), one
plain proforma for direct clients — prices every line, and annotates partial
fixed-quotes from **`qty_approved`** (not the original proposal). It **raises**,
listing every offender, if any approved item has no resolvable price or no
`morning_client_id` — never guess a price, never half-build a batch.

`create_and_record` is the RECORD step (docs/05 step 9): it writes each proforma id
to `proforma_doc_ref` (which marks the item pending and makes CREATE idempotent;
settlement later moves the linkage to `morning_doc_ref` as the issued-invoice id) and
leaves `qty_billed_actual` empty — **only next cycle's settlement fills that, from
the *issued* document** (morning is truth; never accumulate `qty_billed_to_date` from
a proposal or an approval).

Hard limits that still hold here:
- **Proformas only.** `create_proforma` hard-locks `type=300`; it is structurally
  incapable of issuing a tax invoice. Issuance = the human converting a proforma
  inside morning, out of this system's scope.
- **Dry-run by default.** With `DRY_RUN=true` the bridge returns the payload and
  makes no network call — use it to show the user exactly what would be created
  before anything touches morning.
- **Idempotent within a cycle.** `build_proforma_requests` skips any item that
  already carries a `proforma_doc_ref` (a proforma was already created for it this
  cycle), so re-running CREATE does not duplicate. `create_and_record` also persists
  the ledger after each proforma, so an interrupted run leaves at most one created
  proforma unrecorded on disk. Still: morning has **no API delete**, so if a run is
  interrupted, glance at morning before retrying. Settlement clears
  `proforma_doc_ref`, so a reverted/re-opened item becomes billable again next cycle.
- Never call any issue / send / close / delete endpoint (the bridge has none).

## Never

- Issue, send, close, or email any document; never create anything but a Proforma.
- Set `status_confirmed` / `decision` / `qty_approved` at the gate's expense —
  those are the human's columns. (SETTLE does write `status_confirmed` from issued
  truth, and `qty_billed_actual` / `qty_billed_to_date` — that is morning's reality,
  not a proposal.)
- Accumulate `qty_billed_to_date` from anything but a settled, issued document.
- Call any morning write endpoint other than `create_proforma` (the bridge has none).
