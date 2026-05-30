"""
Unit tests for MorningClient auth and caching logic.

All HTTP is mocked — no real network calls, no real credentials.
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from morning_bridge.client import (
    TOKEN_TTL_SECONDS,
    MorningClient,
    _load_dotenv,
    client_from_env,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _ok(json_data: dict) -> MagicMock:
    """Build a mock 200 httpx.Response."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = json_data
    r.content = b'{"ok": true}'
    r.raise_for_status = MagicMock()
    r.headers = {}
    return r


def _token_response(token: str = "test-jwt-token") -> MagicMock:
    r = _ok({"token": token})
    r.headers = {}
    return r


def _unauthorized() -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 401
    r.content = b"Unauthorized"
    r.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=r)
    )
    r.headers = {}
    return r


def _make_client(token: str = "test-jwt") -> tuple[MorningClient, MagicMock]:
    """Return (MorningClient, mock_http) where the mock returns token on auth."""
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.return_value = _token_response(token)
    mock_http.request.return_value = _ok({"items": []})
    client = MorningClient("key-id", "key-secret", sandbox=True, http_client=mock_http)
    return client, mock_http


# ── token caching ────────────────────────────────────────────────────────────


def test_token_fetched_on_first_request():
    client, mock_http = _make_client()
    client.get("/some/path")
    mock_http.post.assert_called_once()
    post_call = mock_http.post.call_args
    assert "/account/token" in post_call.args[0]
    assert post_call.kwargs["json"] == {"id": "key-id", "secret": "key-secret"}


def test_token_cached_across_requests():
    client, mock_http = _make_client()
    client.get("/path/1")
    client.get("/path/2")
    client.get("/path/3")
    # Token should only be fetched once despite three requests.
    mock_http.post.assert_called_once()


def test_token_refreshed_after_ttl():
    client, mock_http = _make_client()
    mock_http.post.side_effect = [_token_response("tok1"), _token_response("tok2")]

    client.get("/path/1")
    assert mock_http.post.call_count == 1

    # Wind the clock past the TTL.
    client._token_obtained_at = time.monotonic() - TOKEN_TTL_SECONDS - 1

    client.get("/path/2")
    assert mock_http.post.call_count == 2


def test_token_never_stored_on_disk(tmp_path, monkeypatch):
    """
    Sanity: the client never writes a token or credential file.

    client_from_env() creates a real httpx.Client, so we replace _http
    with a mock after construction to prevent any network calls.
    """
    monkeypatch.setenv("MORNING_API_KEY_ID", "fake-id")
    monkeypatch.setenv("MORNING_API_SECRET", "fake-secret")
    monkeypatch.setenv("MORNING_ENV", "sandbox")
    monkeypatch.chdir(tmp_path)

    client = client_from_env()
    # Swap in a mock transport so no real requests are made.
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.request.return_value = _ok({"me": "account"})
    client._http = mock_http
    # Pre-seed a valid token so _fetch_token is not called.
    client._token = "fake-jwt"
    client._token_obtained_at = time.monotonic()

    client.get("/account/me")

    files = list(tmp_path.rglob("*"))
    assert not files, f"Unexpected credential/token files written to disk: {files}"


# ── 401 retry ────────────────────────────────────────────────────────────────


def test_retry_on_401_refreshes_token_and_succeeds():
    mock_http = MagicMock(spec=httpx.Client)
    # First auth, then 401, then re-auth, then success.
    mock_http.post.side_effect = [_token_response("tok1"), _token_response("tok2")]
    mock_http.request.side_effect = [_unauthorized(), _ok({"data": "ok"})]

    client = MorningClient("id", "secret", sandbox=True, http_client=mock_http)
    result = client.get("/documents/123")

    assert result == {"data": "ok"}
    assert mock_http.post.call_count == 2  # two token fetches


def test_retry_on_401_raises_if_still_unauthorized():
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.return_value = _token_response()
    mock_http.request.return_value = _unauthorized()

    client = MorningClient("id", "secret", sandbox=True, http_client=mock_http)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("/documents/123")


# ── client_from_env ──────────────────────────────────────────────────────────


