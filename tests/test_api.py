from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import urllib.error
import uuid

import pytest


def managed_broadcast(api, broadcast_id, lifecycle, *, generation="generation-1", stream_id="stream-1"):
    return {
        "id": broadcast_id,
        "snippet": {
            "title": "Camera Live",
            "description": api.broadcast_description("encoder-1", generation),
            "scheduledStartTime": "2026-07-10T21:00:00Z",
        },
        "contentDetails": {
            "boundStreamId": stream_id,
            "monitorStream": {"enableMonitorStream": True, "broadcastStreamDelayMs": 0},
        },
        "status": {
            "lifeCycleStatus": lifecycle,
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }


def configure_reconciliation(api, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "STATE_FILE", tmp_path / "youtube-live-state.json")
    monkeypatch.setattr(api, "LOCK_FILE", tmp_path / "youtube-live-state.lock")
    monkeypatch.setattr(api, "instance_id", lambda: "encoder-1")
    monkeypatch.setattr(
        api,
        "stream_by_id",
        lambda stream_id: {"id": stream_id, "status": {"streamStatus": "active"}},
    )


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


def test_write_json_durable_creates_private_unique_temp(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_private_temp")
    path = tmp_path / "youtube-token.json"
    real_dump = api.json.dump
    observed_modes = []

    def inspect_mode(data, handle, *args, **kwargs):
        observed_modes.append(os.fstat(handle.fileno()).st_mode & 0o777)
        return real_dump(data, handle, *args, **kwargs)

    monkeypatch.setattr(api.json, "dump", inspect_mode)

    api.write_json_durable(path, {"refresh_token": "refresh-token"})

    assert observed_modes == [0o600]
    assert list(tmp_path.glob(".youtube-token.json.*.tmp")) == []


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


def test_http_json_preserves_oauth_device_error_reason(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_oauth_error")
    body = json.dumps(
        {
            "error": "authorization_pending",
            "error_description": "The user has not completed authorization.",
        }
    ).encode()

    def fail_request(_request, timeout):
        assert timeout == 30
        raise urllib.error.HTTPError(
            "https://oauth2.googleapis.com/token",
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", fail_request)

    with pytest.raises(api.YouTubeApiError) as raised:
        api.http_json("POST", "https://oauth2.googleapis.com/token", form={"device_code": "code"})

    assert raised.value.reasons == ("authorization_pending",)
    assert str(raised.value) == "The user has not completed authorization."


def test_authorize_retries_pending_device_flow(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_authorize_pending")
    calls = []
    responses = iter(
        [
            {
                "verification_url": "https://example.test/device",
                "user_code": "ABCD-EFGH",
                "device_code": "device-code",
                "interval": 1,
                "expires_in": 600,
            },
            api.YouTubeApiError(
                status=400,
                reasons=("authorization_pending",),
                message="authorization_pending",
            ),
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600},
        ]
    )

    def fake_http(*_args, **_kwargs):
        calls.append(True)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(api, "TOKEN_FILE", tmp_path / "youtube-token.json")
    monkeypatch.setattr(api, "client_config", lambda: {"client_id": "id", "client_secret": "secret"})
    monkeypatch.setattr(api, "http_json", fake_http)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)

    assert api.authorize(argparse.Namespace()) == 0
    assert len(calls) == 3
    assert json.loads(api.TOKEN_FILE.read_text(encoding="utf-8"))["refresh_token"] == "refresh"


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


@pytest.mark.parametrize(
    "reason",
    ["concurrentBroadcastsExceedLimit", "sharedIngestionBroadcastsExceedLimit"],
)
def test_concurrent_broadcast_limits_use_quota_backoff(load_script, reason):
    api = load_script("youtube-autoencoder-api", f"yta_api_rate_{reason}")
    error = api.YouTubeApiError(status=403, reasons=(reason,), message="broadcast limit")

    assert error.retry_class == "quota"


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


def test_description_contains_exact_instance_and_generation_markers(load_script):
    api = load_script("youtube-autoencoder-api", "yta_api_markers")

    description = api.broadcast_description("rpi5-streamer", "generation-1")

    assert "[youtube-autoencoder-instance:rpi5-streamer]" in description.splitlines()
    assert "[youtube-autoencoder-generation:generation-1]" in description.splitlines()


@pytest.mark.parametrize("lifecycle", ["created", "ready", "testStarting", "testing", "liveStarting", "live"])
def test_reconcile_reuses_nonterminal_broadcast_without_insert(load_script, monkeypatch, tmp_path, lifecycle):
    api = load_script("youtube-autoencoder-api", f"yta_api_reuse_{lifecycle}")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
        }
    )
    broadcast = managed_broadcast(api, "broadcast-1", lifecycle)
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: broadcast)
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))
    monkeypatch.setattr(api, "bind_broadcast", lambda *_args: pytest.fail("bind called"))

    state = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
    )

    assert state["broadcast_id"] == "broadcast-1"
    assert state["last_broadcast_id"] == "broadcast-1"
    assert state["lifecycle"] == lifecycle


