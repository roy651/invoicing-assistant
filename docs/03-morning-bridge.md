# 03 — morning Bridge

A hardened, importable Python library around the morning (Green Invoice) API. The bridge
is defined by its **operation whitelist**, enforced structurally — the module simply does
not define issue/send/payment/etc., so a caller physically cannot invoke them.

Built as a plain Python package (`morning-bridge/morning_bridge/`). No MCP server, no HTTP
transport, no FastAPI — the orchestrator imports the functions directly. If integration
later requires a model-in-the-loop call path, a ~20-line stdio MCP shim can wrap this same
library without changing it.

**If the MCP stdio shim is ever built**, it must expose only the functions in `reads.*` and
`create_proforma` — never the generic `MorningClient.get` / `MorningClient.post` methods
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

### Allow — write (proforma only)

| Capability | Constraint |
| --- | --- |
| `create_proforma` | Creates **type 300 (Proforma / חשבון עסקה)** only. Type is hard-coded; passing any other type raises. Non-fiscal, deletable, own series (40001+). |

**Why Proforma and not Tax Invoice (305):**
`POST /documents` with `type=305` issues a real fiscal document immediately — there is no
"save as draft" mode for invoices in morning's API. `signed=false` does not prevent
issuance. The review artifact is a Proforma (type 300): non-fiscal, non-reported, deletable,
and convertible to a real invoice by the human in morning when they are satisfied. That
conversion — clicking "issue" in the dashboard — is the issuance step and is **explicitly
out of this system's automated scope**.

Note: morning has **no document-DELETE API endpoint**. Deleting a proforma is a
dashboard-only action. The bridge has no delete function.

### Deny — structurally absent (must not exist as callable functions)

- Issue / finalize / close a proforma or any document into a real (numbered) tax invoice.
- Send document by email / share.
- Any payment, clearing, credit-card, or charge endpoint.
- Create or modify clients, items, expenses, suppliers, webhooks.
- Any delete function (no API endpoint exists for documents; client/item deletes omitted).

If the upstream MCP exposes these, **remove the tools**, don't just avoid calling them.

## Safety controls

1. **Proforma-only** — `create_proforma` hard-codes `type=300` and raises at runtime if
   the caller attempts to inject a different type. The bridge is physically incapable of
   creating type-305 or any other fiscal document.
2. **Sandbox first** — develop and pass tests against morning test keys. Live keys are
   wired only at the validation phase (`06`, `07`).
3. **Dry-run mode** (`DRY_RUN=true`) — `create_proforma` returns the *exact payload it
   would send* and creates nothing. This is what the validation phase consumes.
4. **Double-bill guard** — before creating a proforma, cross-check existing proformas for
   the same client. Returns a soft warning (not a hard block) if descriptions match —
   recurring unit_based items bill identically each month. Authoritative dedup runs at
   settlement via `morning_doc_ref` + `qty_billed_to_date`.
5. **Keychain credentials** — never in the skill folder, repo, or plaintext.

## Create-proforma input contract

The bridge accepts a normalized request and maps to morning's API fields (it owns the
exact field names). Semantic shape:

```
CreateProformaRequest {
  bill_to_client_id: string        # morning client id
  language: "en" | "he"
  currency: "USD" | "ILS"
  vat_rate: decimal
  lines: [
    { quantity, description, unit_price }   # subtitle line is quantity 1, unit_price 0
  ]
  # NOTE: 'type' must NOT be included — bridge hard-codes 300 and raises if present
}
```

Grouping (one proforma per end-client for agencies; subtitle line first) and progress
annotations are produced by the invoicing skill per `01-data-contracts.md §5`; the bridge
does not reason about billing — it validates and creates.

## Definition of done

- Token auth works against sandbox.
- All deny-list capabilities are absent from the tool list (assert in a test).
- Read endpoints return clients/items/documents.
- `create_proforma` (dry-run) emits a correct payload for: a direct Hebrew/ILS/18%
  proforma, and an agency English/USD/0% proforma with a subtitle line + a partial line
  + a unit line.
- `create_proforma` (sandbox) creates a type-300 document visible in the sandbox dashboard
  in the 40001+ series. The human confirms it appears as a proforma (חשבון עסקה), not an
  invoice.
- Double-bill guard surfaces a warning on description match (not a hard block).