def test_client_from_env_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("MORNING_API_KEY_ID", raising=False)
    monkeypatch.delenv("MORNING_API_SECRET", raising=False)
    # Point _load_dotenv at a directory with no .env so it can't load one.
    with patch("morning_bridge.client.Path") as mock_path:
        mock_path.return_value.__truediv__ = lambda *_: MagicMock(exists=lambda: False)
        with pytest.raises(RuntimeError, match="MORNING_API_KEY_ID"):
            client_from_env()


def test_client_from_env_sandbox_by_default(monkeypatch):
    monkeypatch.setenv("MORNING_API_KEY_ID", "id")
    monkeypatch.setenv("MORNING_API_SECRET", "sec")
    monkeypatch.setenv("MORNING_ENV", "sandbox")
    c = client_from_env()
    assert "sandbox" in c._base


def test_client_from_env_live_when_env_set(monkeypatch):
    monkeypatch.setenv("MORNING_API_KEY_ID", "id")
    monkeypatch.setenv("MORNING_API_SECRET", "sec")
    monkeypatch.setenv("MORNING_ENV", "live")
    c = client_from_env()
    assert "sandbox" not in c._base


# ── dotenv parsing edge cases ────────────────────────────────────────────────


def _write_env(tmp_path: Path, content: str) -> Path:
    """Write a .env file at the root of a fake two-level dir tree and return the
    path of a fake 'source file' two levels below that root, so _load_dotenv's
    upward walk finds the .env on its second step (matching the real layout where
    client.py is at morning-bridge/morning_bridge/client.py)."""
    (tmp_path / "pkg").mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text(content)
    fake_source = tmp_path / "pkg" / "client.py"
    return fake_source


def test_dotenv_hash_in_value_preserved(monkeypatch, tmp_path):
    """A '#' not preceded by whitespace is part of the value, not a comment."""
    fake_source = _write_env(tmp_path, "MORNING_API_SECRET=abc#def\n")
    monkeypatch.delenv("MORNING_API_SECRET", raising=False)
    _load_dotenv(fake_source)
    assert os.environ.get("MORNING_API_SECRET") == "abc#def"


def test_dotenv_inline_comment_stripped(monkeypatch, tmp_path):
    """A '#' preceded by whitespace is a comment; the value must be clean."""
    fake_source = _write_env(tmp_path, "MORNING_ENV=sandbox  # sandbox | live\n")
    monkeypatch.delenv("MORNING_ENV", raising=False)
    _load_dotenv(fake_source)
    assert os.environ.get("MORNING_ENV") == "sandbox"


def test_dotenv_quoted_value_unquoted(monkeypatch, tmp_path):
    """Double-quoted values must have their quotes stripped."""
    fake_source = _write_env(tmp_path, 'MORNING_API_KEY_ID="my-key-id"\n')
    monkeypatch.delenv("MORNING_API_KEY_ID", raising=False)
    _load_dotenv(fake_source)
    assert os.environ.get("MORNING_API_KEY_ID") == "my-key-id"


# ── reads.py smoke ───────────────────────────────────────────────────────────


def test_search_clients_builds_correct_body():
    client, mock_http = _make_client()
    # Prime the token cache so we can inspect the search call.
    client._token = "tok"
    client._token_obtained_at = time.monotonic()

    from morning_bridge.reads import search_clients

    search_clients(client, name="Acme", active=True)

    request_call = mock_http.request.call_args
    assert request_call.kwargs["json"]["name"] == "Acme"
    assert request_call.kwargs["json"]["active"] is True
    assert "/clients/search" in request_call.args[1]


def test_search_documents_builds_correct_body():
    client, mock_http = _make_client()
    client._token = "tok"
    client._token_obtained_at = time.monotonic()

    from morning_bridge.reads import (
        search_documents,
        DOC_TYPE_TAX_INVOICE,
        DOC_STATUS_OPEN,
    )

    search_documents(
        client,
        doc_type=[DOC_TYPE_TAX_INVOICE],
        status=[DOC_STATUS_OPEN],
        from_date="2026-01-01",
        to_date="2026-03-31",
    )

    body = mock_http.request.call_args.kwargs["json"]
    assert body["type"] == [305]
    assert body["status"] == [0]
    assert body["fromDate"] == "2026-01-01"
    assert body["toDate"] == "2026-03-31"
