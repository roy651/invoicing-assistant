"""
imap-fetch — Task 1.5.

Read-only IMAP fetch skill.  Pulls the month's correspondence off the user's
hosted mailbox and normalises it into Message records for the invoicing skill.

Public API:
  fetch_since(client, watermark) -> list[Message]
  ImapClient                     connection + auth
  Message                        normalised email record
  load_watermark / save_watermark watermark persistence

Credentials (in order of precedence):
  1. Passed directly to ImapClient(host, port, user, password).
  2. Env vars: IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_APP_PASSWORD.
  3. macOS Keychain — see ImapClient.from_keychain() (production path).

Safety:
  - Read-only.  IMAP SELECT is issued in read-only mode (examine mode).
    No STORE, COPY, MOVE, EXPUNGE, DELETE, or FLAG command is ever issued.
  - The write-command ban is asserted in tests.
"""

from imap_fetch.fetch import Message, fetch_since
from imap_fetch.watermark import load_watermark, save_watermark
from imap_fetch.client import ImapClient

__all__ = [
    "ImapClient",
    "Message",
    "fetch_since",
    "load_watermark",
    "save_watermark",
]
