from __future__ import annotations


def test_test_pattern_ffmpeg_args(load_script):
    test_pattern = load_script("youtube-autoencoder-test-pattern", "yta_test_pattern_args")
    output_url = "rtmps://youtube.example/live/key"

    args = test_pattern.ffmpeg_args(output_url)

    assert args[0] == "ffmpeg"
    assert "testsrc2=size=1280x720:rate=30" in args
    assert "sine=frequency=440:sample_rate=48000" in args
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[-1] == output_url


def test_test_pattern_redacts_rtmp_secret_from_log_lines(load_script):
    test_pattern = load_script("youtube-autoencoder-test-pattern", "yta_test_pattern_redaction")

    redacted = test_pattern.redact_text("target=rtmps://youtube.example/live/secret-stream-key")

    assert redacted == "target=rtmps://youtube.example/<redacted>"
    assert "secret-stream-key" not in redacted
