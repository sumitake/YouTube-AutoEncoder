# README Linked Diagrams Design

## Summary

Reduce README visual density by moving its three Mermaid diagrams into one dedicated documentation page. The README remains the executive and operational entry point, with short deep links to the detailed system architecture, recovery state machine, and provisioning flow.

## Decision

Create `docs/architecture-and-flows.md` with exactly these existing diagrams:

1. System architecture.
2. Recovery state machine.
3. Provisioning and deployment flow.

The Mermaid definitions move unchanged so the reviewed component names, transitions, and retry semantics do not drift. The README replaces each block with one contextual link to the matching heading on the new page.

## Goals

- Make the README faster to scan without removing operational guidance.
- Keep all visual architecture and process material on one discoverable page.
- Preserve direct links to each diagram through stable Markdown headings.
- Retain the diagrams as GitHub-native Mermaid without generated assets or build tooling.
- Keep the current runtime, deployment, OAuth, and recovery documentation authoritative and unchanged.

## Non-Goals

- Split the diagrams across multiple files.
- Hide the diagrams in collapsible README sections.
- Redesign, relabel, or otherwise alter the reviewed Mermaid definitions.
- Move the component table, persistent-state details, production loop, failure table, installation commands, OAuth guide, operations, caveats, or changelog out of the README.
- Modify runtime code, configuration, tests, systemd units, release metadata, or historical design and implementation documents.

## Dedicated Diagram Page

`docs/architecture-and-flows.md` will use this structure:

- `# Architecture and Operational Flows`
- A short orientation that identifies the README as the source for setup commands, configuration, failure details, and operations.
- `## System Architecture`, followed by the current `flowchart TB` block.
- `## Recovery State Machine`, followed by the current `stateDiagram-v2` block.
- `## Provisioning and Deployment`, followed by the current `flowchart TD` block.
- A link from each section back to its corresponding README section.
- A link to the idempotent lifecycle design for algorithm and test-level detail.

The page will not duplicate the README tables or command sequences. Its single responsibility is to present the three visual flows with enough context to interpret them.

## README Integration

The README will contain no Mermaid blocks after this change.

- Under `## Architecture`, replace the diagram with a link labeled `system architecture diagram` targeting `docs/architecture-and-flows.md#system-architecture`.
- Under `### Recovery Behavior`, replace the state diagram with a link labeled `recovery state machine` targeting `docs/architecture-and-flows.md#recovery-state-machine`.
- Under `## Installation`, replace the deployment flow with a link labeled `provisioning and deployment flow` targeting `docs/architecture-and-flows.md#provisioning-and-deployment`.
- Add `docs/architecture-and-flows.md` to the repository layout listing.

Each link will sit in the existing orienting prose so readers understand what the destination contains before leaving the README. All adjacent operational content remains in its current order.

## Validation

- Confirm `README.md` contains zero Mermaid fences.
- Confirm `docs/architecture-and-flows.md` contains exactly three Mermaid blocks: one `flowchart TB`, one `stateDiagram-v2`, and one `flowchart TD`.
- Compare the moved Mermaid bodies byte-for-byte with the current README blocks before removing them.
- Render all three moved diagrams and visually inspect them for unchanged labels, arrows, and layout.
- Verify all three README deep links resolve to headings on the new page.
- Run Markdown lint over tracked Markdown files, `git diff --check`, Ruff, Python compilation, executable checks, and the full test suite.
- Verify the diff contains only README and documentation files and introduces no credential-like values.

## Acceptance Criteria

- The README is materially shorter and contains three concise diagram links instead of embedded diagrams.
- One dedicated page contains all three unchanged, rendered Mermaid diagrams.
- Component tables, recovery details, setup commands, OAuth provisioning, operations, caveats, and the latest-only changelog remain in the README.
- Diagram links and back-links work on GitHub.
- Runtime behavior and release metadata are unchanged.
- Local validation and repository CI pass.
