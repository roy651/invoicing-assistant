# 06 — Build Plan

Bounded tasks for an implementer agent. Each task names the spec to read, its acceptance
criteria, and a recommended model. The rule of thumb: a **strong model authored these
specs; a weaker model executes bounded tasks against them.** Drop to Sonnet wherever the
schema, algorithm, and "done" are pinned (most tasks). Keep Opus for genuinely
architectural moments.

## Current phase

**Phase 1 — components.** Phase 0 preconditions all confirmed (2026-05-30). Done:
1.1–1.9, 1.6.5, 1.6.6 (all components built). **Next: 1.10** (mail-evidence
runner/CLI), then **Phase 2** = real-fixtures go/no-go gate (`docs/07`). Update this
pointer as tasks close.

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
| 1.9 | Settlement reconciliation incl. orphans + revert + diff report; **+ CREATE idempotency** | `02 §C` | Second-run scenario reconciles qty edit, deleted line, orphan. Adds `proforma_doc_ref` column; CREATE skips items already carrying this cycle's `proforma_doc_ref` so a re-run never duplicates proformas (1.8 left this as a within-cycle gap — interim hardening only). | Opus then Sonnet* |
| 1.10 | `mail-evidence` runner/CLI (live fetch entry point) | `mail-evidence-SPEC §3.1,§5` | Drive `run()` against a real mailbox: fetch INBOX+Sent, print/export a batch, probe connection. **Owns the real batch/watermark contract** the `pipeline.run()` docstrings defer: surface a per-batch high-water timestamp (or compute it from durably-persisted records) and `commit_watermark` only after a batch is persisted, so a crash re-fetches ≤1 batch. **Also owns settlement's fetch window:** choose `fetch_issued_invoices` `from_date` from the oldest unsettled proforma, and pass `fetch_open_proformas` into `settle_ledger` (else false reverts → duplicate proformas). Re-homes the CLI/probe retired with imap-fetch in 1.6.6. Needed before Phase 2 live fetch. | Sonnet |
| 1.11 | Phase-2 **offline** fixture harness (NOT live — see scope fence) | `07`, `02 §C`, `01 §5` | **A (mail-evidence, generic):** `.eml`/`.mbox` ingestion adapter reusing `imap.py` decoders + `_assign_thread_ids` + `_raw_to_record` → byte-identical `EvidenceRecord`/threading to live; honors INBOX/Sent; passes portability guard. **B (invoicing, domain):** runnable that ingests fixtures → `unify`, loads fixture state, exposes a documented seam where the **model** writes `status_agent`/`confidence`/`qty_proposed`, runs `resolve_all`+`build_review_packet`+`settle_ledger` (fixture `live_proforma_ids`), and scores vs `fixtures/expected-ledger.csv` on the §07 metrics. Judge/Store injectable with fixture defaults. **Scope fence:** no live IMAP runner / watermark / live fetch wiring (those are 1.10). | Sonnet |

> **1.5/1.6 migration note:** 1.6.5 migrated the imap-fetch fetch logic into
> `mail-evidence` (now INBOX+Sent, cross-folder threading) and moved `EvidenceRecord`
> ownership there. 1.6.6 collapsed the two email schemas onto `mail_evidence.EvidenceRecord`
> and deleted `skills/imap-fetch/` — including its `cli.py`/`probe_connection.py`. The
> package now has the fetch engine but **no runner/CLI**; 1.10 re-homes that (gap is not on
> the critical path to 1.8/1.9, but is required before Phase 2 touches a real mailbox).

*1.7 and 1.9 carry the hardest reasoning. Draft the approach/prompt with Opus, then let
Sonnet implement and iterate against fixtures.

Gate: each task passes its row before the next that depends on it.

### Open refinements (deferred — revisit when real fixtures justify)

- **Dedup identity-first anchoring (1.6.5).** `dedup.py` matches in-thread quotes by
  content-substring + a min-token floor (achieves §6.4's protective goal). The spec
  (§3.3) also describes resolving the attribution to a sibling Message-ID / sender+date.
  Add that anchoring if Phase-2 fixtures surface coincidental long-block collisions.
- **`proforma_doc_ref` audit retention (1.9).** Implemented as a *pending marker*
  (cleared at settlement); the durable proforma→invoice trail lives in morning's
  `linkedDocumentIds`. Reviewer-accepted, but if local auditability of a reverted
  proforma is later wanted, switch to retained id + a separate `proforma_pending` flag.

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
