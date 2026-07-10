# Idempotent YouTube Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make YouTube AutoEncoder recover one YouTube broadcast across camera, FFmpeg, service, and Pi restarts without creating duplicate scheduled events, then promote a confirmed healthy live event from unlisted to public.

**Architecture:** Keep the existing two-script deployment boundary. `youtube-autoencoder-api` becomes the authoritative reconciliation and mutation client with durable local cache, exact instance/generation markers, structured errors, and bounded locks. `youtube-autoencoder` becomes a child-aware state machine that supervises FFmpeg while issuing one-shot API commands, persists retry cooldowns through the helper, and stops polling after the event is stable and public.

**Tech Stack:** Python 3.11 standard library, FFmpeg/FFprobe, YouTube Data API v3, systemd, pytest, Ruff, Markdownlint, GitHub Actions.

## Global Constraints

- No new runtime Python dependencies.
- Preserve Python 3.11 compatibility and the existing executable script layout under `bin/`.
- Run the service as unprivileged user `josumi`; root is used only for installation, backup, and systemd management.
- Use one reusable YouTube `liveStream` and one nonterminal managed `liveBroadcast` per instance.
- Set new broadcasts to `unlisted`; set this Pi to `public` only after two consecutive healthy live observations.
- Never complete a broadcast on FFmpeg exit, camera loss, SIGTERM, service restart, or Pi reboot.
- Never create a broadcast until FFmpeg is alive, making media progress, and YouTube ingest is active.
- Treat YouTube as authoritative; local JSON is a durable recovery cache only.
- Never adopt by title prefix. Require exact instance and generation markers.
- Do not complete, unbind, privatize, or delete existing legacy duplicate broadcasts during this change.
- Preserve camera credentials, stream keys, OAuth files, and tokens outside Git and redact them from logs.
- Keep Raspberry Pi Connect and the VS Code Remote Tunnel enabled and operational.

---

### Task 1: Contain And Snapshot The Current Pi

**Files:**

- Create remotely: `/home/josumi/.config/youtube-autoencoder/backups/$stamp/`
- Read remotely: `/usr/local/bin/youtube-autoencoder`
- Read remotely: `/usr/local/bin/youtube-autoencoder-api`
- Read remotely: `/usr/local/bin/youtube-autoencoder-test-pattern`
- Read remotely: `/etc/systemd/system/youtube-autoencoder@.service`
- Read remotely: `/home/josumi/.config/youtube-autoencoder/youtube-autoencoder.env`
- Read remotely: `/home/josumi/.config/youtube-autoencoder/youtube-live-state.json`

**Interfaces:**

- Consumes: Programmatic VS Code Remote Tunnel RPC to `rpi5-streamer2`.
- Produces: A stopped but enabled service, a private rollback snapshot, and a redacted pre-state record.

- [ ] **Step 1: Stop the defective service**

Run remotely:

```bash
systemctl stop youtube-autoencoder@josumi.service
systemctl show youtube-autoencoder@josumi.service \
  -p ActiveState -p SubState -p UnitFileState -p Result
```

Expected: `ActiveState=inactive`, `SubState=dead`, and `UnitFileState=enabled`.

- [ ] **Step 2: Verify the retry loop is absent**

Run remotely:

```bash
ps -eo pid,user,args | grep -E '[y]outube-autoencoder|[f]fmpeg' || true
```

Expected: no encoder, API-helper, or FFmpeg process except the inspection shell.

- [ ] **Step 3: Create a private rollback snapshot**

Run remotely as root:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/home/josumi/.config/youtube-autoencoder/backups/$stamp
install -d -m 0700 -o josumi -g josumi "$backup"
install -m 0755 /usr/local/bin/youtube-autoencoder "$backup/youtube-autoencoder"
install -m 0755 /usr/local/bin/youtube-autoencoder-api "$backup/youtube-autoencoder-api"
install -m 0755 /usr/local/bin/youtube-autoencoder-test-pattern "$backup/youtube-autoencoder-test-pattern"
install -m 0644 /etc/systemd/system/youtube-autoencoder@.service "$backup/youtube-autoencoder@.service"
install -m 0600 -o josumi -g josumi \
  /home/josumi/.config/youtube-autoencoder/youtube-autoencoder.env \
  "$backup/youtube-autoencoder.env"
