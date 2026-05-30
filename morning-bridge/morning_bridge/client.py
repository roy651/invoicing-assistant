"""
HTTP client with in-memory JWT cache.

Auth flow (docs/03):
  POST /account/token  {"id": key_id, "secret": key_secret}
  Response body {"token": "..."} or header X-Authorization-Bearer.
  TTL ~30 min; we refresh at 25 min (TOKEN_TTL_SECONDS).

Token policy:
  Cached in memory only.  Never written to disk, env, or log.
  On 401 the cache is cleared and the request retried once with a fresh token.

Credentials:
  Sandbox: MORNING_API_KEY_ID + MORNING_API_SECRET from env / .env file.
  Production: load from macOS Keychain and pass to MorningClient() directly.

Rate limit: ~3 req/s; MIN_REQUEST_INTERVAL enforces 350 ms between calls.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

_log = logging.getLogger(__name__)

PRODUCTION_BASE = "https://api.greeninvoice.co.il/api/v1"
SANDBOX_BASE = "https://sandbox.d.greeninvoice.co.il/api/v1"

TOKEN_TTL_SECONDS: float = 25 * 60  # refresh at 25 min; actual TTL ~30 min
MIN_REQUEST_INTERVAL: float = 0.35  # 350 ms between requests (~3 req/s)


class MorningClient:
    """
    Synchronous HTTP client for the morning API.

    Pass api_id / api_secret from Keychain in production.
    Use client_from_env() for sandbox development.
    """

    def __init__(
        self,
        api_id: str,
        api_secret: str,
        *,
        sandbox: bool = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_secret = api_secret
        self._base = SANDBOX_BASE if sandbox else PRODUCTION_BASE
        self._token: str | None = None
        self._token_obtained_at: float = 0.0
        self._last_request_at: float = 0.0
        self._http = http_client or httpx.Client(timeout=30.0)
        env_label = "SANDBOX" if sandbox else "⚠ PRODUCTION"
        _log.warning("morning-bridge: %s — %s", env_label, self._base)

    # ── rate limiting ────────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_at = time.monotonic()

    # ── token management ─────────────────────────────────────────────────────

    def _fetch_token(self) -> str:
        """POST /account/token — no auth header required."""
        self._rate_limit()
        r = self._http.post(
            f"{self._base}/account/token",
            json={"id": self._api_id, "secret": self._api_secret},
        )
        r.raise_for_status()
        data = r.json()
        token: str | None = data.get("token") or r.headers.get("X-Authorization-Bearer")
        if not token:
            raise RuntimeError("No token in auth response from morning API")
        return token

    def _get_token(self) -> str:
        age = time.monotonic() - self._token_obtained_at
        if self._token and age < TOKEN_TTL_SECONDS:
            return self._token
        self._token = self._fetch_token()
        self._token_obtained_at = time.monotonic()
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_obtained_at = 0.0

    # ── HTTP primitives ──────────────────────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}",
        }

    def _do(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        self._rate_limit()
        return self._http.request(
            method,
            f"{self._base}{path}",
            headers=self._auth_headers(),
            json=body,
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        r = self._do(method, path, body)
        if r.status_code == 401:
            # Token may have expired mid-session; refresh and retry once.
            self._invalidate_token()
            r = self._do(method, path, body)
        r.raise_for_status()
        return r.json() if r.content else {}

    # ── public HTTP helpers (used by reads.py / drafts.py) ──────────────────

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body)

    # ── restricted write path (used only by drafts.py) ───────────────────────

    _WRITE_ALLOWLIST: frozenset[str] = frozenset({"/documents"})
    _DOC_TYPE_PROFORMA: int = 300  # hard-coded here too — defence in depth

    def _create(self, path: str, body: dict) -> dict:
        """
        Structural write allowlist — only /documents is reachable, and only
        with type=300 (Proforma).

        Two independent type-300 guards:
          1. drafts.create_proforma raises if the caller passes 'type' in the
             request (outer guard — prevents accidental injection upstream).
          2. HERE: reject any body whose 'type' != 300 before the network call
             (inner guard — no code path can POST a non-proforma even if called
             directly, bypassing drafts.py).

        Called exclusively by drafts.create_proforma; do not call from reads.py.
        """
        if path not in self._WRITE_ALLOWLIST:
            raise ValueError(f"Write path not in allowlist: {path!r}")
        doc_type = body.get("type")
        if doc_type != self._DOC_TYPE_PROFORMA:
            raise ValueError(
                f"client._create: body['type'] must be {self._DOC_TYPE_PROFORMA} "
                f"(Proforma) — got {doc_type!r}.  "
                "Tax invoices and other fiscal documents are structurally blocked."
            )
        return self._request("POST", path, body)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MorningClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ── factory ──────────────────────────────────────────────────────────────────


def _load_dotenv(_here: Path | None = None) -> None:
    """
    Load .env from the repo root if it exists (walks up from this file).

    _here is a private parameter for testing only — callers omit it.
    """
    from dotenv import load_dotenv

    here = (_here or Path(__file__)).resolve()
    for candidate in [here.parent, here.parent.parent, here.parent.parent.parent]:
        env_file = candidate / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return


def client_from_env(*, sandbox: bool | None = None) -> MorningClient:
    """
    Build a MorningClient from environment variables (sandbox development only).

    Reads MORNING_API_KEY_ID, MORNING_API_SECRET, MORNING_ENV.
    Loads .env from the repo root automatically if env vars are missing.

    In production, retrieve credentials from macOS Keychain and instantiate
    MorningClient(api_id, api_secret, sandbox=False) directly — never store
    live keys in .env or environment variables on the production machine.
    """
    _load_dotenv()
    api_id = os.environ.get("MORNING_API_KEY_ID", "")
    api_secret = os.environ.get("MORNING_API_SECRET", "")
    if not api_id or not api_secret:
        raise RuntimeError(
            "MORNING_API_KEY_ID and MORNING_API_SECRET are not set.\n"
            "Copy .env.example → .env and fill in your sandbox keys."
        )
    if sandbox is None:
        sandbox = os.environ.get("MORNING_ENV", "sandbox").lower() == "sandbox"
    return MorningClient(api_id, api_secret, sandbox=sandbox)
