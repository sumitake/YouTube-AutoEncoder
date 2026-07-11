# README Architecture Diagrams Design

## Summary

Update the executive README with three GitHub-native Mermaid diagrams for a balanced audience of deployment operators and code contributors. The diagrams will replace the current ASCII architecture sketch and make recovery and deployment behavior visually traceable without duplicating the implementation-level lifecycle design.

## Audience

The README must serve two reader groups equally:

- Operators need to understand deployment order, credential boundaries, failure handling, and what the service does after a restart or outage.
- Contributors need to understand process ownership, state boundaries, external interfaces, and where lifecycle decisions occur.

The README remains an executive and operational entry point. Detailed algorithms, edge cases, and test cases stay in the existing lifecycle design and Raspberry Pi runbook.

## Goals

- Replace the ASCII architecture block with a component and data-flow diagram.
- Add a recovery state machine that explains same-event recovery and classified cooldowns.
- Add a provisioning and deployment flow from YouTube setup through reboot verification.
- Keep adjacent prose concise and consistent with the deployed implementation.
- Render directly on GitHub without generated image assets or additional repository dependencies.

## Non-Goals

- Add a separate normal-production lifecycle diagram.
- Change runtime behavior, configuration defaults, systemd units, API calls, or retry policy.
- Rewrite the OAuth provisioning guide, operational commands, caveats, or repository layout.
- Add screenshots, static SVG files, generated images, or diagram-specific build tooling.
- Add a new changelog entry or alter the README's latest-only changelog convention.

## Diagram Set

### 1. System Architecture

Placement: replace the existing text diagram at the start of `## Architecture`.

Format: Mermaid `flowchart LR` with three visually distinct logical regions:

1. Sources and compatibility inputs: RTSP camera, optional OBS scene, and optional OBS service profile.
2. Encoder host: systemd, supervisor, FFprobe, FFmpeg, API helper, durable state and locks, and OAuth files.
3. YouTube: reusable ingest stream, YouTube Data API v3, and one marked broadcast/watch page.

The arrows must distinguish the important contracts through short labels:

- Source discovery and probe data enter the supervisor.
- The supervisor starts and monitors FFmpeg through progress output.
- FFmpeg sends media to the reusable ingest stream.
- The supervisor invokes bounded JSON helper commands.
- The API helper reads and writes durable local state and performs OAuth-authorized API operations.
- YouTube stream health and broadcast lifecycle state return through the API helper.

The diagram will show ownership boundaries, not every function call. Existing component and persistent-state tables remain authoritative for paths and file details.

### 2. Recovery State Machine

Placement: under `### Recovery Behavior`, before the existing failure table.

Format: Mermaid `stateDiagram-v2` focused on recovery decisions:

- Startup first checks a persisted retry deadline.
- An unavailable source enters source backoff without creating a broadcast.
- Healthy local media must produce active YouTube ingest before reconciliation.
- Reconciliation reuses exactly one marked nonterminal broadcast or creates one unlisted event only when none exists.
- API, quota, and ambiguous failures persist their classified cooldowns.
- Camera, FFmpeg, network, service, or host interruption preserves the same nonterminal event and watch URL.
- A replacement generation is allowed only after the prior managed event is terminal or confirmed missing.
- A verified public stream stops nonessential API polling while FFmpeg remains supervised.

The state machine may name the testing/live/public gates only where necessary to show recovery destinations. It will not become the omitted normal-production lifecycle diagram.

### 3. Provisioning And Deployment Flow

Placement: at the start of `## Installation`, before command-level installation instructions.

Format: Mermaid `flowchart TD` covering:

1. Enable YouTube Live for the target channel.
2. Enable YouTube Data API v3 in a Google Cloud project.
3. Configure an External or otherwise compatible OAuth audience.
4. Create a TV or Limited Input OAuth client.
5. Install FFmpeg, Python, and project scripts.
6. Create private configuration and OAuth files.
7. Authorize the channel account and persist the refresh token.
8. Configure the camera and reusable ingest stream.
9. Run the visible unlisted validation flow.
10. Enable the systemd service.
11. Reboot and verify the encoder plus remote-management path.
12. Enter unattended operation.

Decision nodes will identify the two operator-significant branches:

- Existing reusable stream versus provisioning one with `--create-stream`.
- Validation failure versus successful service enablement.

The existing numbered OAuth section remains the source of command and console detail.

## Mermaid Conventions

- Use only syntax supported by GitHub's Mermaid renderer.
- Use stable alphanumeric node IDs and quoted human-readable labels.
- Avoid custom themes, colors, HTML labels, icons, and external assets.
- Keep labels short enough to scan on desktop and mobile GitHub layouts.
- Use subgraphs only for real ownership boundaries.
- Never include camera credentials, stream keys, OAuth values, host-specific IDs, or deployment secrets.

## Prose Integration

- Replace, rather than retain, the ASCII architecture diagram.
- Add one short orienting paragraph before each new diagram.
- Keep the component table, persistent-state section, numbered production loop, recovery table, OAuth guide, caveats, and latest changelog entry.
- Remove or tighten only sentences that restate diagram edges verbatim.
- Link to the detailed lifecycle design for readers who need algorithm and test-level depth.

## Validation

- Run the repository's Markdown lint checks.
- Parse or render all Mermaid blocks with an available Mermaid-compatible CLI.
- Confirm each diagram uses balanced fences and unique node IDs.
- Review the rendered diagrams for readable labels and correct arrow direction.
- Run `git diff --check` and verify that only the README and this design/plan documentation change.
- Confirm the README contains no credentials, deployment-specific identifiers, or additional changelog entries.

## Acceptance Criteria

- The README contains exactly the three approved Mermaid diagrams.
- The dedicated normal-production lifecycle diagram is omitted.
- Operators can trace provisioning, failure backoff, restart, and public recovery behavior.
- Contributors can identify supervisor, helper, FFmpeg, state, OAuth, systemd, and YouTube ownership boundaries.
- All diagram transitions and labels match the current implementation.
- Mermaid and Markdown validation pass.
- Runtime files and behavior remain unchanged.
