"""
IMAP connection — auth + read-only SELECT.

ImapClient wraps an imapclient.IMAPClient and enforces:
  - TLS-only (ssl=True, STARTTLS not used — port 993 assumed).
  - EXAMINE (read-only SELECT) instead of SELECT so the mailbox's \\Seen
    flag is never set by the fetcher.
  - No STORE / COPY / MOVE / EXPUNGE.  These are never called here; the
    test suite asserts they are not reachable.

Credentials (in priority order):
  1. Explicit constructor args.
  2. Env vars: IMAP_HOST, IMAP_PORT (default 993), IMAP_USER,
     IMAP_APP_PASSWORD.
  3. macOS Keychain via the `keyring` library — production path.
     Call ImapClient.from_keychain(service_name) to use this path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_DEFAULT_PORT = 993
_DEFAULT_MAILBOX = "INBOX"


class ImapClient:
    """
    Thin, read-only IMAP session wrapper.

    Usage (context manager preferred):

        with ImapClient.from_env() as c:
            messages = fetch_since(c, watermark)
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        mailbox: str = _DEFAULT_MAILBOX,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._mailbox = mailbox
        self._imap: object | None = None  # imapclient.IMAPClient, lazily connected

    # ── factories ────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, *, mailbox: str = _DEFAULT_MAILBOX) -> "ImapClient":
        """
        Build from environment variables (sandbox / dev path).

        Loads .env from repo root if env vars are not yet set.
        """
        _load_dotenv()
        host = os.environ.get("IMAP_HOST", "")
        port = int(os.environ.get("IMAP_PORT", str(_DEFAULT_PORT)))
        user = os.environ.get("IMAP_USER", "")
        password = os.environ.get("IMAP_APP_PASSWORD", "")
        if not host or not user or not password:
            raise RuntimeError(
                "IMAP_HOST, IMAP_USER, and IMAP_APP_PASSWORD must be set.\n"
                "Copy .env.example → .env and fill in your IMAP credentials."
            )
        return cls(host, port, user, password, mailbox=mailbox)

    @classmethod
    def from_keychain(
        cls,
        service_name: str = "imap-fetch",
        *,
        mailbox: str = _DEFAULT_MAILBOX,
    ) -> "ImapClient":
        """
        Build from macOS Keychain (production path).

        Expects three Keychain entries under `service_name`:
          account="host"     → IMAP hostname
          account="user"     → IMAP username / email address
          account="password" → app-password (NOT the main email password)

        Store them once:
          security add-generic-password -s imap-fetch -a host     -w <host>
          security add-generic-password -s imap-fetch -a user     -w <user>
          security add-generic-password -s imap-fetch -a password -w <app-pw>
        """
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "keyring is required for Keychain access.  Install it: uv add keyring"
            ) from exc

        def _get(account: str) -> str:
            val = keyring.get_password(service_name, account)
            if not val:
                raise RuntimeError(
                    f"Keychain entry missing: service={service_name!r} "
                    f"account={account!r}"
                )
            return val

        host = _get("host")
        user = _get("user")
        password = _get("password")
        port_str = keyring.get_password(service_name, "port")
        port = int(port_str) if port_str else _DEFAULT_PORT
        return cls(host, port, user, password, mailbox=mailbox)

    # ── connection ───────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open TLS connection and authenticate.  Idempotent if already open."""
        if self._imap is not None:
            return
        try:
            import imapclient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "imapclient is required.  Install it: uv add imapclient"
            ) from exc

        server = imapclient.IMAPClient(self._host, port=self._port, ssl=True)
        server.login(self._user, self._password)
        # EXAMINE = read-only SELECT.  Crucially, this means the server
        # will never set the \Seen flag on messages we fetch.
        server.select_folder(self._mailbox, readonly=True)
        self._imap = server

    def disconnect(self) -> None:
        """Gracefully log out and close the connection."""
        if self._imap is not None:
            try:
                self._imap.logout()  # type: ignore[union-attr]
            except Exception:
                pass
            self._imap = None

    # ── raw IMAP access (read-only) ──────────────────────────────────────────

    def search(self, criteria: list) -> list[int]:
        """Return UIDs matching `criteria`.  Raises if not connected."""
        if self._imap is None:
            raise RuntimeError("Call connect() before search()")
        return self._imap.search(criteria)  # type: ignore[union-attr]

    def fetch(self, uids: list[int], parts: list[str]) -> dict:
        """Fetch message parts for `uids`.  Raises if not connected."""
        if self._imap is None:
            raise RuntimeError("Call connect() before fetch()")
        return self._imap.fetch(uids, parts)  # type: ignore[union-attr]

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()


# ── dotenv helper (mirrors morning_bridge.client._load_dotenv) ────────────────


def _load_dotenv(_here: Path | None = None) -> None:
    """
    Load .env from the repo root if it exists (walks up from this file).

    _here is a private parameter for testing only — callers omit it.
    """
    from dotenv import load_dotenv

    here = (_here or Path(__file__)).resolve()
    # Walk up: imap_fetch/ → imap-fetch/ → skills/ → repo-root/
    # Four levels because the layout is skills/imap-fetch/imap_fetch/client.py.
    for candidate in [
        here.parent,
        here.parent.parent,
        here.parent.parent.parent,
        here.parent.parent.parent.parent,
    ]:
        env_file = candidate / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return
