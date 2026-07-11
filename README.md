# YouTube AutoEncoder

[![CI](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/ci.yml/badge.svg)](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/ci.yml)
[![CodeQL](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/codeql.yml/badge.svg)](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/codeql.yml)
[![Secret Scan](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/sumitake/YouTube-AutoEncoder/actions/workflows/secret-scan.yml)

YouTube AutoEncoder is a headless, self-recovering live-stream bridge for unattended RTSP-style cameras and YouTube Live. It is designed for small Linux hosts such as Raspberry Pi systems where a full OBS desktop stack is too heavy, but where the stream still needs to recover from camera power loss, encoder crashes, network interruptions, host reboots, and YouTube broadcast lifecycle edge cases.

The project runs FFmpeg under systemd, optionally reuses OBS profile data for camera and stream-key compatibility, and can manage YouTube Live broadcasts through the YouTube Data API.

## Executive Summary

YouTube AutoEncoder turns a dedicated Linux device into an unattended YouTube streaming encoder. The core service performs three jobs:

- Validate the camera source, stage one marked unlisted broadcast, and require healthy media before lifecycle transitions.
- Push the stream with FFmpeg using low-CPU video copy mode or explicit transcode mode.
- Reconcile one marked YouTube broadcast, stage it unlisted, and publish it only after live health is confirmed.

The intended outcome is an appliance-like encoder that can be deployed on a Raspberry Pi, left headless, and managed remotely through normal Linux tools. Camera, encoder, service, network, and host failures retain the same nonterminal broadcast and watch URL. A replacement is allowed only after YouTube confirms that the previous event is terminal or missing.

## Current Status

- Project maturity: operational package with unattended lifecycle recovery.
- Primary target: Debian/Raspberry Pi OS with systemd.
- Runtime dependencies: Python 3.11 or newer, FFmpeg, FFprobe.
- Python dependencies: standard library only.
- YouTube integration: OAuth device flow plus YouTube Data API v3 live-streaming endpoints.

## Architecture

See the [system architecture diagram](docs/architecture-and-flows.md#system-architecture) for component boundaries and media/control data flows.

The supervisor owns local process health, polling, backoff, and publication timing. The API helper owns OAuth, remote resource reconciliation, durable lifecycle state, and serialized mutations. YouTube remains authoritative; the local state file is a recovery cache and is revalidated before mutations.

For the detailed reconciliation algorithm, lifecycle states, and test strategy, see the [idempotent lifecycle recovery design](docs/superpowers/specs/2026-07-10-idempotent-youtube-lifecycle-design.md).

### Components

| Component | Path | Responsibility |
| --- | --- | --- |
| Supervisor | `bin/youtube-autoencoder` | Source probing, FFmpeg capability and progress supervision, lifecycle timing, publication gates, and persisted recovery policy. |
| API helper | `bin/youtube-autoencoder-api` | OAuth, reusable stream management, exact broadcast reconciliation, serialized mutations, privacy verification, and explicit completion. |
| Test pattern | `bin/youtube-autoencoder-test-pattern` | FFmpeg-generated moving video and tone for end-to-end YouTube ingest testing. |
| Example config | `config/youtube-autoencoder.env.example` | Service environment variables for source selection, FFmpeg mode, YouTube lifecycle, retry timing, and transcode settings. |
| System service | `systemd/youtube-autoencoder@.service` | System-level service template for a dedicated encoder user. |
| User service | `systemd/user/youtube-autoencoder.service` | User-level service alternative. |
| Pi runbook | `docs/raspberry-pi.md` | Raspberry Pi deployment notes. |

### Persistent Local State

By default the runtime expects service-owned files under:

```text
~/.config/youtube-autoencoder/
```

Important files:

- `youtube-autoencoder.env`: private service configuration.
- `google-oauth-client.json`: Google OAuth client configuration.
- `youtube-token.json`: OAuth access and refresh token cache.
- `youtube-live-state.json`: versioned, non-secret lifecycle cache containing instance, generation, stream, broadcast, privacy, lifecycle, and retry metadata.
- `youtube-live-state.lock`: serialized mutation lock for create, bind, transition, privacy, and explicit completion operations.
- `supervisor.lock`: process-lifetime lock preventing two encoder supervisors from running concurrently.

OBS compatibility mode can also read and update:

```text
~/.config/obs-studio/basic/profiles/YouTube_AutoEncoder/service.json
~/.config/obs-studio/basic/scenes/Untitled.json
```

## Process Flow

### Normal Production Loop

1. systemd starts `youtube-autoencoder`.
2. The supervisor acquires its single-instance lock and honors any persisted recovery deadline.
3. The source and ingest URLs are resolved from direct settings or OBS compatibility files.
4. FFprobe checks the RTSP source; unavailable sources use bounded source backoff without a broadcast insert.
5. Before ingest starts, the helper lists recoverable events, blocks any unmarked event bound to the reusable stream, and reuses or stages exactly one marked unlisted broadcast.
6. The helper durably stores the broadcast ID and verifies binding before the supervisor starts FFmpeg.
7. FFmpeg emits machine-readable progress and pushes to the reusable YouTube stream.
8. One-shot API checks wait for both recent media progress and active YouTube ingest.
9. Before and after each `testing` or `live` transition, the supervisor rechecks FFmpeg progress and YouTube ingest.
10. Two consecutive healthy `live` observations are required before visibility changes to `YTA_YOUTUBE_LIVE_PRIVACY`.
11. The helper verifies the privacy readback, clears recovery state, and the supervisor stops nonessential API polling.
12. FFmpeg remains supervised until source loss, process failure, service stop, rotation, or host interruption; failures persist a class-specific cooldown and reuse the same broadcast without completing it.

### Recovery Behavior

Every recovery path first preserves ownership and retry state. A passing source probe permits one marked unlisted event to be staged before ingest. Fresh media and active YouTube ingest remain mandatory before `testing`, `live`, or public promotion.

See the [recovery state machine](docs/architecture-and-flows.md#recovery-state-machine) for startup, managed-generation, and durable-cooldown transitions.

| Failure | Expected behavior |
| --- | --- |
| Camera offline before stream start | Source probe fails; no new broadcast is created; source backoff is persisted. |
| Camera loses power during stream | FFmpeg exits; the same broadcast and watch URL remain; source backoff continues until the camera returns. |
| Host reboots | systemd restarts the service, the durable cache is reconciled, and the same nonterminal event resumes. |
| FFmpeg exits or stalls | The child and any in-flight API helper stop; the same event is retained for retry. |
| YouTube ingest does not become active | The single staged unlisted event is retained; no transition or publication occurs. |
| YouTube API rate limit or outage | The retry class and deadline persist. A previously verified public stream can continue without control-plane mutation. |
| Unmarked event is bound to the reusable stream | Reconciliation fails closed before FFmpeg starts, preventing an unintended legacy auto-start. |
| Ambiguous or unknown remote state | Reconciliation fails closed and creates nothing until the ambiguity is resolved. |
| OAuth access token expires | API helper refreshes from the stored refresh token. |
| Previous broadcast is `complete`, `revoked`, or confirmed missing | One new unlisted generation may be staged after the source probe passes. |

Recovery deadlines survive service and host restarts. Exponential backoff uses these class floors and caps:

| Class | Initial floor | Maximum | Typical causes |
| --- | ---: | ---: | --- |
| Source/encoder | 10 seconds | 5 minutes | Camera unavailable, FFmpeg exit, stale media progress, inactive ingest. |
| API | 30 seconds | 15 minutes | Network timeout, transient HTTP failure, unavailable API service. |
| Quota | 15 minutes | 6 hours | `userRequestsExceedRateLimit`, quota errors, HTTP 429. |
| Ambiguous | 5 minutes | 1 hour | Multiple managed candidates, unknown lifecycle, conflicting state. |

Every managed event description contains exact instance and generation markers. A write-ahead generation is persisted before insert, so a lost API response is reconciled by marker before another insert can occur. Title matching is never used as proof of ownership.

### Test Pattern Flow

For visual validation, `youtube-autoencoder-api run-visible-test` starts `youtube-autoencoder-test-pattern`, waits for active ingest, reconciles the managed event, and transitions it live. It leaves the event running unless `--complete` is explicitly supplied.

This verifies the YouTube account, OAuth token, reusable stream, ingest URL, broadcast transitions, FFmpeg output, and visible end-stream quality without requiring the real camera to be online. Use `--complete` for a disposable test so the later production camera run receives a fresh event and title.

## Installation

Provision the YouTube control plane and the encoder host in this order. The detailed console steps and commands remain in the sections that follow.

See the [provisioning and deployment flow](docs/architecture-and-flows.md#provisioning-and-deployment) for the end-to-end setup sequence.

Install runtime packages:

```bash
sudo apt update
sudo apt install -y ffmpeg python3
```

Install the scripts:

```bash
sudo install -m 0755 bin/youtube-autoencoder /usr/local/bin/youtube-autoencoder
sudo install -m 0755 bin/youtube-autoencoder-api /usr/local/bin/youtube-autoencoder-api
sudo install -m 0755 bin/youtube-autoencoder-test-pattern /usr/local/bin/youtube-autoencoder-test-pattern
```

Create a config directory for the service user:

```bash
mkdir -p ~/.config/youtube-autoencoder
cp config/youtube-autoencoder.env.example ~/.config/youtube-autoencoder/youtube-autoencoder.env
chmod 600 ~/.config/youtube-autoencoder/youtube-autoencoder.env
```

Install the system service for a dedicated user named `encoder`:

```bash
sudo install -m 0644 systemd/youtube-autoencoder@.service /etc/systemd/system/youtube-autoencoder@.service
sudo systemctl daemon-reload
```

For a user service instead:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/youtube-autoencoder.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

The unit is installed but deliberately left disabled until OAuth, source, ingest, and visible-stream validation succeed.

For Raspberry Pi specific notes, see `docs/raspberry-pi.md`.

## Configuration Model

The service is configured entirely through environment variables. The most important values are:

| Variable | Purpose |
| --- | --- |
| `YTA_SOURCE_URL` | Direct camera/source URL. Preferred for non-OBS deployments. |
| `YTA_OBS_SERVICE_FILE` | OBS service file containing YouTube ingest server and stream key. |
| `YTA_OBS_SCENE_FILE` | OBS scene file used to discover a VLC/RTSP source when `YTA_SOURCE_URL` is not set. |
| `YTA_OBS_SOURCE_NAME` | Optional OBS VLC source name selector. |
| `YTA_MODE` | `copy` for low CPU video copy, or `transcode` for re-encoding. |
| `YTA_YOUTUBE_LIFECYCLE` | Enable or disable API-managed broadcast lifecycle. |
| `YTA_INSTANCE_ID` | Stable deployment identity used in exact broadcast ownership markers. |
| `YTA_YOUTUBE_STAGING_PRIVACY` | Visibility for a new event before health confirmation; use `unlisted`. |
| `YTA_YOUTUBE_LIVE_PRIVACY` | Visibility applied after two healthy live observations; use `public` for a public channel stream. |
| `YTA_YOUTUBE_TITLE_PREFIX` | Prefix used for generated broadcast titles. |
| `YTA_YOUTUBE_POLL_INTERVAL_SEC` | Poll interval used only during bounded ingest, transition, and publication gates. |
| `YTA_YOUTUBE_TRANSITION_TIMEOUT_SEC` | Upper bound for a lifecycle transition sequence. |
| `YTA_FFMPEG_PROGRESS_TIMEOUT_SEC` | Maximum time without increasing FFmpeg media output before restart. |
| `YTA_SOURCE_PROBE` | Probe the camera before creating a broadcast. |
| `YTA_MAX_RUNTIME` | Optional forced FFmpeg rotation interval. `0` disables rotation. |

Minimal direct source configuration:

```text
YTA_SOURCE_URL=rtsp://camera.example.local/stream1
```

OBS compatibility configuration:

```text
YTA_OBS_SERVICE_FILE=/home/encoder/.config/obs-studio/basic/profiles/Stream/service.json
YTA_OBS_SCENE_FILE=/home/encoder/.config/obs-studio/basic/scenes/Untitled.json
YTA_OBS_SOURCE_NAME=Camera RTSP
```

The OBS service file supplies the reusable YouTube RTMPS server and stream key. It must exist, be writable by the service user, and contain a non-empty `settings.key` before `--create-stream` runs. For a fresh deployment, keep the service disabled and seed a placeholder key:

```json
{
  "settings": {
    "key": "provision-new-stream"
  }
}
```

The placeholder is not a YouTube stream key. `--create-stream` uses it to confirm that no existing stream matches, then replaces it with the new reusable stream key and writes the ingest server before starting the test encoder. A missing file or empty key does not enter the creation path.

## YouTube API and OAuth Provisioning

YouTube AutoEncoder needs OAuth, not just an API key. The service creates and manages `liveBroadcast` and `liveStream` resources, binds them, transitions broadcasts through `testing` and `live`, and updates visibility. Completion is an explicit operator action. These operations must be authorized by the Google account that owns or manages the YouTube channel.

Official references:

- [YouTube Live Streaming API Overview](https://developers.google.com/youtube/v3/live/getting-started)
- [Obtaining authorization credentials](https://developers.google.com/youtube/registering_an_application)
- [OAuth device flow for limited-input devices](https://developers.google.com/youtube/v3/guides/auth/devices)
- [Google Auth Platform audience settings](https://support.google.com/cloud/answer/15549945)
- [Google Auth Platform OAuth clients](https://support.google.com/cloud/answer/15549257)

### 1. Prepare the YouTube Channel

1. Sign in to YouTube Studio with the Google account that owns or manages the channel.
2. Confirm that live streaming is enabled for the channel. New channels, restricted channels, or channels with policy holds may not be able to stream immediately.
3. If the channel is a Brand Account, authorize with a Google account that can manage that Brand Account.

### 2. Create or Select a Google Cloud Project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project for the encoder, or select an existing project dedicated to this deployment.
3. Open **APIs & Services > Library**.
4. Enable **YouTube Data API v3** for the project.

The Live Streaming API is exposed through the YouTube Data API v3 for the broadcast and stream operations this project uses.

### 3. Configure Google Auth Platform

Open **Google Auth Platform** for the same project and configure the app before creating the OAuth client.

Audience:

- Use **External** when the Google account authorizing the YouTube channel may be outside your Google Cloud Organization.
- Use **Internal** only when every authorizing account is in the same Google Cloud Organization as the project.
- If you see `org_internal` during authorization, the OAuth app is limited to organization users. Change the audience to External or authorize with an account inside that organization.

Publishing status:

- **Testing** is fine for initial setup. Add the streaming Google account as a test user before authorizing.
- Testing-mode authorizations for non-basic scopes can expire after seven days, including refresh tokens. For unattended deployments, move the app to **In production** and complete any required Google verification.
- In production, users may see an unverified-app warning until Google verifies the app and requested scopes.

Data access / scopes:

- Add `https://www.googleapis.com/auth/youtube`.
- This scope is broad, but it is the scope this project uses to manage YouTube Live broadcasts and streams.
- Avoid adding extra scopes unless the code actually needs them; additional sensitive or restricted scopes can increase verification requirements.

Branding:

- Use an app name that identifies the deployment, such as `YouTube AutoEncoder`.
- Provide a monitored support email.
- Add privacy policy, terms, and authorized domain information if Google requires them for your app state.

### 4. Create the OAuth Client

1. Open **Google Auth Platform > Clients**.
2. Click **Create client**.
3. Choose **TVs and Limited Input devices** where available. This matches the device-code flow used by `youtube-autoencoder-api authorize`.
4. Name the client, for example `YouTube AutoEncoder`.
5. Create the client and download the JSON credentials.

If the console only offers a generic installed-app flow in your environment, use the device or installed/native option intended for command-line or limited-input devices. If authorization later fails with `invalid_client`, create a new client with the explicit **TVs and Limited Input devices** application type.

### 5. Install the OAuth Client JSON

Copy the downloaded JSON to the service user's config directory:

```text
~/.config/youtube-autoencoder/google-oauth-client.json
```

Lock down the file:

```bash
chmod 600 ~/.config/youtube-autoencoder/google-oauth-client.json
```

The file contains OAuth client credentials. Do not commit it, paste it into issue trackers, or store it in a world-readable location.

### 6. Authorize the Encoder

```bash
youtube-autoencoder-api authorize
```

The command prints a verification URL and user code. Open the URL on any browser-capable device, enter the code, and approve access with the Google account that owns or manages the target YouTube channel.

After approval, the helper stores the OAuth token cache at:

```text
~/.config/youtube-autoencoder/youtube-token.json
```

Lock down the token file:

```bash
chmod 600 ~/.config/youtube-autoencoder/youtube-token.json
```

Keep this file private. It contains the refresh token used for unattended operation.

### 7. Validate the YouTube API Setup

If you already have an OBS-compatible `service.json` with a YouTube stream key:

```bash
youtube-autoencoder-api status
```

For a fresh setup, create the writable OBS service file with the placeholder `settings.key` shown in the Configuration Model. Then provision the reusable stream through an API-managed visible test after the rest of the encoder config is in place:

```bash
YTA_INSTANCE_ID=encoder-hostname youtube-autoencoder-api run-visible-test \
  --duration 900 --privacy unlisted --create-stream --complete
```

This validates OAuth, reusable-stream provisioning, idempotent broadcast reconciliation, stream binding, ingest detection, transitions to `testing` and `live`, and explicit completion. The normal unattended service never completes on exit.

### 8. Enable the Service

After visible validation succeeds, enable the system service:

```bash
sudo systemctl enable --now youtube-autoencoder@encoder.service
```

For a user service instead:

```bash
systemctl --user enable --now youtube-autoencoder.service
```

### Common Authorization Problems

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `org_internal` | OAuth app audience is Internal and the authorizing account is outside the project's Google Cloud Organization. | Change the app audience to External, or authorize with an account inside the organization. |
| `invalid_client` | OAuth client type does not support the device-code flow. | Create a client for TVs and Limited Input devices, then replace `google-oauth-client.json`. |
| `authorization_pending` | The browser approval has not completed yet. | Finish the device-code flow; the CLI will keep polling until the code expires. |
| `slow_down` | Polling is too frequent. | The helper backs off automatically. |
| Token works briefly then expires | App is still in Testing mode. | Add the correct test user for setup, then move the app to In production for unattended use and complete required verification. |
| API calls fail despite valid OAuth | The account does not own/manage the YouTube channel, live streaming is not enabled, or quota/policy blocks the operation. | Reauthorize with the right channel account, enable live streaming, and check project quota and YouTube Studio restrictions. |

## Operations

Check service status:

```bash
systemctl status youtube-autoencoder@encoder.service
```

Follow logs:

```bash
journalctl -u youtube-autoencoder@encoder.service -f
```

Check YouTube stream status:

```bash
youtube-autoencoder-api stream-status
youtube-autoencoder-api state
youtube-autoencoder-api broadcast-status
```

Run a direct test-pattern stream using the configured OBS service file:

```bash
youtube-autoencoder-test-pattern 900
```

Run a disposable visible API-managed test broadcast:

```bash
youtube-autoencoder-api run-visible-test --duration 900 --privacy unlisted --complete
```

Complete the last known broadcast manually:

```bash
youtube-autoencoder-api complete
```

## Caveats

- YouTube Live must already be enabled on the channel. New or restricted channels may not be allowed to stream immediately.
- The YouTube Data API flow requires OAuth user consent. A simple API key is not enough for creating, binding, or transitioning live broadcasts.
- Google OAuth app restrictions can block authorization if the app is limited to an organization that does not include the streaming account.
- A source that passes its probe and then fails can leave one marked unlisted upcoming event. Recovery reuses that event; it does not create another generation.
- YouTube API quota, API outages, or account policy restrictions can prevent lifecycle operations even when FFmpeg is healthy.
- Recovery reuses one exactly marked nonterminal broadcast. A new generation is permitted only after the previous managed event is `complete`, `revoked`, or confirmed missing.
- Broadcasts created by older releases do not contain ownership markers. They are not adopted or deleted automatically; inventory and clean up legacy duplicates separately after verifying their lifecycle and watch URLs.
- A live broadcast cannot return to a scheduled or testing state. After an event reaches `live`, recovery preserves it until an operator explicitly completes it.
- A public event intentionally remains public during a camera or host outage, so its existing watch page may temporarily show unavailable video while the encoder recovers.
- `YTA_MODE=copy` is lowest CPU, but it only works when the camera video stream is compatible with YouTube ingest expectations. H.265 or unusual camera output usually requires `YTA_MODE=transcode`.
- The production encoder adds synthetic silent audio because YouTube ingest generally behaves better with audio present. It does not preserve camera audio today.
- The OBS scene parser is intentionally narrow. It looks for VLC sources and playlist URLs; complex OBS scenes, filters, browser sources, or arbitrary OBS plugins are not reproduced.
- The scripts redact credentials from their own logs, but privileged local users may still see full FFmpeg command-line arguments while a stream is active.
- Copy mode can expose camera timestamp or codec defects. Public promotion remains blocked unless YouTube reports active, healthy, live ingest; inspect health details before selecting a transcode mode.
- Keep at least one non-GUI remote-management path operational before disabling the desktop. Raspberry Pi Connect screen sharing still depends on a graphical session, while SSH or a remote shell remains suitable for headless service management.
- This project does not install or configure firewalling, VPN, remote management, OS hardening, or camera power control.

## Changelog

Only the most recent changelog entry is shown here. See `CHANGELOG.md` for full history.

### 2026-07-11 - Pre-Ingest Broadcast Staging

- Stages and binds one marked unlisted event before FFmpeg ingest, blocks unmarked bound conflicts, and logs structured API operation and rate-limit details without exposing credentials.

## Repository Layout

```text
bin/youtube-autoencoder              Production FFmpeg and lifecycle supervisor
bin/youtube-autoencoder-api          YouTube OAuth and Live Streaming API helper
bin/youtube-autoencoder-test-pattern Temporary moving test-pattern stream
config/youtube-autoencoder.env.example
systemd/youtube-autoencoder@.service System service template
systemd/user/youtube-autoencoder.service User service template
docs/architecture-and-flows.md       Architecture and operational flow diagrams
docs/raspberry-pi.md                 Raspberry Pi deployment notes
CHANGELOG.md                         Full project changelog
```

## License

MPL-2.0. See `LICENSE`.
