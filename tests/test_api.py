from __future__ import annotations

import io
import json
import urllib.error

import pytest


def test_client_config_accepts_installed_json(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_client_config")
    client_file = tmp_path / "google-oauth-client.json"
    client_file.write_text(
        json.dumps({"installed": {"client_id": "client-id", "client_secret": "client-secret"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "CLIENT_FILE", client_file)

    assert api.client_config() == {"client_id": "client-id", "client_secret": "client-secret"}


def test_write_secret_json_uses_private_permissions(load_script, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_secret_write")
    path = tmp_path / "youtube-token.json"

    api.write_secret_json(path, {"refresh_token": "refresh-token"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"refresh_token": "refresh-token"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_write_secret_json_fsyncs_file_and_parent(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_secret_fsync")
    path = tmp_path / "youtube-token.json"
    real_fsync = api.os.fsync
    fsync_calls = []

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(api.os, "fsync", tracking_fsync)

    api.write_secret_json(path, {"refresh_token": "refresh-token"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"refresh_token": "refresh-token"}
    assert len(fsync_calls) >= 2
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_http_json_exposes_youtube_error_reason(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_http_error")
    body = json.dumps(
        {
            "error": {
                "code": 403,
                "message": "User requests exceed the rate limit.",
                "errors": [
                    {
                        "domain": "youtube.liveBroadcast",
                        "message": "User requests exceed the rate limit.",
                        "reason": "userRequestsExceedRateLimit",
                    }
                ],
            }
        }
    ).encode()

    def fail_request(_request, timeout):
        assert timeout == 30
        raise urllib.error.HTTPError(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts",
            403,
            "Forbidden",
            {"Retry-After": "120"},
            io.BytesIO(body),
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", fail_request)

    with pytest.raises(api.YouTubeApiError) as raised:
        api.http_json("GET", "https://www.googleapis.com/youtube/v3/liveBroadcasts", token="access-token")

    error = raised.value
    assert error.status == 403
    assert error.reasons == ("userRequestsExceedRateLimit",)
    assert error.retry_class == "quota"
    assert error.retry_after == 120
    assert api.error_payload(error) == {
        "error": True,
        "http_status": 403,
        "message": "User requests exceed the rate limit.",
        "reasons": ["userRequestsExceedRateLimit"],
        "retry_after": 120,
        "retry_class": "quota",
    }


def test_corrupt_state_is_quarantined(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_corrupt_state")
    state = tmp_path / "youtube-live-state.json"
    state.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(api, "STATE_FILE", state)

    assert api.read_state() == {}

    quarantined = list(tmp_path.glob("youtube-live-state.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{broken"
    assert not state.exists()


def test_mutation_lock_times_out(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_mutation_lock")
    monkeypatch.setattr(api, "LOCK_FILE", tmp_path / "youtube-live-state.lock")

    with api.mutation_lock(timeout=0.2):
        with pytest.raises(TimeoutError):
            with api.mutation_lock(timeout=0.05):
                pass


def test_cli_main_emits_structured_retryable_error(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_cli_error")

    def fail_main():
        raise api.YouTubeApiError(
            status=503,
            reasons=("backendError",),
            message="YouTube is temporarily unavailable.",
        )

    monkeypatch.setattr(api, "main", fail_main)

    assert api.cli_main() == 75
    payload = json.loads(capsys.readouterr().err)
    assert payload["http_status"] == 503
    assert payload["retry_class"] == "api"
    assert payload["reasons"] == ["backendError"]


def test_create_broadcast_payload(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_broadcast_payload")
    calls = []

    def fake_api(method, path, params, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return {"id": "broadcast-id"}

    monkeypatch.setattr(api, "api", fake_api)

    result = api.create_broadcast("Camera Live", "unlisted")

    assert result == {"id": "broadcast-id"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/liveBroadcasts"
    assert calls[0]["params"] == {"part": "snippet,contentDetails,status"}
    assert calls[0]["body"]["snippet"]["title"] == "Camera Live"
    assert calls[0]["body"]["snippet"]["scheduledStartTime"].endswith("Z")
    assert calls[0]["body"]["status"]["privacyStatus"] == "unlisted"
    assert calls[0]["body"]["status"]["selfDeclaredMadeForKids"] is False
    assert calls[0]["body"]["contentDetails"]["monitorStream"]["enableMonitorStream"] is True


def test_token_refresh_preserves_refresh_token(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_token_refresh")
    client_file = tmp_path / "google-oauth-client.json"
    token_file = tmp_path / "youtube-token.json"
    client_file.write_text(
        json.dumps({"installed": {"client_id": "client-id", "client_secret": "client-secret"}}),
        encoding="utf-8",
    )
    api.write_secret_json(
        token_file,
        {"access_token": "expired-token", "refresh_token": "refresh-token", "expires_in": 1, "created_at": 0},
    )
    monkeypatch.setattr(api, "CLIENT_FILE", client_file)
    monkeypatch.setattr(api, "TOKEN_FILE", token_file)

    def fake_http_json(method, url, *, token=None, form=None, body=None):
        assert method == "POST"
        assert url == "https://oauth2.googleapis.com/token"
        assert token is None
        assert body is None
        assert form["client_id"] == "client-id"
        assert form["client_secret"] == "client-secret"
        assert form["refresh_token"] == "refresh-token"
        assert form["grant_type"] == "refresh_token"
        return {"access_token": "new-access-token", "expires_in": 3600}

    monkeypatch.setattr(api, "http_json", fake_http_json)

    assert api.token() == "new-access-token"
    stored = json.loads(token_file.read_text(encoding="utf-8"))
    assert stored["access_token"] == "new-access-token"
    assert stored["refresh_token"] == "refresh-token"
    assert token_file.stat().st_mode & 0o777 == 0o600