def test_reconcile_replaces_terminal_broadcast_once(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_terminal_replacement")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "old-generation",
            "stream_id": "stream-1",
            "broadcast_id": "old-broadcast",
        }
    )
    old = managed_broadcast(api, "old-broadcast", "complete", generation="old-generation")
    new_generation = "00000000-0000-0000-0000-000000000001"
    created = managed_broadcast(api, "new-broadcast", "created", generation=new_generation, stream_id="")
    bound = managed_broadcast(api, "new-broadcast", "ready", generation=new_generation)
    creates = []
    monkeypatch.setattr(api, "broadcast_by_id", lambda broadcast_id: old if broadcast_id == "old-broadcast" else None)
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [])
    monkeypatch.setattr(api.uuid, "uuid4", lambda: uuid.UUID("00000000-0000-0000-0000-000000000001"))

    def create(title, privacy, description=None):
        creates.append((title, privacy, description))
        return created

    monkeypatch.setattr(api, "create_broadcast", create)
    monkeypatch.setattr(api, "bind_broadcast", lambda _broadcast_id, _stream_id: bound)

    state = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
    )

    assert len(creates) == 1
    assert state["broadcast_id"] == "new-broadcast"
    assert state["generation_id"] == "00000000-0000-0000-0000-000000000001"
    assert state["lifecycle"] == "ready"


def test_reconcile_recovers_lost_insert_response_by_generation(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_lost_insert")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": None,
            "pending_action": "create",
        }
    )
    found = managed_broadcast(api, "broadcast-1", "created", stream_id="")
    bound = managed_broadcast(api, "broadcast-1", "ready")
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [found])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))
    monkeypatch.setattr(api, "bind_broadcast", lambda _broadcast_id, _stream_id: bound)

    state = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
    )

    assert state["broadcast_id"] == "broadcast-1"
    assert state["lifecycle"] == "ready"


def test_reconcile_recovers_generation_from_single_remote_candidate(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_remote_generation")
    configure_reconciliation(api, monkeypatch, tmp_path)
    found = managed_broadcast(api, "broadcast-1", "live", generation="remote-generation")
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [found])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    state = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
    )

    assert state["broadcast_id"] == "broadcast-1"
    assert state["generation_id"] == "remote-generation"


def test_reconcile_blocks_multiple_managed_candidates(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_ambiguous")
    configure_reconciliation(api, monkeypatch, tmp_path)
    candidates = [
        managed_broadcast(api, "broadcast-1", "ready", generation="generation-1"),
        managed_broadcast(api, "broadcast-2", "ready", generation="generation-2"),
    ]
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: candidates)
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError) as raised:
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
        )

    assert raised.value.retry_class == "ambiguous"


def test_reconcile_blocks_conflicting_managed_generation(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_generation_conflict")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": None,
            "pending_action": "create",
        }
    )
    conflicting = managed_broadcast(api, "broadcast-2", "ready", generation="generation-2")
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [conflicting])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="generation"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
        )


def test_reconcile_blocks_unknown_lifecycle(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_unknown_lifecycle")
    configure_reconciliation(api, monkeypatch, tmp_path)
    unknown = managed_broadcast(api, "broadcast-1", "mystery")
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [unknown])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="unknown"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
        )


