# FailureList: per-row Retry state and names — Design

## Decisions

- **Busy state comes from the mutation cache, not a hand-kept set.** `FailuresPage` gives its
  retry mutation a dedicated `mutationKey` and reads every pending retry's variables with
  React Query's `useMutationState` (filter: that key, status `pending`), turning them into the
  set of in-flight task keys. Tradeoff: leans on the mutation cache instead of local state, but
  there is no `onMutate`/`onSettled` bookkeeping to drift, and it tracks concurrent calls,
  which `isPending`/`variables` of a single `useMutation` cannot (they follow only the latest
  call).
- **Success/error handling stays in the `useMutation` options.** Callbacks passed to
  `mutate()` fire only for the latest call; the options' callbacks fire for every concurrent
  retry, so each row's error slot keeps being set. Tradeoff: none, it is where they are today.
- **The dedicated `mutationKey` is required.** The `QueryClient` is app-wide; filtering by
  status alone would count other pages' pending mutations as retries. Tradeoff: one more
  constant to keep unique.
- **`FailureList` takes the in-flight keys as a set, keyed like `errors`.** The `busy?: boolean`
  prop is replaced by a per-task prop: a `ReadonlySet<string>` of `${repository}#${task_issue}`
  keys, defaulting to empty. Tradeoff: a breaking prop change, but `FailuresPage` is its only
  caller.
- **Named rows follow ADR 0007; button names stay unchanged.** Each existing Retry row
  gets `role="group"` and `aria-label="<repository>#<task_issue>"`; its button's accessible
  name and visible text stay `Retry`, without a button `aria-label`. Tests scope the button
  query with `within(getByRole('group', { name: 'widget#38' })).getByRole('button', { name: 'Retry' })`.
  Tradeoff: task context is announced on entering the group rather than on each button.
  The operator confirmed on 2026-11-10 that ADR 0007 supersedes this task's earlier
  button-renaming decision, with no exception.
- **One ticket.** Both defects live in the same button of the same component and the same test
  file, and the rename of the existing test queries depends on the new name; split, each half
  would be a few lines and the two would restructure the same render.

## Data model

None. The change is frontend-only state; no table, column, or API shape changes.

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | — |

## Identities

None widened. The busy state adopts the existing task key `${repository}#${task_issue}` (the
`errors` key) instead of having none.

- `data-testid="retry-error-<task_issue>"` — out of scope: keyed by issue number only, so it
  can collide across repositories, but spec requirement 4 keeps it as is.

## Seams

- `FailuresPage` rendered by `renderWithProviders` against MSW handlers, in
  `frontend/src/pages/__tests__/FailuresPage.test.tsx` — existing. All scenarios drive through
  it, including the two reproduction tests already committed there.
