# 0016. The console is the only operator input

Date: 2026-11-11

## Context

The operator could act on a task from Telegram (`/attach`, replies, plain
text, login codes, `/boost`, `/next`) and from the console. Two inputs meant
two code paths for every operator action and two fields kept only for
Telegram (`park_msg_id`, `hold_for_attach`).

## Decision

- Operator actions reach the dispatcher only as console intents.
- Telegram is outbound only: notifications and the morning digest.
- A login code is typed in the console, on the card of a task held at a login
  prompt. It is never written to the event log.

## Consequences

- An operator action that the console lacks is added to the console, not to
  Telegram.
- A notification never asks for a reply in Telegram.
