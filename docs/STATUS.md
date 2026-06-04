# Project Status — Invoicing Assistant

_Snapshot: 2026-06-04. Audience: future maintainers + the project owner._

## Mission (recap)

Read a freelancer's (Avigail / studio **ula**) monthly **email** (+ later transcripts),
infer which design work items were done and how complete they are, and prepare **draft
proformas** in **morning** (Green Invoice) for her to review. The human gate is the
**proforma → invoice conversion** in morning; the agent never issues, converts, or sends.

## Where we are: the whole loop works end-to-end, including against production

Every layer has been exercised on **real data**, and the create path on **live production**:

| Stage | Mechanism | Status |
| --- | --- | --- |
| **Fetch email** | `mail_evidence.runner` — multi-account IMAP (ula + Gmail), per-account watermark, export to mbox, merge by Message-ID | ✅ live, read-only |
| **Pull invoices** | `scripts/fetch_invoices.py` (read-only) | ✅ |
| **Settle** | `invoicing_rules.settle.settle_ledger` — reconcile issued invoices → ledger (3-way: settled / pending / reverted; content-match fallback) | ✅ Gate 2: 16/18 on real invoices |
| **Reason (MODEL SEAM)** | the model reads conditioned evidence → `status_agent` / `qty_proposed` / new items | ✅ supervised (see Gaps) |
| **Price** | `invoicing_rules.pricing.resolve_all` — Price Book / logged Agreement; unresolved → 0 with marker | ✅ |
| **Propose + CREATE** | `invoicing_rules.handoff` — proforma-as-gate: draft proformas from the agent's own proposal | ✅ |
| **Real production write** | `morning_bridge.drafts.create_proforma` (type-300 hard-lock) | ✅ proforma **#40003** created in live morning (then deleted) |
| **Oracle / scoring** | `scripts/project_ledger.py` (mechanical invoice→ledger) + `invoicing_rules.phase2` scorer | ✅ |

### Validation results
- **Blind March 2026** (reasoned from email *before* seeing the invoice; mechanical oracle):
  true recall **~0.83** with February carry-forward. Misses were all explainable
  (recurring retainer, pre-window carry-forward) — not reasoning failures.
- **May 2026** (live both-account pull, 1,538 messages → 106 work threads): reproduces the
  oracle, true recall **~11–12 / 13**. Single genuine miss = the `$425 Poster` catch-up
  (billed with no email evidence).
- **Gate 2 settlement** on real invoices: 16/18 settled, 3-way correct, no cross-match.
- **Production proforma write**: validated and reverted.

### Key design decisions that held up
- **Drafts only; conversion is the gate** (CLAUDE.md invariant 2). The agent creates draft
  proformas autonomously; Avigail edits/prices/prunes at conversion.
- **Mechanical oracle** (`project_ledger.py`) de-circularizes scoring — it even caught two
  price errors in the hand-authored oracle.
- **Recall is the gate, precision is informational** — the agent over-surfaces; Avigail prunes.
- **`DRY_RUN=true`** is the standing guardrail; production writes require a deliberate flip.
- **Multi-account is necessary**: ula `Sent` (outbound) is unique to the domain, and ~4
  May work threads arrived **directly on Gmail** — single-account would miss them.

## Gaps & deferred (known, with the evidence behind them)

1. **The MODEL SEAM is supervised, not autonomous.** Reasoning over the conditioned corpus is
   done by the model in a session, not a headless job. Productizing it (a scripted reasoning
   pass) is the main step between "supervised monthly run" and "Avigail runs it herself."
2. **Zoom transcripts (deferred).** Some briefs/approvals happen on calls, not email — the
   `$425 Poster` and the Ostial Flyer both traced to off-email. Email-only reasoning
   structurally can't catch these. The transcript source is designed for but not wired.
3. **Learning / learned-facts layer (deferred).** Name-aliasing (Verge = Ostial = RoVo;
   RMX = RhythMedix), per-client price overrides, and a monthly gate-feedback loop should
   live in a persisted store. Today the model re-derives these each run.
4. **Recurring retainers have no source.** Items like "Miracor Annual Hosting / maintenance"
   are billed with no email — they need a contract/ledger source. Also: Miracor isn't in
   `client_profiles.csv`, so it currently settles as pending+orphan (add the profile).
5. **Settlement content-matcher** doesn't use `end_client` to disambiguate same-description
   lines across an agency's invoices (latent — aggregate settlement stays correct because the
   primary `linkedDocuments` path matches agent-created proformas by id; only the fallback
   is affected). Matcher polish is low priority (it's a *test scaffold* — see below).
6. **The scorer's matcher is a testing scaffold, not production logic.** During validation the
   model is the real matcher; the token-overlap matcher only needs to be "good enough" to flag
   discrepancies. Its real future role is the online ledger/learning loop.
7. **WhatsApp source (deferred).** Some interactions/agreements happen on WhatsApp.
8. **Single supervised production run only.** The create path is proven, but we have not yet
   run an unsupervised end-to-end month.

## Ideas for down the road
- **Headless reasoning pass** (Agent SDK) so a month runs without a human in the loop, with
  the human only at morning's conversion gate.
- **Learned-facts store** keyed by client: aliases, price overrides, "this item recurs monthly,"
  fed by each month's gate edits (the score becomes a gradient, not just a grade).
- **Multi-modal evidence**: fold Zoom transcripts + WhatsApp into the same `EvidenceRecord`
  stream (the unify layer already exists for transcripts).
- **Confidence-tiered proformas**: high-confidence lines priced, low-confidence surfaced at 0
  for Avigail — already partially done via the unresolved-price marker.
- **Settlement provenance**: keep `end_client` in the content matcher; retain proforma→invoice
  links for audit beyond morning's own `linkedDocuments`.

## Next checkpoint
**End of June 2026** — run the full process on June's mail and see if it produces June's draft
proformas correctly. First couple of runs will be **supervised together**. Owner may return
earlier to build the **Zoom** and **learning** layers.

See `docs/RUNBOOK.md` for how to operate a monthly run.
