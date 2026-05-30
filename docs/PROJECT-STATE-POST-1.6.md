# PROJECT-STATE — invoicing-assistant

Handoff brief. Paste this as the first message of a new conversation to continue
without carrying the full deliberation transcript. **The repo is the real memory**
(`docs/` + `CLAUDE.md` hold the durable spec); this doc is the bridge plus the
decisions made in discussion that aren't yet committed.

Repo: `github.com/roy651/invoicing-assistant` (public). No secrets in this doc.

---

## 1. What this is

An assistant that reads a freelance designer's (Avigail) monthly correspondence
(email + Zoom transcripts), reasons about which work items were done and how
complete they are, and prepares **draft** billing documents in **morning**
(Green Invoice) for her to review and issue. It assists; a human always reviews
and issues. Runs on her Mac in **Claude Cowork** (paid plan; runs while Mac awake +
app open). She bills primarily through an agency (SPRIG) with per-end-client
invoices, plus some direct clients. Work spans months (defer vs. partial billing).

A **second assistant** (daily task-list manager) is planned for her later and will
reuse the mail-processing layer — see §7 (transportability).

## 2. Non-negotiable invariants

1. **Proforma-only.** The system creates morning **Proformas (type 300)** as review
   artifacts — never tax invoices. Issuing is the human converting a proforma to an
   invoice in morning, out of automated scope. Enforced in code (see §5 bridge).
2. **Human gate decides.** The agent proposes line items + quantities; nothing bills
   without explicit approval. Never auto-bill, never set `status_confirmed`.
3. **morning is truth.** `qty_billed_to_date` accumulates only from
   `qty_billed_actual`, read back from issued documents at settlement — never from
   the proposal or the gate approval.
4. **Never invent a price or item.** Prices come only from the Price Book or a
   confirmed Agreement, with a traceable `price_ref`. Unresolved (e.g. unresolved
   range) → flag at the gate, don't guess.
5. **Every proposed line cites its evidence** (email/transcript ids). No evidence →
   no line.
6. **No secrets in the repo.** Keychain in prod, `.env` (sandbox/dev) only;
   `fixtures/` git-ignored.

## 3. Architecture

- **Runtime:** Claude Cowork on Avigail's Mac. State in her Google Drive (Sheets) +
  local files. Deterministic cycle; Claude performs the judgment steps; testable
  Python helpers do the mechanical work. (Decision: deterministic orchestrator now;
  a thin MCP stdio shim can wrap the library later if a model-in-the-loop path is
  ever needed — never an HTTP/FastAPI server.)
- **Monthly cycle:** SETTLE → SCAN → MATCH → INFER → PRICE → PROPOSE → [human gate] →
  CREATE proformas → RECORD. Settle-first so last month's manual edits in morning are
  absorbed before new reasoning.
- **State (Google Sheets, user-editable):** ClientProfiles, Agreements, Ledger,
  OpeningBalances, Config (epoch). **Price Book is a local file** (`data/price_book.csv`,
  git-ignored) — derived reference data, read from disk, not a Sheet.

## 4. morning / Green Invoice facts (hard-won)

- `POST /documents` **issues** — there is no "save as draft" for invoices.
  `signed:false` does NOT prevent issuance. `/documents/preview` returns only an
  ephemeral base64 render.
