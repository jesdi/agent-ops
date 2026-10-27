# GitHub App capabilities for zero-touch project credentials

Research for #208 (map #205). Facts from docs.github.com and the GitHub changelog as of 2026-10-27.
Items marked **Uncertain** are not settled by the docs and need a spike against the real API.

## 1. Creating the App (manifest flow)

- The manifest flow registers a preconfigured App. A page POSTs a JSON `manifest` (name, `url`,
  `hook_attributes`, `redirect_url`, `callback_urls`, `setup_url`, `public`, `default_permissions`,
  `default_events`, `request_oauth_on_install`, ...) to `https://github.com/settings/apps/new`
  (personal account) or `https://github.com/organizations/ORG/settings/apps/new` (org). [1]
- The person confirms the name in the browser and clicks **Create GitHub App**. GitHub redirects
  to `redirect_url?code=...&state=...`. The server calls `POST /app-manifests/{code}/conversions`,
  which returns the App `id`, private key (`pem`), `webhook_secret` and client secret. All three
  steps must finish within one hour. [1]
- Creation always needs **one browser click by a human**. No API registers an App without that step.
  It is a one-time cost per agent-ops box, not per project.
- Visibility: a private App ("Only on this account") can be installed only on the account that
  owns it. Installing it on other accounts (for example a user-owned App on an org) needs a public
  App ("Any account"). [2]

## 2. Installing, and adding repos to an existing installation

- **Installing is browser-only.** The docs describe only the web flow
  (`https://github.com/apps/APP/installations/new`). No REST endpoint creates an installation. [3]
- Who can install: anyone on their own personal account. On an org, org owners can install.
  Repo admins can install only if the App requests no org permissions and not "Repository
  administration", and only for repos they administer. Owners can turn this off. Other members can
  only *request* an install, which notifies the owners. [3]
- At install time the installer picks **All repositories** or **Only select repositories**. Repos
  the App creates itself are granted automatically. [2]
- **Adding repos by API:** `PUT /user/installations/{installation_id}/repositories/{repository_id}`
  (and the matching `DELETE`). The authenticated user must have admin access to the repo, and
  removal needs `repository_selection: selected`. A classic PAT needs the `repo` scope. [4]
- **Change on 2026-10-13:** for **org-level** installations these endpoints now accept GitHub Apps
  and fine-grained PATs. They need the new repository permission "GitHub App installation repository
  access" (`installation_repositories: write`) **on the repo being added**. A user caller must
  also be an org owner or a repo admin. [5][6]
- Because the permission sits on the target repo, an installation token cannot add a repo that the
  installation doesn't already reach. Our App cannot widen its own installation. A **user access
  token** (the App acting for a repo admin) can.
- **Uncertain:** whether the new App/fine-grained support also covers installations on **personal
  accounts**. The changelog names only org installations. For a personal account, the safe path is
  either an "All repositories" install, or the owner adding the repo in the browser or through a
  user access token.

## 3. Installation tokens: scoping, lifetime, refresh

- Flow: sign a JWT with the App private key (RS256, `exp` at most 10 minutes ahead, `iat` backdated
  about 60 s), then call `POST /app/installations/{installation_id}/access_tokens` with
  `Authorization: Bearer <JWT>`. [7][8]
- Body `repositories` (names) or `repository_ids` narrows the token to at most 500 repos, all of
  which the installation must already reach. Body `permissions` narrows permissions, and cannot
  exceed what the App was granted. If either is left out, the token gets everything the
  installation has. [7][9]
- **Lifetime is 1 hour.** There is no refresh token for installation tokens. You mint a new one
  (from a fresh JWT) before expiry, or after a 401. Octokit SDKs do this automatically. [7][9]
- Token format: since 2026-04-27 newly minted installation tokens are rolling out in a stateless
  `ghs_APPID_JWT` format and are no longer 40 characters. Don't validate tokens by length or regex. [7]
- `DELETE /installation/token` revokes a token early. [10]
- User access tokens (the App acting for a user) expire by default and come with a refresh token.
  Since 2026-10, `offline_access` is also supported for Apps. [5]
- Tokens minted for one installation reach only that account's repos. One box serving several
  accounts holds one App and mints one token per installation.

## 4. Projects v2 boards: user-owned vs org-owned (the crux)

**Org-owned boards: supported.** The org permission "Projects" (read/write) covers the REST
`/orgs/{org}/projectsV2/...` endpoints (list, get, fields, add field, items, add/update/delete
item, drafts, views), for both installation and user access tokens. [9][11] GraphQL also accepts an
installation token with that permission. Some mutations need extra repo permissions, for example
`Contents` to link a project to a repo. [12] Webhooks `projects_v2` and `projects_v2_item` exist
for orgs only (public preview). They need org Projects read. [13]

**User-owned boards: not supported for GitHub Apps.**
- The only Projects permission an App can request is the **organization** permission. The "User
  permissions" list has no Projects entry. [9]
- The REST endpoints for user-owned projects (`/users/{username}/projectsV2...`) exist [14] but
  appear in **neither** "Endpoints available for GitHub App installation access tokens" [10] **nor**
  "... for GitHub App user access tokens" [11]. Only the `/orgs/...` variants are listed.
- `projects_v2` and `projects_v2_item` webhooks list availability `organization` only. [13]
- Fine-grained PATs have the same documented gap: "Using fine-grained personal access token to
  access Projects owned by a user account." GitHub calls it temporary, with no date. [15]
- No changelog entry through 2026-10 announces App or fine-grained support for user-owned projects.
  The 2026-10-13 App improvements don't mention Projects. [5]
- **Working credential for user-owned boards: a classic PAT with the `project` scope (or
  `read:project` to read only).** The docs list classic PATs and installation tokens as the two
  token types for the Projects GraphQL API. [12] Only the classic token reaches user-owned boards.
