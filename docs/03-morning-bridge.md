# 03 — morning Bridge (MCP)

A hardened, importable Python library around the morning (Green Invoice) API. The bridge
is defined by its **operation whitelist**, enforced structurally — the module simply does
not define issue/send/payment/etc., so a caller physically cannot invoke them.

Built as a plain Python package (`morning-bridge/morning_bridge/`). No MCP server, no HTTP
transport, no FastAPI — the orchestrator imports the functions directly. If integration
later requires a model-in-the-loop call path, a ~20-line stdio MCP shim can wrap this same
library without changing it.

**If the MCP stdio shim is ever built**, it must expose only the functions in `reads.*` and
`create_draft` — never the generic `MorningClient.get` / `MorningClient.post` methods
(those accept an arbitrary path and can reach any endpoint). The shim must wrap named
functions, not the raw HTTP primitives.

## Auth

- JWT obtained from `POST /account/token` with `{ id, secret }` (the API key pair).
- Token cached in memory for its TTL; never written to disk.
- Credentials from macOS Keychain in production; `.env` (sandbox) only in development.

## Operation whitelist

### Allow — read

| Capability | Use |
| --- | --- |
| get/search clients | Resolve `morning_client_id` for each `bill_to`. |
| get/search items | Match the studio's catalog. |
| list/get documents | History import; **settlement** reads issued docs back (truth). |
| account / business info | Sanity / profile. |

### Allow — write (restricted)

| Capability | Constraint |
| --- | --- |
| create document **as draft** | Drafts only. morning supports persisted drafts (טיוטה) — confirmed. |

Note: morning has **no document-DELETE API endpoint**. Deleting a draft is a dashboard-only
action. The bridge has no delete function.

### Deny — structurally absent (must not exist as callable functions)

- Issue / finalize / close a draft into a real (numbered) tax invoice.
- Send document by email / share.
- Any payment, clearing, credit-card, or charge endpoint.
- Create or modify clients, items, expenses, suppliers, webhooks.
- Any delete function (no API endpoint exists for documents; client/item deletes omitted).

If the upstream MCP exposes these, **remove the tools**, don't just avoid calling them.

## Safety controls

1. **Drafts only** — belt and suspenders: even a wrong draft is a draft the user deletes,
   and morning's own dashboard is a second review surface before issuing.
2. **Sandbox first** — develop and pass tests against morning test keys. Live keys are
   wired only at the validation phase (`06`, `07`).
3. **Dry-run mode** (`DRY_RUN=true`) — create-draft returns the *exact payload it would
   send* and creates nothing. This is what the validation phase consumes.
4. **Double-bill guard** — before creating a draft, cross-check recent documents + the
   ledger (`qty_billed_to_date`, `morning_doc_ref`) so an already-billed item is not
   re-billed. Refuse + report on conflict.
5. **Keychain credentials** — never in the skill folder, repo, or plaintext.

## Create-draft input contract

The bridge accepts a normalized request and maps to morning's API fields (it owns the
exact field names). Semantic shape:

```
CreateDraftRequest {
  bill_to_client_id: string        # morning client id
  language: "en" | "he"
  currency: "USD" | "ILS"
  vat_rate: decimal
  lines: [
    { quantity, description, unit_price }   # subtitle line is quantity 1, unit_price 0
  ]
}
```

Grouping (one draft per end-client for agencies; subtitle line first) and progress
annotations are produced by the invoicing skill per `01-data-contracts.md §5`; the bridge
does not reason about billing — it validates and creates.

## Definition of done

- Token auth works against sandbox.
- All deny-list capabilities are absent from the tool list (assert in a test).
- Read endpoints return clients/items/documents.
- create-draft (dry-run) emits a correct payload for: a direct Hebrew/ILS/18% invoice, and
  an agency English/USD/0% invoice with a subtitle line + a partial line + a unit line.
- create-draft (sandbox, live mode off-by-default) persists a draft (status=Open, no invoice
  number) and it is visible in the sandbox dashboard.
- Double-bill guard refuses a duplicate.
