# 08 — Phase-2 Validation Setup (reviewer notes)

Records the changes and decisions made to prepare the Phase-2 offline harness
(task 1.11) to run against the user's real data: a one-month email export (`.mbox`,
979 messages) and the real issued invoices for the surrounding months. Written for an
independent reviewer.

The committed code is reviewable directly. The data fixtures are git-ignored (PII) and
are listed at the end for separate upload if the reviewer wants to assess the
data-dependent decisions.

The model-reasoning pass has **not** been run yet — no agent-extracted annotations or
score exist at the time of writing. This batch is only the setup.

---

## 1. Commits in scope

- **59c8f4f** — `phase2.ingest_evidence` now runs the production conditioning chain
  (`assemble → dedup → tier → condition`) before the reasoning seam, instead of going
  straight from assembly to `unify`. `discover_fixtures` requires `emails/`,
  `client_profiles.csv`, `price_book.csv`; treats `agreements.csv`, `opening_ledger.csv`,
  `open_proformas.json` as optional. The harness supplies default judge/contact-store
  implementations (empty allowlist, keep-all judge).
- **417ef5b** — offline `.mbox` reading iterates `mailbox.mbox` keys and reads each
  message via `get_bytes` rather than constructing message objects. `scripts/fetch_invoices.py`
  added: downloads issued invoices as JSON using the bridge's read endpoints only.
- **795fb6a** — settlement read-shape changes (see §3).

Not committed (git-ignored): the price-book source edit and regeneration (§4), and the
two ledger fixtures (§5).

---

## 2. Conditioning change (59c8f4f)

`ingest_evidence` previously skipped the dedup/tier/drop-bulk steps that the live
pipeline (`mail_evidence.run`) applies. On the real export, 622 of 979 messages carry a
bulk-mail header signal. Without conditioning those reach the reasoning step.

Defaults the harness injects:
- **Contact store: empty.** Consequence: no thread is classed T1 (known contact); every
  non-bulk thread is T2. Relies on the tiering invariant that an empty allowlist only
  adds T2 judgments, never drops a thread.
- **Relevance judge: keep-all.** Consequence: every T2 thread survives to reasoning;
  only T3 (all-records-bulk) threads are dropped. On this export: 213 threads / 357
  records survive, 622 threads dropped.

Effect on scoring: the keep-all judge means tiering removes only deterministic bulk, not
borderline-relevance threads. This was chosen so the harness does not silently drop a
thread the oracle expects to bill. It also means the reasoning step sees more noise than
a production judge would pass.

## 3. Settlement read-shape (795fb6a)

These were anchored previously to the bridge's *create* payload, not a real *read*
response. Verified against 12 real issued documents:

- **`linkedDocumentIds` → `linkedDocuments`.** Real read shape is a list of
  `{id, type, ...}` objects, not id strings. Match now extracts ids from those objects.
- **Orphan unit price `unitPrice` → `price`.** Real income lines use `price` (unit) and
  `amount`/`amountTotal` (line total); there is no `unitPrice` key.
- **`fetch_issued_invoices` status filter removed.** The 11 issued (non-cancelled)
  tax invoices are all `status=0`; the prior `status=[DOC_STATUS_CLOSED=1]` filter would
  have returned zero. Replaced with: fetch type 305, exclude `status==4` (cancelled).

`status==4` as "cancelled" is inferred from one real document (2293) that has `status 4`
and a linked `type 330` document; it is not cross-checked against morning API docs.
`status 0` as "issued" is inferred from the 11 documents being issued tax invoices.

The existing settle unit tests were realigned to the real read shape (helper now emits
`linkedDocuments` objects and `price`-as-unit). A test for the status filter was added.

## 4. Price book

While mapping invoice lines to price-book entries, 5 Marketing-Collateral items present in
the source PDF (`fixtures/2025PriceList.pdf`) were absent from the generated
`price_book.csv`. Root cause traced to the intermediate source sheet
(`fixtures/2026PriceList.csv`), which had 13 of the PDF's 18 Marketing rows; the
normalizer converted an already-incomplete source. Two billed invoice lines mapped to
dropped items ("One Pager – One Side" $1,300; "Clinic Poster" $1,300).

