# Changelog

All notable project changes are recorded here. The README shows only the most recent entry.

## 2026-07-10 - Idempotent YouTube Lifecycle Recovery

- Added durable instance and generation markers so one nonterminal broadcast is reconciled and reused across camera, FFmpeg, service, and host restarts.
- Added write-ahead state, mutation and supervisor locks, structured API failures, persisted class-specific cooldowns, and fail-closed handling for ambiguous remote state.
- Removed automatic broadcast completion and unconditional recreation from unattended recovery; only an explicit operator command can complete a confirmed live event.
- Made FFmpeg RTSP options capability-aware, removed the incompatible default `-rw_timeout`, and added machine-readable media-progress supervision during API operations.
- Added unlisted staging and verified public promotion only after two consecutive healthy live observations, while preserving the public watch URL through later outages.

## 2026-07-06 - Public Repository Hardening

- Added GitHub Actions CI for Python 3.11, 3.12, and 3.13 with Ruff, py_compile, pytest, executable-bit checks, and systemd unit verification.
- Added CodeQL, Gitleaks-based secret scanning, Dependabot updates, CODEOWNERS, public issue templates, a PR template, `SECURITY.md`, and `CONTRIBUTING.md`.
- Added offline unit coverage for URL redaction, OBS source selection, FFmpeg argument construction, OAuth client parsing, token refresh, secret-file permissions, and broadcast payload construction.
- Added repository metadata hygiene files for editor behavior, line endings, and Markdown linting.

## 2026-07-06 - YouTube Provisioning Documentation

- Added Google Cloud, YouTube Data API, Google Auth Platform, OAuth client, device-code authorization, validation, and common-error instructions for fresh deployments.
- Documented the `org_internal`, `invalid_client`, testing-mode token expiry, and channel-permission failure modes observed during setup.

## 2026-07-06 - Executive Summary Documentation

- Expanded the README into an executive summary covering architecture, runtime process flow, operational commands, recovery behavior, and caveats.
- Added this standalone changelog so README history stays concise.

## 2026-07-06 - Initial Generic AutoEncoder

- Added the generic `YouTube AutoEncoder` package with `youtube-autoencoder`, `youtube-autoencoder-api`, and `youtube-autoencoder-test-pattern`.
- Added source probing, FFmpeg supervision, retry behavior, YouTube Live lifecycle automation, and API-managed visible test broadcasts.
- Added example configuration, systemd units, Raspberry Pi deployment notes, and secret-focused ignore rules.
