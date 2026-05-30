"""
morning bridge — importable Python library for the morning (Green Invoice) API.

Public surface:
  morning_bridge.reads   — account, business, client, item, document reads
  morning_bridge.client  — MorningClient, client_from_env

Restricted write surface (proforma-only):
  morning_bridge.drafts  — create_proforma (type 300 / חשבון עסקה)

The bridge is structurally incapable of issuing tax invoices (type 305) or any
other fiscal document.  Issuance is a human action in the morning dashboard.
Nothing else is defined.  issue / send / payment / close / delete do not exist.
"""
