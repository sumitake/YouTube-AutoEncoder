from __future__ import annotations

import configparser
import datetime as dt
import json
import os
import pathlib
import shlex
import subprocess
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def repo_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def environment_assignments(text: str) -> dict[str, str]:
    assignments = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"line {line_number} is not an environment assignment"
        assert key not in assignments, f"duplicate environment assignment: {key}"
        assignments[key] = value
    return assignments


def systemd_sections(relative_path: str) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(repo_text(relative_path))
    return {section: dict(parser[section]) for section in parser.sections()}


def workflow_step_run(workflow: str, step_name: str) -> str:
    lines = workflow.splitlines()
    marker = f"      - name: {step_name}"
    start = lines.index(marker)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("      - name: ")),
        len(lines),
    )
    block = lines[start + 1 : end]
    for index, line in enumerate(block):
        if line.startswith("        run: |"):
            return "\n".join(item[10:] for item in block[index + 1 :] if item.startswith("          "))
        if line.startswith("        run: "):
            return line.removeprefix("        run: ")
    raise AssertionError(f"workflow step has no run command: {step_name}")


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


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "TrUe"])
def test_enable_flag_accepts_true_case_insensitively_and_legacy_one(load_script, monkeypatch, value):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_enabled_{value}")
    monkeypatch.setenv("YTA_TELEMETRY_ENABLED", value)

    assert telemetry.telemetry_enabled() is True


