"""
Watermark persistence.

Stores the most-recently-processed message timestamp so the next run fetches
only new mail. Migrated from skills/imap-fetch.

Storage: plain JSON at <state_dir>/mail_evidence_watermark.json.
state_dir defaults to <repo_root>/state/ (git-ignored).

The consumer is responsible for calling commit_watermark() after successfully
processing a batch — a crash before commit causes at most one batch to be
re-fetched (never dropped).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
_FILENAME = "mail_evidence_watermark.json"


def _default_state_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in [
        here.parent.parent.parent.parent.parent,  # repo root: skills/mail-evidence/mail_evidence/fetch/
        here.parent.parent.parent.parent,
        here.parent.parent.parent,
        here.parent.parent,
    ]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            state_dir = candidate / "state"
            state_dir.mkdir(exist_ok=True)
            return state_dir
    fallback = here.parent / "state"
    fallback.mkdir(exist_ok=True)
    return fallback


def load_watermark(state_dir: Path | None = None) -> datetime | None:
    """
    Load the persisted watermark.

    Returns a UTC-aware datetime, or None (cold start) if no file exists.
    """
    path = (state_dir or _default_state_dir()) / _FILENAME
    if not path.exists():
        _log.info("mail-evidence: no watermark at %s — cold start", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("watermark_utc", "")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        _log.warning(
            "mail-evidence: corrupt watermark at %s — cold start", path, exc_info=True
        )
        return None


def commit_watermark(watermark: datetime, state_dir: Path | None = None) -> None:
    """
    Persist watermark to disk.

    Call this ONLY after the batch has been durably processed. A crash before
    this call causes at most one batch re-fetch on the next run.
    """
    dir_ = state_dir or _default_state_dir()
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / _FILENAME
    utc = watermark.astimezone(timezone.utc)
    data = {"watermark_utc": utc.isoformat()}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _log.info("mail-evidence: watermark committed %s → %s", utc.isoformat(), path)


# Alias for compatibility with imap-fetch callers.
save_watermark = commit_watermark
