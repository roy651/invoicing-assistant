# Operator Runbook — running a monthly invoicing pass

How to run the invoicing assistant for one month. **The first couple of runs are supervised
together** (the reasoning step is done by the model in a session); this runbook is the target
manual procedure Avigail grows into.

> **Golden safety rules** (see `CLAUDE.md` for the full list):
> 1. **Drafts only.** The agent creates type-300 proformas. It NEVER issues, converts, or sends.
> 2. **The gate is the conversion.** Avigail reviews/edits/prices/prunes every line when she
>    converts a proforma → invoice in morning. That is the only place real billing happens.
> 3. **`DRY_RUN=true` is the default.** A real morning write requires a deliberate flip to
>    `DRY_RUN=false`, and only after a dry-run preview.

---

## 0. One-time setup

**Dependencies:** `uv sync` (installs `imapclient`, `keyring`, etc.).

**`.env`** at the repo root (git-ignored — never commit it):

```
# --- morning (Green Invoice) ---
MORNING_ENV=sandbox            # sandbox for testing | live for production
MORNING_API_KEY_ID=...
MORNING_API_SECRET=...
DRY_RUN=true                   # standing guardrail — keep true except during a supervised create

# --- IMAP: two accounts, namespaced ---
IMAP_ACCOUNTS=ula,gmail
IMAP_ULA_HOST=mail.ula.co.il
IMAP_ULA_PORT=993
IMAP_ULA_USER=avigail@ula.co.il
IMAP_ULA_APP_PASSWORD=...
IMAP_ULA_SENT=Sent                       # ula inbound is empty (POP-pulled to Gmail); we want Sent
IMAP_GMAIL_HOST=imap.gmail.com           # NOT mail.gmail.com
IMAP_GMAIL_PORT=993
IMAP_GMAIL_USER=avigail.studio@gmail.com
IMAP_GMAIL_APP_PASSWORD=...               # Gmail App Password (2FA + IMAP enabled), not a service account
IMAP_GMAIL_SENT=[Gmail]/Sent Mail
```

For **production**, prefer morning keys in the macOS **Keychain** over `.env` (see the bridge's
`client.py`). Both accounts are needed: ula holds domain-sent mail; Gmail holds inbound +
gmail-direct work mail.

Set once per shell:
```
export PP="skills/invoicing:skills/mail-evidence:morning-bridge:skills/transcripts"
export M=2026-06            # the month you are billing
export RUN=fixtures/runs/$M # working dir for this run
```

---

## 1. Fetch the month's email (read-only, both accounts)

```
PYTHONPATH=$PP uv run python -m mail_evidence.runner probe          # verify both accounts connect
PYTHONPATH=$PP uv run python -m mail_evidence.runner fetch --root $RUN --since 2026-06-01 --max 2000
```
Exports `$RUN/emails/<account>_<folder>.mbox` and advances a per-account watermark. Re-running
is safe (idempotent by Message-ID). Read-only — no IMAP write is ever issued.

## 2. Settle last month's issued invoices (updates the ledger baseline)

Pull the previously-issued invoices as JSON, then settle them into the ledger so
`qty_billed_to_date` reflects what morning ACTUALLY issued:
```
uv run python scripts/fetch_invoices.py --from 2026-05-01 --to 2026-05-31 --production
```
Settlement runs inside the harness (step 4) and via `settle_ledger`; it reconciles
proforma→invoice (by `linkedDocuments`, content-match fallback) and is 3-way:
settled / still-pending / reverted.

## 3. Build the opening ledger (carry-forward)

Collapse the prior month's invoices into an opening ledger so in-flight items
(partials) carry into this month:
```
uv run python scripts/project_ledger.py --month 2026-05 --invoices fixtures/invoices \
    --as-opening --out $RUN/opening_ledger.csv
```

## 4. Reason — the MODEL SEAM (supervised)

This is the step the model performs in a Claude session: read the conditioned corpus
(`ingest → dedup → tier → drop bulk/billing-artifacts`), identify billable work per
`(bill_to, end_client)`, and write proposals (`status_agent`, `qty_proposed`, new items)
to `$RUN/agent_annotations.csv`. Recall-biased — surface anything plausibly billable;
Avigail prunes at the gate. (Productizing this into a headless pass is the main open task —
see `docs/STATUS.md`.)

To inspect the corpus the model will read:
```
# (helper, see fixtures/runs/2026-05-live/_work.txt for the shape) — conditioned work threads
```

## 5. Preview the proformas (DRY-RUN — nothing is written)

With `DRY_RUN=true`, build the proformas and print the exact payloads:
```
DRY_RUN=true PYTHONPATH=$PP uv run python - <<'PY'
from invoicing_rules.handoff import build_proforma_requests, create_and_record
from invoicing_rules.state import load_client_profiles, load_price_book, load_agreements, load_ledger
# load profiles/price_book/agreements + the ledger with the model's proposals, then:
# reqs = build_proforma_requests(ledger, profiles, price_book, agreements, "2026-06")
# results = create_and_record(None, reqs, ledger, "<out>.csv")  # dry-run prints payloads
PY
```
Review: correct end-client grouping, currency/VAT, partial annotations, and any
`[PRICE UNRESOLVED]` lines (surfaced at 0 for Avigail to price).

## 6. Create the draft proformas (supervised — the only write step)

Only when the preview looks right, and as a deliberate, supervised action:
```
MORNING_ENV=live DRY_RUN=false PYTHONPATH=$PP uv run python <create script>
```
This POSTs **type-300 drafts** to morning. The bridge is structurally incapable of creating
a fiscal document. Record the returned proforma ids/numbers.

## 7. Avigail reviews + converts in morning

In morning, Avigail opens each draft proforma, edits/prices/prunes lines, and **converts the
ones she approves into invoices**. That conversion is the real billing event. The agent does
nothing here. Next month's step 2 settles whatever she issued.

---

## Caveats the operator must know
- **Off-email work is missed.** Briefs/approvals made on **Zoom or WhatsApp** won't appear in
  email — those items won't be proposed (a known gap; transcript support is deferred).
- **Recurring retainers** (e.g. annual hosting) aren't in email and won't be proposed — add
  them manually until a contract source exists.
- **The model over-surfaces on purpose** (recall-bias). Expect a few speculative lines; prune
  them at the gate. A missing item is worse than an extra one.
- **morning has no delete API.** Test/unwanted drafts are deleted by hand in the morning UI.

## Quick troubleshooting
- `probe` DNS error on Gmail → host must be `imap.gmail.com` (not `mail.gmail.com`).
- Gmail "cannot select Sent" → folder is `[Gmail]/Sent Mail`.
- Settlement leaves an item pending+orphan → its client isn't in `client_profiles.csv`.
- A proforma wasn't created → the item needs `status_agent ∈ {in_progress, complete}` and
  `qty_proposed > 0` (proforma-as-gate selection).