def test_reconcile_blocks_broadcast_bound_to_different_stream(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_wrong_stream")
    configure_reconciliation(api, monkeypatch, tmp_path)
    wrong_stream = managed_broadcast(api, "broadcast-1", "ready", stream_id="stream-2")
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [wrong_stream])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="different stream"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
        )


def test_reconcile_refuses_create_until_ingest_is_active(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_ingest_gate")
    configure_reconciliation(api, monkeypatch, tmp_path)
    monkeypatch.setattr(api, "list_managed_broadcasts", lambda _instance: [])
    monkeypatch.setattr(
        api,
        "stream_by_id",
        lambda stream_id: {"id": stream_id, "status": {"streamStatus": "inactive"}},
    )
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="active"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
        )


def test_offline_reconcile_stages_one_unlisted_broadcast_before_ingest(
    load_script, monkeypatch, tmp_path
):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_stage")
    configure_reconciliation(api, monkeypatch, tmp_path)
    monkeypatch.setattr(
        api,
        "stream_by_id",
        lambda stream_id: {"id": stream_id, "status": {"streamStatus": "inactive"}},
    )
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: [], raising=False)
    generation = "00000000-0000-0000-0000-000000000001"
    created = managed_broadcast(api, "broadcast-1", "created", generation=generation, stream_id="")
    bound = managed_broadcast(api, "broadcast-1", "ready", generation=generation)
    creates = []
    monkeypatch.setattr(api.uuid, "uuid4", lambda: uuid.UUID(generation))

    def create(title, privacy, description=None):
        creates.append((title, privacy, description))
        return created

    monkeypatch.setattr(api, "create_broadcast", create)
    monkeypatch.setattr(api, "bind_broadcast", lambda _broadcast_id, _stream_id: bound)

    state = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
        offline_create=True,
    )

    assert len(creates) == 1
    assert state["generation_id"] == generation
    assert state["broadcast_id"] == "broadcast-1"
    assert state["privacy"] == "unlisted"


def test_offline_reconcile_blocks_unmarked_broadcast_bound_to_same_stream(
    load_script, monkeypatch, tmp_path
):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_unmarked_conflict")
    configure_reconciliation(api, monkeypatch, tmp_path)
    unmarked = {
        "id": "legacy-broadcast",
        "snippet": {"title": "Legacy Live", "description": "Created outside AutoEncoder."},
        "contentDetails": {"boundStreamId": "stream-1", "enableAutoStart": True},
        "status": {"lifeCycleStatus": "ready", "privacyStatus": "unlisted"},
    }
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: [unmarked], raising=False)
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="unmarked"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
            offline_create=True,
        )


def test_offline_reconcile_checks_unmarked_conflicts_with_cached_managed_broadcast(
    load_script, monkeypatch, tmp_path
):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_cached_conflict")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
        }
    )
    cached = managed_broadcast(api, "broadcast-1", "ready")
    unmarked = {
        "id": "legacy-broadcast",
        "snippet": {"title": "Legacy Live", "description": "Created outside AutoEncoder."},
        "contentDetails": {"boundStreamId": "stream-1", "enableAutoStart": True},
        "status": {"lifeCycleStatus": "ready", "privacyStatus": "unlisted"},
    }
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: cached)
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: [cached, unmarked])

    with pytest.raises(api.ReconciliationError, match="unmarked"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
            offline_create=True,
        )


def test_offline_reconcile_refuses_public_staging(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_public")
    configure_reconciliation(api, monkeypatch, tmp_path)
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: [])
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))

    with pytest.raises(api.ReconciliationError, match="unlisted"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="public",
            allow_create=True,
            offline_create=True,
        )


