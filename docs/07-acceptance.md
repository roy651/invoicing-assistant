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
| Item recall | billable items correctly surfaced / items that should be billed | ≥ 0.90 |
| Item precision | correctly surfaced / all surfaced (low false positives) | ≥ 0.90 |
| Grouping accuracy | items placed under the right bill_to + end_client | 100% |
| Price accuracy | line `unit_price` matches the issued invoice (or correctly flagged unresolved) | 100% on resolved; 0 silent wrong prices |
| Completion calls | `status_agent` matching reality, weighted toward not over-billing | high recall on "not complete"; false "complete" is the worst error |
| Qty proposal | within tolerance of issued qty, or flagged as judgment | informative, not exact |

### Pass condition

- Grouping and price accuracy at 100% (no silent wrong prices, no misrouted clients).
- Item precision/recall ≥ 0.90.
- **Zero** items auto-marked complete/billed without the gate (invariant, not a metric).
- The settle re-run reconciles a simulated qty edit, deleted line, and orphan, and reports them.

A near-miss on qty *fractions* is acceptable (the user sets them at the gate). A wrong
*price* or a misrouted *client* or a false *complete* is not — those are the failures that
would put bad numbers in front of a client.

## What "not passing" means

If precision/recall miss, the fix is the rules skill (task 1.7) — prompt, evidence handling,
matching — not the plumbing. Iterate against fixtures before touching the user's live morning.

## Privacy

The oracle contains real client names, amounts, and PII. It stays in git-ignored `fixtures/`.
Never copy its contents into committed docs, tests, commit messages, or model prompts that
might be logged outside the local session. Sanitize before sharing anything derived from it.
