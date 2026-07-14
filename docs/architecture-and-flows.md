# Architecture and Operational Flows

This page collects the project's visual system and operational flows. Use the [README](../README.md) for installation commands, configuration, failure details, OAuth provisioning, and operations.

For reconciliation algorithms, lifecycle invariants, and test strategy, see the [idempotent lifecycle recovery design](superpowers/specs/2026-07-10-idempotent-youtube-lifecycle-design.md).

## System Architecture

The encoder separates local media supervision from serialized YouTube lifecycle control.

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
        TelemetryTimer["Optional telemetry timer"]
        TelemetryCollector["youtube-autoencoder-telemetry"]
        TelemetryFiles["Private local telemetry files"]
    end

    subgraph ArchitectureYouTube["YouTube"]
        direction LR
        Ingest["Reusable liveStream ingest"]
        Api["YouTube Data API v3"]
        Broadcast["One owned liveBroadcast and watch page"]
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
    TelemetryTimer -.->|"disabled-by-default one-shot"| TelemetryCollector
    State -.->|"live identity and cooldown"| TelemetryCollector
    TelemetryCollector -.->|"video-metrics"| Helper
    Helper -.->|"one read-only videos.list call"| Api
    TelemetryCollector -.->|"throttle and validated samples"| TelemetryFiles
```

Dashed edges are the optional telemetry path. It reads durable lifecycle state, calls the existing OAuth-aware helper only for an eligible live broadcast, and stores private local snapshots. It has no edge back to the supervisor, FFmpeg, broadcast lifecycle, privacy, or recovery state.

Return to [Architecture](../README.md#architecture).

## Recovery State Machine

Recovery preserves ownership, retries through durable cooldowns, and replaces an event only after it is terminal or confirmed missing.

```mermaid
stateDiagram-v2
    state "Startup or restart" as RecoveryStartup
    state "Managed stream generation" as RecoveryGeneration {
        state "Probe camera source" as RecoveryProbe
        state "Validate exact cached ID and conflicts" as RecoveryReconcile
        state "Persist intent, insert without description, and bind" as RecoveryCreate
        state "Resume one nonterminal event" as RecoveryManaged
        state "Start FFmpeg and require active ingest" as RecoveryMedia
        state "Testing, live, and publication gates" as RecoveryGates
        state "Verified public stream" as RecoveryStable
        state "Public stream with API cooldown" as RecoveryPublicFallback
        state "Public stream waiting for OAuth replacement" as RecoveryPublicOAuthBlocked

        [*] --> RecoveryProbe
        RecoveryProbe --> RecoveryReconcile : source available
        RecoveryReconcile --> RecoveryManaged : one owned nonterminal event
        RecoveryReconcile --> RecoveryCreate : none, terminal, or missing
        RecoveryCreate --> RecoveryManaged : insert and bind verified
        RecoveryManaged --> RecoveryMedia : ownership and binding durable
        RecoveryMedia --> RecoveryGates : media fresh and ingest active
        RecoveryGates --> RecoveryStable : two healthy live observations and privacy readback
        RecoveryStable --> RecoveryPublicFallback : API unavailable, media healthy
        RecoveryPublicFallback --> RecoveryStable : API recovers
        RecoveryStable --> RecoveryPublicOAuthBlocked : OAuth rejected, media healthy
        RecoveryPublicOAuthBlocked --> RecoveryStable : token changes and exact IDs revalidate
    }
    state "Persist classified cooldown" as RecoveryBackoff
    state "Wait for OAuth token replacement" as RecoveryOAuthBlocked

    [*] --> RecoveryStartup
    RecoveryStartup --> RecoveryBackoff : retry deadline active
    RecoveryBackoff --> RecoveryStartup : deadline expires or host restarts
    RecoveryStartup --> RecoveryGeneration : no active deadline
    RecoveryGeneration --> RecoveryBackoff : recoverable failure, preserve ownership
    RecoveryGeneration --> RecoveryOAuthBlocked : OAuth rejected before verified public ingest
    RecoveryOAuthBlocked --> RecoveryStartup : token content changes
    RecoveryCreate --> RecoveryBackoff : insert outcome uncertain, never reinsert
```

The helper never sets or updates YouTube descriptions. Schema-v3 state stores the exact broadcast ID and a write-ahead create fingerprint. A lost insert response enters `verify_create`; recovery may adopt exactly one normalized remote match, but zero or multiple matches stay blocked under durable ambiguous backoff. Cached access tokens rejected with HTTP 401 receive one refresh under a shared local `fcntl` lock and one exact buffered request replay. If that replay is also rejected, the supervisor waits on the post-refresh token fingerprint so its own token write cannot trigger a busy loop. Other OAuth rejection does not enter remote backoff: the service makes no further API calls until the private token file changes. Legacy description markers are read only for one-way schema-v2 migration.

Return to [Recovery Behavior](../README.md#recovery-behavior).

## Provisioning and Deployment

Provisioning validates YouTube, OAuth, ingest, and media before enabling unattended service operation.

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

Return to [Installation](../README.md#installation).
