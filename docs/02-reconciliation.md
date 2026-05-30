# 02 — Reconciliation Logic

This is the part that makes multi-month billing correct. Read `01-data-contracts.md`
first; this doc uses those terms exactly.

Principle (restated, because everything here follows from it): **the ledger reconciles
to morning, never the reverse.** morning's issued documents are truth.

---

## A. Cold start (one-time)

You cannot reliably reverse-engineer in-flight partial positions from invoices + email —
an issued line "0.3 of project X" carries no machine link to "0.7 remaining." That link
lives only in the user's head. So cold start is part automated, part declared.

1. **Import history** from morning (last ~6–12 months of issued docs) via the bridge's
   read endpoints. Seeds Client Profiles' `morning_client_id`, observed item/price
   patterns, and a record of what was already billed — so nothing historical is re-proposed.
2. **Declare opening balances** (one-time, ~30 min, user-owned). For each genuinely *open*
   `fixed_quote` item (partially billed, or deferred-and-pending), the user records:
   `item_id, bill_to, end_client, description, unit_price, total_qty, qty_billed_to_date`.
   The agent **may assist** by scanning the last month or two and proposing candidate open
   items, but the user confirms. Do not let the agent be confident about money in flight.
3. **Set the epoch** date. Email before the epoch is "already accounted for" (folded into
   opening balances). The agent reasons forward from the epoch for new billing only. This
   is what stops it re-litigating the entire inbox.

Opening balances live in a dedicated tab and are merged into the ledger on first run.

---

## B. The monthly cycle (state machine)

```
        ┌─────────────┐
        │ 1. SETTLE   │  read morning issued docs since last run; reconcile ledger to them
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 2. SCAN     │  fetch email + transcripts since watermark; load open items (always)
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 3. MATCH    │  evidence → existing item_id, or propose new item (surface uncertain)
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 4. INFER    │  status_agent + completion_evidence + confidence + qty_proposed
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 5. PROPOSE  │  review packet grouped by bill_to / end_client
        └──────┬──────┘
               ▼
        ╔═════════════╗
        ║ HUMAN GATE  ║  user sets status_confirmed, decision, qty_approved; trims/adds
        ╚══════┬══════╝
               ▼
        ┌─────────────┐
        │ 6. CREATE   │  bridge creates DRAFTS only, grouped per contract
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 7. RECORD   │  store qty_approved + draft refs. qty_billed_actual stays empty
        └─────────────┘   until NEXT cycle's SETTLE reads the issued doc back.
```

The cycle is intentionally settle-first: last month's manual edits in morning are absorbed
before this month's reasoning starts.

---

## C. Settlement (step 1, in detail)

Diff each ledger row that was drafted/expected against the actual issued morning documents.

```
for each issued_doc since last settlement:
    for each line in issued_doc:
        item = match_line_to_ledger(line)          # by morning_doc_ref / item_id / heuristic
        if item is None:
            handle_orphan(line, issued_doc)         # see below
        else:
            item.qty_billed_actual = line.quantity  # TRUTH (may differ from qty_approved)
            item.morning_doc_ref   = issued_doc.id
            item.qty_billed_to_date += item.qty_billed_actual
            item.last_billed_month  = issued_doc.month
            recompute_status(item)                  # complete if qty_billed_to_date >= total_qty

# items that were drafted last cycle but DO NOT appear in any issued doc:
for each item expected_but_absent:
    revert_to_open(item)        # draft was deleted / invoice deleted. qty_billed_to_date unchanged.
                                # work is NOT lost; item re-enters the open set, carries its evidence.
```

Edit cases this handles automatically:

| User did in morning | Settlement result |
| --- | --- |
| Changed qty 0.7 → 0.6 | `qty_billed_actual = 0.6`; remaining auto-corrects to 0.4. |
| Deleted a line | Item reverts to open; `qty_billed_to_date` unchanged; re-proposed next cycle. |
| Deleted a whole invoice | All its lines revert to open the same way. |
| Added a line manually (phone/WhatsApp call) | Orphan → back-filled (below). |

### Orphans

A line in an issued doc with no matching ledger item — typically work added by the user
directly in morning from an off-channel conversation the agent never saw.

```
handle_orphan(line, doc):
    profile = client_profile(doc.bill_to)
    if profile.managed_by_agent == false:
        record_silently(line, doc)     # expected: manual clients (e.g. PV/electric). NOT flagged.
    else:
        create_ledger_row(line, doc, status_confirmed=complete,
                          qty_billed_actual=line.quantity, note="added manually in morning")
        flag_in_diff_report(line)       # tell the user so they can confirm classification
```

This is precisely why settlement reads morning rather than trusting the gate: it is the
only way off-channel additions and manual invoices ever enter the books — which is why
skipping WhatsApp ingestion costs nothing in correctness.

### Diff report

Settlement ends by telling the user, briefly: *"since last time — you changed these N,
deleted these M, I back-filled these K orphans; ledger updated."* The user can correct the
reconciliation before new proposing starts.

Settlement uses **only read endpoints** already on the bridge whitelist. It needs no new
write capability; the drafts-only contract holds.

---

## D. Re-anchoring (no double-processing, nothing lost)

The trap: the agent consumes emails to propose an item; the draft is later deleted; next
month it must neither lose the work nor double-count it. Mechanism:

1. **Track processing per-item, not per-email.** There is no global "email processed" flag.
   Each email is linked as evidence on the item it supports (`completion_evidence`).
   "Have I seen this?" is answered by item-evidence linkage.
2. **Watermark for efficiency.** Fresh-scan only email since the last run's timestamp.
3. **Open-items carry-forward for correctness.** Every *open* ledger item is re-evaluated
   against new evidence every cycle, regardless of the watermark. A reverted (deleted-draft)
   item is naturally back in the open set and gets re-proposed, evidence intact.

Net: watermark bounds the scan; open-items carry-forward guarantees nothing falls through;
per-item evidence prevents re-counting. Reading morning back each cycle is the anchor to
reality.

---

## E. Quantity inference (step 4 guidance)

- `fixed_quote` + `defer` + agent thinks complete → `qty_proposed = total_qty - qty_billed_to_date`.
- `fixed_quote` + `partial` + in progress → `qty_proposed` = a **suggested** fraction from
  progress signals, explicitly flagged as judgment (the exact fraction is the user's call).
- `fixed_quote` + not complete + `defer` → `qty_proposed = 0` (defer).
- `unit_based` → `qty_proposed` = count of units evidenced this period; no fraction logic.

The agent never auto-marks `status_confirmed` and never auto-bills. `status_confirmed` is
the gate. This is the single most important invariant given that completion inference is the
weakest link in the system.
