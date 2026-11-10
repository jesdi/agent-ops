# 0010. Retry mutations have a dedicated key

Date: 2026-11-10

## Context

The QueryClient is shared across the app. Pending mutations on other pages are not FailureList retries and may have different variable shapes.

## Decision

Give FailuresPage retries a dedicated mutationKey, shared by the retry mutation and the useMutationState filter. Select both that key and pending status; do not derive retry busy state from status alone.

## Consequences

The retry owner must keep its key unique. Cache-variable interpretation remains local to that owner, and unrelated pending mutations cannot disable Retry buttons.
