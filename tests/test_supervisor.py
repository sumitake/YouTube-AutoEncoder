from __future__ import annotations


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

    redacted = supervisor.redact_text(f"input={camera_url} output={ingest_url}", [camera_url, ingest_url])
    assert "user" not in redacted
    assert "pass" not in redacted
    assert "secret-stream-key" not in redacted


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

    args = supervisor.ffmpeg_args("rtsp://camera.local/stream", "rtmps://youtube.example/live/key")

    assert args[0] == "ffmpeg"
    assert args[args.index("-c:v") + 1] == "copy"
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in args
    assert args[-1] == "rtmps://youtube.example/live/key"


def test_ffmpeg_transcode_args(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_transcode_args")
    monkeypatch.setenv("YTA_MODE", "transcode")
    monkeypatch.setenv("YTA_TRANSCODE_SCALE", "-2:1080")
    monkeypatch.setenv("YTA_FPS", "30")

    args = supervisor.ffmpeg_args("rtsp://camera.local/stream", "rtmps://youtube.example/live/key")

    assert args[args.index("-c:v") + 1] == "libx264"
    assert "scale=-2:1080,fps=30,format=yuv420p" in args
    assert args[args.index("-f", args.index("-c:a")) + 1] == "flv"