def test_offline_insert_throttle_preserves_write_ahead_generation(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_insert_throttle")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": None,
            "pending_action": "create",
        }
    )
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: [])
    creates = []

    def throttled_insert(*_args, **_kwargs):
        creates.append(True)
        raise api.YouTubeApiError(
            status=403,
            reasons=("userRequestsExceedRateLimit",),
            message="User requests exceed the rate limit.",
        )

    monkeypatch.setattr(api, "create_broadcast", throttled_insert)

    with pytest.raises(api.YouTubeApiError):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
            offline_create=True,
        )

    state = api.read_state()
    assert creates == [True]
    assert state["generation_id"] == "generation-1"
    assert state["broadcast_id"] is None
    assert state["pending_action"] == "create"


def test_offline_bind_failure_reuses_persisted_broadcast_without_reinsert(
    load_script, monkeypatch, tmp_path
):
    api = load_script("youtube-autoencoder-api", "yta_api_offline_bind_retry")
    configure_reconciliation(api, monkeypatch, tmp_path)
    generation = "00000000-0000-0000-0000-000000000001"
    created = managed_broadcast(api, "broadcast-1", "created", generation=generation, stream_id="")
    bound = managed_broadcast(api, "broadcast-1", "ready", generation=generation)
    inventory = []
    creates = []
    monkeypatch.setattr(api.uuid, "uuid4", lambda: uuid.UUID(generation))
    monkeypatch.setattr(api, "list_recoverable_broadcasts", lambda: list(inventory))

    def create(*_args, **_kwargs):
        creates.append(True)
        inventory[:] = [created]
        return created

    monkeypatch.setattr(api, "create_broadcast", create)
    monkeypatch.setattr(
        api,
        "bind_broadcast",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("bind failed")),
    )

    with pytest.raises(ConnectionError, match="bind failed"):
        api.reconcile_broadcast(
            stream_id="stream-1",
            title="Camera Live",
            staging_privacy="unlisted",
            allow_create=True,
            offline_create=True,
        )

    pending = api.read_state()
    assert pending["broadcast_id"] == "broadcast-1"
    assert pending["pending_action"] == "bind"

    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: created)
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("second insert called"))
    monkeypatch.setattr(api, "bind_broadcast", lambda *_args: bound)

    recovered = api.reconcile_broadcast(
        stream_id="stream-1",
        title="Camera Live",
        staging_privacy="unlisted",
        allow_create=True,
        offline_create=True,
    )

    assert creates == [True]
    assert recovered["broadcast_id"] == "broadcast-1"
    assert recovered["pending_action"] is None
    assert recovered["lifecycle"] == "ready"


def test_list_managed_broadcasts_requires_exact_marker(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_exact_marker")
    exact = managed_broadcast(api, "broadcast-1", "ready")
    title_only = {
        **managed_broadcast(api, "broadcast-2", "ready"),
        "snippet": {"title": "encoder-1 Camera Live", "description": "Started by YouTube AutoEncoder."},
    }
    monkeypatch.setattr(api, "list_broadcasts", lambda _status: [exact, title_only])

    assert [item["id"] for item in api.list_managed_broadcasts("encoder-1")] == ["broadcast-1"]


def test_set_broadcast_privacy_preserves_required_fields_and_verifies_readback(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_privacy")
    current = managed_broadcast(api, "broadcast-1", "live")
    public = {
        **current,
        "status": {**current["status"], "privacyStatus": "public"},
    }
    calls = []
    reads = iter([current, public])
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: next(reads))

    def fake_api(method, path, params, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        return public

    monkeypatch.setattr(api, "api", fake_api)

    result = api.set_broadcast_privacy("broadcast-1", "public")

    assert result["status"]["privacyStatus"] == "public"
    assert calls == [
        {
            "method": "PUT",
            "path": "/liveBroadcasts",
            "params": {"part": "status"},
            "body": {
                "id": "broadcast-1",
                "snippet": {"scheduledStartTime": "2026-07-10T21:00:00Z"},
                "contentDetails": {"monitorStream": {"enableMonitorStream": True, "broadcastStreamDelayMs": 0}},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            },
        }
    ]


def test_set_privacy_command_reuses_verified_readback(load_script, monkeypatch, tmp_path, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_privacy_command")
    configure_reconciliation(api, monkeypatch, tmp_path)
    public = managed_broadcast(api, "broadcast-1", "live")
    public["status"]["privacyStatus"] = "public"
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
        }
    )
    monkeypatch.setattr(api, "set_broadcast_privacy", lambda _broadcast_id, _privacy: public)
    monkeypatch.setattr(
        api,
        "broadcast_status",
        lambda _broadcast_id: pytest.fail("third broadcast read called"),
    )

    args = argparse.Namespace(broadcast_id="broadcast-1", privacy="public")

    assert api.set_privacy_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["broadcast_id"] == "broadcast-1"
    assert output["privacy"] == "public"


def test_missing_broadcast_status_disables_cached_public_fallback(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_missing_broadcast_status")
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: None)

    with pytest.raises(api.ReconciliationError, match="not found") as raised:
        api.broadcast_status("missing-broadcast")

    payload = api.error_payload(raised.value)
    assert payload["retry_class"] == "ambiguous"
    assert payload["public_fallback_allowed"] is False


def test_retry_state_updates_preserve_broadcast_identity(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_retry_state")
    configure_reconciliation(api, monkeypatch, tmp_path)
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
        }
    )

    stored = api.set_retry_state("quota", 3, "2026-07-11T03:00:00Z")

    assert stored["broadcast_id"] == "broadcast-1"
    assert stored["retry_class"] == "quota"
    assert stored["retry_attempt"] == 3
    assert stored["retry_not_before"] == "2026-07-11T03:00:00Z"
    cleared = api.clear_retry_state()
    assert cleared["broadcast_id"] == "broadcast-1"
    assert "retry_class" not in cleared
    assert "retry_attempt" not in cleared
    assert "retry_not_before" not in cleared


