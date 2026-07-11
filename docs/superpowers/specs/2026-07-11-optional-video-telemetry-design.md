# Optional Video Telemetry Design

**Status:** Approved for implementation
**Date:** 2026-07-11  
**Project:** YouTube AutoEncoder

## Summary

YouTube AutoEncoder will add an optional, read-only telemetry path backed by the existing YouTube Data API credentials. A new `video-metrics` API-helper command will issue one `videos.list` request for the managed broadcast. A separate one-shot `youtube-autoencoder-telemetry` executable will collect normalized live-video metrics into private local files when invoked by an explicitly enabled systemd timer.

Telemetry is not part of the streaming control plane. It cannot start or stop FFmpeg, create or mutate YouTube resources, transition a broadcast, change visibility, or update lifecycle retry state. The encoder must continue operating identically when telemetry is disabled, unavailable, rate-limited, malformed, or failing.

## Goals

- Add a read-only `videos.list` query for the managed broadcast ID.
- Record current concurrent viewers and cumulative video statistics while a managed broadcast is live.
- Keep telemetry disabled by default and require an explicit configuration flag plus timer enablement.
- Bound telemetry to at most one YouTube Data API request per 300 seconds.
- Store compact, private, locally inspectable telemetry without external services.
- Preserve Raspberry Pi CPU, memory, storage, and API quota for the stream bridge.
- Support both system-level and user-level systemd installations.

## Non-Goals

- Do not add the YouTube Analytics API or YouTube Reporting API in this change.
- Do not add paid storage, hosted monitoring, dashboards, alerting vendors, or billable cloud resources.
- Do not use telemetry as a stream-health, publication, lifecycle, or recovery signal.
- Do not change the existing OAuth scopes or provision a second credential.
- Do not expose an HTTP endpoint or listen on a network socket.
- Do not collect audience identity, geography, demographics, chat content, or monetary metrics.
- Do not create daily broadcasts or otherwise change event boundaries for reporting convenience.

## Cost And Quota Contract

The implementation uses the already-enabled YouTube Data API and existing OAuth token. It adds no separately priced API, package, storage service, compute service, or network service. Google documents `videos.list` as a one-unit quota request and documents a default 10,000-unit daily allocation for the shared Data API bucket. References:

- <https://developers.google.com/youtube/v3/docs/videos/list>
- <https://developers.google.com/youtube/v3/getting-started>

The following controls are mandatory:

- `YTA_TELEMETRY_ENABLED` defaults to `false`.
- The telemetry timers are installed but never enabled automatically.
- The collector performs no API request unless the managed lifecycle cache says the broadcast is `live` and has a broadcast ID.
- The collector performs no API request while a persisted lifecycle retry deadline is still active.
- `YTA_TELEMETRY_MIN_INTERVAL_SEC` defaults to `300` and is clamped to a minimum of `300`.
- A write-ahead `last_attempt_at` timestamp is persisted locally and checked before the API helper starts.
- The timer is non-persistent, so host downtime does not produce catch-up calls.
- With one timer mode active, a continuously live broadcast can consume no more than 288 `videos.list` units in any 24-hour period at the minimum interval.

Invalid requests still consume quota, so all local eligibility checks occur before invoking the API helper.

## Components

### Data API Helper Command

`bin/youtube-autoencoder-api` will add:

```text
youtube-autoencoder-api video-metrics [video_id]
```

If `video_id` is omitted, the command reads `broadcast_id` or `last_broadcast_id` from the durable lifecycle cache without quarantining, replacing, or updating that file. It sends exactly one authorized request:

```text
GET /youtube/v3/videos
  ?id=<video_id>
  &part=liveStreamingDetails,statistics
  &fields=items(id,liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime,scheduledEndTime,concurrentViewers),statistics(viewCount,likeCount,commentCount))
```

The helper will normalize numeric strings to integers and preserve absent values as JSON `null`. In particular, a missing `concurrentViewers` field must remain `null`; it must not be reported as zero.

The lifecycle-cache path continues to honor the existing `YTA_YOUTUBE_STATE_FILE` override. A malformed cache is an explicit error and remains untouched. The command output contract is:

```json
{
  "video_id": "example",
  "actual_start_time": "2026-07-11T00:00:00Z",
  "actual_end_time": null,
  "scheduled_start_time": "2026-07-11T00:00:00Z",
  "scheduled_end_time": null,
  "concurrent_viewers": 12,
  "view_count": 345,
  "like_count": 6,
  "comment_count": 2
}
```

