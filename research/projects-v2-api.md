# Projects v2 API for project onboarding

Research for #209 (map #205, requirements R4–R6). Checked on 2026-10-27 against
GitHub's published GraphQL schema (`https://docs.github.com/public/fpt/schema.docs.graphql`),
live introspection with `gh api graphql`, and read-only queries against the
`jesdi` user boards. Nothing was created or changed.

Fields agent-ops depends on today (`dispatcher/board.py`, `dispatcher/config.py`,
`.backlog/project-meta.json`): `Status` (single select; options Inbox, Ready,
In progress, Done, plus an optional Won't do), `Area` (single select), and the
number fields `Impact`, `Effort`, `Score` and `Boost`. agent-ops stores field
and option **IDs** in config, so any operation that changes an ID breaks a
deployed project.

## 1. Create a project (user or org owner)

- `createProjectV2(input: {ownerId, title, repositoryId?, teamId?})`. `ownerId`
  can be an `Organization` or a `User` node ID. The same mutation covers both
  owners. `repositoryId` also links a repository during creation.
  `teamId` (org only) gives that team read access.
- `copyProjectV2(input: {projectId, ownerId, title, includeDraftIssues})` copies
  an existing project. Its fields, options and views come along, and so do its
  workflow settings, according to GitHub's docs on copying projects
  (not verified here). The copy gets fresh field and option IDs.
- REST has no endpoint that creates a Projects v2 board. Creation is GraphQL only.
- Look up an existing project by `user(login){ projectV2(number) }` /
  `organization(login){ projectV2(number) }`, or list `projectsV2(query:)` to
  reuse one by title.
- Owner node ID: `user(login){id}` / `organization(login){id}`.

## 2. Link repositories

- `linkProjectV2ToRepository(input: {projectId, repositoryId})`. The reverse is
  `unlinkProjectV2FromRepository`. Read existing links with
  `ProjectV2.repositories`. Confirmed live: board #3 links `jesdi/agent-ops`.
- Constraint: a project can be linked only to a repository owned by the **same**
  user or org ("You can only list projects that are owned by the same user or
  organization that owns the repository").
- Linking does **not** add issues to the project. It only makes the project
  visible from the repository.
- For a GitHub App, linking needs the repository `Contents` permission as well
  as Projects write (the docs state this for `createProjectV2` with `repositoryId`).

## 3. Read field definitions

- `ProjectV2.fields(first:100)` returns `ProjectV2Field`, `ProjectV2SingleSelectField`
  (`options { id name color description }`), `ProjectV2IterationField` and
  `ProjectV2MultiSelectField`. They share `ProjectV2FieldCommon { id name dataType }`.
- `ProjectV2.field(name: "Status")` looks one field up by name. Verified live.
- Option IDs are short hex strings such as `89e7db7c`. Field IDs look like
  `PVTSSF_…` and `PVTF_…`.
- A new board comes with a default `Status` field whose options are Todo,
  In Progress and Done. agent-ops needs Inbox, Ready, In progress and Done, so
  that field has to be edited, not added.

## 4. Add fields and options without destroying data

- **Add a field**: `createProjectV2Field(input: {projectId, dataType, name, singleSelectOptions?})`.
  `dataType` is TEXT, NUMBER, DATE, SINGLE_SELECT, ITERATION or MULTI_SELECT.
  A SINGLE_SELECT field needs at least one option. Creating a field cannot touch
  existing data. Field names must be unique, so check by name first.
- **Edit options**: `updateProjectV2Field(input: {fieldId, name?, singleSelectOptions?})`.
  The schema says of `singleSelectOptions`: *"Empty input is ignored, provided
  values overwrite existing options, and existing options should be fetched for
  partial updates."* **There is no append operation. The list you send replaces
  the whole option list.**
- **Option IDs**: `ProjectV2SingleSelectFieldOptionInput` has an optional `id`:
  *"The ID of an existing single select option. Include this to preserve the
  option's identity during updates, preventing item field values from being
  cleared."* Verified in the live schema. To append options safely:
  1. Read `options { id name color description }`.
  2. Send back every existing option with its `id`, unchanged `name`, `color`
     and `description`. These are required inputs; send `""` when the
     description is empty.
  3. Add the new options without an `id`.
  Any option you leave out is deleted, and the items that used it lose their
  value. Any existing option you send without its `id` probably gets a new ID,
  which clears its items' values and breaks agent-ops' stored option IDs.
- **Uncertain:** the `id` input looks like a recent addition. Older
  write-ups say any update regenerates all option IDs. I did not test this,
  because the only boards available are real ones. Before relying on it,
  run one test against a throwaway project: update with the existing ids, then
  check that the ids are unchanged and an item's value is kept.
- `deleteProjectV2Field` exists. Onboarding must never call it.
- `updateProjectV2Field` on `singleSelectOptions` also overwrites `color` and
  `description`. Send back the values you read so they stay as they are.

## 5. Add issues as items idempotently

- `addProjectV2ItemById(input: {projectId, contentId})`, where `contentId` is
  an Issue or PullRequest node ID. Docs: *"If you try to add an item that
  already exists, the existing item ID is returned instead."* That makes it
  idempotent, so a re-run import cannot create duplicates. The returned item
  keeps its existing field values. I have not checked this live, because it
  is a mutation.
- You cannot add an item and set its fields in one call. Call
  `addProjectV2ItemById`, then `updateProjectV2ItemFieldValue`. Several
  updates can go in one request as aliased mutations, as `board.py` already does.
- To avoid overwriting values on items that were already there, check first:
  `issue { projectItems { nodes { id project { id } } } }` (verified live), or
  read the board's items once. Set Status and the other defaults only on items
  that are new.
- REST alternative: `POST /orgs/{org}/projectsV2/{n}/items` and
  `POST /users/{user}/projectsV2/{n}/items` with `{type: Issue, owner, repo, number}`.
  The docs do not say how REST handles an existing item. Use GraphQL.
- Import source: open issues of each member repository, from the `issues`
  connection or `gh issue list`. Rate limit: about 1 mutation per issue, so a
  few hundred issues is fine. Pace the requests to avoid the secondary rate
  limits on mutations.

## 6. Auto-add of future issues

- **The API cannot configure it.** The schema has `ProjectV2.workflows` /
  `workflow(number)` (read: `name`, `number`, `enabled`) and
  `deleteProjectV2Workflow`. It has no mutation to create, enable or edit a
  workflow or set its filter, and the filter cannot be read either. Verified
  live: board #3 has "Auto-add to project" (#7, enabled), while
  "@jesdi's untitled project" has no auto-add workflow at all. The workflow
  only exists after someone creates it in the UI.
- UI behaviour: the workflow adds an item *"when created or updated if the item
  matches your filter"*, and *"existing items matching your criteria will not
  be added"*. Each workflow targets one repository. Limits: Free 1, Pro and
  Team 5, Enterprise 20 workflows per project. A free account can therefore
  auto-add from only one repository.
- `copyProjectV2` from a template board that already has the auto-add workflow
  may carry the workflow over, but its repository filter would still point at
  the template's repository. Treat this as unverified.
- Fallbacks, best first:
  1. **App webhook `issues.opened` / `issues.transferred`.** This works for
     both user and org repos. It needs only the repository `Issues` read
     permission to subscribe. The handler calls `addProjectV2ItemById`, which
     is idempotent. It needs a token that can write the project; see section 7
     for user-owned projects.
  2. **GitHub Actions** in each member repo, using `actions/add-to-project`
     on `issues: [opened, transferred]`. `GITHUB_TOKEN` *"cannot access
     projects"*, so it needs a stored PAT or App credentials. Each repository
     needs a workflow file and a secret, which is more setup per repo.
  3. **Reconcile by polling.** agent-ops already polls GitHub: on each cycle,
     list the open issues that are not yet on the board and add them. It is
     idempotent and works for every owner type and token. It is also the safety
     net for missed webhooks.
- Use `projects_v2_item` webhooks only to listen for board changes. They are
  *organization only* and in public preview.

## 7. Token types

| Operation | Classic PAT | Fine-grained PAT | App installation token | App user token |
|---|---|---|---|---|
| Read project / fields / items | `read:project` | org: Projects read; user project: **no** | org: Projects read; user project: **no** | follows the user (see note) |
| create / link / fields / items (mutations) | `project` (+`repo` for private repo content) | org: Projects write; user project: **no** | org: Projects write (+ repo `Contents` to link); user project: **no** | see note |
| Read issues for import | `repo` | repo Issues read | repo Issues read | repo Issues read |

- Classic PAT: `read:project` for queries, `project` for queries and mutations
  (official). This is what agent-ops uses today: a `gho_` OAuth token with
  `project` and `repo`. It works for both user and org projects.
- Fine-grained PAT: "Projects" exists only as an **organization** permission.
  GitHub lists *"Using fine-grained personal access token to access Projects
  owned by a user account"* as a known gap.
- GitHub App installation token: the "Projects" permission exists only as an
  **organization** permission. There is no account-level Projects permission.
  So **an App installation token cannot read or write user-owned projects.**
  This is not stated outright in GitHub's docs. It follows from the permission
  list, and community reports back it up: "Resource not accessible by
  integration" on `user(login){projectsV2}` and on `createProjectV2` for a user
  owner. Note that the "Projects" *repository* permission covers classic
  projects only.
- App user access token (OAuth/device flow): it acts as the user, but it is
  capped by the App's permissions. One report says a device-flow user token
  could create a user project. **Unverified**. If the App direction has to
  support user-owned boards, test this first.
- A GitHub App can create projects (`createProjectV2`) for an org owner with
  organization Projects write. The docs list that permission for fields and
  items. They do not give a permissions row for project creation, so
  **verify** this before relying on it.

## Implications for onboarding

- **Owner type decides the credential.** For an org-owned project, the
  installation token of the planned GitHub App covers everything except auto-add
  setup. Permissions: org Projects read/write, repo Issues read, Contents (to
  link). For a user-owned project (all of `jesdi`'s boards today), the App
  installation token cannot work, so onboarding needs a user credential: a
  classic PAT with `project` or the existing `gh` OAuth token, or a verified App
  user token. Either decide that user-owned boards need this credential, or
  require projects to live in an org. This is a spec decision for R4–R6.
- **Reuse before create.** Look the board up by number or title. Create it with
  `createProjectV2(ownerId, title, repositoryId)` only when it is missing. Then
  link the remaining member repos. Every member repo must have the same owner
  as the board.
- **Field reconciliation is read, merge, write.** Add missing fields with
  `createProjectV2Field`. Add missing options with `updateProjectV2Field`,
  sending **the full list with existing `id`s** plus the new options. Never
  rename, recolour or remove what the operator already has. Edit the default
  `Status` field in place rather than creating a second one. After writing,
  read the IDs again and write them into config (`project-meta.json` /
  targets.yaml). Never derive IDs locally.
- **Import is naturally idempotent.** Use `addProjectV2ItemById` per open
  issue. Set the initial Status (Inbox) only on items that were not already on
  the board, checked through `projectItems` before adding, so a re-run never
  resets an operator's triage.
- **Auto-add cannot be automated through the API.** Make the reconcile pass the
  thing agent-ops relies on: an idempotent "open issues not on board → add"
  step in the existing poll loop, so every member repo and owner type behaves
  the same. Optionally use the App's `issues.opened` webhook for lower latency.
  The UI auto-add workflow stays an optional operator step: the onboarding
  checklist can link to the project's Workflows page, and it is capped at one
  repository on Free.
- **One throwaway-project test is still needed** before relying on option-ID
  preservation (section 4) and App tokens on user projects (section 7).

## Sources

- GraphQL schema, quoted for input descriptions: https://docs.github.com/public/fpt/schema.docs.graphql
- GraphQL Projects reference: https://docs.github.com/en/graphql/reference/projects
- Using the API to manage Projects (scopes, existing-item behaviour, no add and update in one call): https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
- Adding items automatically (auto-add filters, existing items, plan limits): https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically
- Automating Projects using Actions (`GITHUB_TOKEN` cannot access projects; App org permission; PAT recommended for user projects): https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions
- Adding your project to a repository (same-owner restriction): https://docs.github.com/en/issues/planning-and-tracking-with-projects/managing-your-project/adding-your-project-to-a-repository
- REST Projects items: https://docs.github.com/en/rest/projects/items
- Permissions for fine-grained PATs (Projects is an org permission): https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
- Permissions for GitHub Apps (no user-level Projects permission): https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
- Fine-grained PAT limitations (user-owned Projects gap): https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- Webhook events (`projects_v2*` org-only, preview; `issues`): https://docs.github.com/en/webhooks/webhook-events-and-payloads
- `actions/add-to-project`: https://github.com/actions/add-to-project
- Community (non-official): https://github.com/orgs/community/discussions/46681 ,
  https://dev.to/mfauveau/why-your-github-app-cant-see-your-personal-projects-4d39
