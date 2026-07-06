# YouTube AutoEncoder

YouTube AutoEncoder is a headless, self-recovering live-stream bridge for unattended RTSP-style cameras and YouTube Live. It is designed for small Linux hosts such as Raspberry Pi systems where a full OBS desktop stack is too heavy, but where the stream still needs to recover from camera power loss, encoder crashes, network interruptions, host reboots, and YouTube broadcast lifecycle edge cases.

The project runs FFmpeg under systemd, optionally reuses OBS profile data for camera and stream-key compatibility, and can manage YouTube Live broadcasts through the YouTube Data API.

## Executive Summary

YouTube AutoEncoder turns a dedicated Linux device into an unattended YouTube streaming encoder. The core service performs three jobs:

- Validate the camera source before creating public-facing YouTube objects.
- Push the stream with FFmpeg using either low-CPU video copy mode or explicit transcode mode.
- Create, bind, transition, complete, and recreate YouTube Live broadcasts as needed.

The intended outcome is an appliance-like encoder that can be deployed on a Raspberry Pi, left headless, and managed remotely through normal Linux tools. When the camera is offline, the service waits without creating new broadcasts. When the camera returns, it prepares YouTube, starts ingest, transitions the broadcast live, and keeps watching FFmpeg until the next interruption.

## Current Status

- Project maturity: early operational package.
- Primary target: Debian/Raspberry Pi OS with systemd.
- Runtime dependencies: Python 3.11 or newer, FFmpeg, FFprobe.
- Python dependencies: standard library only.
- YouTube integration: OAuth device flow plus YouTube Data API v3 live-streaming endpoints.

## Architecture

```text
             camera / RTSP source
                      |
                      v
              ffprobe source probe
                      |
          source reachable? no -> wait and retry
                      |
                     yes
                      |
                      v
+------------------------------------------------+
| youtube-autoencoder                            |
|                                                |
| - loads config and OBS compatibility files     |
| - prepares YouTube lifecycle when enabled      |
| - starts and supervises FFmpeg                 |
| - redacts sensitive values in service logs     |
| - completes broadcast on exit when configured  |
+---------------------+--------------------------+
                      |
        invokes       | starts
                      v
+-------------------+     +----------------------+
| youtube-          |     | FFmpeg               |
| autoencoder-api   |     |                      |
|                   |     | RTSP input           |
| OAuth refresh     |     | synthetic audio      |
| stream lookup     |     | copy or transcode    |
| broadcast create  |     | RTMPS/RTMP output    |
| bind/transition   |     +----------+-----------+
+---------+---------+                |
          |                          v
          v                    YouTube ingest
 YouTube Data API                    |
          |                          v
          +------------------ YouTube Live broadcast
```

### Components

| Component | Path | Responsibility |
| --- | --- | --- |
| Supervisor | `bin/youtube-autoencoder` | Main loop, source probing, FFmpeg supervision, retry behavior, broadcast completion on exit. |
| API helper | `bin/youtube-autoencoder-api` | OAuth device authorization, token refresh, YouTube stream lookup/create, broadcast create/bind/transition/complete, visible test workflow. |
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
- `youtube-live-state.json`: last prepared broadcast and stream IDs.

OBS compatibility mode can also read and update:

```text
~/.config/obs-studio/basic/profiles/YouTube_AutoEncoder/service.json
~/.config/obs-studio/basic/scenes/Untitled.json
```

## Process Flow

### Normal Production Loop

1. systemd starts `youtube-autoencoder`.
2. The supervisor loads configuration from the service environment file.
3. The source URL is resolved from `YTA_SOURCE_URL` or from an OBS scene collection.
4. The YouTube ingest URL is resolved from OBS `service.json`.
5. FFprobe checks the RTSP source when `YTA_SOURCE_PROBE=true`.
6. If the source is unreachable, the service waits `YTA_RESTART_DELAY` and retries without creating a YouTube broadcast.
7. If YouTube lifecycle management is enabled, the API helper locates or creates a reusable YouTube live stream.
8. The API helper creates a broadcast, binds it to the reusable stream, and stores the IDs in `youtube-live-state.json`.
9. The supervisor starts FFmpeg.
10. The API helper waits until YouTube reports active ingest.
11. The broadcast transitions to `testing`, waits `YTA_YOUTUBE_TESTING_DELAY_SEC`, then transitions to `live`.
12. The supervisor streams until FFmpeg exits, the service is stopped, or `YTA_MAX_RUNTIME` rotates the process.
13. On exit, the supervisor completes the YouTube broadcast when `YTA_YOUTUBE_COMPLETE_ON_EXIT=true`.
14. The loop sleeps and restarts from source probing.

### Recovery Behavior

