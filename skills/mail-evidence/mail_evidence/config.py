"""
Fetch configuration for mail-evidence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# TODO(fact §0.4): confirm Asura's actual Sent folder name before enabling live fetch.
# The folder name is mailbox-specific — query via LIST or check webmail settings.
_SENT_FOLDER_TODO = "Sent"


@dataclass
class ImapAccount:
    """One IMAP mailbox to fetch from. A freelancer may have several (e.g. a custom
    domain whose inbound is POP-pulled into Gmail — so received mail lives in Gmail
    while sent mail stays on the domain). The runner fetches each and merges into one
    corpus; cross-account duplicates collapse by Message-ID at ingest."""

    name: str
    host: str
    port: int
    user: str
    password: str
    inbox_folder: str = "INBOX"
    sent_folder: str = "Sent"

    @property
    def folders(self) -> list[str]:
        return [self.inbox_folder, self.sent_folder]


def load_imap_accounts() -> list[ImapAccount]:
    """Resolve IMAP accounts from the environment.

    Multi-account: `IMAP_ACCOUNTS=ula,gmail` + per-account `IMAP_<NAME>_HOST` /
    `_PORT` / `_USER` / `_APP_PASSWORD` (and optional `_INBOX` / `_SENT` folder
    overrides). Falls back to a single legacy `IMAP_HOST/PORT/USER/APP_PASSWORD`
    account named "default" when `IMAP_ACCOUNTS` is unset.
    """
    from mail_evidence.fetch.imap import _load_dotenv

    _load_dotenv()
    names = [
        n.strip() for n in os.environ.get("IMAP_ACCOUNTS", "").split(",") if n.strip()
    ]
    if not names:
        host = os.environ.get("IMAP_HOST", "")
        if not host:
            return []
        return [
            ImapAccount(
                "default",
                host,
                int(os.environ.get("IMAP_PORT", "993")),
                os.environ.get("IMAP_USER", ""),
                os.environ.get("IMAP_APP_PASSWORD", ""),
            )
        ]
    accounts: list[ImapAccount] = []
    for name in names:
        p = f"IMAP_{name.upper()}_"
        accounts.append(
            ImapAccount(
                name=name,
                host=os.environ.get(p + "HOST", ""),
                port=int(os.environ.get(p + "PORT", "993")),
                user=os.environ.get(p + "USER", ""),
                password=os.environ.get(p + "APP_PASSWORD", ""),
                inbox_folder=os.environ.get(p + "INBOX", "INBOX"),
                sent_folder=os.environ.get(p + "SENT", "Sent"),
            )
        )
    return accounts


@dataclass
class FetchConfig:
    """
    Configuration for the IMAP fetch pipeline.

    inbox_folder:  IMAP folder name for received mail. Almost always "INBOX".
    sent_folder:   IMAP folder name for sent mail. Provider-dependent.
                   TODO(§0.4): confirm live value for Asura before use.
    window_days:   Fetch messages from this many days back. Bounds cold-start cost.
    max_messages:  Hard cap on total messages fetched per run (both folders combined).
    batch_size:    Number of threads yielded per batch from run().
    """

    inbox_folder: str = "INBOX"
    sent_folder: str = _SENT_FOLDER_TODO
    window_days: int = 35
    max_messages: int = 500
    batch_size: int = 50
    folders: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self.folders = [self.inbox_folder, self.sent_folder]
