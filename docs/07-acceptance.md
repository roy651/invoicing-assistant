# 07 — Acceptance & Validation

How we know it works. The biggest risk is not plumbing — it is whether the agent can reason
work items and completion from messy email + transcripts. This doc makes that testable.

## The oracle

`fixtures/` holds real, **git-ignored** ground truth:

- `fixtures/invoices/` — the actually-issued invoices for a validation month (the truth the
  system must reproduce as drafts).
- `fixtures/emails/` — the email + transcripts for that month (the input).
- `fixtures/expected-ledger.csv` — a hand-built ledger snapshot for that month: the items,
  kinds, modes, prices, quantities, and bill-to/end-client the invoices imply.

The validation month should include the hard cases the fixtures already exhibit:

- an agency invoice with a **subtitle line** + a **partial fixed-quote** line (e.g. a
  scrolling-website 2nd payment) + a **unit-based** line (e.g. roll-ups ×N);
- a partial item billed at a **negotiated** price that differs from the Price Book;
- a **direct Hebrew/ILS** invoice with VAT;
- a **manual** invoice for a `managed_by_agent=false` client with no email trail.

## Component DoD

Each Phase-1 task has its own DoD in `03`/`04`/`05`/`06`. Those gate the parts.

## End-to-end validation gate (Phase 2)

Run the full cycle on the validation month's emails (dry-run bridge) and compare the
proposed drafts to `fixtures/invoices/`.

### Metrics

| Metric | Definition | Target (v1) |
| --- | --- | --- |
| Item recall | billable items correctly surfaced / items that should be billed | **≥ 0.90 — the gate** |
| Item precision | correctly surfaced / all surfaced | **informative, not gated** — the agent deliberately over-surfaces suspicious items (Avigail prunes at conversion), so precision < 1 is expected; a *very* low value flags noise to investigate |
| Grouping accuracy | items placed under the right bill_to + end_client | 100% |
| Price accuracy | line `unit_price` matches the issued invoice (or correctly flagged unresolved / zero-priced) | 100% on resolved; 0 silent wrong prices; report resolved-of-proposed count |
| Completion calls | `status_agent` matching reality | no false "complete" on billed items; but a *missed item* is worse (see below) |
| Qty proposal | within tolerance of issued qty, or flagged as judgment | informative, not exact |

### Pass condition

- **Item recall ≥ 0.90 — the gate.** A missed billable item is the costly error: Avigail
  prunes what's surfaced at conversion, but cannot bill what she never sees.
- Grouping and price accuracy at 100% (no silent wrong prices, no misrouted clients).
- Item precision is **reported, not gated** — the agent deliberately over-surfaces (Metrics).
- **Zero** items auto-marked complete/billed without the gate (invariant, not a metric).
- The settle re-run reconciles a simulated qty edit, deleted line, and orphan, and reports them.

A near-miss on qty *fractions* is acceptable (the user sets them at the gate). The worst
error is a **missed billable item** — it is silent, and Avigail cannot prune what she never
sees. A wrong *price* or a misrouted *client* is next (bad numbers in front of a client). A
false *complete* is less severe — it is caught at conversion, where Avigail reviews and sets
every row — though the agent should still not over-call completion.

## What "not passing" means

If recall misses (or precision collapses into noise), the fix is the rules skill (task 1.7) —
prompt, evidence handling, matching — not the plumbing. Iterate against fixtures before
touching the user's live morning.

## Privacy

The oracle contains real client names, amounts, and PII. It stays in git-ignored `fixtures/`.
Never copy its contents into committed docs, tests, commit messages, or model prompts that
might be logged outside the local session. Sanitize before sharing anything derived from it.