def test_prepare_broadcast_uses_reconciliation_not_direct_insert(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_prepare_reconcile")
    args = argparse.Namespace(
        create_stream=False,
        title="Camera Live",
        title_prefix="AutoEncoder Live",
        privacy="unlisted",
    )
    monkeypatch.setattr(api, "ensure_stream", lambda create_if_missing: {"id": "stream-1"})
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("direct insert called"))
    monkeypatch.setattr(
        api,
        "reconcile_broadcast",
        lambda **kwargs: {
            "broadcast_id": "broadcast-1",
            "stream_id": kwargs["stream_id"],
            "lifecycle": "ready",
            "privacy": kwargs["staging_privacy"],
        },
    )

    assert api.prepare_broadcast(args) == 0
    assert json.loads(capsys.readouterr().out)["broadcast_id"] == "broadcast-1"


def test_reconcile_command_forwards_explicit_offline_create(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_reconcile_offline_command")
    args = argparse.Namespace(
        allow_create=True,
        offline_create=True,
        create_stream=False,
        title="Camera Live",
        title_prefix="AutoEncoder Live",
        privacy="unlisted",
    )
    captured = {}
    monkeypatch.setattr(api, "ensure_stream", lambda create_if_missing: {"id": "stream-1"})

    def reconcile(**kwargs):
        captured.update(kwargs)
        return {
            "broadcast_id": "broadcast-1",
            "stream_id": kwargs["stream_id"],
            "lifecycle": "ready",
            "privacy": kwargs["staging_privacy"],
        }

    monkeypatch.setattr(api, "reconcile_broadcast", reconcile)

    assert api.reconcile_command(args) == 0
    assert json.loads(capsys.readouterr().out)["broadcast_id"] == "broadcast-1"
    assert captured["offline_create"] is True


def test_reconcile_parser_accepts_explicit_offline_create(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_reconcile_offline_parser")
    captured = {}
    monkeypatch.setattr(
        api.sys,
        "argv",
        ["youtube-autoencoder-api", "reconcile-broadcast", "--allow-create", "--offline-create"],
    )

    def command(args):
        captured["allow_create"] = args.allow_create
        captured["offline_create"] = args.offline_create
        return 0

    monkeypatch.setattr(api, "reconcile_command", command)

    assert api.main() == 0
    assert captured == {"allow_create": True, "offline_create": True}


def test_transition_command_serializes_mutation(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_transition_lock")
    events = []

    @contextlib.contextmanager
    def fake_lock(timeout):
        events.append(("lock", timeout))
        yield
        events.append(("unlock", timeout))

    monkeypatch.setattr(api, "mutation_lock", fake_lock)
    monkeypatch.setattr(api, "lock_timeout", lambda: 7.0)
    monkeypatch.setattr(
        api,
        "transition",
        lambda broadcast_id, status: {
            "id": broadcast_id,
            "status": {"lifeCycleStatus": status},
        },
    )
    args = argparse.Namespace(broadcast_id="broadcast-1", status="live")

    assert api.transition_command(args) == 0
    assert events == [("lock", 7.0), ("unlock", 7.0)]
    assert json.loads(capsys.readouterr().out)["status"]["lifeCycleStatus"] == "live"


def test_complete_requires_confirmed_live_broadcast(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_complete_live")
    configure_reconciliation(api, monkeypatch, tmp_path)
    broadcast = managed_broadcast(api, "broadcast-1", "live")
    transitions = []
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: broadcast)
    monkeypatch.setattr(
        api,
        "transition",
        lambda broadcast_id, status: transitions.append((broadcast_id, status))
        or {"id": broadcast_id, "status": {"lifeCycleStatus": "complete"}},
    )

    args = argparse.Namespace(broadcast_id="broadcast-1")

    assert api.complete(args) == 0
    assert transitions == [("broadcast-1", "complete")]


def test_complete_refuses_nonlive_broadcast(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_complete_nonlive")
    configure_reconciliation(api, monkeypatch, tmp_path)
    broadcast = managed_broadcast(api, "broadcast-1", "ready")
    monkeypatch.setattr(api, "broadcast_by_id", lambda _broadcast_id: broadcast)
    monkeypatch.setattr(api, "transition", lambda *_args: pytest.fail("transition called"))

    with pytest.raises(api.ReconciliationError, match="not confirmed live"):
        api.complete(argparse.Namespace(broadcast_id="broadcast-1"))


def test_generic_transition_parser_cannot_complete_broadcast(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_no_generic_complete")
    monkeypatch.setattr(
        api.sys,
        "argv",
        ["youtube-autoencoder-api", "transition", "complete", "broadcast-1"],
    )

    with pytest.raises(SystemExit) as raised:
        api.main()

    assert raised.value.code == 2


def test_state_command_outputs_non_secret_recovery_state(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_state_command")
    monkeypatch.setattr(
        api,
        "read_state",
        lambda: {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "broadcast_id": "broadcast-1",
            "retry_class": "quota",
        },
    )

    assert api.state_command(argparse.Namespace()) == 0
    assert json.loads(capsys.readouterr().out)["retry_class"] == "quota"


def test_visible_test_requires_explicit_complete_flag(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_test_complete_default")
    seen = {}

    def fake_visible_test(args):
        seen["complete"] = args.complete
        return 0

    monkeypatch.setattr(api, "run_visible_test", fake_visible_test)
    monkeypatch.setattr(api.sys, "argv", ["youtube-autoencoder-api", "run-visible-test"])

    assert api.main() == 0
    assert seen["complete"] is False


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


def test_video_metrics_uses_one_read_only_videos_list_call(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics")
    calls = []

    def request(method, path, params, body=None):
        calls.append((method, path, params, body))
        return {
            "items": [{
                "id": "video-1",
                "liveStreamingDetails": {
                    "actualStartTime": "2026-07-11T00:00:00Z",
                    "concurrentViewers": "12",
                },
                "statistics": {"viewCount": "345", "likeCount": "6", "commentCount": "2"},
            }]
        }

    monkeypatch.setattr(api, "api", request)
    result = api.video_metrics("video-1")

    assert calls == [(
        "GET",
        "/videos",
        {
            "id": "video-1",
            "part": "liveStreamingDetails,statistics",
            "fields": (
                "items(id,liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime,"
                "scheduledEndTime,concurrentViewers),statistics(viewCount,likeCount,commentCount))"
            ),
        },
        None,
    )]
    assert result["concurrent_viewers"] == 12
    assert result["view_count"] == 345


def test_video_metrics_leaves_absent_optional_values_as_none(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_missing")
    monkeypatch.setattr(
        api,
        "api",
        lambda *_args, **_kwargs: {"items": [{"id": "video-1", "statistics": {}}]},
    )

    result = api.video_metrics("video-1")

    assert result["actual_start_time"] is None
    assert result["actual_end_time"] is None
    assert result["scheduled_start_time"] is None
    assert result["scheduled_end_time"] is None
    assert result["concurrent_viewers"] is None
    assert result["view_count"] is None
    assert result["like_count"] is None
    assert result["comment_count"] is None


@pytest.mark.parametrize("value", [True, False, -1, "-1", "not-a-number"])
def test_metric_int_rejects_invalid_counts(load_script, value):
    api = load_script("youtube-autoencoder-api", f"yta_api_metric_int_{str(value).replace('-', 'n')}")

    with pytest.raises(api.ReconciliationError, match="invalid viewCount metric"):
        api.metric_int(value, "viewCount")


def test_video_metrics_rejects_empty_items(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_empty")
    monkeypatch.setattr(api, "api", lambda *_args, **_kwargs: {"items": []})

    with pytest.raises(api.ReconciliationError, match="video not found: video-1"):
        api.video_metrics("video-1")


def test_video_metrics_rejects_mismatched_response_video_id(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_mismatch")
    monkeypatch.setattr(api, "api", lambda *_args, **_kwargs: {"items": [{"id": "video-2"}]})

    with pytest.raises(api.ReconciliationError, match="unexpected video id"):
        api.video_metrics("video-1")


def test_video_metrics_command_explicit_id_does_not_read_lifecycle_state(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_explicit_command")
    result = {"video_id": "video-1", "view_count": 345}

    def fail_read():
        raise AssertionError("explicit video metrics must not read lifecycle state")

    monkeypatch.setattr(api, "read_state", fail_read)
    monkeypatch.setattr(api, "read_state_snapshot", fail_read)
    monkeypatch.setattr(api, "video_metrics", lambda video_id: result if video_id == "video-1" else None)

    assert api.video_metrics_command(argparse.Namespace(video_id="video-1")) == 0
    assert json.loads(capsys.readouterr().out) == result


def test_video_metrics_command_reads_broadcast_id_from_snapshot(load_script, monkeypatch, capsys):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_snapshot_command")
    monkeypatch.setattr(api, "read_state_snapshot", lambda: {"broadcast_id": "video-1"})
    monkeypatch.setattr(api, "video_metrics", lambda video_id: {"video_id": video_id, "view_count": 345})

    assert api.video_metrics_command(argparse.Namespace(video_id=None)) == 0
    assert json.loads(capsys.readouterr().out) == {"video_id": "video-1", "view_count": 345}


def test_video_metrics_command_rejects_missing_cached_broadcast_id(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_missing_snapshot")
    state = tmp_path / "youtube-live-state.json"
    state.write_text(json.dumps({"lifecycle": "live"}), encoding="utf-8")
    monkeypatch.setattr(api, "STATE_FILE", state)

    with pytest.raises(ValueError, match="no broadcast id provided"):
        api.video_metrics_command(argparse.Namespace(video_id=None))


def test_video_metrics_command_does_not_mutate_malformed_state(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_malformed_snapshot")
    state = tmp_path / "youtube-live-state.json"
    state.write_text("{broken", encoding="utf-8")
    before = state.read_bytes()
    monkeypatch.setattr(api, "STATE_FILE", state)

    with pytest.raises(json.JSONDecodeError):
        api.video_metrics_command(argparse.Namespace(video_id=None))

    assert state.exists()
    assert state.read_bytes() == before
    assert list(tmp_path.glob("youtube-live-state.json.corrupt.*")) == []


def test_video_metrics_parser_accepts_optional_video_id(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_video_metrics_parser")
    observed = []

    def command(args):
        observed.append(args.video_id)
        return 0

    monkeypatch.setattr(api, "video_metrics_command", command)
    monkeypatch.setattr(api.sys, "argv", ["youtube-autoencoder-api", "video-metrics"])

    assert api.main() == 0
    assert observed == [None]