test ! -e /home/josumi/.config/youtube-autoencoder/youtube-live-state.json || \
  install -m 0600 -o josumi -g josumi \
    /home/josumi/.config/youtube-autoencoder/youtube-live-state.json \
    "$backup/youtube-live-state.json"
printf '%s\n' "$backup"
```

Expected: one timestamped directory owned by `josumi`, mode `0700`, with private config/state copies.

- [ ] **Step 4: Record non-secret pre-state**

Run remotely:

```bash
sha256sum /usr/local/bin/youtube-autoencoder \
  /usr/local/bin/youtube-autoencoder-api \
  /usr/local/bin/youtube-autoencoder-test-pattern
systemctl is-enabled youtube-autoencoder@josumi.service
systemctl is-enabled rpi-connect.service 2>/dev/null || true
systemctl --user -M josumi@ is-enabled vscode-tunnel.service 2>/dev/null || true
```

Expected: encoder unit remains enabled; remote-management state is captured without printing credentials.

### Task 2: Add Durable State, Locking, And Structured API Errors

**Files:**

- Modify: `bin/youtube-autoencoder-api`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: Existing `http_json`, `read_json`, `write_secret_json`, and CLI dispatcher.
- Produces: `YouTubeApiError`, `write_json_durable`, `read_state`, `write_state`, `mutation_lock`, `error_payload`, and local state commands used by later tasks.

- [ ] **Step 1: Write failing durable-state and error tests**

Add tests that exercise these signatures:

```python
def test_http_json_exposes_youtube_error_reason(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_http_error")
    error = api.YouTubeApiError(
        status=403,
        reasons=("userRequestsExceedRateLimit",),
        message="User requests exceed the rate limit.",
    )
    assert error.retry_class == "quota"
    assert api.error_payload(error)["reasons"] == ["userRequestsExceedRateLimit"]


def test_corrupt_state_is_quarantined(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_corrupt_state")
    state = tmp_path / "youtube-live-state.json"
    state.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(api, "STATE_FILE", state)
    assert api.read_state() == {}
    assert len(list(tmp_path.glob("youtube-live-state.json.corrupt.*"))) == 1


def test_mutation_lock_times_out(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_lock")
    monkeypatch.setattr(api, "LOCK_FILE", tmp_path / "youtube-live-state.lock")
    with api.mutation_lock(timeout=0.2):
        with pytest.raises(TimeoutError):
            with api.mutation_lock(timeout=0.05):
                pass
```

Extend the existing secret-write test to assert valid JSON, mode `0600`, and no leftover `.tmp` file after replacement.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```bash
.venv/bin/pytest tests/test_api.py -k 'error_reason or corrupt_state or mutation_lock' -v
```

Expected: failures because the new exception, state reader, and lock do not exist.

- [ ] **Step 3: Implement the durable primitives**

Implement these exact public signatures: `YouTubeApiError.__init__(*, status: int | None, reasons: tuple[str, ...], message: str, retry_after: float | None = None) -> None`, `write_json_durable(path: pathlib.Path, data: dict[str, Any], mode: int = 0o600) -> None`, `read_state() -> dict[str, Any]`, `write_state(data: dict[str, Any]) -> None`, `error_payload(exc: BaseException) -> dict[str, Any]`, and the context manager `mutation_lock(timeout: float | None = None)`.

`write_json_durable` must flush and `fsync` the temporary file, `replace` the destination, chmod the destination, and `fsync` the parent directory. `read_state` must rename malformed JSON to `.corrupt.YYYYMMDDTHHMMSSZ` and return `{}`. `YouTubeApiError.retry_class` must map rate-limit reasons and 429 to `quota`, HTTP 5xx/timeouts to `api`, and permanent 4xx failures to `fatal`.

- [ ] **Step 4: Add structured CLI failure output**

Wrap command dispatch so failures emit one final JSON object to stderr:

```json
{
  "error": true,
  "http_status": 403,
  "message": "User requests exceed the rate limit.",
  "reasons": ["userRequestsExceedRateLimit"],
  "retry_after": null,
  "retry_class": "quota"
}
```

Return exit code `75` for `quota` and `api`, `78` for `fatal` or ambiguous state, and preserve `130` for interruption.

- [ ] **Step 5: Run API tests and commit**

Run:

```bash
.venv/bin/pytest tests/test_api.py -q
.venv/bin/ruff check bin/youtube-autoencoder-api tests/test_api.py
```

Expected: all API tests pass and Ruff reports no errors.

Commit:

```bash
git add bin/youtube-autoencoder-api tests/test_api.py
git commit -m "api: add durable state and structured failures"
```

### Task 3: Implement Idempotent Broadcast Reconciliation

**Files:**

- Modify: `bin/youtube-autoencoder-api`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: Task 2 state, locking, and structured error interfaces.
- Produces: `instance_id`, `broadcast_description`, `broadcast_by_id`, `list_managed_broadcasts`, `reconcile_broadcast`, `set_broadcast_privacy`, `stream-status`, `broadcast-status`, `reconcile-broadcast`, `set-privacy`, `state`, `set-retry`, and `clear-retry` commands.

- [ ] **Step 1: Write failing marker and candidate tests**

Cover exact behavior:

```python
def test_description_contains_exact_instance_and_generation_markers(load_script, monkeypatch):
    api = load_script("youtube-autoencoder-api", "yta_api_markers")
    description = api.broadcast_description("rpi5-streamer", "generation-1")
    assert "[youtube-autoencoder-instance:rpi5-streamer]" in description
    assert "[youtube-autoencoder-generation:generation-1]" in description


def test_reconcile_reuses_nonterminal_broadcast_without_insert(load_script, monkeypatch, tmp_path):
    api = load_script("youtube-autoencoder-api", "yta_api_reuse")
    monkeypatch.setattr(api, "STATE_FILE", tmp_path / "youtube-live-state.json")
    api.write_state(
        {
            "schema_version": 2,
            "instance_id": "encoder-1",
            "generation_id": "generation-1",
            "stream_id": "stream-1",
            "broadcast_id": "broadcast-1",
        }
    )
    broadcast = {
        "id": "broadcast-1",
        "snippet": {"description": api.broadcast_description("encoder-1", "generation-1")},
        "contentDetails": {"boundStreamId": "stream-1"},
        "status": {"lifeCycleStatus": "live", "privacyStatus": "unlisted"},
    }
    monkeypatch.setattr(api, "instance_id", lambda: "encoder-1")
    monkeypatch.setattr(api, "broadcast_by_id", lambda _id: broadcast)
    monkeypatch.setattr(api, "create_broadcast", lambda *_args, **_kwargs: pytest.fail("insert called"))
    state = api.reconcile_broadcast(stream_id="stream-1", title="Camera", staging_privacy="unlisted", allow_create=True)
    assert state["broadcast_id"] == "broadcast-1"
    assert state["lifecycle"] == "live"
```

Also test `created`, `ready`, `testStarting`, `testing`, and `liveStarting`; terminal replacement; unknown-state blocking; unrelated title matches; multiple marker matches; and exact generation recovery after a simulated lost insert response.

- [ ] **Step 2: Run the focused tests and observe failure**

Run:

```bash
.venv/bin/pytest tests/test_api.py -k 'marker or reconcile or terminal or candidate' -v
```

Expected: failures because reconciliation functions do not exist.

- [ ] **Step 3: Implement write-ahead reconciliation**

Add `RECOVERABLE_STATES = {"created", "ready", "testStarting", "testing", "liveStarting", "live"}` and `TERMINAL_STATES = {"complete", "revoked"}`. Implement the exact signatures `instance_id() -> str`, `broadcast_description(instance: str, generation: str) -> str`, `broadcast_by_id(broadcast_id: str) -> dict[str, Any] | None`, `list_managed_broadcasts(instance: str) -> list[dict[str, Any]]`, and `reconcile_broadcast(*, stream_id: str, title: str, staging_privacy: str, allow_create: bool) -> dict[str, Any]`.

The create path must persist `pending_action=create` and a UUID generation before `liveBroadcasts.insert`, re-list by the exact generation marker, then insert only when absent. Persist the returned ID before bind. A retry after bind/transition failure must never call insert.

- [ ] **Step 4: Add one-shot status and privacy operations**

Implement the exact signatures `stream_status(stream_id: str | None = None) -> dict[str, Any]`, `broadcast_status(broadcast_id: str) -> dict[str, Any]`, and `set_broadcast_privacy(broadcast_id: str, privacy: str) -> dict[str, Any]`.

`set_broadcast_privacy` must fetch the current resource, preserve every field required by `liveBroadcasts.update`, update only privacy, then fetch and verify the readback. All mutations acquire `mutation_lock`; read-only status commands do not.

- [ ] **Step 5: Add persisted retry commands**

`set-retry` stores `retry_class`, `retry_attempt`, and UTC `retry_not_before` in schema version 2 without removing broadcast identity. `clear-retry` removes those values only after sustained progress or a stable public state. `state` returns the cache without secret values.

- [ ] **Step 6: Keep compatibility commands safe**

Change `prepare-broadcast` to call reconciliation instead of unconditional insert. Remove automatic completion from `run-visible-test` failure paths; a test may complete only its known live broadcast when explicitly requested.

- [ ] **Step 7: Run API tests and commit**

Run:

```bash
.venv/bin/pytest tests/test_api.py -q
.venv/bin/ruff check bin/youtube-autoencoder-api tests/test_api.py
```

Expected: all API tests pass and no lint errors.

Commit:

```bash
git add bin/youtube-autoencoder-api tests/test_api.py
git commit -m "api: reconcile one managed YouTube broadcast"
```

### Task 4: Make FFmpeg Supervision Child-Aware

**Files:**

- Modify: `bin/youtube-autoencoder`
- Modify: `tests/test_supervisor.py`

**Interfaces:**

- Consumes: Existing source discovery, source probe, redaction, and FFmpeg argument functions.
- Produces: `ffmpeg_supports_rtsp_option`, `ProgressWatchdog`, `drain_ffmpeg_output`, `ApiCommandError`, and `api_command_while_streaming`.

- [ ] **Step 1: Write failing FFmpeg compatibility and progress tests**

Add:

```python
def test_ffmpeg_args_omit_rw_timeout(load_script, monkeypatch):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_no_rw_timeout")
    monkeypatch.setenv("YTA_MODE", "copy")
    args = supervisor.ffmpeg_args("rtsp://camera/stream", "rtmps://youtube/live/key", rtsp_timeout_supported=True)
    assert "-rw_timeout" not in args
    assert args[args.index("-timeout") + 1] == "5000000"


def test_progress_watchdog_detects_stall(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_supervisor_progress")
    watch = supervisor.ProgressWatchdog(started_at=10.0, timeout=30.0)
    assert watch.observe("out_time_us=5000000", now=20.0)
    assert watch.stalled(now=49.9) is False
    assert watch.stalled(now=50.1) is True
```

Also test that an unsupported capability omits `-timeout`, normal log lines do not count as progress, and stream-key/camera credentials remain redacted.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
.venv/bin/pytest tests/test_supervisor.py -k 'rw_timeout or progress or capability' -v
```

Expected: failures because the new interfaces do not exist and current args contain `-rw_timeout`.

- [ ] **Step 3: Implement capability-safe FFmpeg arguments**

Add a five-second capability probe:

```python
def ffmpeg_supports_rtsp_option(option: str) -> bool:
    result = subprocess.run(
        [env("YTA_FFMPEG", "ffmpeg"), "-hide_banner", "-h", "demuxer=rtsp"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return any(line.lstrip().startswith(f"-{option} ") for line in result.stdout.splitlines())
```

Change `ffmpeg_args` to accept `rtsp_timeout_supported: bool | None = None`, omit `-rw_timeout`, optionally include `-timeout`, and add `-stats_period 5 -progress pipe:1`.

- [ ] **Step 4: Implement progress parsing and child-aware API waits**

Add a `@dataclasses.dataclass` named `ProgressWatchdog` with fields `started_at: float`, `timeout: float`, `last_progress_at: float | None = None`, and `last_out_time_us: int = -1`. Implement `observe(self, line: str, now: float) -> bool` and `stalled(self, now: float) -> bool`; only a strictly increasing nonnegative `out_time_us` refreshes progress.

`api_command_while_streaming(args, runtime, timeout)` starts the API helper with `Popen`, drains FFmpeg through the existing selector every 0.5 seconds, checks `child.poll()`, checks the watchdog, and terminates the helper when FFmpeg exits or stalls. Only after the helper exits does it parse its small stdout/stderr result.

- [ ] **Step 5: Test early-child-exit cancellation**

Use fake `Popen` objects to prove an API wait is terminated and raises `EncoderStopped` when the FFmpeg child exits. Assert no later lifecycle callback executes.

- [ ] **Step 6: Run supervisor tests and commit**

Run:

```bash
.venv/bin/pytest tests/test_supervisor.py -q
.venv/bin/ruff check bin/youtube-autoencoder tests/test_supervisor.py
```

Expected: all supervisor tests pass and no lint errors.

Commit:

```bash
git add bin/youtube-autoencoder tests/test_supervisor.py
git commit -m "encoder: supervise FFmpeg during API operations"
```

### Task 5: Implement Recovery, Backoff, And Public Promotion

**Files:**

- Modify: `bin/youtube-autoencoder`
- Modify: `tests/test_supervisor.py`

**Interfaces:**

- Consumes: Task 3 API commands and Task 4 child-aware runtime.
- Produces: `classify_api_error`, `retry_delay`, `reconcile_lifecycle`, `healthy_live_observation`, and the production recovery loop.

- [ ] **Step 1: Write failing backoff and lifecycle matrix tests**

Add parameterized tests:

```python
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


def test_quota_backoff_is_long_and_capped(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_quota_backoff")
    assert supervisor.retry_delay("quota", attempt=1, jitter=0.0) == 900
    assert supervisor.retry_delay("quota", attempt=20, jitter=0.0) == 21600
```

Also test source/API/ambiguous delays, `Retry-After`, persisted future cooldown on startup, and no API polling after stable public state.

- [ ] **Step 2: Write failing publication-gate tests**

Prove two observations are required and that all fields gate promotion:

```python
def test_publication_requires_two_healthy_live_observations(load_script):
    supervisor = load_script("youtube-autoencoder", "yta_public_gate")
    gate = supervisor.PublicationGate(required=2)
    healthy = {"stream_status": "active", "health": "good", "lifecycle": "live"}
    assert gate.observe(healthy) is False
    assert gate.observe(healthy) is True
```

Reset the streak for `inactive`, `bad`, `noData`, non-live lifecycle, dead child, or stale media. Simulate FFmpeg exit after `transition live` and assert `set-privacy` is never invoked.

- [ ] **Step 3: Run focused tests and observe failure**

Run:

```bash
.venv/bin/pytest tests/test_supervisor.py -k 'lifecycle or backoff or publication or public' -v
```

Expected: failures because the state machine and gate do not exist.

- [ ] **Step 4: Implement the lifecycle loop**

The loop order is exact:

1. Honor persisted `retry_not_before`.
2. Probe source.
3. Start FFmpeg.
4. Wait for FFmpeg media progress while polling one-shot stream status.
5. When stream is active, call `reconcile-broadcast --allow-create` once.
6. Recheck child/progress/stream before `testing`.
7. Recheck immediately after `testing` and before `live`.
8. Recheck immediately after `live`.
9. Require two healthy live observations.
10. Call `set-privacy public`, verify readback, clear retry, and stop nonessential polling.
11. Continue draining and supervising FFmpeg until interruption.

An already-live event skips transitions. An already-public event is never demoted. Any pre-public failure retains the same unlisted ID. Any post-public source failure retains the same public ID.

- [ ] **Step 5: Remove completion-on-exit behavior**

Delete the `finally` call that completes a broadcast. Keep process cleanup only. Emit a deprecation log if `YTA_YOUTUBE_COMPLETE_ON_EXIT=true`; do not honor it in unattended lifecycle mode.

- [ ] **Step 6: Persist classified backoff**

Use the helper's `set-retry` command before sleeping. Source/encoder errors use 10 seconds to 5 minutes; API errors use 30 seconds to 15 minutes; quota errors use 15 minutes to 6 hours; ambiguous states use 5 minutes to 1 hour. Apply full jitter in production and inject deterministic jitter in tests.

- [ ] **Step 7: Run all tests and commit**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
python3 -m py_compile bin/youtube-autoencoder bin/youtube-autoencoder-api bin/youtube-autoencoder-test-pattern
```

Expected: all tests, lint, and compile checks pass.

Commit:

```bash
git add bin/youtube-autoencoder tests/test_supervisor.py
git commit -m "encoder: recover one broadcast and publish when healthy"
```

### Task 6: Update Configuration And Operator Documentation

**Files:**

- Modify: `config/youtube-autoencoder.env.example`
- Modify: `README.md`
- Modify: `docs/raspberry-pi.md`
- Modify: `CHANGELOG.md`
- Test: Markdownlint across `**/*.md`

**Interfaces:**

- Consumes: Final Task 2-5 environment variables and CLI commands.
- Produces: Deployable configuration, accurate recovery documentation, and a current changelog entry.

- [ ] **Step 1: Update example configuration**

Document exact values:

```text
YTA_INSTANCE_ID=encoder-hostname
YTA_YOUTUBE_STAGING_PRIVACY=unlisted
YTA_YOUTUBE_LIVE_PRIVACY=public
YTA_YOUTUBE_POLL_INTERVAL_SEC=5
YTA_FFMPEG_PROGRESS_TIMEOUT_SEC=45
YTA_YOUTUBE_COMPLETE_ON_EXIT=false
```

Add retry base/max variables only when implemented as operator-tunable values; otherwise document fixed defaults in README.

- [ ] **Step 2: Correct architecture and caveats**

Replace statements that each recovery creates a broadcast. Document exact marker adoption, terminal-only replacement, unlisted-to-public promotion, persistent backoff, explicit-only completion, and the intentional public-unavailable watch page during outages.

- [ ] **Step 3: Add the current changelog entry**

Add `2026-07-10 - Idempotent YouTube Lifecycle Recovery` to `CHANGELOG.md` and make it the only entry shown in README. Include FFmpeg option compatibility, duplicate prevention, persisted backoff, and public-after-live behavior.

- [ ] **Step 4: Run documentation and repository checks**

Run:

```bash
npm_config_cache=/tmp/yta-npm-cache npx --yes markdownlint-cli2@0.18.1 '**/*.md'
.venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: zero Markdown, test, lint, or whitespace failures.

- [ ] **Step 5: Commit**

```bash
git add config/youtube-autoencoder.env.example README.md docs/raspberry-pi.md CHANGELOG.md
git commit -m "docs: document idempotent lifecycle recovery"
```

### Task 7: Review, Publish, And Merge The Branch

**Files:**

- Review: all branch changes against `origin/main`
- Create remotely: GitHub pull request

**Interfaces:**

- Consumes: Complete local implementation and passing verification.
- Produces: Reviewed, CI-green, self-merged `main` commit suitable for Pi installation.

- [ ] **Step 1: Run the full local verification suite**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
python3 -m py_compile bin/youtube-autoencoder bin/youtube-autoencoder-api bin/youtube-autoencoder-test-pattern
npm_config_cache=/tmp/yta-npm-cache npx --yes markdownlint-cli2@0.18.1 '**/*.md'
git diff --check origin/main...HEAD
```

Expected: all checks pass with no secret-bearing files in `git status`.

- [ ] **Step 2: Run independent code review**

Review the complete diff for lifecycle races, write-ahead idempotency, API quota behavior, privacy sequencing, lock semantics, and log redaction. Address every material finding and rerun the full suite.

- [ ] **Step 3: Push and open the PR**

Use a PR body that records root cause, design, rollback, verification, and the public-safety checklist. Do not include Pi configuration, camera URL, stream key, OAuth token, or API response secrets.

- [ ] **Step 4: Wait for branch-protection checks and delayed review**

Confirm CI, CodeQL, and secret scan complete successfully. Sweep unresolved review comments before merge.

- [ ] **Step 5: Self-merge and synchronize local main**

Merge only after all required checks pass, then fetch `origin/main` and record the exact merged commit SHA.

### Task 8: Deploy And Verify On The Raspberry Pi

**Files:**

- Install remotely: `/usr/local/bin/youtube-autoencoder`
- Install remotely: `/usr/local/bin/youtube-autoencoder-api`
- Install remotely: `/usr/local/bin/youtube-autoencoder-test-pattern`
- Modify remotely: `/home/josumi/.config/youtube-autoencoder/youtube-autoencoder.env`
- Preserve remotely: OAuth, OBS service, and camera source files

**Interfaces:**

- Consumes: Exact merged `origin/main` files and Task 1 rollback snapshot.
- Produces: Enabled, operational bridge using one public, self-recovering YouTube broadcast.

- [ ] **Step 1: Verify deployment target and stopped state**

Read back hostname `rpi5-streamer`, architecture `aarch64`, service user `josumi`, inactive encoder unit, and enabled Raspberry Pi Connect/VS Code Tunnel before installation.

- [ ] **Step 2: Install exact merged scripts**

Transfer without exposing secrets, then run remotely:

```bash
install -m 0755 /tmp/youtube-autoencoder /usr/local/bin/youtube-autoencoder
install -m 0755 /tmp/youtube-autoencoder-api /usr/local/bin/youtube-autoencoder-api
install -m 0755 /tmp/youtube-autoencoder-test-pattern /usr/local/bin/youtube-autoencoder-test-pattern
```

Read back SHA-256 values and compare them with the merged checkout.

- [ ] **Step 3: Apply bounded config changes**

Use a structured environment-file editor to set:

```text
YTA_INSTANCE_ID=rpi5-streamer
YTA_YOUTUBE_STAGING_PRIVACY=unlisted
YTA_YOUTUBE_LIVE_PRIVACY=public
YTA_YOUTUBE_POLL_INTERVAL_SEC=5
YTA_FFMPEG_PROGRESS_TIMEOUT_SEC=45
YTA_YOUTUBE_COMPLETE_ON_EXIT=false
```

Preserve all camera, OBS, stream-key, OAuth, and unrelated values. Read back only a safe allowlist of non-secret keys.

- [ ] **Step 4: Validate camera-to-local FFmpeg behavior**

Run the deployed command builder against a local FLV sink for at least 30 seconds. Expected: no unsupported-option exit, continuous progress records, and exit code 0 after the bounded test.

- [ ] **Step 5: Inventory legacy broadcasts without cleanup**

When rate limit permits, list active/upcoming broadcasts and record ID, title, lifecycle, privacy, scheduled time, and bound stream ID. Do not mutate any legacy event.

- [ ] **Step 6: Start the service and observe staged lifecycle**

Run:

```bash
systemctl start youtube-autoencoder@josumi.service
systemctl show youtube-autoencoder@josumi.service \
  -p ActiveState -p SubState -p Result -p ExecMainStatus
journalctl -u youtube-autoencoder@josumi.service -f
```

Expected sequence: source healthy, FFmpeg progress, stream active, one exact-marker event reconciled/created as unlisted, testing, live, two healthy observations, privacy readback public.

- [ ] **Step 7: Verify YouTube state and visual output**

Read back one broadcast ID, `lifeCycleStatus=live`, `privacyStatus=public`, bound stream ID, stream `active`, and health `good` or `ok`. Provide the watch URL for visual confirmation.

- [ ] **Step 8: Inject FFmpeg recovery**

Record the broadcast ID, terminate only the FFmpeg child with SIGTERM, and observe automatic restart. Expected: same broadcast ID, no insert, active healthy ingest, public privacy retained.

- [ ] **Step 9: Inject service recovery**

Restart `youtube-autoencoder@josumi.service`. Expected: same broadcast ID and watch URL, no scheduled-event count increase, active healthy ingest, public privacy retained.

- [ ] **Step 10: Verify boot and management durability**

Confirm the encoder remains enabled, Raspberry Pi Connect remains enabled/active, and the VS Code Remote Tunnel is still connected. A full Pi reboot is performed only after these checks and with the rollback snapshot available; after reboot, repeat service, broadcast-ID, ingest-health, and management checks.

- [ ] **Step 11: Exercise local rollback readiness**

Without changing YouTube lifecycle, prove the rollback snapshot contains executable binaries and a private config. Document the exact restore command and retain the snapshot until the deployment has remained stable.

- [ ] **Step 12: Report legacy cleanup inventory separately**

Report which legacy broadcasts appear safe to complete/delete and request a separate cleanup decision. Do not include cleanup in the deployment success criteria.
