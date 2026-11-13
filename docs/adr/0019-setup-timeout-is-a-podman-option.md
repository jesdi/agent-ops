# 0019. The setup timeout is a podman run option

Date: 2026-11-13

## Context

Arguments after the image name belong to the container command rather than podman.

## Decision

Place --timeout and its seconds value among the podman run options, before the image, when building setup argv.

## Consequences

Command construction tests must keep checking option placement, not only presence.
