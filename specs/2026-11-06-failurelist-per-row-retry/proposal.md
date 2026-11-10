# FailureList: per-row Retry state and names

## Why

On the Failures page (`frontend/src/pages/FailuresPage.tsx`) every quarantined task has a
Retry button, rendered by `FailureList` (`frontend/src/components/FailureList.tsx`). Two
defects, both reproduced by the failing tests committed ahead of this spec in
`frontend/src/pages/__tests__/FailuresPage.test.tsx`:

1. **One in-flight retry disables every Retry button.** Root cause: `FailuresPage` uses a
   single `useMutation` and passes `busy={retry.isPending}` to `FailureList`, which takes
   `busy?: boolean` and renders `disabled={busy}` on every row's button. `isPending` says
   "some retry is in flight", not which one, so the busy state carries no task identity. The
   per-task error channel next to it (`errors`, keyed `${repository}#${task_issue}`) already
   has the right key; the busy state never got one.
2. **Every Retry button has the same accessible name "Retry".** Root cause: the button's
   only label is its text content `Retry`, with no `aria-label`. That was a deliberate
   shortcut in the web console plan (3/3) to keep the plan's
   `getByRole('button', { name: 'Retry' })` queries working. A screen-reader user tabbing
   through the list hears "Retry, button" N times and can't tell which task each one retries.

## For whom

The operator using the web console: sighted users who retry several quarantined tasks in a
row, and screen-reader users who need to know which task a Retry button acts on.

## Goal

Each Retry button is disabled only while a retry for its own task is in flight, and belongs to
a named group identifying its task (`<repository>#<task_issue>`), following ADR 0007.

## Non-goals

- GhostCard's shared action names and shared busy state (#84) and the collapsed-column
  markers (#85). An issue comment suggests fixing them together. They are separate issues
  with their own components, and this slice stays inside FailureList and FailuresPage.
- Renaming the Retry buttons. The operator confirmed ADR 0007: name the existing row
  group and keep the accessible button name `Retry`.
- Any change to the visible button text (it stays `Retry`), the retry API, or the inline
  error channel.
- Rate-limiting or deduplicating retries beyond disabling the in-flight row's button.
