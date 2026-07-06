# YouTube AutoEncoder

YouTube AutoEncoder is a headless live-stream bridge for unattended cameras and other RTSP-style sources. It runs FFmpeg under systemd and can manage the YouTube Live broadcast lifecycle through the YouTube Data API.

The intended deployment target is a small Linux host such as a Raspberry Pi that should survive:

- camera power loss
- encoder process crashes
- host reboots
- network interruptions
- YouTube broadcasts ending when ingest stops

## What It Does

- Probes the source before creating a YouTube broadcast.
- Creates a new YouTube broadcast only after the source is reachable.
- Binds the broadcast to a reusable YouTube live stream.
- Starts FFmpeg ingest.
- Waits for YouTube to report ingest as active.
- Transitions the broadcast through `testing` and then `live`.
- Completes the broadcast when ingest exits.
- Retries from the top until the source returns.

## Repository Layout

```text
bin/youtube-autoencoder              Production FFmpeg + lifecycle supervisor
bin/youtube-autoencoder-api          YouTube OAuth and Live Streaming API helper
bin/youtube-autoencoder-test-pattern Temporary moving test-pattern stream
config/youtube-autoencoder.env.example
systemd/youtube-autoencoder@.service System service template
systemd/user/youtube-autoencoder.service User service template
docs/raspberry-pi.md                 Raspberry Pi deployment notes
```

## Requirements

- Linux with systemd
- Python 3.11 or newer
- FFmpeg and FFprobe
- A YouTube channel with live streaming enabled
- A Google OAuth client JSON file for an installed/device-style app

No third-party Python packages are required.

## Quick Install

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

Edit the env file for your source and YouTube settings.

Install a service. For a system service running as user `encoder`:

```bash
sudo install -m 0644 systemd/youtube-autoencoder@.service /etc/systemd/system/youtube-autoencoder@.service
sudo systemctl daemon-reload
sudo systemctl enable --now youtube-autoencoder@encoder.service
```

For a user service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/youtube-autoencoder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now youtube-autoencoder.service
```

## YouTube Authorization

Create an OAuth client in Google Cloud Console and place the downloaded JSON at:

```text
~/.config/youtube-autoencoder/google-oauth-client.json
```

Then run:

```bash
youtube-autoencoder-api authorize
```

Open the displayed device-flow URL, enter the code, and approve access for the YouTube channel account. The refresh token is stored at:

```text
~/.config/youtube-autoencoder/youtube-token.json
```

Keep both files private.

## Source Configuration

The simplest configuration is a direct RTSP source:

```text
YTA_SOURCE_URL=rtsp://camera.example.local/stream1
```

Alternatively, the encoder can reuse an OBS profile and scene collection:

```text
YTA_OBS_SERVICE_FILE=/home/encoder/.config/obs-studio/basic/profiles/Stream/service.json
YTA_OBS_SCENE_FILE=/home/encoder/.config/obs-studio/basic/scenes/Untitled.json
YTA_OBS_SOURCE_NAME=Camera RTSP
```

The OBS service file supplies the reusable YouTube RTMPS server and stream key. If YouTube AutoEncoder creates a reusable stream through the API, it updates that service file.

## Normal Operation

```bash
systemctl status youtube-autoencoder@encoder.service
journalctl -u youtube-autoencoder@encoder.service -f
youtube-autoencoder-api status
```

When the camera is offline, the service stays active but does not run FFmpeg and does not create YouTube broadcasts. Logs will show source-probe failures and retry timing.

When the camera comes back, the service creates and starts a fresh YouTube broadcast automatically.

## Test Pattern

To visually validate YouTube ingest quality:

```bash
youtube-autoencoder-test-pattern 900
```

For a complete visible API-managed test:

```bash
youtube-autoencoder-api run-visible-test --duration 900 --privacy unlisted
```

## Security Notes

- Do not commit OAuth client files, refresh tokens, stream keys, or `.env` files.
- The scripts redact RTSP credentials and YouTube stream keys from their own logs.
- System process listings can still expose full FFmpeg command lines to privileged local users while a stream is active.
- Use a dedicated Google OAuth client and a dedicated encoder user where practical.

## License

MPL-2.0. See [LICENSE](LICENSE).