An empty `items` response is an explicit non-success result. The helper uses its existing structured error and redaction behavior. The command performs no write and does not acquire the lifecycle mutation lock.

### Telemetry Collector

`bin/youtube-autoencoder-telemetry` will be a Python 3.11 standard-library executable. Each invocation performs at most one collection attempt.

The collector will:

1. Exit successfully without subprocess or network activity when `YTA_TELEMETRY_ENABLED` is false or unset.
2. Acquire a nonblocking telemetry-only lock; an overlapping invocation exits successfully without API activity.
3. Read the lifecycle cache without modifying or quarantining it.
4. Skip unless the cache has a broadcast ID and lifecycle `live`.
5. Skip while `retry_not_before` is a valid future UTC timestamp.
6. Skip if the latest recorded API attempt is younger than the effective minimum interval.
7. Atomically persist the current UTC timestamp as `last_attempt_at`; if this write fails, make no API request.
8. Invoke `youtube-autoencoder-api video-metrics <broadcast_id>` without a shell.
9. Parse and validate the helper's JSON output.
10. Add local collection and lifecycle context.
11. Append one private JSONL record, atomically replace the latest snapshot, record `last_success_at`, and prune expired daily files.

The collector must not import or duplicate OAuth logic. Its only API boundary is the helper subprocess.

### Local Storage

The default telemetry directory is:

```text
~/.local/state/youtube-autoencoder/telemetry/
```

Files are:

```text
latest.json
YYYY-MM-DD.jsonl
collector-state.json
telemetry.lock
```

Each sample contains:

```json
{
  "collected_at": "2026-07-11T00:05:00Z",
  "instance_id": "encoder-hostname",
  "generation_id": "generation-id",
  "broadcast_id": "video-id",
  "lifecycle": "live",
  "privacy": "public",
  "video": {
    "video_id": "video-id",
    "actual_start_time": "2026-07-11T00:00:00Z",
    "actual_end_time": null,
    "scheduled_start_time": "2026-07-11T00:00:00Z",
    "scheduled_end_time": null,
    "concurrent_viewers": 12,
    "view_count": 345,
    "like_count": 6,
    "comment_count": 2
  }
}
```

The directory mode is `0700`; regular files are `0600`. `latest.json` uses temporary-file, flush, `fsync`, atomic replace, and parent-directory `fsync`. Daily JSONL appends are flushed and `fsync`ed before success is reported.

`collector-state.json` is a private durable document containing `last_attempt_at` and, after a successful sample, `last_success_at`. The collector writes `last_attempt_at` before starting the API helper. A crash, API failure, malformed response, or sample-write failure therefore still consumes the minimum-interval window and cannot trigger a rapid retry. A manual `video-metrics` command is an explicit operator action outside this telemetry throttle.

`YTA_TELEMETRY_RETENTION_DAYS` defaults to `30` and is clamped to at least `1`. Retention deletes only files whose complete basename matches `YYYY-MM-DD.jsonl` and whose parsed date is older than the retained UTC window. It never follows symlinks or deletes unrelated files.

`YTA_TELEMETRY_DIR` may override the storage directory.

### systemd Units

System-level installations add:

```text
systemd/youtube-autoencoder-telemetry@.service
systemd/youtube-autoencoder-telemetry@.timer
```

User-level installations add:

```text
systemd/user/youtube-autoencoder-telemetry.service
systemd/user/youtube-autoencoder-telemetry.timer
```

The service is `Type=oneshot`, uses the same environment file as the encoder, runs at lower CPU and I/O priority, and has no dependency from the encoder service. The timer uses:

```text
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s
RandomizedDelaySec=30s
Persistent=false
```

The timer units have normal install targets but are not enabled by installation commands. System and user timers are alternative activation modes; operators must enable exactly one for a deployment. Operators opt in only after setting:

```text
YTA_TELEMETRY_ENABLED=true
```

and explicitly enabling the appropriate timer.

## Error Handling And Isolation

