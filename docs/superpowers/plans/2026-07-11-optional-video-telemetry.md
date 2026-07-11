# Optional Video Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a disabled-by-default, quota-bounded telemetry collector that retrieves live-video aggregate metrics through one read-only YouTube Data API `videos.list` call and stores private local samples without affecting stream recovery.

**Architecture:** Extend the existing OAuth-aware API helper with a read-only `video-metrics` command. Add a separate one-shot collector that performs all local eligibility and write-ahead throttle checks before invoking that command, then install independent system and user systemd timers that remain disabled until explicitly enabled.

**Tech Stack:** Python 3.11 standard library, YouTube Data API v3, systemd, pytest, Ruff, Markdownlint, GitHub Actions.

## Global Constraints

- Do not add YouTube Analytics, YouTube Reporting, paid services, external storage, third-party runtime packages, network listeners, or new OAuth scopes.
- `YTA_TELEMETRY_ENABLED` defaults to `false`; installation and Pi deployment leave all telemetry timers disabled.
- A collector invocation performs zero API calls unless the cached broadcast is `live`, has an ID, is outside lifecycle cooldown, and is outside the telemetry minimum interval.
- Clamp `YTA_TELEMETRY_MIN_INTERVAL_SEC` to at least `300`; persist `last_attempt_at` before the API subprocess so failed calls and crashes remain throttled.
- A continuously live deployment with one timer mode active consumes at most 288 `videos.list` quota units in 24 hours at the default cadence.
- Telemetry cannot invoke or influence FFmpeg, broadcast reconciliation, lifecycle transitions, privacy changes, retry mutation, or encoder service restart behavior.
- Use private `0700` directories and `0600` files; never log credentials, URLs, environments, arbitrary helper output, or source data.
- Use TDD for every runtime behavior: write the focused test, verify the expected failure, implement the minimum behavior, and verify green before proceeding.
- Keep implementation compatible with Python 3.11, Raspberry Pi OS, the repository's standard-library runtime, and existing system/user installation patterns.

---

### Task 1: Read-Only `videos.list` API Command

**Files:**

- Modify: `tests/test_api.py`
- Modify: `bin/youtube-autoencoder-api`

**Interfaces:**

- Produces: `metric_int(value: Any, field: str) -> int | None`
- Produces: `read_state_snapshot() -> dict[str, Any]`
- Produces: `video_metrics(video_id: str) -> dict[str, Any]`
- Produces: `video_metrics_command(args: argparse.Namespace) -> int`
- Produces CLI: `youtube-autoencoder-api video-metrics [video_id]`

- [ ] **Step 1: Write the failing request-shape and normalization tests**

Add tests that monkeypatch `api()` and assert one call with the exact method, path, and parameters:

```python
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

    assert calls == [("GET", "/videos", {
        "id": "video-1",
        "part": "liveStreamingDetails,statistics",
        "fields": (
            "items(id,liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime,"
            "scheduledEndTime,concurrentViewers),statistics(viewCount,likeCount,commentCount))"
        ),
    }, None)]
    assert result["concurrent_viewers"] == 12
    assert result["view_count"] == 345
```

Also add focused tests proving absent optional values remain `None`, boolean/negative/non-numeric counts fail, an empty `items` list fails, and a mismatched response video ID fails.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_api.py -k 'video_metrics'
```

Expected: failures because `video_metrics` and `metric_int` do not exist.

- [ ] **Step 3: Implement normalized `videos.list` access**

Add exact constants and functions:

```python
VIDEO_METRICS_FIELDS = (
    "items(id,liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime,"
    "scheduledEndTime,concurrentViewers),statistics(viewCount,likeCount,commentCount))"
)


def metric_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ReconciliationError(f"invalid {field} metric")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"invalid {field} metric") from exc
    if parsed < 0:
        raise ReconciliationError(f"invalid {field} metric")
    return parsed


