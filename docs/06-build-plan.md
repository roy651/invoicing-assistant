# 06 — Build Plan

Bounded tasks for an implementer agent. Each task names the spec to read, its acceptance
criteria, and a recommended model. The rule of thumb: a **strong model authored these
specs; a weaker model executes bounded tasks against them.** Drop to Sonnet wherever the
schema, algorithm, and "done" are pinned (most tasks). Keep Opus for genuinely
architectural moments.

## Current phase

**Phase 1 — components.** Phase 0 preconditions all confirmed (2026-05-30). Done:
1.1–1.7 + 1.6.5. **Next: 1.6.6** (rewire 1.7 onto mail-evidence, retire imap-fetch),
then **1.8** (gate → create_proforma). 1.9 (settlement) after. Update this pointer as
tasks close.

---

## Phase 0 — Preconditions (no code)

- [ ] Confirm the morning subscription tier includes API access; generate sandbox keys.
- [ ] Confirm the mail host (Asura) allows IMAP; obtain host/port + app-password.
- [ ] Confirm agency invoice structure: one draft per end-client (assumed; matches fixtures).
- [ ] Verify the unofficial Green Invoice MCP's license permits a private/relicensed clone.

Gate: all four checked before Phase 1 write tasks begin.

---

## Phase 1 — Components against sandbox/fixtures

Order matters: data shapes → bridge → fetch → rules. Skip WhatsApp; subcontractor signal
is handled as completion evidence (CC threads), not a separate module.

| # | Task | Read | Acceptance | Model |
| --- | --- | --- | --- | --- |
| 1.1 | Sheet templates: Client Profiles, Price Book, Agreements, Ledger, Opening Balances tabs | `01` | Tabs exist with exact columns; one seeded profile per fixture client. | Sonnet |
| 1.2 | Price-list normalizers: PDF→rows and Sheet→rows → normalized Price Book | `01 §2` | 2026 Sheet + 2025 PDF both produce valid rows; ranges flagged `is_range`; versions/effective dates set. | Sonnet |
| 1.3 | morning bridge: auth + read endpoints | `03` | Token works; clients/items/documents fetch from sandbox. | Sonnet |
| 1.4 | morning bridge: create-draft (dry-run + sandbox) + double-bill guard; deny-list absent | `03` | DoD in `03`; deny tools asserted absent. | Sonnet |
| 1.5 | IMAP fetch skill (read-only, watermark, CC/thread) | `04` | DoD in `04`; write-command test fails closed. | Sonnet |
| 1.6 | Transcript folder reader → evidence shape | `04` (Transcripts) | Text files normalize to evidence records. | Sonnet |
| 1.6.5 | Portable `mail-evidence` package (fetch INBOX+Sent, References-chain threading, in-thread dedup, header tiering T1/T2/T3, injected RelevanceJudge/ContactStore, batch+watermark) | `mail-evidence-SPEC` | All 8 SPEC §8 ACs tested; import-guard fails on Google/invoicing/`billable`. | Sonnet |
| 1.7 | Invoicing rules skill: settle → scan → match → infer → price → propose | `05`, `02`, `01` | Produces a review packet on the fixtures month. | Opus then Sonnet* |
| 1.6.6 | Rewire 1.7 evidence onto mail-evidence; retire `skills/imap-fetch/` | `mail-evidence-SPEC §7` | `invoicing_rules.unify` consumes `mail_evidence` output (reconcile Address-obj vs flat-str from_/to/cc); imap-fetch deleted; suite green. | Sonnet |
| 1.8 | Rules skill: gate handoff → bridge create-draft (dry-run) + ledger record | `05`, `01 §5` | Approved items emit correct payloads; ledger updated. | Sonnet |
| 1.9 | Settlement reconciliation incl. orphans + revert + diff report | `02 §C` | Second-run scenario reconciles qty edit, deleted line, orphan. | Opus then Sonnet* |

> **1.5/1.6 migration note:** 1.6.5 migrated the imap-fetch fetch logic into
> `mail-evidence` (now INBOX+Sent, cross-folder threading) and moved `EvidenceRecord`
> ownership there (transcripts now import it from `mail_evidence.records`). Until 1.6.6,
> `skills/imap-fetch/` and `invoicing_rules.evidence` still use the legacy
> `imap_fetch.Message` — two email schemas coexist by design; 1.6.6 collapses them.

*1.7 and 1.9 carry the hardest reasoning. Draft the approach/prompt with Opus, then let
Sonnet implement and iterate against fixtures.

Gate: each task passes its row before the next that depends on it.

---

## Phase 2 — Validation on the user's Mac (go / no-go)

This is the decisive gate — does extraction work on **real** messy email?

- [ ] Install bridge + skills + connectors on the user's Mac; OAuth Drive; Keychain creds.
- [ ] Cold start: import history; declare opening balances; set epoch (`02 §A`).
- [ ] Point at **last month's real emails**; generate a review packet.
- [ ] Compare drafts (dry-run) against the invoices actually issued (`fixtures/`, `07`).

Gate: meet the accuracy targets in `07-acceptance.md`. If yes → Phase 3. If no → fix the
rules skill (1.7) before investing further. Do not wire live morning writes until this passes.

---

## Phase 3 — Productionize (only after Phase 2 passes)

- [ ] Live morning keys; first real drafts (still drafts; user issues).
- [ ] Monthly Cowork scheduled task.
- [ ] Optional: richer subcontractor reporting; transcript ingestion polish.
- [ ] Optional: cloud Routines (laptop-closed) — only if the awake-and-open constraint bites.

---

## Notes for the implementer

- Stop at acceptance; don't expand scope. Flag design questions rather than guessing.
- Keep `fixtures/` git-ignored. Never paste real PII into code, tests, or commits.
- After ~10–12 steps on a long task, post a short progress note and confirm direction.
