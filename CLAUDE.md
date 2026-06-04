# CLAUDE.md

Always-on context for Claude Code. Keep this file lean — it is loaded every turn.
Heavy detail lives in `docs/`; pull in the one doc a task points to.

## Mission

Read a freelancer's monthly email + call transcripts, infer which work items were
done and how complete they are, and prepare **draft** invoices in morning
(Green Invoice) for a human to review and issue.

## Non-negotiable safety rules

1. **Drafts only.** Never issue, finalize, close, or email a document. The morning
   bridge must not expose those endpoints — enforce in code, not just prompt.
2. **The human gate is the conversion.** The agent proposes line items + quantities and
   may create **draft proformas** autonomously (type 300 — non-fiscal, deletable). The
   gate is Avigail's review at **proforma → invoice conversion** in morning, where she
   edits/prunes/prices every line. The agent NEVER issues or converts. "Never auto-bill"
   = never issue an invoice (creating a reviewable draft is not billing). *(CREATE no
   longer waits on pre-approval gate columns; wired in task 1.10.)*
3. **morning is truth.** `qty_billed_to_date` accumulates from what was *actually
   issued* (read back from morning), never from the agent's proposal or the gate.
4. **Never invent prices.** A price comes from the normalized Price Book or a logged
   Agreement, with a traceable `price_ref`. Unresolved → flag, do not guess.
5. **No secrets in the repo.** Keychain for credentials; `fixtures/` is git-ignored.

## Repo map

| Area | Code | Spec to read first |
| --- | --- | --- |
| morning bridge (MCP) | `morning-bridge/` | `docs/03-morning-bridge.md` |
| IMAP fetch skill | `skills/imap-fetch/` | `docs/04-imap-skill.md` |
| Invoicing rules skill | `skills/invoicing/` | `docs/05-rules-skill.md` |
| Price Book / sheets | `sheets/` | `docs/01-data-contracts.md` |
| Data shapes (all) | — | `docs/01-data-contracts.md` |
| Monthly logic | — | `docs/02-reconciliation.md` |

## Working agreement

- Read the spec section named in the task before writing code. Do not infer schema
  or algorithm from memory — they are pinned in `docs/`.
- One bounded component per task. Stop at the acceptance criteria in
  `docs/06-build-plan.md`; don't expand scope.
- Build and test against the morning **sandbox** first. Live keys are wired only at
  the validation phase.
- The validation oracle is `fixtures/` (last month's real invoices). "Done" for the
  end-to-end system = reproduce those from the emails. See `docs/07-acceptance.md`.

## Current phase

See `docs/06-build-plan.md` → "Current phase". Update that pointer as phases close.
