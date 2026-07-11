# README Architecture Diagrams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three GitHub-native Mermaid diagrams to the README for a balanced audience of deployment operators and code contributors.

**Architecture:** Replace the current ASCII architecture sketch with a component flowchart, add a recovery-focused state machine before the recovery table, and add a provisioning/deployment flow before installation commands. Keep the detailed prose, OAuth guide, caveats, and latest-only changelog authoritative; diagrams summarize ownership and decisions without changing runtime behavior.

**Tech Stack:** GitHub Flavored Markdown, Mermaid, markdownlint-cli2, Mermaid CLI, Git, GitHub Actions.

## Global Constraints

- The README must contain exactly three Mermaid diagrams: system architecture, recovery state machine, and provisioning/deployment flow.
- Do not add a dedicated normal-production lifecycle diagram.
- Use GitHub-supported Mermaid syntax with stable alphanumeric IDs and quoted labels.
- Do not use custom themes, colors, HTML labels, icons, generated images, or external diagram assets.
- Do not include camera credentials, stream keys, OAuth values, deployment-specific IDs, or other secrets.
- Preserve the component table, persistent-state section, numbered production loop, recovery table, OAuth guide, caveats, and latest-only changelog entry.
- Do not modify runtime code, configuration, systemd units, release metadata, or runtime behavior.

---

### Task 1: Replace The ASCII Architecture Sketch

**Files:**

- Modify: `README.md:29-53`

**Interfaces:**

- Consumes: Component names and ownership boundaries documented in `README.md` and `docs/superpowers/specs/2026-07-10-idempotent-youtube-lifecycle-design.md`.
- Produces: One `flowchart TB` Mermaid block under `## Architecture`; later validation relies on its `ArchitectureInputs`, `ArchitectureHost`, and `ArchitectureYouTube` subgraph IDs.

- [ ] **Step 1: Verify the three-diagram contract currently fails**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 3
```

Expected: nonzero exit because the current README contains no Mermaid blocks.

- [ ] **Step 2: Replace the ASCII block with the architecture diagram**

Keep the existing introductory and ownership paragraphs. Replace only the fenced `text` diagram with:

```mermaid
flowchart TB
    subgraph ArchitectureInputs["Source and compatibility inputs"]
        direction LR
        Camera["RTSP camera"]
        ObsScene["Optional OBS scene"]
        ObsService["Optional OBS service profile"]
    end

    subgraph ArchitectureHost["Encoder host"]
        direction TB
        Systemd["systemd service"]
        Supervisor["youtube-autoencoder supervisor"]
        Probe["FFprobe source validation"]
        Encoder["FFmpeg media pipeline"]
        Helper["youtube-autoencoder-api"]
        State["Durable state and locks"]
        OAuth["OAuth client and token"]
    end

    subgraph ArchitectureYouTube["YouTube"]
        direction LR
        Ingest["Reusable liveStream ingest"]
        Api["YouTube Data API v3"]
        Broadcast["One marked liveBroadcast and watch page"]
    end

    Systemd -->|"start and restart"| Supervisor
    ObsScene -->|"source discovery"| Supervisor
    ObsService -->|"ingest compatibility"| Supervisor
    Supervisor -->|"probe"| Probe
    Camera -->|"RTSP media"| Probe
    Probe -->|"source health"| Supervisor
    Supervisor -->|"spawn and supervise"| Encoder
    Camera -->|"video"| Encoder
    Encoder -->|"progress"| Supervisor
    Encoder -->|"RTMPS media"| Ingest
    Supervisor -->|"bounded JSON commands"| Helper
    Helper <-->|"read and write"| State
    OAuth -->|"authorization"| Helper
    Helper <-->|"lifecycle and health"| Api
    Ingest -->|"stream health"| Api
    Api <-->|"create, bind, transition, verify"| Broadcast
```

After the ownership paragraph, add this implementation-depth pointer:

```markdown
For the detailed reconciliation algorithm, lifecycle states, and test strategy, see the [idempotent lifecycle recovery design](docs/superpowers/specs/2026-07-10-idempotent-youtube-lifecycle-design.md).
```

- [ ] **Step 3: Validate the architecture block**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 1
rg -n 'ArchitectureInputs|ArchitectureHost|ArchitectureYouTube|bounded JSON commands' README.md
git diff --check
```

Expected: one Mermaid block, all four identifiers found, and zero diff-check errors.

- [ ] **Step 4: Commit the architecture diagram**

```bash
git add README.md
git commit -S -m "docs: diagram encoder architecture"
```

### Task 2: Add The Recovery State Machine

**Files:**

- Modify: `README.md` under `### Recovery Behavior`, before the failure table.

**Interfaces:**

- Consumes: Retry classes, reconciliation rules, and public fallback behavior already documented in the recovery prose and table.
- Produces: One `stateDiagram-v2` Mermaid block using recovery state aliases prefixed with `Recovery`.

- [ ] **Step 1: Confirm the recovery diagram is absent**

Run:

```bash
test "$(rg -c '^stateDiagram-v2$' README.md)" -eq 0
```

Expected: exit 0 before the diagram is added.

