# Raspberry Pi Deployment Notes

These notes describe a low-overhead Raspberry Pi deployment for a dedicated streaming bridge.

## Install Runtime Packages

```bash
sudo apt update
sudo apt install -y ffmpeg python3
```

## Install YouTube AutoEncoder

From a checkout of this repository:

```bash
sudo install -m 0755 bin/youtube-autoencoder /usr/local/bin/youtube-autoencoder
sudo install -m 0755 bin/youtube-autoencoder-api /usr/local/bin/youtube-autoencoder-api
sudo install -m 0755 bin/youtube-autoencoder-test-pattern /usr/local/bin/youtube-autoencoder-test-pattern
sudo install -m 0755 bin/youtube-autoencoder-telemetry /usr/local/bin/youtube-autoencoder-telemetry
sudo install -m 0644 systemd/youtube-autoencoder@.service /etc/systemd/system/youtube-autoencoder@.service
sudo install -m 0644 systemd/youtube-autoencoder-telemetry@.service /etc/systemd/system/youtube-autoencoder-telemetry@.service
sudo install -m 0644 systemd/youtube-autoencoder-telemetry@.timer /etc/systemd/system/youtube-autoencoder-telemetry@.timer
sudo systemctl daemon-reload
```

These commands install but do not enable the system telemetry timer. The user timer also remains disabled unless it is separately installed and enabled.

Create the dedicated account if it does not already exist, then install its private configuration:

```bash
sudo useradd --create-home --user-group --shell /usr/sbin/nologin encoder
sudo install -d -m 0700 -o encoder -g encoder /home/encoder/.config/youtube-autoencoder
sudo install -m 0600 -o encoder -g encoder config/youtube-autoencoder.env.example /home/encoder/.config/youtube-autoencoder/youtube-autoencoder.env
```

Edit the config file for your camera URL, OBS profile path, stable instance ID, staging/live privacy, and bitrate. For a public production stream, the lifecycle settings should include:

```text
YTA_INSTANCE_ID=encoder-hostname
YTA_YOUTUBE_STAGING_PRIVACY=unlisted
YTA_YOUTUBE_LIVE_PRIVACY=public
YTA_YOUTUBE_POLL_INTERVAL_SEC=5
YTA_FFMPEG_PROGRESS_TIMEOUT_SEC=45
YTA_YOUTUBE_COMPLETE_ON_EXIT=false
```

## Authorize YouTube

Install your OAuth client JSON for the system service account:

```bash
sudo install -m 0600 -o encoder -g encoder google-oauth-client.json /home/encoder/.config/youtube-autoencoder/google-oauth-client.json
```

Then run:

```bash
sudo -u encoder -H youtube-autoencoder-api authorize
```

Approve the device code in a browser for the Google account that owns the YouTube channel.

For a fresh deployment without an existing reusable stream, provision and validate it before enabling the production service:

```bash
sudo -u encoder -H env YTA_INSTANCE_ID=encoder-hostname youtube-autoencoder-api run-visible-test \
  --duration 900 --privacy unlisted --create-stream --complete
```

The explicit `--complete` makes this a disposable validation event. Unattended production recovery never completes a broadcast automatically.

## Enable the Service

For user `encoder`:

```bash
sudo systemctl enable --now youtube-autoencoder@encoder.service
```

Watch logs:

```bash
journalctl -u youtube-autoencoder@encoder.service -f
```

## Optional Video Telemetry

Telemetry is disabled by default and never controls encoder recovery. To opt in, set this in `/home/encoder/.config/youtube-autoencoder/youtube-autoencoder.env`:

```text
YTA_TELEMETRY_ENABLED=true
```

Choose exactly one timer mode. For the system service user `encoder`:

```bash
sudo systemctl enable --now youtube-autoencoder-telemetry@encoder.timer
```

For a user-service deployment, install the user unit pair and enable its timer instead:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/youtube-autoencoder-telemetry.service ~/.config/systemd/user/
cp systemd/user/youtube-autoencoder-telemetry.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now youtube-autoencoder-telemetry.timer
```

Inspect the system timer and the service account's local samples:

```bash
sudo systemctl status youtube-autoencoder-telemetry@encoder.timer
sudo -u encoder python3 -m json.tool /home/encoder/.local/state/youtube-autoencoder/telemetry/latest.json
sudo -u encoder tail /home/encoder/.local/state/youtube-autoencoder/telemetry/"$(date -u +%F)".jsonl
```

For user mode, inspect `systemctl --user status youtube-autoencoder-telemetry.timer` and samples under `~/.local/state/youtube-autoencoder/telemetry/`. For system mode, disable the timer before setting the flag back to `false`:

```bash
sudo systemctl disable --now youtube-autoencoder-telemetry@encoder.timer
```

For user mode:

```bash
systemctl --user disable --now youtube-autoencoder-telemetry.timer
```

At the five-minute minimum interval, one active timer uses at most 288 `videos.list` quota units per 24 hours. It adds no separately priced service or external storage. Current-viewer and other optional YouTube values may be `null` when the API omits them.

## Headless Mode

If the Pi is only a streaming bridge, set the default target to non-GUI:

```bash
sudo systemctl set-default multi-user.target
```

Keep SSH, Raspberry Pi Connect, a remote tunnel, or another remote-management path enabled and verified before rebooting. Disabling the graphical target can remove screen-sharing capability even when command-line remote access remains available, so verify the management mode required for deployment before removing desktop packages.

## Recovery Model

- Camera offline: source probe fails, no new broadcast is created, and source backoff is persisted.
- Camera returns: after the source probe passes, the helper stages and binds one marked unlisted event before FFmpeg starts ingest.
- Pre-live failure: the same unlisted broadcast ID remains available for the next attempt.
- Confirmed live health: two consecutive healthy observations promote the event to configured live visibility.
- FFmpeg or camera failure after live: the event and watch URL remain; the service resumes ingest without completing it.
- Pi reboot: systemd starts the service, honors persisted cooldowns, and reconciles the same nonterminal event.
- API quota or outage before FFmpeg starts: startup fails closed and waits under a persisted cooldown rather than trusting cached public state.
- API quota or outage after validated public ingest is active: the already-running stream can continue without nonessential API polling.
- Unmarked bound event: the helper blocks before FFmpeg starts so legacy auto-start settings cannot expose an unmanaged event.
- Ambiguous remote state: the helper creates nothing until an operator resolves the ambiguity.

## Test Pattern

Run a visible, API-managed test broadcast:

```bash
youtube-autoencoder-api run-visible-test --duration 900 --privacy unlisted --complete
```

Stop a test encoder early without completing its broadcast:

```bash
pkill -INT -x ffmpeg
```

Complete only a known live broadcast through the explicit operator command:

```bash
youtube-autoencoder-api complete
```