def video_metrics(video_id: str) -> dict[str, Any]:
    data = api(
        "GET",
        "/videos",
        {
            "id": video_id,
            "part": "liveStreamingDetails,statistics",
            "fields": VIDEO_METRICS_FIELDS,
        },
    )
    items = data.get("items") or []
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ReconciliationError(f"video not found: {video_id}")
    item = items[0]
    if str(item.get("id") or "") != video_id:
        raise ReconciliationError("videos.list returned an unexpected video id")
    live = item.get("liveStreamingDetails") or {}
    statistics = item.get("statistics") or {}
    if not isinstance(live, dict) or not isinstance(statistics, dict):
        raise ReconciliationError("videos.list returned malformed metrics")
    return {
        "video_id": video_id,
        "actual_start_time": live.get("actualStartTime"),
        "actual_end_time": live.get("actualEndTime"),
        "scheduled_start_time": live.get("scheduledStartTime"),
        "scheduled_end_time": live.get("scheduledEndTime"),
        "concurrent_viewers": metric_int(live.get("concurrentViewers"), "concurrentViewers"),
        "view_count": metric_int(statistics.get("viewCount"), "viewCount"),
        "like_count": metric_int(statistics.get("likeCount"), "likeCount"),
        "comment_count": metric_int(statistics.get("commentCount"), "commentCount"),
    }
```

Use the existing `api()` function so OAuth refresh, structured YouTube errors, and redaction remain centralized.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Task 1 focused command. Expected: all `video_metrics` tests pass.

- [ ] **Step 5: Write the failing CLI and non-mutating cache tests**

Add four focused tests proving that the command:

- Uses an explicit video ID without reading lifecycle state.
- Reads the broadcast ID from a valid lifecycle-state snapshot when no ID is supplied.
- Rejects a missing cached broadcast ID without mutation.
- Registers `video_id` as an optional parser argument.

For the cache test, point `STATE_FILE` at malformed JSON and assert the command raises without renaming, deleting, or rewriting the file.

- [ ] **Step 6: Run the CLI tests and verify RED**

Expected: failures because the command and parser are absent.

- [ ] **Step 7: Implement the CLI boundary**

Add a non-mutating snapshot reader that calls `read_json(STATE_FILE)` directly and never calls `read_state()`. Add `video_metrics_command`, print sorted JSON, and register:

```python
video_metrics_p = sub.add_parser("video-metrics", help="Show read-only aggregate metrics for a live video")
video_metrics_p.add_argument("video_id", nargs="?")
video_metrics_p.set_defaults(func=video_metrics_command)
```

- [ ] **Step 8: Verify Task 1 and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_api.py
.venv/bin/ruff check bin/youtube-autoencoder-api tests/test_api.py
python3 -m py_compile bin/youtube-autoencoder-api
```

Expected: pass. Commit:

```bash
git add bin/youtube-autoencoder-api tests/test_api.py
git commit -S -m "api: add read-only video metrics"
```

---

### Task 2: Quota-Bounded One-Shot Collector

**Files:**

- Create: `tests/test_telemetry.py`
- Create: `bin/youtube-autoencoder-telemetry`

**Interfaces:**

- Produces: `TelemetryError`, `HelperError`
- Produces: `parse_utc(value: str, field: str) -> datetime`
- Produces: `durable_json(path: pathlib.Path, data: dict[str, Any]) -> None`
- Produces: `append_jsonl(path: pathlib.Path, data: dict[str, Any]) -> None`
- Produces: `eligibility(state: dict[str, Any], collector_state: dict[str, Any], now: datetime) -> tuple[bool, str]`
- Produces: `invoke_video_metrics(video_id: str) -> dict[str, Any]`
- Produces: `collect_once(now: datetime | None = None) -> int`

- [ ] **Step 1: Write disabled and ineligible RED tests**

Create `tests/test_telemetry.py` with fixtures that load the extensionless script. Add tests proving disabled mode does not create a directory or call `subprocess.run`, and enabled missing/non-live state skips before subprocess.

```python
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
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
.venv/bin/pytest -q tests/test_telemetry.py -k 'disabled or missing or non_live'
```

Expected: load failure because the collector does not exist.

- [ ] **Step 3: Implement configuration and pre-lock eligibility**

Create an executable Python 3.11 script using only standard-library modules. Define dynamic environment defaults for lifecycle state, telemetry directory, helper path, minimum interval, and retention. Check `YTA_TELEMETRY_ENABLED` before creating any path. Read lifecycle JSON strictly and skip missing/non-live/cooldown states without writes.

