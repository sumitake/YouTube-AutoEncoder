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
sudo install -m 0644 systemd/youtube-autoencoder@.service /etc/systemd/system/youtube-autoencoder@.service
sudo systemctl daemon-reload
```

Create the service configuration:

```bash
mkdir -p ~/.config/youtube-autoencoder
cp config/youtube-autoencoder.env.example ~/.config/youtube-autoencoder/youtube-autoencoder.env
chmod 600 ~/.config/youtube-autoencoder/youtube-autoencoder.env
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

Copy your OAuth client JSON to:

```text
~/.config/youtube-autoencoder/google-oauth-client.json
```

Then run:

```bash
youtube-autoencoder-api authorize
```

Approve the device code in a browser for the Google account that owns the YouTube channel.

For a fresh deployment without an existing reusable stream, provision and validate it before enabling the production service:

```bash
YTA_INSTANCE_ID=encoder-hostname youtube-autoencoder-api run-visible-test \
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

## Headless Mode

If the Pi is only a streaming bridge, set the default target to non-GUI:

```bash
sudo systemctl set-default multi-user.target
```

Keep SSH, Raspberry Pi Connect, a remote tunnel, or another remote-management path enabled and verified before rebooting. Disabling the graphical target can remove screen-sharing capability even when command-line remote access remains available, so verify the management mode required for deployment before removing desktop packages.

## Recovery Model

- Camera offline: source probe fails, no new broadcast is created, and source backoff is persisted.
- Camera returns: FFmpeg establishes active ingest before the helper reconciles or creates an unlisted event.
- Pre-live failure: the same unlisted broadcast ID remains available for the next attempt.
- Confirmed live health: two consecutive healthy observations promote the event to configured live visibility.
- FFmpeg or camera failure after live: the event and watch URL remain; the service resumes ingest without completing it.
- Pi reboot: systemd starts the service, honors persisted cooldowns, and reconciles the same nonterminal event.
- API quota or outage: lifecycle mutations pause under a persisted cooldown; a verified public stream can continue without nonessential API polling.
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
