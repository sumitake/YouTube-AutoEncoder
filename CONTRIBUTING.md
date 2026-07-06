# Contributing

Thanks for helping improve YouTube AutoEncoder.

## Development Setup

Install development tools:

```bash
python3 -m pip install -r requirements-dev.txt
```

Run the local checks:

```bash
ruff check .
python3 -m py_compile bin/youtube-autoencoder bin/youtube-autoencoder-api bin/youtube-autoencoder-test-pattern
pytest -q
```

On Linux hosts with systemd, also verify the unit files:

```bash
sudo install -m 0755 bin/youtube-autoencoder /usr/local/bin/youtube-autoencoder
systemd-analyze verify systemd/youtube-autoencoder@.service systemd/user/youtube-autoencoder.service
```

## Pull Requests

- Keep runtime dependencies minimal. The production scripts should remain standard-library Python unless a dependency is clearly justified.
- Keep configuration examples generic.
- Never commit OAuth client files, refresh tokens, YouTube stream keys, camera credentials, OBS `service.json`, generated state files, or captured video output.
- Update documentation for user-facing changes.
- Add or update tests for behavior changes.

## Security Reports

Report suspected vulnerabilities through GitHub private vulnerability reporting. Do not open a public issue for security-sensitive reports.