- **Uncertain:** whether a GitHub App **user access token** can still read or write a user-owned
  board through **GraphQL**. GraphQL authorizes as the user, but the token is capped by the App's
  permissions, and no user-level Projects permission exists. Community reports say it fails. [16]
  A 30-minute spike would settle it. Plan on "no".

## 5. Pull requests: create, merge, auto-merge, branch protection, required checks

- Create a PR: `POST /repos/{o}/{r}/pulls` with repo permission **Pull requests: write**. Pushing
  the branch needs **Contents: write**. Changing files under `.github/workflows/` also needs
  **Workflows: write**. [9]
- Merge: `PUT /repos/{o}/{r}/pulls/{n}/merge` (and `merge-async`) with **Contents: write**. Both
  token types work. [9]
- Auto-merge merges once required reviews and checks pass. Anyone with write access can enable it,
  and the repo setting "Allow auto-merge" must be on. The option appears only when the PR cannot
  merge right away. Auto-merge is cancelled if someone without write access pushes to the head
  branch. [17] Through the API this is the GraphQL `enablePullRequestAutoMerge` mutation.
  **Uncertain:** the docs don't say which App permission it needs. Expect Contents and Pull requests
  write.
- Workflows: PRs and pushes made with an App installation token **do** start workflow runs. Events
  from `GITHUB_TOKEN` do not. [18]
- Required checks can be pinned to a specific App as the expected source, so a status from any
  other actor won't satisfy the check. Strict mode needs the branch up to date with the base. [19]
- **Bypass:**
  - Rulesets: GitHub Apps can be added to the bypass list ("Always allow" or "For pull requests
    only", which means merging past the rules through a PR). [20]
  - Classic branch protection: "Actors may only be added to bypass lists when the repository
    belongs to an organization." **On a personal repo an App cannot be a bypass actor under classic
    branch protection.** Use a ruleset there. [19]
  - **Uncertain:** whether ruleset bypass by an App behaves the same on personal repos. The
    ruleset docs list Apps as eligible bypass actors without an org-only caveat. Rulesets on private
    personal repos need a paid plan.
- An App that opens a PR cannot approve it, because an author can't approve their own PR. If
  required reviews apply, the PR merges only through a human review or a bypass entry.

## 6. Rate limits

- Installation token, REST: 5,000 requests/hour per installation. Above 20 repos it adds 50 per
  repo, and above 20 org users it adds 50 per user, capped at 12,500. On GitHub Enterprise Cloud the
  limit is 15,000. [21]
- Installation token, GraphQL: 5,000 points/hour, scaling the same way to 12,500, or 10,000 on GHEC. [22]
- User access tokens share the user's own limit (5,000/hour) with that user's PATs and other apps. [21]
- Secondary limits for every token: 100 concurrent requests, 900 points/min per REST endpoint, 2,000
  points/min for GraphQL, about 80 content-creating requests/min and 500/hour, and 2,000 OAuth token
  requests/hour. [21]
- Each installation has its own budget, so a busy project can't drain another account's.

## Implications for onboarding

1. **One manual moment per box, one per account.** Registering the App takes one browser
   click (manifest flow) per agent-ops deployment. Installing it on each GitHub account (user or
   org) takes another click. Nothing per project, if the install is "All repositories".
2. **Prefer "All repositories" installs.** New repos then need no credential step at all. With
   "selected", adding a repo by API needs a user access token from a repo admin (or, for org
   installs since 2026-10, a token holding `installation_repositories: write` on that repo). The
   App's own installation token can't do it.
3. **Keep only the App private key as the stored secret.** The dispatcher mints 1-hour installation
   tokens on demand, scoped per project with `repositories` and `permissions`. Worker agents get a
   token narrowed to that project's repos. Re-mint before expiry, and never persist tokens.
4. **Personal boards need a fallback credential (R7/R8: yes).** A GitHub App cannot reach a
   Projects v2 board owned by a user account. Fine-grained PATs can't either. The fallback is a
   classic PAT with the `project` scope, stored per user account. It is board-only, so repo work
   stays on App tokens. The alternative is requiring org-owned boards, where the App's org Projects
   permission covers everything, including webhooks for automatic issue-adding.
5. **Watch for board changes by polling on user accounts.** `projects_v2_item` webhooks are
   org-only.
6. **Config PR merge credential: the App.** Contents + Pull requests write is enough to open and
   merge the targets.yaml PR, and App-created PRs start CI. Required reviews still block unless a
   human approves or the App is a ruleset bypass actor. That is the blocker R8 must surface and
   resume. On personal repos use a ruleset, not classic branch protection, if the App must bypass.
7. **Spikes before committing:** (a) a user access token against a user-owned board through
   GraphQL; (b) the add-repo endpoint on a personal-account installation; (c) ruleset bypass by an
   App on a personal repo.

## Sources

1. https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
2. https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app
3. https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party
4. https://docs.github.com/en/rest/apps/installations
5. https://github.blog/changelog/2026-10-13-github-app-improvements-for-least-privilege-and-management/
6. https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps (Repository permissions for "GitHub App installation repository access")
7. https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
8. https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
9. https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
10. https://docs.github.com/en/rest/authentication/endpoints-available-for-github-app-installation-access-tokens
11. https://docs.github.com/en/rest/authentication/endpoints-available-for-github-app-user-access-tokens
12. https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects
13. https://docs.github.com/en/webhooks/webhook-events-and-payloads (projects_v2, projects_v2_item)
14. https://docs.github.com/en/rest/projects/projects
15. https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
16. https://github.com/orgs/community/discussions/192922 (community, not official)
17. https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request
18. https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
19. https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
20. https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository
21. https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
22. https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
