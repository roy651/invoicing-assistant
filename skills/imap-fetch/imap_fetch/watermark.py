"""
Watermark persistence.

Stores the timestamp of the most recently processed message so the next run
fetches only new mail.

Storage: a plain JSON file at <state_dir>/imap_watermark.json.
  state_dir defaults to <repo_root>/state/ (git-ignored).

The watermark is the max `date` of the messages returned by the last
fetch_since() call.  The caller advances it after successfully processing all
returned messages — if processing fails mid-batch, the watermark is NOT
advanced, so the batch is retried on the next run.

Format:
  {"watermark_utc": "2026-04-30T18:00:00+00:00"}

Thread-safety: single-process, single-thread only.  Sufficient for the
scheduled monthly runner on the user's Mac.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
_FILENAME = "imap_watermark.json"


def _default_state_dir() -> Path:
    """
    Locate <repo_root>/state/ by walking up from this file.

    Returns the state/ directory (creates it if absent).
    """
    here = Path(__file__).resolve()
    # Walk up: imap_fetch/ → imap-fetch/ → skills/ → repo root
    for candidate in [
        here.parent.parent.parent.parent,  # repo root if layout is skills/imap-fetch/imap_fetch/
        here.parent.parent.parent,
        here.parent.parent,
    ]:
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            state_dir = candidate / "state"
            state_dir.mkdir(exist_ok=True)
            return state_dir
    # Fallback: next to this file.
    fallback = here.parent / "state"
    fallback.mkdir(exist_ok=True)
    return fallback


def load_watermark(state_dir: Path | None = None) -> datetime | None:
    """
    Load the persisted watermark.

    Returns a UTC-aware datetime, or None if no watermark exists (cold start).
    state_dir defaults to <repo_root>/state/.
    """
    path = (state_dir or _default_state_dir()) / _FILENAME
    if not path.exists():
        _log.info("imap-fetch: no watermark file found at %s — cold start", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("watermark_utc", "")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        _log.info("imap-fetch: loaded watermark %s", dt.isoformat())
        return dt.astimezone(timezone.utc)
    except Exception:
        _log.warning(
            "imap-fetch: corrupt watermark file %s — treating as cold start",
            path,
            exc_info=True,
        )
        return None


def save_watermark(watermark: datetime, state_dir: Path | None = None) -> None:
    """
    Persist `watermark` (UTC-aware datetime) to disk.

    Creates the state directory if it doesn't exist.
    state_dir defaults to <repo_root>/state/.
    """
    dir_ = state_dir or _default_state_dir()
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / _FILENAME
    # Normalise to UTC before storing.
    utc = watermark.astimezone(timezone.utc)
    data = {"watermark_utc": utc.isoformat()}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _log.info("imap-fetch: saved watermark %s → %s", utc.isoformat(), path)
