# Prompt-free completed onboarding reruns

## Why

Issue https://github.com/jesdi/agent-ops/issues/252 reports that rerunning project add for a completed onboarding asks for setup, gate and verification commands before saying already onboarded. With closed stdin it fails instead. Operators need the completed-state rerun to report its result immediately.

## For whom

Operators running agent-ops project add as the agent account.

## Goal

A completed onboarding rerun succeeds without reading stdin and displays the stored status and steps with an already-onboarded notice. Existing incomplete-onboarding and user-validation behavior remains intact.

## Non-goals

Changing onboarding engine transitions, editing completed projects, changing configuration or record schemas, reconciling deployment drift, and changing permissions.
