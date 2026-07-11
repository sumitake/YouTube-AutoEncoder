from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import types

import pytest


def configure_collector(telemetry, monkeypatch, tmp_path, state):
    lifecycle = tmp_path / "youtube-live-state.json"
    lifecycle.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("YTA_TELEMETRY_ENABLED", "1")
    monkeypatch.setattr(telemetry, "LIFECYCLE_FILE", lifecycle)
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path / "telemetry")


def live_state():
    return {
        "instance_id": "encoder-1",
        "generation_id": "generation-1",
        "lifecycle": "live",
        "privacy": "unlisted",
        "broadcast_id": "video-1",
    }


NOW = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.UTC)


def helper_payload(**overrides):
    payload = {
        "video_id": "video-1",
        "actual_start_time": "2026-07-10T11:00:00Z",
        "actual_end_time": None,
        "scheduled_start_time": "2026-07-10T10:55:00Z",
        "scheduled_end_time": None,
        "concurrent_viewers": 4,
        "view_count": 20,
        "like_count": None,
        "comment_count": 2,
    }
    payload.update(overrides)
    return payload


def successful_helper(payload):
    return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_disabled_collector_has_no_files_or_subprocess(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_disabled")
    monkeypatch.delenv("YTA_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("disabled telemetry invoked helper"),
    )

    assert telemetry.collect_once() == 0
    assert not telemetry.TELEMETRY_DIR.exists()


def test_missing_lifecycle_state_skips_before_storage_or_helper(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_missing_lifecycle")
    monkeypatch.setenv("YTA_TELEMETRY_ENABLED", "1")
    monkeypatch.setattr(telemetry, "LIFECYCLE_FILE", tmp_path / "youtube-live-state.json")
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing lifecycle state invoked helper"),
    )

    assert telemetry.collect_once() == 0
    assert not telemetry.TELEMETRY_DIR.exists()


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"lifecycle": "ready", "broadcast_id": "video-1"},
        {"lifecycle": "live"},
        {
            "lifecycle": "live",
            "broadcast_id": "video-1",
            "retry_not_before": "2999-01-01T00:00:00Z",
        },
    ],
    ids=["missing", "non_live", "missing_broadcast", "lifecycle_cooldown"],
)
def test_locally_ineligible_collector_skips_before_storage_or_helper(load_script, monkeypatch, tmp_path, state):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_ineligible_{len(state)}")
    configure_collector(telemetry, monkeypatch, tmp_path, state)
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ineligible telemetry invoked helper"),
    )

    assert telemetry.collect_once() == 0
    assert not telemetry.TELEMETRY_DIR.exists()


def test_telemetry_minimum_interval_clamps_to_five_minutes(load_script, monkeypatch):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_interval")
    monkeypatch.setenv("YTA_TELEMETRY_MIN_INTERVAL_SEC", "1")

    assert telemetry.telemetry_min_interval() == 300


@pytest.mark.parametrize(
    ("attempt_at", "reason"),
    [
        ("2026-07-10T11:59:59Z", "telemetry_cooldown"),
        ("2026-07-10T12:00:01Z", "telemetry_cooldown"),
    ],
    ids=["recent", "future"],
)
def test_recent_or_future_attempt_is_ineligible(load_script, attempt_at, reason):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_attempt_{attempt_at[-3:-1]}")

    assert telemetry.eligibility(live_state(), {"last_attempt_at": attempt_at}, NOW) == (False, reason)