- [ ] **Step 4: Verify the initial collector tests GREEN**

Run the focused tests and confirm pass.

- [ ] **Step 5: Write throttle and concurrency RED tests**

Add tests for:

- Configured intervals below 300 clamp to 300.
- A prior attempt younger than 300 seconds skips.
- A future attempt timestamp skips.
- Malformed collector state fails without helper invocation.
- The collector writes `last_attempt_at` before invoking the helper.
- A failure writing collector state prevents the helper call.
- A held nonblocking lock causes a clean skip.

The write-ahead ordering test must inspect `collector-state.json` from inside the mocked subprocess callback.

- [ ] **Step 6: Run and verify throttle tests RED**

Expected: missing lock, durable state, and throttle behavior.

- [ ] **Step 7: Implement private storage, lock, and write-ahead throttle**

Implement `telemetry_min_interval()` as the maximum of 300 seconds and the
configured integer value. Implement `telemetry_lock()` as a context manager
that creates the telemetry directory with mode `0700`, opens the lock file
with mode `0600`, attempts `fcntl.LOCK_EX | fcntl.LOCK_NB`, and yields whether
the lock was acquired. Always release an acquired lock and close its file
descriptor on exit.

`durable_json` must use a unique private temporary file in the destination directory, flush, `fsync`, `replace`, chmod `0600`, and `fsync` the parent. Fail closed on malformed `collector-state.json`; never reset it automatically.

Inside the lock, re-read lifecycle and collector state, re-evaluate eligibility, then atomically write `last_attempt_at` before the helper subprocess.

- [ ] **Step 8: Verify throttle tests GREEN**

Run focused tests and confirm all pass.

- [ ] **Step 9: Write collection and validation RED tests**

Add tests proving:

- Helper invocation is exactly `[YTA_TELEMETRY_API, "video-metrics", broadcast_id]`, `shell=False`, text capture, and bounded timeout.
- Valid helper JSON must match the requested video ID and exact nullable/string/integer field types.
- A successful sample contains lifecycle context and helper payload.
- Daily JSONL and `latest.json` contain the same sample and use `0600`.
- The collector directory uses `0700`.
- Helper nonzero, timeout, malformed JSON, wrong ID, invalid metric type, and file-write failure produce no latest snapshot and preserve `last_attempt_at`.
- Structured failure logs include only return code, retry class, HTTP status, and reasons.

- [ ] **Step 10: Run and verify collection tests RED**

Expected: collection and sample helpers are absent.

- [ ] **Step 11: Implement helper invocation and private sample persistence**

Use `subprocess.run` with an argument list, `shell=False`, `capture_output=True`, `text=True`, and `timeout=45`. Parse successful stdout as one JSON object and validate every allowed field. Parse failure stderr only as a structured helper-error object; never echo raw stderr.

Append one compact sorted JSON line using `os.open` with `O_APPEND`, `O_CREAT`, `O_WRONLY`, and `O_NOFOLLOW` where available. Set/fix mode `0600`, flush, and `fsync`. Then atomically write `latest.json` and update `collector-state.json` with `last_success_at` while retaining `last_attempt_at`.

- [ ] **Step 12: Verify collection tests GREEN**

Run the focused collection tests and confirm pass.

- [ ] **Step 13: Write and implement retention RED/GREEN tests**

Test exact `YYYY-MM-DD.jsonl` matching, UTC cutoff behavior, retention clamp to one day, and symlink/unrelated-file preservation. Implement `prune_daily_files(now)` using `lstat`, a full-match regex, strict date parsing, and no symlink following.

- [ ] **Step 14: Verify Task 2 and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_telemetry.py
.venv/bin/ruff check bin/youtube-autoencoder-telemetry tests/test_telemetry.py
python3 -m py_compile bin/youtube-autoencoder-telemetry
test -x bin/youtube-autoencoder-telemetry
```

Expected: pass. Commit:

```bash
git add bin/youtube-autoencoder-telemetry tests/test_telemetry.py
git commit -S -m "telemetry: add quota-bounded collector"
```

---

### Task 3: Opt-In Configuration And systemd Timers

**Files:**

- Modify: `config/youtube-autoencoder.env.example`
- Create: `systemd/youtube-autoencoder-telemetry@.service`
- Create: `systemd/youtube-autoencoder-telemetry@.timer`
- Create: `systemd/user/youtube-autoencoder-telemetry.service`
- Create: `systemd/user/youtube-autoencoder-telemetry.timer`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_telemetry.py`

