# 04 — IMAP Fetch Skill

A small, **read-only** local skill that pulls the month's correspondence off the user's
hosted mailbox (currently Asura Hosting; assume IMAP available, verify). Chosen over
migrating mail into Gmail so the mailbox stays put and mail never leaves the Mac except
as the user already has it.

## Scope

- **Read only.** No move, delete, flag, or send. IMAP `SELECT` is read-only; no `STORE`.
- Fetches headers + bodies (text; strip HTML to text) for messages since the watermark.
- Emits a normalized structure the invoicing skill consumes; does no billing reasoning.

## Credentials

- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USER`, and an **app-password** (not the main password),
  from macOS Keychain in production.

## Fetch contract

```
fetch_since(watermark_ts) -> [Message]

Message {
  id: string              # IMAP UID + mailbox, stable for evidence linkage
  thread_id: string       # by References/In-Reply-To, else subject-normalized
  date: datetime
  from: address
  to: [address]
  cc: [address]           # needed: subcontractor↔client confirmations CC the user
  subject: string
  body_text: string       # HTML stripped to text
  attachments_meta: [ {filename, mime, size} ]   # metadata only; do not auto-open
}
```

## Watermark

- Persist the max processed `date` (or UID) locally (git-ignored `state/`).
- Next run fetches strictly after it.
- The watermark bounds the *fresh scan* only. Open-item carry-forward (re-evaluating open
  ledger items) is the invoicing skill's job, not the fetcher's — see `02-reconciliation.md §D`.

## Why CC matters

Subcontractors sometimes confirm completion **directly to the client**, with the user
CC'd. Those threads are first-class completion evidence. The fetcher must preserve `cc`
and full thread grouping so the invoicing skill can attribute a confirmation to the right
`assignee` work item. Do not drop CC-only messages.

## Transcripts

Zoom transcripts are a parallel, simpler input: a watched local folder (or Drive folder)
of text files. They are enrichment for completion/scope/price signals, not the primary
source. A thin reader normalizes them into the same evidence shape (`id`, `date`,
`body_text`, optional participants). Out of scope for the IMAP skill itself; noted here so
the evidence model stays consistent.

## Definition of done

- Connects read-only to the sandbox/test mailbox with Keychain creds.
- Returns normalized Messages since a given watermark, with CC and thread grouping intact.
- Persists/advances the watermark.
- Never issues a write IMAP command (assert in a test).