def test_malformed_collector_state_fails_without_helper(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_malformed_state")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    telemetry.TELEMETRY_DIR.mkdir()
    (telemetry.TELEMETRY_DIR / "collector-state.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("malformed state invoked helper"),
    )

    assert telemetry.collect_once(NOW) == 1


def test_attempt_is_durable_before_helper_invocation(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_write_ahead")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())

    def check_attempt(*_args, **_kwargs):
        state = json.loads((telemetry.TELEMETRY_DIR / "collector-state.json").read_text(encoding="utf-8"))
        assert state["last_attempt_at"] == "2026-07-10T12:00:00Z"
        raise telemetry.HelperError("stop after write-ahead assertion")

    monkeypatch.setattr(telemetry.subprocess, "run", check_attempt)

    assert telemetry.collect_once(NOW) == 1


def test_failed_attempt_write_prevents_helper_invocation(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_attempt_write_failure")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    monkeypatch.setattr(telemetry, "durable_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no space")))
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("failed write invoked helper"),
    )

    assert telemetry.collect_once(NOW) == 1


def test_held_nonblocking_lock_skips_cleanly(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_lock")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("held lock invoked helper"),
    )

    with telemetry.telemetry_lock() as acquired:
        assert acquired is True
        assert telemetry.collect_once(NOW) == 0


def test_video_metrics_helper_invocation_is_bounded_and_argument_safe(load_script, monkeypatch):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_helper_args")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return successful_helper(helper_payload())

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)
    monkeypatch.setattr(telemetry, "TELEMETRY_API", "/test/youtube-autoencoder-api")

    assert telemetry.invoke_video_metrics("video-1") == helper_payload()
    assert calls == [
        (
            (["/test/youtube-autoencoder-api", "video-metrics", "video-1"],),
            {"shell": False, "capture_output": True, "text": True, "timeout": 45, "check": False},
        )
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (helper_payload(video_id="video-2"), "video_id_mismatch"),
        (helper_payload(actual_start_time=4), "invalid_timestamp"),
        (helper_payload(concurrent_viewers=True), "invalid_metric"),
        (helper_payload(view_count=-1), "invalid_metric"),
        ({key: value for key, value in helper_payload().items() if key != "comment_count"}, "invalid_payload"),
        ({**helper_payload(), "unexpected": "field"}, "invalid_payload"),
    ],
    ids=["wrong_id", "bad_timestamp", "boolean_metric", "negative_metric", "missing_field", "extra_field"],
)
def test_video_metrics_rejects_wrong_or_invalid_schema(load_script, monkeypatch, payload, reason):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_invalid_payload")
    monkeypatch.setattr(telemetry.subprocess, "run", lambda *_args, **_kwargs: successful_helper(payload))

    with pytest.raises(telemetry.HelperError) as excinfo:
        telemetry.invoke_video_metrics("video-1")
    assert excinfo.value.reasons == (reason,)