Action taken: added the 5 rows to the source CSV and renamed two brochures whose names
did not match the PDF, then re-ran the normalizer. Result: 63 → 68 entries per version.

- 2025 prices for the restored rows are taken from the PDF.
- 2026 prices for the restored rows are **derived** (≈+10%, the pattern in the rest of the
  sheet), not from a source. They are marked draft / not-in-effect; the normalizer leaves
  2026 `effective_from` blank, so version selection does not use them for current quotes.
- The fix was applied to the local CSV only. The upstream Google Sheet still has the gap.
- The normalizer's spot-check validates only items already present, so it did not flag the
  missing rows. No completeness check against the PDF was added.

## 5. Oracle and opening-ledger construction

Two fixtures were hand-built (single author, no independent check):

- **`opening_ledger.csv`** — 5 fixed-quote items still open entering May, with a
  `qty_billed_to_date` carried from prior billing.
- **`expected_ledger.csv`** — the scoring oracle: 13 items billed in May (the cancelled
  invoice 2293 and the reversal line excluded).

Construction rules used:
- **End-client** from the document-level `description` field, cross-checked against the
  zero-priced income "subtitle" lines. Empty on the one direct client (no end-client).
- **`status_agent`** by rule: fixed-quote = `complete` if cumulative qty ≥ total else
  `in_progress`; unit-based = `complete`.
- **Prices** = price-book price of the mapped item (per the user's stated policy:
  price-book is the default; an explicit email agreement overrides; when unsure use the
  price-book price). Where the billed amount differs from catalog with no in-window source
  for the difference, the oracle keeps the catalog price.
- **Item kind**: fractional qty + "Nth payment" wording → fixed-quote partial; integer
  qty / per-unit or hourly tasks → unit-based.

Some `qty_billed_to_date` values are inferred from description text:
- Pocket Folder 0.7 and APREO website 0.9 come from explicit in-line notes ("so far 0.7",
  "90% so far").
- VIP brochure 0.9 and Poster 0.8 are inferred from the May payment-stage label alone
  ("3rd & last", "completion") with **no in-window source document** for the prior amount.

Items flagged uncertain in the oracle's `notes` column: VIP brochure catalog mapping;
Poster billed below catalog ($425 vs $1,300); IVORY item/price (ILS, no USD mapping);
RoVo "additional charge" ($500, not catalog); Business Card ($400, not a standalone
catalog item). The user's position: items 3 and 4 should be resolvable from the email in
the reasoning pass, not pre-filled.

### 5a. Cold-start agreement gap (interpretation caveat)

Some price agreements that override the price book may have been negotiated in months
prior to the captured one, so they will not appear in this batch's email. Two effects:

- For catalog items, this does **not** make a line score wrong: the oracle's expected
  price is the price-book price (not the invoice amount), and the agent also defaults to
  the price-book price, so both sides agree regardless of an unseen agreement. The actual
  billed amount may differ from both (e.g. a line billed at $425 against a $1,300 catalog
  item); that difference is Avigail's gate-time amendment, which the agent is not expected
  to predict and which the metric does not test.
- For the non-catalog items (IVORY IFU, RoVo "additional charge", Business Card), the
  price exists only in an agreement. If that agreement predates the captured month, the
  agent cannot recover it. Policy (set by the user): such a line is still placed in the
  proforma but **priced at 0** — an explicit unresolved marker that surfaces the item on
  the human's table to be priced at the gate, rather than being guessed or omitted. This
  is a specification of invariant #4 ("flag, do not guess"), not a contradiction of it. In
  the score these count as identified items but are excluded from the resolved-price
  metric, so the expected symptom is a lower *resolved-line count*, not a wrong price.
  (Implementation note: a 0-priced unresolved *item* must be distinguishable from the
  0-priced end-client *subtitle* line, which `settle._is_zero_priced` treats as non-
  billable.)

