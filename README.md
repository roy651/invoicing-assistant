# invoicing-assistant

A personal-assistant workflow that reads a freelancer's monthly correspondence
(email + call transcripts), reasons about which work items were done and how
complete they are, and prepares **draft** invoices in
[morning](https://www.morning.co.il) (Green Invoice) for review.

It is designed to run on the user's own Mac inside **Claude Cowork**, using
connectors and a small set of local skills. A human always reviews and issues;
the system never issues or sends anything itself.

## Status

Design / pre-implementation. The specification under [`docs/`](./docs) is complete
enough to implement against. Build order and acceptance gates are in
[`docs/06-build-plan.md`](./docs/06-build-plan.md).

## What's here

| Path | What it is |
| --- | --- |
| `CLAUDE.md` | Always-on context for Claude Code. Start here if you are an agent. |
| `docs/` | The specification. Read the doc named in a task before touching its code. |
| `morning-bridge/` | (to build) Hardened, drafts-only MCP wrapper for the morning API. |
| `skills/invoicing/` | (to build) The Cowork skill: the monthly reasoning + orchestration. |
| `skills/imap-fetch/` | (to build) Read-only IMAP fetch skill. |
| `sheets/` | (to build) Templates + price-list normalizers (PDF/Sheet → Price Book). |
| `fixtures/` | Local-only test data (git-ignored). Real correspondence/invoices live here. |

## Core safety properties

These are invariants, not preferences. They are restated in `CLAUDE.md` and
enforced in code where possible, not only in prompts.

1. **Drafts only.** Nothing is ever issued, finalized, or emailed by the system.
2. **The human gate is the source of authority.** The agent proposes; the user's
   confirmation is the only thing that leads to a draft.
3. **morning is the source of truth for what was billed.** The ledger reconciles
   *to* the issued documents, never the reverse.
4. **No secrets in the repo.** Credentials live in the macOS Keychain. See
   `.env.example` and `.gitignore`.

## Development setup

```bash
uv sync --group dev        # installs pre-commit into the project venv
uv run pre-commit install  # wires hooks into .git/hooks/pre-commit
```

Every commit then runs automatically:
- **gitleaks** — content-scans the diff for detected secrets (API keys, tokens,
  private keys). Blocks the commit if anything is found.
- **detect-private-key** — additional guard for PEM/SSH private key blocks.
- **ruff + ruff-format** — lint and format Python files.
- File hygiene: no large files, no merge-conflict markers, consistent newlines.

To run hooks manually against all files: `uv run pre-commit run --all-files`

To update hook versions: `uv run pre-commit autoupdate`

> **Note:** gitleaks scans file *content*, not just filenames. A real API key
> anywhere in a staged file will block the commit regardless of `.gitignore`.

## Secrets & privacy

This repo may become public. Before pushing:

- Never commit API keys, secrets, account numbers, IBANs, tax IDs, phone numbers,
  or real client/correspondence data.
- `fixtures/` is git-ignored and holds all real data. Keep it that way.
- Use `.env.example` as the template; never commit a real `.env`.
