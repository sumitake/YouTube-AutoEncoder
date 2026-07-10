from __future__ import annotations

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
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: api_process)
    runtime = supervisor.StreamRuntime(
        process=FakeFfmpegProcess(returncode=None),
        selector=FakeSelector(),
        watchdog=supervisor.ProgressWatchdog(started_at=supervisor.time.monotonic(), timeout=30.0),
    )

    with pytest.raises(supervisor.ApiCommandError) as raised:
        supervisor.api_command_while_streaming(["stream-status"], runtime, timeout=5)

    assert raised.value.retry_class == "quota"
    assert raised.value.retry_after == 120