- Document types by number series: **Proforma = 300** (series 40001+, non-fiscal,
  non-reported, deletable, convertible to an invoice). **Tax Invoice = 305** (series
  50001+ in sandbox; Avigail's live account uses her own 2XXX series).
- Therefore the system creates **Proformas**; the human converts approved ones to
  invoices (= issuance). morning links them via `linkedDocumentIds`.
- Auth: JWT from `POST /account/token` {id, secret}, ~30 min TTL. Bases:
  sandbox `sandbox.d.greeninvoice.co.il/api/v1`, prod `api.greeninvoice.co.il/api/v1`.
- No document-DELETE endpoint (issued docs can't be deleted via API; proformas
  deleted in dashboard).

## 5. Build status — done & verified (tasks 1.1–1.6)

All Python, `uv`/`pyproject`, pre-commit (gitleaks + `detect-private-key` + ruff),
`.gitleaks.toml` with a morning-key rule. ~98 tests green. Library-first throughout.

- **`sheets/`** — schema, idempotent `setup.py` (gspread), and the price-list
  normalizer. Price Book versioned (2025 **active** / 2026 **draft**, draft not
  selectable for current quotes); ranges flagged `is_range` and **never
  auto-resolved**; `price_id` is a contract = `{version}-{category}-{item}` and must
  equal ledger `price_ref`. Real prices → git-ignored `data/`, committed seed stays
  placeholder.
- **`morning-bridge/`** — COMPLETE and safety-locked. Importable Python library.
  **Proforma-only:** `create_proforma` hard-codes `type=300`, raises on any `type`
  in the request, AND a second independent guard in `client._create` rejects any
  non-300 body before the network. Verified in sandbox (created proforma #40001,
  type 300). Reads: clients/items/documents/account. Generic `client.get/post` exist
  but are private-by-convention; if the MCP shim is ever built it must wrap named
  functions only.
- **`skills/imap-fetch/`** — read-only (examine mode; write-command ban tested),
  live-verified against Asura. Emits `Message`(id, thread_id, date, **from_/to/cc**,
  subject, body_text, attachments_meta). Watermark in git-ignored `state/`.
  **Currently INBOX only — see §6 (needs +Sent).**
- **`skills/transcripts/`** — emits `EvidenceRecord`(id, thread_id, source, date,
  participants, body_text, filename). Handles .txt/.vtt/.md, VTT speaker attribution,
  date from filename→VTT header→mtime.

### Ledger schema (the core, in `docs/01`)
Per work item: identity (`item_id`, `bill_to`, `end_client`, `description`,
`assignee`=self|subcontractor, `item_kind`=fixed_quote|unit_based,
`billing_mode`=defer|partial); pricing (`unit_price`, `price_source`, `price_ref`);
progress (`total_qty`, `qty_billed_to_date`, `last_billed_month`); agent read
(`status_agent`, `completion_evidence`, `confidence`, `qty_proposed`); gate + truth
(`status_confirmed`, `decision`, `qty_approved`, `qty_billed_actual`, `morning_doc_ref`,
`+ proforma_doc_ref` at 1.9). Three-stage qty: proposed → approved → **billed_actual
(only this accumulates)**.

### Reconciliation model (`docs/02`)
Settle-first; morning issued docs = truth; cold-start = history import + one-time
opening balances + epoch date; per-item evidence linkage (not a global email flag) +
watermark for scan bound + open-item carry-forward for correctness; orphans (manual
morning additions) back-filled; `managed_by_agent=false` clients (e.g. the
manually-created electric-company invoices) never proposed, never flagged as orphans.

## 6. Open decisions made in discussion (NOT yet in repo)

These came out of the mail-handling discussion and must be built/committed:

1. **Tiered evidence conditioning, allowlist never hard-drops.**
   - **Tier 1** known contacts → full evidence.
   - **Tier 2** unknown-but-human (no bulk-mail headers) → an **LLM relevance pass
     auto-promotes** confirmed billable-work senders into a **Contacts** sheet
     (`role=other`, `source=auto`, logged). No manual gate. (Polish: age out unused
     auto-added contacts.)
   - **Tier 3** bulk/marketing → dropped, but ONLY on bulk-mail signals
     (List-Unsubscribe, no-reply, bulk Precedence) — never on allowlist absence.
   This inverts the failure mode: a stale list costs a few extra Tier-2 items to
   judge, never a missed invoice.
2. **Contacts sheet** (separate from ClientProfiles): `email`/`domain`,
   `role`=client|subcontractor|agency-manager|other, `source`=auto|manual,
   optional `bill_to` link. `role` drives the subcontractor-completion logic.
3. **Fetch INBOX + Sent.** Her sent mail often holds completion statements ("final
   logo attached, invoice to follow"). Small change to the IMAP skill; confirm
   Asura's Sent folder name.
4. **No quote-stripping.** Forwards / "added you to an existing thread" mean quoted
   bodies are sometimes the ONLY evidence. Keep full bodies. Dedup ONLY quoted blocks
   that have an in-thread origin (a real reply-chain repeat); preserve forwarded /
   external quoted history.
5. **Thread assembly.** Group by `thread_id`, chronological; one thread = one
   evidence unit. More legible to the reasoning and cleaner for the CC logic.
6. **Unified evidence must preserve email `from_/to/cc`.** The transcript
   `EvidenceRecord` has no `cc`; unifying email into it must NOT collapse from/to/cc
   into `participants` — the subcontractor-CC completion signal depends on it.
7. **Large-delta guard.** Fetch accepts a bounded window + cap; on cold-start / long
   gaps, process in batches with checkpoints rather than one giant pull/pass.

## 7. Transportability (decide boundary before building 1.6.5)

The generic mail engine — IMAP fetch (INBOX+Sent), tiering, thread assembly, the
`EvidenceRecord` shape — should be a **standalone installable package** (e.g.
`mail-evidence`, its own repo) shared by invoicing-assistant AND the future
task-management assistant. Invoicing-specific bits (the "is this *billable*"
judgment, Contacts↔`bill_to` linkage) stay in invoicing-assistant. Build it portable
from the start; `EvidenceRecord` + fetch/assemble are its public API.

## 8. Task backlog

- **1.6.5 — shared `mail-evidence` package** (the conditioning stage between fetch
  and reasoning): tiering + LLM auto-promote, INBOX+Sent, thread assembly, in-thread
  dedup (no quote-strip), large-delta guard, unified `EvidenceRecord` with from_/to/cc.
  Spec to be written (portable per §7). Independent — parallel to 1.7.
- **1.7 — invoicing rules skill (reasoning core), IN FLIGHT, Opus.** SCAN→MATCH→
  INFER→PRICE→PROPOSE → review packet. SKILL.md (deterministic cycle + rules) +
  testable helpers (evidence unification preserving cc; state loaders; price
  resolution with version selection + range flagging; review-packet builder). Stop at
  the packet (no create, no settle). Validate against `fixtures/expected-ledger.csv`:
  grouping 100%, price 100% on resolved, item precision/recall ≥0.90, zero false
  "complete", zero auto-bill. Build/dev on synthetic samples; real-fixtures run is
  the Phase-2 gate.
- **1.8 — gate → `create_proforma` handoff + ledger write.**
- **1.9 — settlement:** read issued invoices back, reconcile qty edits / deleted
  lines / deleted invoices / orphans, diff report; match issued invoice → ledger via
  `linkedDocumentIds` (primary) + content (fallback); add `proforma_doc_ref`.
- **Phase 2 — validation on real fixtures (go/no-go).** Then live cutover with
  guardrails: live key in Keychain only, active-env startup log, re-verify base URLs.

## 9. Fixtures (Phase-2 input — Avigail's task, parallel)

Assemble via **targeted export in her mail client** (search the agency + its
end-client senders + CC'd subcontractor threads across ~Jan–Mar; export to
`fixtures/emails/`, matching invoices to `fixtures/invoices/`). Pull 2–3 months so
multi-month partials' history is visible (the agent must SEE a partial reach 0.4
before reasoning "0.3 more → 0.7 so far"). `fixtures/expected-ledger.csv` already
drafted from the March invoices. `fixtures/` is git-ignored.

## 10. Working rhythm

Opus authors specs + the reasoning core (1.7) + reviews; Sonnet executes bounded
tasks. `CLAUDE.md` auto-loads in Claude Code. Review each commit against its
acceptance criteria; never relax an invariant to make a check pass. Supervision has
been: read the actual diff (not the summary) on safety-critical tasks — fetch the
commit URL the user provides.

## 11. Immediate next actions

1. Launch / continue **1.7** (Opus prompt already issued).
2. Write the **1.6.5 spec as the portable `mail-evidence` package** (§6 + §7).
3. Avigail assembles **fixtures** (§9) in parallel.