| Failure | Expected behavior |
| --- | --- |
| Camera offline before stream start | Source probe fails; no broadcast is created; service retries. |
| Camera loses power during stream | FFmpeg exits; broadcast is completed; service retries until the camera returns. |
| Host reboots | systemd restarts the service after network-online target; normal loop resumes. |
| FFmpeg crashes | systemd and the internal loop restart the attempt. |
| YouTube ingest does not become active | API wait times out; attempt exits; service retries. |
| OAuth access token expires | API helper refreshes from the stored refresh token. |
| Reusable YouTube stream missing | With `YTA_YOUTUBE_CREATE_STREAM=true`, API helper creates one and writes it to OBS `service.json`. |

### Test Pattern Flow

For visual validation, `youtube-autoencoder-api run-visible-test` creates and binds a broadcast, starts `youtube-autoencoder-test-pattern`, waits for active ingest, transitions to live, and completes the broadcast when the test exits.

This verifies the YouTube account, OAuth token, reusable stream, ingest URL, broadcast transitions, FFmpeg output, and visible end-stream quality without requiring the real camera to be online.

## Installation

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
sudo systemctl enable --now youtube-autoencoder@encoder.service
```

For a user service instead:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/youtube-autoencoder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now youtube-autoencoder.service
```

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
| `YTA_YOUTUBE_PRIVACY` | YouTube broadcast privacy: `public`, `unlisted`, or `private`. |
| `YTA_YOUTUBE_TITLE_PREFIX` | Prefix used for generated broadcast titles. |
| `YTA_YOUTUBE_CREATE_STREAM` | Create a reusable YouTube stream when the configured stream key is not found. |
| `YTA_SOURCE_PROBE` | Probe the camera before creating a broadcast. |
| `YTA_RESTART_DELAY` | Delay between recovery attempts. |
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

The OBS service file supplies the reusable YouTube RTMPS server and stream key. If YouTube AutoEncoder creates a reusable stream through the API, it updates that service file.

## YouTube Authorization

Create a Google OAuth client and place the downloaded JSON at:

```text
~/.config/youtube-autoencoder/google-oauth-client.json
```

Run:

```bash
youtube-autoencoder-api authorize
```

Open the displayed device-flow URL, enter the code, and approve access for the YouTube channel account. The refresh token is stored in:

```text
~/.config/youtube-autoencoder/youtube-token.json
```

Keep both files private.

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
youtube-autoencoder-api status
```

Run a direct test-pattern stream using the configured OBS service file:

```bash
youtube-autoencoder-test-pattern 900
```

Run a complete visible API-managed test broadcast:

```bash
youtube-autoencoder-api run-visible-test --duration 900 --privacy unlisted
```

Complete the last known broadcast manually:

```bash
youtube-autoencoder-api complete
```

## Caveats

- YouTube Live must already be enabled on the channel. New or restricted channels may not be allowed to stream immediately.
- The YouTube Data API flow requires OAuth user consent. A simple API key is not enough for creating, binding, or transitioning live broadcasts.
- Google OAuth app restrictions can block authorization if the app is limited to an organization that does not include the streaming account.
- YouTube API quota, API outages, or account policy restrictions can prevent lifecycle operations even when FFmpeg is healthy.
- Each recovery cycle can create a new broadcast when lifecycle management is enabled. This is intentional for self-recovery, but it can leave multiple completed broadcasts in YouTube Studio after unstable camera or network periods.
- `YTA_MODE=copy` is lowest CPU, but it only works when the camera video stream is compatible with YouTube ingest expectations. H.265 or unusual camera output usually requires `YTA_MODE=transcode`.
- The production encoder adds synthetic silent audio because YouTube ingest generally behaves better with audio present. It does not preserve camera audio today.
- The OBS scene parser is intentionally narrow. It looks for VLC sources and playlist URLs; complex OBS scenes, filters, browser sources, or arbitrary OBS plugins are not reproduced.
- The scripts redact credentials from their own logs, but privileged local users may still see full FFmpeg command-line arguments while a stream is active.
- If the camera flaps after a broadcast is created but before YouTube reports active ingest, the attempt will fail and retry. The incomplete broadcast may remain in YouTube Studio.
- Raspberry Pi Connect screen sharing requires a graphical session. A headless deployment should rely on SSH, remote shell, VPN, or another non-GUI management path.
- This project does not install or configure firewalling, VPN, remote management, OS hardening, or camera power control.

## Changelog

Only the most recent changelog entry is shown here. See `CHANGELOG.md` for full history.

### 2026-07-06 - Executive Summary Documentation

- Expanded the README into an executive summary covering architecture, runtime process flow, operational commands, recovery behavior, and caveats.
- Added a standalone changelog so the README can show only the newest entry.

## Repository Layout

```text
bin/youtube-autoencoder              Production FFmpeg and lifecycle supervisor
bin/youtube-autoencoder-api          YouTube OAuth and Live Streaming API helper
bin/youtube-autoencoder-test-pattern Temporary moving test-pattern stream
config/youtube-autoencoder.env.example
systemd/youtube-autoencoder@.service System service template
systemd/user/youtube-autoencoder.service User service template
docs/raspberry-pi.md                 Raspberry Pi deployment notes
CHANGELOG.md                         Full project changelog
```

## License

MPL-2.0. See `LICENSE`.
