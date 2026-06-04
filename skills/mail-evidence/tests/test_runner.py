"""
Tests for the live runner / CLI (task 1.10).

Drives mail_evidence.runner against a mocked read-only ImapClient: verifies the
batch/watermark contract (export then commit, ≤1-batch re-fetch via idempotent
Message-ID skip), the --no-advance / --dry-run guards, and that an exported mbox
re-ingests byte-identically through the offline path.
"""

from __future__ import annotations

import mailbox
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mail_evidence import runner
from mail_evidence.fetch.imap import ImapClient
from mail_evidence.fetch.offline import ingest_email_export
from mail_evidence.fetch.watermark import load_watermark


def _raw(message_id: str, subject: str, date: str, body: str = "hi") -> bytes:
    lines = [
        "From: client@agency.com",
        "To: designer@studio.com",
        f"Subject: {subject}",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: 8bit",
        f"Message-ID: {message_id}",
        "",
        body,
    ]
    return "\r\n".join(lines).encode("utf-8")


def _mock_client(per_folder: dict[str, dict[int, bytes]]) -> MagicMock:
    """A read-only ImapClient mock serving distinct RFC822 per selected folder."""
    client = MagicMock(spec=ImapClient)
    client._mailbox = "INBOX"

    def select(folder: str) -> None:
        client._mailbox = folder

    def search(_criteria):
        return list(per_folder.get(client._mailbox, {}).keys())

    def fetch(uids, _parts):
        msgs = per_folder.get(client._mailbox, {})
        return {u: {b"RFC822": msgs[u], b"UID": u} for u in uids if u in msgs}

    client.select_folder.side_effect = select
    client.search.side_effect = search
    client.fetch.side_effect = fetch
    return client


@pytest.fixture
def two_folder_client() -> MagicMock:
    return _mock_client(
        {
            "INBOX": {1: _raw("<a@x.com>", "Logo", "Wed, 01 Apr 2026 10:00:00 +0000")},
            "Sent": {
                2: _raw("<b@x.com>", "Re: Logo", "Thu, 02 Apr 2026 11:00:00 +0000")
            },
        }
    )


def _patch_client(monkeypatch, client: MagicMock) -> None:
    monkeypatch.setattr(runner, "_make_client", lambda **_: client)


def _mbox_count(path: Path) -> int:
    box = mailbox.mbox(str(path))
    try:
        return len(box)
    finally:
        box.close()


# ── fetch → export → advance ──────────────────────────────────────────────────


def test_fetch_exports_mboxes_and_advances_watermark(
    tmp_path, monkeypatch, two_folder_client
):
    _patch_client(monkeypatch, two_folder_client)
    rc = runner.main(["fetch", "--root", str(tmp_path), "--since", "2026-04-01"])
    assert rc == 0

    inbox = tmp_path / "emails" / "INBOX.mbox"
    sent = tmp_path / "emails" / "Sent.mbox"
    assert _mbox_count(inbox) == 1
    assert _mbox_count(sent) == 1

    wm = load_watermark(state_dir=tmp_path)
    assert wm is not None
    assert wm.year == 2026 and wm.month == 4 and wm.day == 2  # high-water = latest msg


def test_exported_mbox_reingests_through_offline(
    tmp_path, monkeypatch, two_folder_client
):
    """The runner's whole point: what it writes, the offline harness reads back."""
    _patch_client(monkeypatch, two_folder_client)
    runner.main(["fetch", "--root", str(tmp_path), "--since", "2026-04-01"])

    records = ingest_email_export(tmp_path / "emails")
    subjects = {r.subject for r in records}
    assert subjects == {"Logo", "Re: Logo"}


# ── idempotency: ≤1-batch re-fetch never duplicates ───────────────────────────


def test_refetch_is_idempotent_by_message_id(tmp_path, monkeypatch, two_folder_client):
    _patch_client(monkeypatch, two_folder_client)
    runner.main(["fetch", "--root", str(tmp_path), "--since", "2026-04-01"])
    # Re-run with the SAME messages (day-granular SINCE overlap) — must not duplicate.
    runner.main(["fetch", "--root", str(tmp_path), "--since", "2026-04-01"])

    assert _mbox_count(tmp_path / "emails" / "INBOX.mbox") == 1
    assert _mbox_count(tmp_path / "emails" / "Sent.mbox") == 1


# ── guards ────────────────────────────────────────────────────────────────────


def test_no_advance_persists_batch_but_not_watermark(
    tmp_path, monkeypatch, two_folder_client
):
    _patch_client(monkeypatch, two_folder_client)
    runner.main(
        ["fetch", "--root", str(tmp_path), "--since", "2026-04-01", "--no-advance"]
    )

    assert _mbox_count(tmp_path / "emails" / "INBOX.mbox") == 1
    assert load_watermark(state_dir=tmp_path) is None


def test_dry_run_writes_nothing(tmp_path, monkeypatch, two_folder_client):
    _patch_client(monkeypatch, two_folder_client)
    runner.main(
        ["fetch", "--root", str(tmp_path), "--since", "2026-04-01", "--dry-run"]
    )

    assert not (tmp_path / "emails").exists()
    assert load_watermark(state_dir=tmp_path) is None


# ── watermark + probe commands ────────────────────────────────────────────────


def test_watermark_command_cold_then_warm(
    tmp_path, monkeypatch, two_folder_client, capsys
):
    runner.main(["watermark", "--root", str(tmp_path)])
    assert "cold start" in capsys.readouterr().out

    _patch_client(monkeypatch, two_folder_client)
    runner.main(["fetch", "--root", str(tmp_path), "--since", "2026-04-01"])
    runner.main(["watermark", "--root", str(tmp_path)])
    assert "2026-04-02" in capsys.readouterr().out


def test_probe_reports_message_count(tmp_path, monkeypatch, capsys):
    client = _mock_client(
        {"INBOX": {1: _raw("<a@x.com>", "Hi", "Wed, 01 Apr 2026 10:00:00 +0000")}}
    )
    _patch_client(monkeypatch, client)
    rc = runner.main(["probe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 message" in out
    client.disconnect.assert_called_once()