def test_successful_collection_persists_one_private_sample(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_success")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    payload = helper_payload()
    monkeypatch.setattr(telemetry.subprocess, "run", lambda *_args, **_kwargs: successful_helper(payload))

    assert telemetry.collect_once(NOW) == 0

    daily = telemetry.TELEMETRY_DIR / "2026-07-10.jsonl"
    latest = telemetry.TELEMETRY_DIR / "latest.json"
    sample = json.loads(latest.read_text(encoding="utf-8"))
    assert set(sample) == {
        "collected_at",
        "instance_id",
        "generation_id",
        "broadcast_id",
        "lifecycle",
        "privacy",
        "video",
    }
    assert sample["collected_at"] == "2026-07-10T12:00:00Z"
    assert sample["instance_id"] == "encoder-1"
    assert sample["generation_id"] == "generation-1"
    assert sample["broadcast_id"] == "video-1"
    assert sample["lifecycle"] == "live"
    assert sample["privacy"] == "unlisted"
    assert sample["video"] == payload
    assert json.loads(daily.read_text(encoding="utf-8")) == sample
    assert os.stat(telemetry.TELEMETRY_DIR).st_mode & 0o777 == 0o700
    assert os.stat(daily).st_mode & 0o777 == 0o600
    assert os.stat(latest).st_mode & 0o777 == 0o600
    collector_state = json.loads((telemetry.TELEMETRY_DIR / "collector-state.json").read_text(encoding="utf-8"))
    assert collector_state == {"last_attempt_at": "2026-07-10T12:00:00Z", "last_success_at": "2026-07-10T12:00:00Z"}


@pytest.mark.parametrize(
    ("result", "expected_log"),
    [
        (
            types.SimpleNamespace(
                returncode=75,
                stdout="ignored output",
                stderr=json.dumps({"retry_class": "quota", "http_status": 403, "reasons": ["quotaExceeded"]}),
            ),
            {
                "operation": "video-metrics",
                "return_code": 75,
                "retry_class": "quota",
                "http_status": 403,
                "reasons": ["quotaExceeded"],
            },
        ),
        (
            subprocess.TimeoutExpired(["helper"], 45, output="ignored", stderr="ignored"),
            {
                "operation": "video-metrics",
                "return_code": None,
                "retry_class": "timeout",
                "http_status": None,
                "reasons": ["timeout"],
            },
        ),
        (
            successful_helper({"not": "metrics"}),
            {
                "operation": "video-metrics",
                "return_code": None,
                "retry_class": "helper",
                "http_status": None,
                "reasons": ["invalid_payload"],
            },
        ),
    ],
    ids=["nonzero", "timeout", "malformed_json"],
)
def test_helper_failures_preserve_attempt_without_latest(load_script, monkeypatch, tmp_path, capsys, result, expected_log):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_helper_failure")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())

    def fake_run(*_args, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    assert telemetry.collect_once(NOW) == 1
    assert not (telemetry.TELEMETRY_DIR / "latest.json").exists()
    state = json.loads((telemetry.TELEMETRY_DIR / "collector-state.json").read_text(encoding="utf-8"))
    assert state == {"last_attempt_at": "2026-07-10T12:00:00Z"}
    assert json.loads(capsys.readouterr().err) == expected_log


def test_sample_write_failure_preserves_attempt_without_latest(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_sample_write_failure")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    monkeypatch.setattr(telemetry.subprocess, "run", lambda *_args, **_kwargs: successful_helper(helper_payload()))
    monkeypatch.setattr(telemetry, "append_jsonl", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no space")))

    assert telemetry.collect_once(NOW) == 1
    assert not (telemetry.TELEMETRY_DIR / "latest.json").exists()
    state = json.loads((telemetry.TELEMETRY_DIR / "collector-state.json").read_text(encoding="utf-8"))
    assert state == {"last_attempt_at": "2026-07-10T12:00:00Z"}


def test_helper_failure_log_is_structured_and_sanitized(load_script, monkeypatch, tmp_path, capsys):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_sanitized_log")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=75,
            stdout="sensitive output",
            stderr=json.dumps(
                {
                    "retry_class": "quota",
                    "http_status": 403,
                    "reasons": ["quotaExceeded"],
                    "message": "sensitive message",
                    "url": "https://example.invalid/private",
                }
            ),
        ),
    )

    assert telemetry.collect_once(NOW) == 1

    logged = json.loads(capsys.readouterr().err)
    assert logged == {
        "operation": "video-metrics",
        "return_code": 75,
        "retry_class": "quota",
        "http_status": 403,
        "reasons": ["quotaExceeded"],
    }


def test_retention_prunes_only_old_exact_regular_daily_files(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_retention")
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setenv("YTA_TELEMETRY_RETENTION_DAYS", "2")
    telemetry.TELEMETRY_DIR.mkdir()
    old_daily = telemetry.TELEMETRY_DIR / "2026-07-08.jsonl"
    retained_daily = telemetry.TELEMETRY_DIR / "2026-07-09.jsonl"
    current_daily = telemetry.TELEMETRY_DIR / "2026-07-10.jsonl"
    invalid_date = telemetry.TELEMETRY_DIR / "2026-02-31.jsonl"
    unrelated = telemetry.TELEMETRY_DIR / "2026-07-08.jsonl.bak"
    target = tmp_path / "outside.jsonl"
    linked_daily = telemetry.TELEMETRY_DIR / "2026-07-07.jsonl"
    for path in (old_daily, retained_daily, current_daily, invalid_date, unrelated, target):
        path.write_text("{}\n", encoding="utf-8")
    linked_daily.symlink_to(target)
    dated_directory = telemetry.TELEMETRY_DIR / "2026-07-06.jsonl"
    dated_directory.mkdir()

    telemetry.prune_daily_files(NOW)

    assert not old_daily.exists()
    assert retained_daily.exists()
    assert current_daily.exists()
    assert invalid_date.exists()
    assert unrelated.exists()
    assert linked_daily.is_symlink()
    assert target.exists()
    assert dated_directory.is_dir()


def test_retention_clamps_to_current_utc_day(load_script, monkeypatch, tmp_path):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_retention_clamp")
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setenv("YTA_TELEMETRY_RETENTION_DAYS", "0")
    telemetry.TELEMETRY_DIR.mkdir()
    previous = telemetry.TELEMETRY_DIR / "2026-07-09.jsonl"
    current = telemetry.TELEMETRY_DIR / "2026-07-10.jsonl"
    previous.write_text("{}\n", encoding="utf-8")
    current.write_text("{}\n", encoding="utf-8")

    telemetry.prune_daily_files(NOW)

    assert not previous.exists()
    assert current.exists()
