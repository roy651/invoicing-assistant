# Handoff — June 2026 run + the turnkey push

_Written 2026-06-29 from a session in another repo. Read this first, then `CLAUDE.md`,
the project memory, and `docs/STATUS.md` + `docs/RUNBOOK.md`._

## UPDATE 2026-06-29 (turnkey BUILT + June preview produced) — read this first
The turnkey flow now exists and June ran through it end-to-end to a **dry-run preview**
(no morning write). New code (294 tests green):
- `skills/mail-evidence/mail_evidence/render.py` — `render_corpus()` conditioned evidence → `_work.txt`.
- `skills/invoicing/invoicing_rules/reason.py` — the model seam productized: `build_prompt` /
  `parse_proposals` (tolerant) / `apply_proposals` (merges onto opening ledger, never touches gate
  cols) + two backends: `ProxyReasoner` (wyckoff-style hermes proxy, DORMANT until deployed) and
  `ManualReasoner` (writes `_reason_prompt.txt`, halts via `ReasonPending`, resumes from
  `_reason_response.json` — this is how a session/me supplies the reasoning now).
- `scripts/run_month.py` — orchestrator: condition → opening-ledger (project_ledger) → settle
  (auto-skipped if no pending proformas) → reason → **dry-run preview** → `--create` (default OFF)
  for the real type-300 write. `--create` is the single write switch (forces DRY_RUN accordingly).
- `scripts/thread_index.py` — operator triage helper (one line per work thread; work/internal/noise).
- `phase2.py` refactor: `condition_corpus(emails_dir, ...)` extracted (runner + harness share it).

**Run it:** `uv run python scripts/run_month.py --month 2026-06`  (preview; writes nothing)
then `… --month 2026-06 --create` (the real type-300 write — only after preview is approved).

**June preview result:** 8 draft proformas (one per SPRIG end-client), 20 lines. Hourly website
work priced at the $130 book rate; project-fee deliverables surface at $0 `[PRICE UNRESOLVED]` for
Avigail. VDyne deferred (qty 0, carried forward). Artifacts in `fixtures/runs/2026-06/`
(`_work.txt`, `_candidates.txt`, `opening_ledger.csv`, `agent_annotations.csv`, `ledger.csv`,
`_reason_response.json`). **Awaiting Roy's review of the preview before any `--create`.**

Open quality notes: (1) conditioning kept 313 threads / ~64K tok — ~209 are newsletter/notification
noise that survived the empty ContactStore (recall-safe but token-heavy); seeding a ContactStore +
RelevanceJudge from client_profiles is the clear next refinement. (2) Sensoils did real June
brochure work but isn't in `client_profiles.csv` → can't be billed (add a profile). (3) IMDS:
Avigail explicitly declined hourly and quoted project-based — noted on that line.

## Where we are (June 2026 run — staged, not yet reasoned)
- ✅ **June mail fetched** — 1,535 msgs (ula 22, gmail 1,513) → `fixtures/runs/2026-06/emails/`, watermarks advanced to today. (`mail_evidence.runner fetch --root fixtures/runs/2026-06 --since 2026-06-01`).
- ✅ **May's issued invoices pulled** — 7 docs (types 305/320, live, read-only) → `fixtures/invoices/`.
- ✅ Creds verified working (both IMAP accounts auth; Morning live; 4 Sheets IDs set). `.venv`+`uv` ready. **`MORNING_ENV=live` + `DRY_RUN=true`** (safe).
- ⏭️ **Not done yet:** build opening ledger → condition corpus → **reason** → preview → create.

## The reframed goal: drive toward TURNKEY (a hermes-launchable skill)
Instead of doing June's reasoning by hand again, **productize it** — one flow + LLM wiring —
using June as the test case. This is STATUS.md's "main open task" (headless reasoning pass).

### Architecture decision — the reasoning can be a PLAIN LLM call (NOT CC-agentic)
The reason step is: given (conditioned corpus + opening ledger + price_book + profiles + rules)
→ produce structured proposals. That's reason-over-provided-text → structured-output — a **plain
LLM completion**, exactly like the `wyckoff` skill (deterministic engine + a single proxy LLM call
+ robust parse). **No CC agentic tool-loop is required**, so it runs on hermes' plain LLM access.
- Deterministic Python does all data-gathering (fetch/settle/ledger/condition) + assembles the prompt.
- Small inputs (price_book, profiles, agreements) go **in the prompt** — no lookup tools needed.
- Caveats to honour: (1) **recall is the gate** — a single call may miss vs a careful interactive
  read; use a strongly recall-biased prompt and likely a **2-pass (propose → completeness-critic)**.
  (2) **corpus size** ~30K tok is fine in one prompt; chunk per-client if a month is bigger.
  (3) **structured output** — same lesson as wyckoff: Opus answers in prose, so parse tolerantly
  (try JSON → embedded JSON → explicit tag) rather than strict `json.loads`.

### Proposed shape
`run_month.py --month YYYY-MM [--dry-run]` chaining: fetch → settle/pull → opening-ledger →
condition → **reason.py (plain LLM via proxy/configurable endpoint) → agent_annotations.csv** →
preview proformas → (only if not dry-run) create type-300 drafts. Hermes later launches this as a
kind-B skill; the LLM call goes through the claude-proxy (`localhost:8765`) just like wyckoff.

## June-specific note (from Avigail)
June is **irregular — several projects billed hourly** this month (she conceded to hourly work).
Surface them, set `billing_mode=hourly`, and since hours rarely appear in email, flag them at
`[UNRESOLVED]`/best-effort for **Avigail to set hours+rate at conversion**. She amends at the gate
— don't try to nail the hourly amounts.

## Next steps (the new session)
1. Build the **conditioning** (no turnkey command exists — it was assembled in-session). Use the
   `mail_evidence` pipeline (ingest → dedup → tier → drop bulk/billing-artifacts) on
   `fixtures/runs/2026-06/emails/*.mbox` → June's `_work.txt` (cf. `fixtures/runs/2026-05-live/_work.txt`).
2. Build the **opening ledger** (carry-forward; the ledger is stateful via `qty_billed_to_date` /
   `last_billed_month`, canonical in the Google Sheet `LEDGER_SHEET_ID`) — `scripts/project_ledger.py`.
3. Build **`reason.py`** — plain LLM call → proposals (recall-biased, hourly flagged) → `agent_annotations.csv`.
4. Wire **`run_month.py`** and test on June → **preview** (dry-run, like `fixtures/runs/may_capstone.py`)
   → bring the per-client preview to Roy → on his go, create the type-300 drafts → Avigail converts in morning.

## Safety (non-negotiable — see CLAUDE.md)
Drafts only; never issue/convert. `DRY_RUN=true` is the standing guard; the create step is the ONLY
write (type-300 proformas — deletable by hand in morning, no delete API). `MORNING_ENV=live`, so a
dry-run preview MUST come before any create.

## Reference points
- `fixtures/runs/may_capstone.py` — the exact May procedure (reason output → dry-run proformas → score).
- `docs/STATUS.md` — validated end-to-end (incl. live write #40003); May recall ~11–12/13.
- The `wyckoff` skill (in `roy651/hermes-skills`) — the model for "deterministic engine + plain proxy
  LLM call + tolerant structured-output parse" we're mirroring here.
