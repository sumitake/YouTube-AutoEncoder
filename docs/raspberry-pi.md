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

Edit the config file for your camera URL, OBS profile path, privacy mode, and bitrate.

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

Keep SSH or another remote-management path enabled before rebooting.

## Recovery Model

- Camera offline: source probe fails, no broadcast is created.
- Camera returns: broadcast is created, ingest starts, YouTube transitions to live.
- FFmpeg exits after a live broadcast: broadcast is completed, then the service retries.
- Pi reboots: systemd starts the service and the same source-probe workflow resumes.

## Test Pattern

Run a visible, API-managed test broadcast:

```bash
youtube-autoencoder-api run-visible-test --duration 900 --privacy unlisted
```

Stop a test early:

```bash
pkill -INT -x ffmpeg
youtube-autoencoder-api complete
```