@pytest.mark.parametrize("value", [None, "false", "0", "yes", "enabled", ""])
def test_enable_flag_rejects_unset_false_zero_and_unrelated_values(load_script, monkeypatch, value):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_disabled_{value}")
    if value is None:
        monkeypatch.delenv("YTA_TELEMETRY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("YTA_TELEMETRY_ENABLED", value)

    assert telemetry.telemetry_enabled() is False


def test_disabled_collector_has_no_files_subprocess_and_logs_skip_reason(
    load_script, monkeypatch, tmp_path, capsys
):
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
    assert json.loads(capsys.readouterr().err) == {"skip_reason": "disabled"}


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


def test_lifecycle_ineligible_exit_logs_fixed_skip_reason(load_script, monkeypatch, tmp_path, capsys):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_non_live_skip_reason")
    configure_collector(telemetry, monkeypatch, tmp_path, {"lifecycle": "ready", "broadcast_id": "video-1"})
    monkeypatch.setattr(
        telemetry.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ineligible telemetry invoked helper"),
    )

    assert telemetry.collect_once(NOW) == 0
    assert not telemetry.TELEMETRY_DIR.exists()
    assert json.loads(capsys.readouterr().err) == {"skip_reason": "not_live"}


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


MISSING = object()


@pytest.mark.parametrize("field", ["instance_id", "generation_id", "privacy"])
@pytest.mark.parametrize(
    ("value", "case"),
    [(MISSING, "missing"), ("", "empty"), (False, "bool"), (7, "non_string")],
)
def test_invalid_sample_context_fails_before_attempt_or_helper(
    load_script, monkeypatch, tmp_path, field, value, case
):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_invalid_context_{field}_{case}")
    state = live_state()
    if value is MISSING:
        state.pop(field)
    else:
        state[field] = value
    configure_collector(telemetry, monkeypatch, tmp_path, state)
    helper_calls = []

    def fake_run(*_args, **_kwargs):
        helper_calls.append(True)
        return successful_helper(helper_payload())

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    assert telemetry.collect_once(NOW) == 1
    assert helper_calls == []
    assert not (telemetry.TELEMETRY_DIR / "collector-state.json").exists()
    assert not (telemetry.TELEMETRY_DIR / "latest.json").exists()


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
    ("result", "expected_status", "expected_log"),
    [
        (
            types.SimpleNamespace(
                returncode=75,
                stdout="ignored output",
                stderr=json.dumps({"retry_class": "quota", "http_status": 403, "reasons": ["quotaExceeded"]}),
            ),
            75,
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
            1,
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
            1,
            {
                "operation": "video-metrics",
                "return_code": None,
                "retry_class": "helper",
                "http_status": None,
                "reasons": ["invalid_payload"],
            },
        ),
        (
            types.SimpleNamespace(
                returncode=-9,
                stdout="ignored output",
                stderr=json.dumps({"retry_class": "fatal", "http_status": None, "reasons": ["signal"]}),
            ),
            1,
            {
                "operation": "video-metrics",
                "return_code": -9,
                "retry_class": "fatal",
                "http_status": None,
                "reasons": ["signal"],
            },
        ),
    ],
    ids=["nonzero", "timeout", "malformed_json", "negative_signal"],
)
def test_helper_status_and_failures_preserve_attempt_without_latest(
    load_script, monkeypatch, tmp_path, capsys, result, expected_status, expected_log
):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_helper_failure")
    configure_collector(telemetry, monkeypatch, tmp_path, live_state())

    def fake_run(*_args, **_kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(telemetry.subprocess, "run", fake_run)

    assert telemetry.collect_once(NOW) == expected_status
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

    assert telemetry.collect_once(NOW) == 75

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


def test_retention_days_defaults_to_thirty(load_script, monkeypatch):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_retention_days_default")
    monkeypatch.delenv("YTA_TELEMETRY_RETENTION_DAYS", raising=False)

    assert telemetry.telemetry_retention_days() == 30


def test_retention_days_invalid_text_falls_back_to_thirty(load_script, monkeypatch):
    telemetry = load_script("youtube-autoencoder-telemetry", "yta_telemetry_retention_days_invalid")
    monkeypatch.setenv("YTA_TELEMETRY_RETENTION_DAYS", "invalid")

    assert telemetry.telemetry_retention_days() == 30


@pytest.mark.parametrize("value", ["0", "-7"])
def test_retention_days_numeric_values_below_one_clamp_to_one(load_script, monkeypatch, value):
    telemetry = load_script("youtube-autoencoder-telemetry", f"yta_telemetry_retention_days_clamp_{value}")
    monkeypatch.setenv("YTA_TELEMETRY_RETENTION_DAYS", value)

    assert telemetry.telemetry_retention_days() == 1


def test_telemetry_units_example_config_is_disabled_and_quota_bounded():
    config = repo_text("config/youtube-autoencoder.env.example")
    assignments = environment_assignments(config)

    assert assignments["YTA_TELEMETRY_ENABLED"] == "false"
    assert assignments["YTA_TELEMETRY_MIN_INTERVAL_SEC"] == "300"
    assert assignments["YTA_TELEMETRY_RETENTION_DAYS"] == "30"
    assert assignments["YTA_TELEMETRY_API"] == "/usr/local/bin/youtube-autoencoder-api"
    assert "Enable exactly one telemetry timer mode" in config


@pytest.mark.parametrize(
    ("path", "environment_file", "working_directory", "user_line"),
    [
        (
            "systemd/youtube-autoencoder-telemetry@.service",
            "/home/%i/.config/youtube-autoencoder/youtube-autoencoder.env",
            "/home/%i",
            "User=%i",
        ),
        (
            "systemd/user/youtube-autoencoder-telemetry.service",
            "%h/.config/youtube-autoencoder/youtube-autoencoder.env",
            "%h",
            None,
        ),
    ],
)
def test_telemetry_units_services_are_isolated_oneshots(path, environment_file, working_directory, user_line):
    sections = systemd_sections(path)
    expected_service = {
        "Type": "oneshot",
        "EnvironmentFile": environment_file,
        "ExecStart": "/usr/local/bin/youtube-autoencoder-telemetry",
        "WorkingDirectory": working_directory,
        "Nice": "10",
        "IOSchedulingClass": "idle",
    }
    if user_line is not None:
        key, value = user_line.split("=", maxsplit=1)
        expected_service[key] = value
    assert sections["Service"] == expected_service
    all_values = {value for section in sections.values() for value in section.values()}
    assert not any("youtube-autoencoder@" in value for value in all_values)
    assert not any("youtube-autoencoder.service" in value for value in all_values)


@pytest.mark.parametrize(
    ("path", "service_unit"),
    [
        ("systemd/youtube-autoencoder-telemetry@.timer", "youtube-autoencoder-telemetry@%i.service"),
        ("systemd/user/youtube-autoencoder-telemetry.timer", "youtube-autoencoder-telemetry.service"),
    ],
)
def test_telemetry_units_timers_have_exact_nonpersistent_cadence(path, service_unit):
    sections = systemd_sections(path)

    assert sections["Timer"] == {
        "OnBootSec": "5min",
        "OnUnitActiveSec": "5min",
        "AccuracySec": "30s",
        "RandomizedDelaySec": "30s",
        "Persistent": "false",
        "Unit": service_unit,
    }
    assert sections["Install"] == {"WantedBy": "timers.target"}


def test_telemetry_units_do_not_couple_existing_encoder_services():
    assert "telemetry" not in repo_text("systemd/youtube-autoencoder@.service").casefold()
    assert "telemetry" not in repo_text("systemd/user/youtube-autoencoder.service").casefold()


def test_telemetry_units_ci_validates_collector_and_all_units():
    workflow = repo_text(".github/workflows/ci.yml")
    compile_command = shlex.split(workflow_step_run(workflow, "Compile scripts"))
    executable_commands = workflow_step_run(workflow, "Verify executable scripts").splitlines()
    systemd_command = workflow_step_run(workflow, "Verify systemd units")
    systemd_lines = systemd_command.splitlines()
    verify_start = systemd_lines.index("systemd-analyze verify \\")
    verify_command = shlex.split(" ".join(line.removesuffix(" \\") for line in systemd_lines[verify_start:]))

    assert compile_command == [
        "python",
        "-m",
        "py_compile",
        "bin/youtube-autoencoder",
        "bin/youtube-autoencoder-api",
        "bin/youtube-autoencoder-test-pattern",
        "bin/youtube-autoencoder-telemetry",
    ]
    assert executable_commands == [
        "test -x bin/youtube-autoencoder",
        "test -x bin/youtube-autoencoder-api",
        "test -x bin/youtube-autoencoder-test-pattern",
        "test -x bin/youtube-autoencoder-telemetry",
    ]
    assert systemd_lines[:2] == [
        "sudo install -m 0755 bin/youtube-autoencoder /usr/local/bin/youtube-autoencoder",
        "sudo install -m 0755 bin/youtube-autoencoder-telemetry /usr/local/bin/youtube-autoencoder-telemetry",
    ]
    assert verify_command == [
        "systemd-analyze",
        "verify",
        "systemd/youtube-autoencoder@.service",
        "systemd/youtube-autoencoder-telemetry@.service",
        "systemd/youtube-autoencoder-telemetry@.timer",
        "systemd/user/youtube-autoencoder.service",
        "systemd/user/youtube-autoencoder-telemetry.service",
        "systemd/user/youtube-autoencoder-telemetry.timer",
    ]
    assert "systemctl" not in workflow
