# 0021. Setup cleanup preserves the original failure

Date: 2026-11-13

## Context

A cleanup error or a second log write could hide the setup failure that the operator needs to diagnose.

## Decision

Cleanup has its own 60-second host timeout, captures its output without writing setup.log, and is best effort: a non-zero exit, SubprocessError or OSError must not replace the original setup TimeoutExpired. Preserve the partial stdout and stderr in .agent/setup.log.

## Consequences

A failed removal is silent here. If the name remains occupied, retry reports that failure in its setup log. The contract does not require a particular subprocess helper.