- [ ] **Step 2: Insert the recovery orienting paragraph and state machine**

Immediately after `### Recovery Behavior`, add:

````markdown
Every recovery path first preserves ownership and retry state. Media must be fresh and YouTube ingest active before reconciliation can create or transition anything.

```mermaid
stateDiagram-v2
    state "Startup or restart" as RecoveryStartup
    state "Managed stream generation" as RecoveryGeneration {
        state "Probe source and start FFmpeg" as RecoveryMedia
        state "Require fresh active ingest" as RecoveryIngest
        state "Reconcile exact ownership markers" as RecoveryReconcile
        state "Create and bind unlisted generation" as RecoveryCreate
        state "Resume one nonterminal event" as RecoveryManaged
        state "Testing, live, and publication gates" as RecoveryGates
        state "Verified public stream" as RecoveryStable
        state "Public stream with API cooldown" as RecoveryPublicFallback

        [*] --> RecoveryMedia
        RecoveryMedia --> RecoveryIngest : media progress fresh
        RecoveryIngest --> RecoveryReconcile : YouTube ingest active
        RecoveryReconcile --> RecoveryManaged : one marked nonterminal event
        RecoveryReconcile --> RecoveryCreate : none, terminal, or missing
        RecoveryCreate --> RecoveryManaged : insert and bind verified
        RecoveryManaged --> RecoveryGates
        RecoveryGates --> RecoveryStable : two healthy live observations and privacy readback
        RecoveryStable --> RecoveryPublicFallback : API unavailable, media healthy
        RecoveryPublicFallback --> RecoveryStable : API recovers
    }
    state "Persist classified cooldown" as RecoveryBackoff

    [*] --> RecoveryStartup
    RecoveryStartup --> RecoveryBackoff : retry deadline active
    RecoveryBackoff --> RecoveryStartup : deadline expires or host restarts
    RecoveryStartup --> RecoveryGeneration : no active deadline
    RecoveryGeneration --> RecoveryBackoff : recoverable failure, preserve ownership
```
````

- [ ] **Step 3: Validate recovery coverage without adding a production lifecycle diagram**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 2
test "$(rg -c '^stateDiagram-v2$' README.md)" -eq 1
rg -n 'RecoveryBackoff|RecoveryPublicFallback|preserve ownership|none, terminal, or missing' README.md
! rg -n '^### (Production Lifecycle Diagram|Normal Lifecycle Diagram)$' README.md
git diff --check
```

Expected: two total Mermaid blocks, one state diagram, all recovery identifiers found, no forbidden lifecycle-diagram heading, and zero diff-check errors.

- [ ] **Step 4: Commit the recovery state machine**

```bash
git add README.md
git commit -S -m "docs: diagram lifecycle recovery"
```

### Task 3: Add The Provisioning And Deployment Flow

**Files:**

- Modify: `README.md` at the start of `## Installation`, before `Install runtime packages:`.

**Interfaces:**

- Consumes: Installation commands, OAuth provisioning requirements, visible-test behavior, and Raspberry Pi remote-management caveat already in the README.
- Produces: One `flowchart TD` Mermaid block with `DeploymentStreamDecision` and `DeploymentValidationDecision` operator branches.

- [ ] **Step 1: Confirm only two approved diagrams exist**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 2
```

Expected: exit 0.

- [ ] **Step 2: Insert the deployment orienting paragraph and flowchart**

Immediately after `## Installation`, add:

````markdown
Provision the YouTube control plane and the encoder host in this order. The detailed console steps and commands remain in the sections that follow.

```mermaid
flowchart TD
    DeploymentChannel["Enable YouTube Live on the target channel"]
    DeploymentProject["Enable YouTube Data API v3"]
    DeploymentAudience["Configure a compatible OAuth audience"]
    DeploymentClient["Create a TV or Limited Input OAuth client"]
    DeploymentRuntime["Install FFmpeg, Python, and project scripts"]
    DeploymentConfig["Create private encoder, OAuth, and writable service files"]
    DeploymentAuthorize["Authorize the channel account"]
    DeploymentCamera["Configure camera source and ingest profile"]
    DeploymentStreamDecision{"Reusable stream already configured?"}
    DeploymentProvision["Run visible test with --create-stream"]
    DeploymentValidate["Run unlisted visible validation"]
    DeploymentValidationDecision{"Validation succeeds?"}
    DeploymentDiagnose["Fix OAuth, source, ingest, or quota issue"]
    DeploymentEnable["Enable the systemd service"]
    DeploymentReboot["Reboot the encoder host"]
    DeploymentVerify["Verify encoder and remote-management recovery"]
    DeploymentOperate["Unattended operation"]

    DeploymentChannel --> DeploymentProject
    DeploymentProject --> DeploymentAudience
    DeploymentAudience --> DeploymentClient
    DeploymentClient --> DeploymentRuntime
    DeploymentRuntime --> DeploymentConfig
    DeploymentConfig --> DeploymentAuthorize
    DeploymentAuthorize --> DeploymentCamera
    DeploymentCamera --> DeploymentStreamDecision
    DeploymentStreamDecision -->|"No"| DeploymentProvision
    DeploymentProvision --> DeploymentValidate
    DeploymentStreamDecision -->|"Yes"| DeploymentValidate
    DeploymentValidate --> DeploymentValidationDecision
    DeploymentValidationDecision -->|"No"| DeploymentDiagnose
    DeploymentDiagnose --> DeploymentStreamDecision
    DeploymentValidationDecision -->|"Yes"| DeploymentEnable
    DeploymentEnable --> DeploymentReboot
    DeploymentReboot --> DeploymentVerify
    DeploymentVerify --> DeploymentOperate
```
````

