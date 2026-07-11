from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest


def test_parse_duration_units(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_duration")

    assert supervisor.parse_duration("0") == 0
    assert supervisor.parse_duration("10s") == 10
    assert supervisor.parse_duration("2m") == 120
    assert supervisor.parse_duration("1h") == 3600
    assert supervisor.parse_duration("0.5d") == 43200


def test_redacts_credentials_and_stream_keys(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_redaction")
    camera_url = "rtsp://user:pass@camera.local:554/stream1"
    ingest_url = "rtmps://a.rtmp.youtube.com/live2/secret-stream-key"

    assert supervisor.redact_url(camera_url) == "rtsp://<redacted>@camera.local:554/stream1"
    assert supervisor.redact_url(ingest_url) == "rtmps://a.rtmp.youtube.com/<redacted>"

    redacted = supervisor.redact_text(f"input={camera_url} output={ingest_url}")
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "secret-stream-key" not in redacted
    assert "rtsp://<redacted>@camera.local:554/stream1" in redacted
    assert "rtmps://a.rtmp.youtube.com/<redacted>" in redacted


def test_selected_vlc_url_prefers_named_selected_source(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_scene")
    monkeypatch.setenv("YTA_OBS_SOURCE_NAME", "Main Camera")

    scene = {
        "sources": [
            {
                "id": "vlc_source",
                "name": "Backup Camera",
                "settings": {"playlist": [{"value": "rtsp://backup.local/stream"}]},
            },
            {
                "id": "vlc_source",
                "name": "Main Camera",
                "settings": {
                    "playlist": [
                        {"value": "rtsp://main.local/low"},
                        {"value": "rtsp://main.local/high", "selected": True},
                    ]
                },
            },
        ]
    }

    assert supervisor.selected_vlc_url(scene) == "rtsp://main.local/high"


def test_ffmpeg_copy_args(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_copy_args")
    monkeypatch.setenv("YTA_MODE", "copy")

    args = supervisor.ffmpeg_args(
        "rtsp://camera.local/stream",
        "rtmps://youtube.example/live/key",
        rtsp_timeout_supported=True,
    )

    assert args[0] == "ffmpeg"
    assert args[args.index("-c:v") + 1] == "copy"
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in args
    assert args[-1] == "rtmps://youtube.example/live/key"


def test_ffmpeg_transcode_args(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_transcode_args")
    monkeypatch.setenv("YTA_MODE", "transcode")
    monkeypatch.setenv("YTA_TRANSCODE_SCALE", "-2:1080")
    monkeypatch.setenv("YTA_FPS", "30")

    args = supervisor.ffmpeg_args(
        "rtsp://camera.local/stream",
        "rtmps://youtube.example/live/key",
        rtsp_timeout_supported=True,
    )

    assert args[args.index("-c:v") + 1] == "libx264"
    assert "scale=-2:1080,fps=30,format=yuv420p" in args
    assert args[args.index("-f", args.index("-c:a")) + 1] == "flv"


def test_ffmpeg_args_omit_rw_timeout_and_emit_progress(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_no_rw_timeout")
    monkeypatch.setenv("YTA_MODE", "copy")

    args = supervisor.ffmpeg_args(
        "rtsp://camera.local/stream",
        "rtmps://youtube.example/live/key",
        rtsp_timeout_supported=True,
    )

    assert "-rw_timeout" not in args
    assert args[args.index("-timeout") + 1] == "5000000"
    assert args[args.index("-stats_period") + 1] == "5"
    assert args[args.index("-progress") + 1] == "pipe:1"


def test_ffmpeg_args_omit_unsupported_timeout(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_no_timeout")
    monkeypatch.setenv("YTA_MODE", "copy")

    args = supervisor.ffmpeg_args(
        "rtsp://camera.local/stream",
        "rtmps://youtube.example/live/key",
        rtsp_timeout_supported=False,
    )

    assert "-timeout" not in args
    assert "-rw_timeout" not in args


def test_ffmpeg_capability_probe_reads_rtsp_demuxer_options(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_capability")
    completed = subprocess.CompletedProcess(
        args=["ffmpeg"],
        returncode=0,
        stdout="  -timeout <int64> set timeout\n",
        stderr="",
    )
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert supervisor.ffmpeg_supports_rtsp_option("timeout") is True
    assert supervisor.ffmpeg_supports_rtsp_option("rw_timeout") is False


def test_ffmpeg_capability_probe_is_cached_per_binary_and_option(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_capability_cache")
    calls = []

    def inspect(*_args, **_kwargs):
        calls.append(True)
        return subprocess.CompletedProcess(
            args=["ffmpeg"],
            returncode=0,
            stdout="  -timeout <int64> set timeout\n",
            stderr="",
        )

    monkeypatch.setattr(supervisor.subprocess, "run", inspect)

    assert supervisor.ffmpeg_supports_rtsp_option("timeout") is True
    assert supervisor.ffmpeg_supports_rtsp_option("timeout") is True
    assert len(calls) == 1


def test_ffmpeg_capability_probe_fails_closed_on_timeout(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_capability_timeout")

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)

    monkeypatch.setattr(supervisor.subprocess, "run", time_out)

    assert supervisor.ffmpeg_supports_rtsp_option("timeout") is False


def test_source_probe_omits_unadvertised_rtsp_options(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_probe_options")
    calls = []

    def capture(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(supervisor.subprocess, "run", capture)

    assert (
        supervisor.source_available(
            "rtsp://camera.local/stream",
            rtsp_timeout_supported=False,
            rtsp_rw_timeout_supported=False,
        )
        is True
    )
    assert "-timeout" not in calls[0]
    assert "-rw_timeout" not in calls[0]


def test_progress_watchdog_detects_stall(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_progress")
    watch = supervisor.ProgressWatchdog(started_at=10.0, timeout=30.0)

    assert watch.observe("out_time_us=5000000", now=20.0) is True
    assert watch.observe("out_time_us=5000000", now=30.0) is False
    assert watch.stalled(now=49.9) is False
    assert watch.stalled(now=50.1) is True


def test_progress_watchdog_ignores_normal_logs(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_progress_logs")
    watch = supervisor.ProgressWatchdog(started_at=10.0, timeout=30.0)

    assert watch.observe("frame=42 fps=20.0", now=20.0) is False
    assert watch.last_progress_at is None
    assert watch.stalled(now=40.1) is True


class FakeSelector:
    def select(self, timeout):
        assert timeout <= 0.5
        return []


class FakeFfmpegProcess:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class FakeApiProcess:
    def __init__(self, *, returncode=None, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        return self.stdout_text, self.stderr_text


def test_api_wait_cancels_when_ffmpeg_exits(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_child_exit")
    api_process = FakeApiProcess()
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: api_process)
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=1),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(started_at=supervisor.time.monotonic(), timeout=30.0),
    )

    with pytest.raises(supervisor.EncoderStopped, match="exited"):
        supervisor.api_command_while_streaming(["stream-status"], runtime, timeout=5)

    assert api_process.terminated is True


def test_api_wait_parses_structured_error(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_api_error")
    payload = {
        "error": True,
        "http_status": 403,
        "message": "rate limited",
        "reasons": ["userRequestsExceedRateLimit"],
        "retry_after": 120,
        "retry_class": "quota",
    }
    api_process = FakeApiProcess(returncode=75, stderr=json.dumps(payload))

    def launch(*_args, **kwargs):
        kwargs["stderr"].write(api_process.stderr_text)
        kwargs["stderr"].flush()
        return api_process

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(started_at=supervisor.time.monotonic(), timeout=30.0),
    )

    with pytest.raises(supervisor.ApiCommandError) as raised:
        supervisor.api_command_while_streaming(["stream-status"], runtime, timeout=5)

    assert raised.value.retry_class == "quota"
    assert raised.value.retry_after == 120


def test_api_error_reports_operation_status_and_reasons_without_secrets(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_api_diagnostic")

    error = supervisor.ApiCommandError(
        returncode=75,
        operation="reconcile-broadcast",
        payload={
            "http_status": 403,
            "message": "request failed for rtmps://youtube.example/live/secret-key",
            "reasons": ["userRequestsExceedRateLimit"],
            "retry_class": "quota",
        },
    )

    message = str(error)
    assert "operation=reconcile-broadcast" in message
    assert "http_status=403" in message
    assert "reasons=userRequestsExceedRateLimit" in message
    assert "secret-key" not in message


def test_api_wait_spools_large_helper_output_without_pipe_backpressure(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_api_spool")
    payload = {"stream_id": "stream-1", "padding": "x" * 100_000}
    api_process = FakeApiProcess(returncode=0)

    def launch(*_args, **kwargs):
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["stderr"] != subprocess.PIPE
        kwargs["stdout"].write(json.dumps(payload))
        kwargs["stdout"].flush()
        return api_process

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    result = supervisor.api_command_while_streaming(["stream-status"], runtime, timeout=5)

    assert result == payload


def test_api_wait_wraps_helper_launch_failure(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_api_missing")
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing helper")),
    )
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    with pytest.raises(supervisor.ApiCommandError) as raised:
        supervisor.api_command_while_streaming(["stream-status"], runtime, timeout=5)

    assert raised.value.retry_class == "fatal"


@pytest.mark.parametrize(
    ("lifecycle", "expected_action"),
    [
        ("created", "testing"),
        ("ready", "testing"),
        ("testStarting", "poll"),
        ("testing", "live"),
        ("liveStarting", "poll"),
        ("live", "confirm"),
    ],
)
def test_lifecycle_action_matrix(load_script, lifecycle, expected_action):
    supervisor = load_script("youtube-autoencoder", f"yta_lifecycle_{lifecycle}")

    assert supervisor.lifecycle_action(lifecycle) == expected_action


def test_created_lifecycle_waits_for_ready_before_transition(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_lifecycle_created_poll")
    events = []
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    monkeypatch.setattr(supervisor, "wait_with_runtime", lambda _runtime, _duration: events.append("wait"))
    monkeypatch.setattr(
        supervisor,
        "current_observation",
        lambda _runtime, _state: events.append("observe")
        or {
            "stream_status": "active",
            "health": "good",
            "lifecycle": "live",
            "privacy": "unlisted",
            "encoder_alive": True,
            "media_fresh": True,
        },
    )
    monkeypatch.setattr(
        supervisor,
        "api_command_while_streaming",
        lambda *_args, **_kwargs: pytest.fail("created broadcast was transitioned before ready"),
    )

    state = supervisor.reconcile_lifecycle(
        runtime,
        {"stream_id": "stream-1", "broadcast_id": "broadcast-1", "lifecycle": "created"},
    )

    assert state["lifecycle"] == "live"
    assert events == ["wait", "observe"]


def test_current_observation_rejects_broadcast_rebound_to_different_stream(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_observation_wrong_stream")
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )
    responses = iter(
        [
            {"stream_id": "stream-1", "stream_status": "active", "health": "good"},
            {
                "broadcast_id": "broadcast-1",
                "lifecycle": "ready",
                "privacy": "unlisted",
                "bound_stream_id": "stream-2",
            },
        ]
    )
    monkeypatch.setattr(
        supervisor,
        "api_command_while_streaming",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(supervisor.ApiCommandError, match="bound to a different stream") as raised:
        supervisor.current_observation(
            runtime,
            {
                "stream_id": "stream-1",
                "broadcast_id": "broadcast-1",
                "lifecycle": "ready",
                "privacy": "unlisted",
            },
        )

    assert raised.value.retry_class == "ambiguous"


@pytest.mark.parametrize(
    ("retry_class", "first", "maximum"),
    [
        ("source", 10, 300),
        ("api", 30, 900),
        ("quota", 900, 21600),
        ("ambiguous", 300, 3600),
    ],
)
def test_retry_backoff_bases_and_caps(load_script, retry_class, first, maximum):
    supervisor = load_script("youtube-autoencoder", f"yta_backoff_{retry_class}")

    assert supervisor.retry_delay(retry_class, attempt=1, jitter=0.0) == first
    assert supervisor.retry_delay(retry_class, attempt=20, jitter=0.0) == maximum


def test_retry_backoff_honors_longer_retry_after(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_backoff_retry_after")

    assert supervisor.retry_delay("api", attempt=1, jitter=0.0, retry_after=120) == 120


def test_quota_jitter_never_drops_below_initial_floor(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_backoff_quota_floor")
    monkeypatch.setattr(supervisor.random, "uniform", lambda lower, _upper: lower)

    assert supervisor.retry_delay("quota", attempt=1) == 900


def test_persisted_future_cooldown_is_honored(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_backoff_persisted")
    now = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.UTC)
    state = {"retry_not_before": "2026-07-10T12:01:00Z"}

    assert supervisor.retry_cooldown_remaining(state, now=now) == 60
    assert supervisor.retry_cooldown_remaining(state, now=now + dt.timedelta(minutes=2)) == 0


def test_classify_api_error_preserves_structured_retry_class(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_classify_api")
    error = supervisor.ApiCommandError(
        returncode=75,
        payload={"message": "rate limited", "retry_class": "quota"},
    )

    assert supervisor.classify_api_error(error) == "quota"


def test_publication_requires_two_healthy_live_observations(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_public_gate")
    gate = supervisor.PublicationGate(required=2)
    healthy = {"stream_status": "active", "health": "good", "lifecycle": "live"}

    assert gate.observe(healthy) is False
    assert gate.observe(healthy) is True


@pytest.mark.parametrize(
    "unhealthy",
    [
        {"stream_status": "inactive", "health": "good", "lifecycle": "live"},
        {"stream_status": "active", "health": "bad", "lifecycle": "live"},
        {"stream_status": "active", "health": "noData", "lifecycle": "live"},
        {"stream_status": "active", "health": "good", "lifecycle": "testing"},
        {
            "stream_status": "active",
            "health": "good",
            "lifecycle": "live",
            "encoder_alive": False,
        },
        {
            "stream_status": "active",
            "health": "good",
            "lifecycle": "live",
            "media_fresh": False,
        },
    ],
)
def test_publication_gate_resets_on_unhealthy_observation(load_script, unhealthy):
    supervisor = load_script("youtube-autoencoder", "yta_public_gate_reset")
    gate = supervisor.PublicationGate(required=2)
    healthy = {"stream_status": "active", "health": "ok", "lifecycle": "live"}

    assert gate.observe(healthy) is False
    assert gate.observe(unhealthy) is False
    assert gate.observe(healthy) is False


def test_already_public_state_skips_youtube_health_polling(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_public_stable")
    calls = []
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    def capture(args, _runtime, timeout):
        calls.append((args, timeout))
        return {}

    monkeypatch.setattr(supervisor, "api_command_while_streaming", capture)
    monkeypatch.setattr(
        supervisor,
        "current_observation",
        lambda *_args, **_kwargs: pytest.fail("stable public state should not poll YouTube"),
    )
    state = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "live",
        "privacy": "public",
    }

    result = supervisor.publish_when_healthy(runtime, state)

    assert result["privacy"] == "public"
    assert [args[0] for args, _timeout in calls] == ["clear-retry"]


def test_encoder_exit_after_live_never_promotes_privacy(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_public_child_exit")
    calls = []
    process = FakeFfmpegProcess(returncode=None)
    runtime = supervisor.StreamRuntime(
        process=process,
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    def observe_once(_runtime, _state):
        process.returncode = 1
        return {"stream_status": "active", "health": "good", "lifecycle": "live"}

    def capture(args, _runtime, timeout):
        calls.append((args, timeout))
        return {}

    monkeypatch.setenv("YTA_YOUTUBE_POLL_INTERVAL_SEC", "0")
    monkeypatch.setattr(supervisor, "current_observation", observe_once)
    monkeypatch.setattr(supervisor, "api_command_while_streaming", capture)
    state = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "live",
        "privacy": "unlisted",
    }

    with pytest.raises(supervisor.EncoderStopped):
        supervisor.publish_when_healthy(runtime, state)

    assert not any(args[0] == "set-privacy" for args, _timeout in calls)


def test_managed_lifecycle_waits_for_ingest_before_reconcile(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_lifecycle_order")
    events = []
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )

    def active(_runtime):
        events.append("active")
        return {"stream_id": "stream-1", "stream_status": "active"}

    def command(args, _runtime, timeout):
        events.append(args[0])
        assert timeout == 120
        assert args[0] == "reconcile-broadcast"
        assert "--allow-create" in args
        return {
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
            "lifecycle": "ready",
            "privacy": "unlisted",
        }

    def lifecycle(_runtime, state):
        events.append("lifecycle")
        return {**state, "lifecycle": "live"}

    def publish(_runtime, state):
        events.append("publish")
        return {**state, "privacy": "public"}

    monkeypatch.setattr(supervisor, "wait_for_active_stream", active)
    monkeypatch.setattr(supervisor, "api_command_while_streaming", command)
    monkeypatch.setattr(supervisor, "reconcile_lifecycle", lifecycle)
    monkeypatch.setattr(supervisor, "publish_when_healthy", publish)

    state = supervisor.manage_youtube_lifecycle(runtime)

    assert events == ["active", "reconcile-broadcast", "lifecycle", "publish"]
    assert state["privacy"] == "public"


def test_prepared_lifecycle_reuses_staged_state_after_ingest(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_prepared_lifecycle")
    events = []
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )
    prepared = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "ready",
        "privacy": "unlisted",
    }

    monkeypatch.setattr(
        supervisor,
        "wait_for_active_stream",
        lambda _runtime: events.append("active") or {"stream_id": "stream-1"},
    )
    monkeypatch.setattr(
        supervisor,
        "api_command_while_streaming",
        lambda *_args, **_kwargs: pytest.fail("prepared state was reconciled again"),
    )
    monkeypatch.setattr(
        supervisor,
        "reconcile_lifecycle",
        lambda _runtime, state: events.append("lifecycle") or {**state, "lifecycle": "live"},
    )
    monkeypatch.setattr(
        supervisor,
        "publish_when_healthy",
        lambda _runtime, state: events.append("publish") or {**state, "privacy": "public"},
    )

    state = supervisor.manage_youtube_lifecycle(runtime, prepared_state=prepared)

    assert events == ["active", "lifecycle", "publish"]
    assert state["privacy"] == "public"


def test_nonpublic_state_is_staged_before_ffmpeg_starts(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_preingest_stage_order")
    events = []
    cached = {
        "stream_id": "stream-1",
        "generation_id": "generation-1",
        "pending_action": "create",
        "privacy": "unlisted",
    }
    prepared = {
        **cached,
        "broadcast_id": "broadcast-1",
        "lifecycle": "ready",
    }

    class Process:
        stdout = None
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(supervisor, "stream_config", lambda: ("rtsp://camera/stream", "rtmps://youtube/live/key"))
    monkeypatch.setattr(supervisor, "source_available", lambda _url: True)
    monkeypatch.setattr(supervisor, "ffmpeg_args", lambda *_args, **_kwargs: ["ffmpeg"])
    monkeypatch.setattr(
        supervisor,
        "prepare_youtube_lifecycle",
        lambda state: events.append("stage") or prepared,
        raising=False,
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: events.append("ffmpeg") or Process(),
    )
    monkeypatch.setattr(
        supervisor,
        "manage_with_public_fallback",
        lambda _runtime, state: events.append(("manage", state)) or state,
    )
    monkeypatch.setattr(
        supervisor,
        "supervise_stream",
        lambda *_args, **_kwargs: events.append("supervise") or 0,
    )

    assert supervisor.run_once(cached_state=cached) == 0
    assert events[0:2] == ["stage", "ffmpeg"]
    assert events[2] == ("manage", prepared)


def test_verified_public_state_skips_preingest_api_staging(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_public_preingest_skip")
    cached = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "live",
        "privacy": "public",
    }
    monkeypatch.setattr(
        supervisor,
        "api_command",
        lambda *_args, **_kwargs: pytest.fail("public recovery was blocked on the API"),
    )

    assert supervisor.prepare_youtube_lifecycle(cached) == cached


def test_legacy_public_privacy_never_changes_staging_default(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_staging_legacy_privacy")
    monkeypatch.delenv("YTA_YOUTUBE_STAGING_PRIVACY", raising=False)
    monkeypatch.setenv("YTA_YOUTUBE_PRIVACY", "public")

    args = supervisor.reconcile_command_args()

    assert args[args.index("--privacy") + 1] == "unlisted"


def test_unattended_supervisor_has_no_completion_helper(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_no_auto_complete")

    assert not hasattr(supervisor, "complete_broadcast")


def test_cached_public_stream_continues_during_api_quota_failure(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_public_api_fallback")
    calls = []
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )
    cached = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "live",
        "privacy": "public",
    }
    failure = supervisor.ApiCommandError(
        returncode=75,
        payload={"message": "rate limited", "retry_class": "quota"},
    )
    monkeypatch.setattr(
        supervisor,
        "manage_youtube_lifecycle",
        lambda _runtime, prepared_state=None: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(supervisor, "retry_delay", lambda *_args, **_kwargs: 900)

    def capture(args, _runtime, timeout):
        calls.append((args, timeout))
        return {}

    monkeypatch.setattr(supervisor, "api_command_while_streaming", capture)

    state = supervisor.manage_with_public_fallback(runtime, cached)

    assert state == cached
    assert calls[0][0][0:4] == ["set-retry", "quota", "1", calls[0][0][3]]
    assert calls[0][1] == 30


def test_prepublic_api_failure_does_not_enter_public_fallback(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_prepublic_api_failure")
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(
            started_at=supervisor.time.monotonic(),
            timeout=30.0,
            last_progress_at=supervisor.time.monotonic(),
        ),
    )
    cached = {
        "stream_id": "stream-1",
        "broadcast_id": "broadcast-1",
        "lifecycle": "live",
        "privacy": "unlisted",
    }
    failure = supervisor.ApiCommandError(
        returncode=75,
        payload={"message": "rate limited", "retry_class": "quota"},
    )
    monkeypatch.setattr(
        supervisor,
        "manage_youtube_lifecycle",
        lambda _runtime, prepared_state=None: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(supervisor.ApiCommandError):
        supervisor.manage_with_public_fallback(runtime, cached)


def test_supervisor_lock_rejects_second_instance(load_script, monkeypatch, tmp_path):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_lock")
    monkeypatch.setattr(supervisor, "SUPERVISOR_LOCK_FILE", tmp_path / "supervisor.lock")

    with supervisor.supervisor_lock():
        with pytest.raises(RuntimeError, match="another supervisor"):
            with supervisor.supervisor_lock():
                pass
