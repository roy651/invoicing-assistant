"""
Fetch configuration for mail-evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# TODO(fact §0.4): confirm Asura's actual Sent folder name before enabling live fetch.
# The folder name is mailbox-specific — query via LIST or check webmail settings.
_SENT_FOLDER_TODO = "Sent"


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