**Interfaces:**

- Produces system timer: `youtube-autoencoder-telemetry@<user>.timer`
- Produces user timer: `youtube-autoencoder-telemetry.timer`

- [ ] **Step 1: Write static RED tests for opt-in units**

Add tests that read repository files and assert:

- Services are `Type=oneshot`, execute `/usr/local/bin/youtube-autoencoder-telemetry`, and use the existing environment-file paths.
- Timers use `OnBootSec=5min`, `OnUnitActiveSec=5min`, `AccuracySec=30s`, `RandomizedDelaySec=30s`, and `Persistent=false`.
- System and user timers are documented as alternative modes that must not both be enabled for one deployment.
- No encoder unit references telemetry.
- Example config uses `YTA_TELEMETRY_ENABLED=false`, interval `300`, retention `30`, and helper path.

- [ ] **Step 2: Run and verify RED**

Expected: unit and config assertions fail because files/settings are absent.

- [ ] **Step 3: Add config and units**

System service:

```ini
[Unit]
Description=YouTube AutoEncoder telemetry (%i)
Documentation=https://github.com/sumitake/YouTube-AutoEncoder
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=%i
EnvironmentFile=/home/%i/.config/youtube-autoencoder/youtube-autoencoder.env
ExecStart=/usr/local/bin/youtube-autoencoder-telemetry
WorkingDirectory=/home/%i
Nice=10
IOSchedulingClass=idle
```

System timer:

```ini
[Unit]
Description=Collect YouTube AutoEncoder telemetry (%i)

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s
RandomizedDelaySec=30s
Persistent=false
Unit=youtube-autoencoder-telemetry@%i.service

[Install]
WantedBy=timers.target
```

Add equivalent user units with `%h` environment/working paths and `Unit=youtube-autoencoder-telemetry.service`.

- [ ] **Step 4: Extend CI validation**

Add the collector to `py_compile` and executable checks. Install the collector into `/usr/local/bin` during unit verification and verify all six encoder/telemetry system and user units with `systemd-analyze verify`.

- [ ] **Step 5: Verify units and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_telemetry.py
sudo systemd-analyze verify \
  systemd/youtube-autoencoder@.service \
  systemd/youtube-autoencoder-telemetry@.service \
  systemd/youtube-autoencoder-telemetry@.timer \
  systemd/user/youtube-autoencoder.service \
  systemd/user/youtube-autoencoder-telemetry.service \
  systemd/user/youtube-autoencoder-telemetry.timer
```

If local macOS lacks `systemd-analyze`, rely on the same command in hosted Ubuntu CI and validate unit text locally. Commit:

```bash
git add config systemd .github/workflows/ci.yml tests/test_telemetry.py
git commit -S -m "systemd: add opt-in telemetry timers"
```

---

### Task 4: Operator Documentation And Changelog

**Files:**

- Modify: `README.md`
- Modify: `docs/raspberry-pi.md`
- Modify: `docs/architecture-and-flows.md`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Produces documented install, opt-in, inspect, disable, quota, and rollback commands.

- [ ] **Step 1: Update installation and repository layout**

Add the telemetry executable and four units to install commands and layout tables. State directly that installation does not enable either timer.

- [ ] **Step 2: Document opt-in operations**

Document system service enablement:

```bash
sudo systemctl enable --now youtube-autoencoder-telemetry@encoder.timer
```

and user service enablement:

```bash
systemctl --user enable --now youtube-autoencoder-telemetry.timer
```

Require `YTA_TELEMETRY_ENABLED=true` first and explicitly require choosing only one timer mode. Include inspection of `latest.json`, daily JSONL, timer status, and corresponding disable commands.

- [ ] **Step 3: Document cost and isolation**

State that telemetry uses one quota unit per eligible collection, at most 288 units per 24 hours at the minimum cadence when one timer mode is active, and adds no paid service. Explain nullable current viewers and that telemetry never controls recovery.

- [ ] **Step 4: Update architecture and changelog**

Add a dashed optional telemetry branch from durable lifecycle state to the collector, API helper `videos.list`, and private local files. Add `2026-07-11 - Optional Video Telemetry` as the newest `CHANGELOG.md` entry and only that entry in the README changelog section.

- [ ] **Step 5: Validate and commit docs**

Run:

```bash
npm_config_cache=/tmp/yta-npm-cache npx --yes markdownlint-cli2@0.18.1 '**/*.md'
git diff --check
```

Expected: zero errors. Commit:

```bash
git add README.md CHANGELOG.md docs
git commit -S -m "docs: document optional video telemetry"
```

---

### Task 5: Full Verification, Review, And PR

**Files:**

- Review all branch changes.

**Interfaces:**

- Produces a reviewed, CI-green pull request containing no credentials and no default runtime activation.

- [ ] **Step 1: Run the complete local gate**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
python3 -m py_compile \
  bin/youtube-autoencoder \
  bin/youtube-autoencoder-api \
  bin/youtube-autoencoder-test-pattern \
  bin/youtube-autoencoder-telemetry
test -x bin/youtube-autoencoder-telemetry
npm_config_cache=/tmp/yta-npm-cache npx --yes markdownlint-cli2@0.18.1 '**/*.md'
git diff --check
```