- Disabled and ineligible states exit zero with a concise skip reason.
- Overlapping invocations exit zero without waiting or calling YouTube.
- Malformed lifecycle or collector state fails closed without an API call; the collector never resets an unreadable throttle ledger or guesses that a call is safe.
- A future `last_attempt_at`, including one caused by a backward clock correction, remains throttled rather than being treated as expired.
- API-helper failures produce no telemetry sample, retain the write-ahead attempt timestamp, and return the helper's nonzero status.
- Helper stderr is parsed as structured JSON. Collector logs expose only operation, retry class, HTTP status, and reason identifiers; arbitrary helper text is not echoed.
- Malformed helper success output is a telemetry failure and produces no sample files beyond the throttle ledger and lock.
- Sample-write failure is reported after the helper call, retains the write-ahead attempt timestamp, and never mutates lifecycle state.
- Telemetry never invokes `set-retry`, `clear-retry`, lifecycle transitions, privacy changes, or broadcast reconciliation.
- Telemetry service or timer failure cannot restart or stop the encoder service.

## Security And Privacy

- Reuse the existing OAuth token and its current YouTube scope; add no credential file or scope.
- Never log or persist access tokens, refresh tokens, OAuth client secrets, ingest URLs, stream keys, source URLs, or command environments.
- Invoke subprocesses with argument arrays and `shell=False`.
- Treat YouTube JSON as untrusted structured input and validate object, field, and integer shapes.
- Persist only broadcast identifiers, lifecycle context, timestamps, and aggregate counts.
- Keep telemetry storage private to the service user.

## Configuration

The example environment file will add:

```text
YTA_TELEMETRY_ENABLED=false
YTA_TELEMETRY_MIN_INTERVAL_SEC=300
YTA_TELEMETRY_RETENTION_DAYS=30
YTA_TELEMETRY_API=/usr/local/bin/youtube-autoencoder-api
# YTA_TELEMETRY_DIR=/home/encoder/.local/state/youtube-autoencoder/telemetry
```

## Testing

API-helper tests will prove:

- Exact `videos.list` method, path, parts, fields, and video ID.
- Numeric strings normalize to integers.
- Missing optional metrics remain `null`.
- Missing video items fail explicitly.
- CLI argument and lifecycle-cache fallback behavior.
- No mutation lock, cache quarantine, or state write occurs.

Collector tests will prove:

- Disabled mode performs no subprocess or file write.
- Missing, non-live, cooldown, too-soon, and overlapping states perform no API call.
- The collector durably records an attempt before invoking the helper and makes no call if that write fails.
- The minimum interval cannot be configured below 300 seconds.
- A valid sample writes one JSONL row and one matching private latest snapshot.
- Helper failure and malformed output write no sample.
- Retention removes only expired date-named JSONL files.
- Paths, subprocess arguments, and logs do not expose credentials.

Repository validation will add the collector to Ruff, `py_compile`, executable-bit checks, and both system and user systemd unit verification.

## Documentation

The README and Raspberry Pi deployment guide will document:

- The quota-only cost model and 288-unit maximum at the default cadence with one timer mode active.
- The disabled-by-default two-step opt-in.
- The requirement to choose either the system timer or the user timer, never both.
- Storage paths, retention, inspection commands, and disable commands.
- The fact that telemetry is descriptive and never part of recovery.
- The current `videos.list` metric limitations, including nullable concurrent viewers.

The newest README changelog entry and `CHANGELOG.md` will describe the optional telemetry addition.

## Deployment And Rollback

After merge, the Raspberry Pi rollout will:

1. Back up the installed API helper, private environment, and systemd state.
2. Install the merged API helper and telemetry executable atomically.
3. Install the new service and timer units and run `systemctl daemon-reload`.
4. Leave `YTA_TELEMETRY_ENABLED` false or unset.
5. Leave both telemetry timers disabled.
6. Run a disabled-mode smoke test and verify it performs no Data API request.
7. Verify the production encoder service remains enabled and operational.

Rollback removes the new telemetry executable and units, restores the prior API helper, reloads systemd, and leaves the encoder lifecycle cache untouched.

## Acceptance Criteria

- The repository exposes a tested `video-metrics` command backed by one `videos.list` request.
- Telemetry is disabled by default and makes zero calls in disabled or ineligible states.
- Enabled telemetry with one timer mode active makes at most one call per 300 seconds, including failed attempts, and therefore no more than 288 calls per 24 hours.
- Telemetry uses no paid service, new OAuth scope, third-party runtime package, or external storage.
- Telemetry failure cannot affect FFmpeg, broadcast lifecycle, visibility, retry state, or service restart behavior.
- Local samples are private, durable, bounded by retention, and contain no credentials.
- System and user timers remain disabled after installation and Pi deployment.
- All local tests, Ruff, bytecode compilation, Markdown lint, systemd verification, secret scanning, and hosted CI checks pass.
