# Security Policy

## Supported Versions

Security fixes are provided for the latest release and the current `main` branch. Older releases are best-effort unless a maintainer explicitly marks them as supported.

## Reporting a Vulnerability

Please use GitHub private vulnerability reporting for suspected vulnerabilities. Do not open a public issue for security-sensitive reports.

Include:

- Affected version or commit.
- Deployment environment.
- Reproduction steps.
- Expected impact.
- Any relevant logs with secrets redacted.

Do not include OAuth client secrets, refresh tokens, YouTube stream keys, camera credentials, or private broadcast management URLs in reports unless GitHub's private advisory flow specifically requires them.

If a credential may have been exposed, rotate it immediately. Removing it from Git history is not a substitute for revocation.
