# Task artifacts quality audit

Scope: PR #116, initial implementation `2bc527b`, compared with
`origin/main` at `028d848`. The crap-gate engine and deep-quality-review
instructions were read from `jesdi/general-skills` origin/main at `b6b1126`.
Neither review had been run before the initial PR.

## Initial CRAP findings

Seven of 28 measured changed production functions failed the default gate
(existing functions: 15; new functions: 9). Generated API types, test fixtures,
and tests are outside production complexity measurement. Python coverage uses
branch coverage; TypeScript coverage uses Vitest's Istanbul JSON report.

| Function | Complexity | CRAP | Limit |
| --- | ---: | ---: | ---: |
| `dispatcher/task_artifacts.py::_registrations` | 27 | 33.2 | 9 |
| `frontend/src/pages/TaskPage.tsx::TaskView` | 28 | 28.5 | 15 |
| `dispatcher/task_artifacts.py::collect` | 27 | 27.1 | 9 |
| `frontend/src/components/RequestPanel.tsx::RequestPanel` | 24 | 24.1 | 15 |
| `frontend/src/components/ArtifactsPanel.tsx::ArtifactsPanel` | 15 | 17.9 | 9 |
| `web/artifacts.py::router.open_artifact` | 11 | 13.1 | 9 |
| `dispatcher/task_artifacts.py::cleanup` | 11 | 11.1 | 9 |

High coverage alone cannot fix the worst offenders: CRAP is always at least
cyclomatic complexity. Collection and the React page/panels need simpler
responsibilities, not more tests that restate their implementation.

## Initial structural review: REQUEST CHANGES

- **Duplicated concept:** terminal timestamps were interpreted independently
  by state serialization and artifact retention. Normalize lifecycle state in
  the state owner and consume the normalized value in retention.
- **Duplicated concept + Wrong layer:** artifact collection implemented its
  own task archive, and web routes repeated active-or-archived lookup. Keep
  task persistence in the state owner and expose detail lookup through Sources.
- **Wrong layer:** artifact routes bypassed the existing Sources boundary for
  filesystem reads and reconstructed destination state separately for listing
  and opening. Resolve artifact delivery behind that boundary once.
- **Weak contract:** the large collector mixed registration, storage,
  publication, expiry and task serialization through untyped dictionaries and
  independently empty publication fields. Use validated artifact records and
  an optional publication reference with explicit revision behavior.
- **Needless sequencing:** each Markdown artifact repeated a remote branch
  lookup. Resolve one branch revision per collection and compare all files
  against that consistent snapshot.

## Reproducing the gate

Install Python development dependencies and frontend dependencies, then run
from the repository root with the desired crap-gate skill checkout:

```sh
python -m pip install -e '.[dev]'
pnpm --dir frontend install --frozen-lockfile
sh /path/to/general-skills/skills/crap-gate/run.sh --json
```

The repository `.crap-gate.json` defines coverage commands and the base ref.
Use a Node version supported by the frontend toolchain. The skill launcher
provisions its own engine runtime; coverage commands use the project runtime
on PATH. There are no threshold relaxations or function suppressions.

## Final result

**APPROVE** after independent review under both skills. All 65 measured
changed production functions pass the unchanged thresholds; zero violations.
The final gate used fresh successful coverage from 1,226 backend tests and
176 frontend tests. All seven browser tests, the production build/typecheck,
API-type freshness check and diff whitespace check passed.

| Function | CRAP before | CRAP after | CC before → after |
| --- | ---: | ---: | --- |
| `_registrations` | 33.2 | 6.0 | 27 → 6 |
| `collect` | 27.1 | 4.0 | 27 → 4 |
| `cleanup` | 11.1 | 4.2 | 11 → 4 |
| `router.open_artifact` | 13.1 | 4.0 | 11 → 4 |
| `TaskView` | 28.5 | 14.1 | 28 → 14 |
| `RequestPanel` | 24.1 | 14.0 | 24 → 14 |
| `ArtifactsPanel` | 17.9 | 7.0 | 15 → 7 |

The refactor puts archive serialization and terminal timestamp normalization
in state, exposes task lookup and artifact status/delivery through Sources,
and represents stored artifacts/publication references with explicit records.
One publisher resolves a remote branch once per collection. React now separates
action lifecycle, destructive confirmation, request media, and artifact rows.
The independent reviewer confirmed the tests exercise observable behavior,
existing effects and disk schema are preserved, and the scope stayed within
the findings. No thresholds were raised or functions suppressed.

The first full backend run exposed a malformed-state lookup regression. It
was fixed by preserving Sources' existing corruption-tolerant task scan; the
original rejection test was retained, and the subsequent full run passed.

The board latency investigation is documented separately in
[the performance report](2026-09-13-board-performance.md). It changed no board
runtime behavior or deployment in this refactor.