Steady-state, agreements negotiated while the system runs are logged the month they are
discussed and persist. Agreements predating the system belong to the cold-start
history/opening-balance import, which is not performed for this first run. The invoice
amounts are deliberately **not** back-filled into `agreements.csv` (doing so would encode
the answer into the test).

## 6. Run model (Stage 1)

The planned first run feeds the harness an **empty** settle input (the downloaded invoice
JSONs are moved out of the settle path). Consequences:
- Settlement is a no-op; the §3 read-shape changes are exercised only by unit tests, not
  end-to-end on the real April invoices.
- Carry-forward is tested through `opening_ledger` quantities (does the agent propose the
  correct remaining fraction), not through the settle read-back path.
- Alternative not taken: feed the April invoices to settle and reconstruct pre-April
  pending state. Rejected for the first run because it requires fabricating pending
  proforma markers (the agent created no proformas in this history).

## 7. Item identity / scoring caveat

`item_precision_recall` and grouping match produced vs. expected rows by exact `item_id`.
The reasoning-pass annotations and the oracle must therefore use identical ids for the
same work. A semantic (description/client) match would not have this coupling. This was
not changed.

## 8. Changes since the first review handoff

- Scorer matches produced↔expected by **(bill_to, normalized description)**, not `item_id`
  (the model assigns its own ids to new items). Duplicate keys are surfaced as an
  informational `key_collisions` line, never silently overwritten.
- **Recall is the gate; precision is informational** (docs/07 updated): the agent
  deliberately over-surfaces suspicious items for the human to prune at conversion. Error
  hierarchy reordered — a missed item is worst; a false "complete" is least severe (caught
  at conversion).
- Price metric reports **resolved-of-proposed** so a clean score on few resolved can't
  read as a full pass.
- SKILL.md **Step 3b** added: the agent creates an Agreement from an explicit in-batch
  email price agreement (confirmed only if both sides clearly agree), else unresolved.
- Unresolved-price lines are surfaced (not omitted/guessed); the CREATE-side "price 0"
  rendering is deferred to a post-run task (does not affect the harness score).

## 9. First reasoning-pass run (result + caveats)

Ran the harness on the May corpus with a hand-authored `agent_annotations.csv` produced by
reading the threads. Result: **PASS** — grouping 13/13; price 12/12 resolved match (12 of
15 proposed resolved; 3 unresolved = IVORY IFU, RoVo additional charge, TurnCare card);
item_recall 0.92 (12/13; the miss is a "$425 payment completion" poster with no May email);
precision 0.80 (3 recall-bias extras surfaced); no false complete; no auto-bill.

What it does NOT establish, and should be weighed by the reviewer:
- **The oracle and the annotations were both authored by the same assistant.** Price and
  description-match agreement is therefore partly circular; the independent signal is
  completion-from-email and recall, not the price metric.
- **Settlement was a no-op** this run (§6) — the §3 read-shape fixes are unit-tested only.
- **Precision is not truly measured** — 0.80 only counts surfaced extras, not whether they
  were good surfacing (that needs the user's actual prune decisions).

Files to review for §9: `agent_annotations.csv` (the extraction, with per-item evidence
citations in `completion_evidence`) alongside `expected_ledger.csv`.

## 10. Not yet done

- End-to-end settlement on real invoices (Stage 2); a second validation month (would avoid
  the oracle-circularity by scoring against independent next-month invoices).

---

## Files needed to review the data-dependent parts (§4, §5, §6)

Committed code covers §2, §3, §7. The following are git-ignored and would need to be
provided to assess §4–§6:

- `fixtures/expected_ledger.csv`, `fixtures/opening_ledger.csv` — the oracle and opening state
- `fixtures/client_profiles.csv` — the two clients
- `fixtures/price_book.csv` — regenerated catalog
- `fixtures/2026PriceList.csv` — edited source sheet
- `fixtures/2025PriceList.pdf` — authoritative price source (to check the restored rows)
- `fixtures/morning_invoices_raw/*.json` — the 12 real issued documents (to check the
  oracle against source)

The raw `.mbox` (979 messages) is not needed to review this batch; its ingestion is
covered by code and tests, and no extraction has been done from it yet.