- [ ] **Step 3: Validate the approved diagram count and deployment branches**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 3
test "$(rg -c '^flowchart TB$' README.md)" -eq 1
test "$(rg -c '^flowchart TD$' README.md)" -eq 1
test "$(rg -c '^stateDiagram-v2$' README.md)" -eq 1
rg -n 'DeploymentStreamDecision|DeploymentValidationDecision|DeploymentDiagnose --> DeploymentStreamDecision|remote-management recovery' README.md
git diff --check
```

Expected: exactly three approved Mermaid blocks, exactly one of each diagram type, both decision IDs found, and zero diff-check errors.

- [ ] **Step 4: Commit the deployment flow**

```bash
git add README.md
git commit -S -m "docs: diagram encoder deployment"
```

### Task 4: Render, Validate, And Publish

**Files:**

- Verify: `README.md`
- Verify: `docs/superpowers/specs/2026-07-10-readme-architecture-diagrams-design.md`
- Verify: `docs/superpowers/plans/2026-07-10-readme-architecture-diagrams.md`

**Interfaces:**

- Consumes: The three Mermaid blocks produced by Tasks 1-3.
- Produces: Rendered temporary SVG evidence, a lint-clean docs change, and a merged pull request.

- [ ] **Step 1: Assert content and scope contracts**

Run:

```bash
test "$(rg -c '^```mermaid$' README.md)" -eq 3
test "$(rg -c '^flowchart TB$' README.md)" -eq 1
test "$(rg -c '^flowchart TD$' README.md)" -eq 1
test "$(rg -c '^stateDiagram-v2$' README.md)" -eq 1
! git diff origin/main...HEAD -- README.md | rg -i 'rtsp://[^<]|stream[_ -]?key\s*=|refresh[_ -]?token\s*='
git diff --check origin/main...HEAD
```

Expected: all assertions pass, the secret-pattern scan returns no matches, and diff check is clean.

- [ ] **Step 2: Lint all Markdown**

Run:

```bash
NPM_CONFIG_CACHE=/tmp/yta-readme-npm-cache \
  npx --yes markdownlint-cli2@0.18.1 '**/*.md'
```

Expected: `Summary: 0 error(s)`.

- [ ] **Step 3: Extract and render every Mermaid block**

Run:

```bash
rm -rf /tmp/yta-readme-mermaid
mkdir -p /tmp/yta-readme-mermaid
awk '
  /^```mermaid$/ { inside=1; count++; next }
  /^```$/ && inside { inside=0; next }
  inside { print > ("/tmp/yta-readme-mermaid/diagram-" count ".mmd") }
' README.md
test "$(find /tmp/yta-readme-mermaid -name '*.mmd' -type f | wc -l | tr -d ' ')" -eq 3
for source in /tmp/yta-readme-mermaid/*.mmd; do
  output="${source%.mmd}.svg"
  NPM_CONFIG_CACHE=/tmp/yta-readme-npm-cache \
    npx --yes @mermaid-js/mermaid-cli -i "$source" -o "$output"
  test -s "$output"
done
```

Expected: three nonempty SVG files and no Mermaid parse errors. Remove `/tmp/yta-readme-mermaid` after visual inspection.

- [ ] **Step 4: Run repository validation**

Run:

```bash
python3 -m venv /tmp/yta-readme-venv
/tmp/yta-readme-venv/bin/pip install -r requirements-dev.txt
/tmp/yta-readme-venv/bin/ruff check .
/tmp/yta-readme-venv/bin/python -m pytest -q
git status -sb
```

Expected: Ruff passes, 89 tests pass, and only committed branch changes differ from `origin/main`.

- [ ] **Step 5: Push, open the pull request, and wait for governance checks**

```bash
git push -u origin codex/readme-architecture-diagrams
gh pr create \
  --base main \
  --head codex/readme-architecture-diagrams \
  --title "docs: add README architecture and recovery diagrams" \
  --body-file /tmp/yta-readme-pr-body.md
gh pr checks --watch --interval 10
```

The PR body must summarize the three diagrams, state that the production-lifecycle diagram was intentionally omitted, list Markdown/Mermaid/Python verification, and declare that runtime behavior is unchanged.

- [ ] **Step 6: Resolve review findings and perform the authorized merge**

Run the unresolved-thread and delayed-comment sweep. If all required checks are green and no actionable review remains:

```bash
gh pr merge --squash --admin --delete-branch
```

Verify the PR is merged, fetch `origin/main`, and confirm the merge commit contains exactly the approved documentation changes.