Expected: all pass.

- [ ] **Step 2: Audit scope, secrets, and activation**

Verify no token, client secret, ingest key, camera URL, private environment, local telemetry sample, or Pi-specific ID is tracked. Confirm all example flags are false and no command enables a timer.

- [ ] **Step 3: Run distinct-family code review**

Send the full branch diff and acceptance criteria to the available non-OpenAI reviewer. Address only verified actionable findings using TDD and rerun the full local gate.

- [ ] **Step 4: Push and open PR**

Use title `Add opt-in quota-bounded video telemetry`. Include cost contract, failure isolation, tests, rollback, and explicit confirmation that telemetry remains disabled.

- [ ] **Step 5: Wait for hosted checks and review sweep**

Require Python 3.11/3.12/3.13, Repository Metadata, Gitleaks, and CodeQL to pass. Inspect all delayed review comments and resolve actionable threads before merge.

- [ ] **Step 6: Merge and sync main**

Use the repository's squash-only admin merge after all gates pass, then fast-forward the main checkout and record the merge SHA.

---

### Task 6: Disabled-By-Default Raspberry Pi Deployment

**Files:**

- Deploy merged `bin/youtube-autoencoder-api`
- Deploy merged `bin/youtube-autoencoder-telemetry`
- Deploy the two system-level telemetry systemd units; keep the user-level
  units as repository artifacts for user-service installations

**Interfaces:**

- Produces installed but disabled telemetry with the existing encoder still active and enabled.

- [ ] **Step 1: Reconnect and capture pre-state**

Verify hostname, service identity, encoder active/enabled state, deployed hashes, current lifecycle state, and absence of telemetry units/timers. Do not print environment or OAuth contents.

- [ ] **Step 2: Back up and stage exact merged artifacts**

Create a timestamped root-private backup containing the installed API helper, encoder unit, private environment, and lifecycle state. Transfer merged artifacts, verify SHA-256, and run remote `py_compile` before stopping anything.

- [ ] **Step 3: Install with telemetry disabled**

Stop the encoder briefly, atomically install the API helper and telemetry executable, install the system telemetry units, run `daemon-reload`, and restart the encoder. Do not add `YTA_TELEMETRY_ENABLED=true`; do not enable or start the timer.

- [ ] **Step 4: Verify zero-call disabled smoke behavior**

Run `youtube-autoencoder-telemetry` under the service user's environment and verify a disabled skip, no telemetry directory/sample, no helper subprocess, and no new YouTube request in telemetry logs. Verify the timer is disabled/inactive.

- [ ] **Step 5: Verify encoder continuity and rollback readiness**

Confirm exact deployed hashes, encoder active/enabled state, no unexpected FFmpeg or broadcast lifecycle regression, Raspberry Pi Connect remains available, and the timestamped backup can restore prior files. Leave telemetry disabled.
