# Claim forecast reads the dispatcher's quarantine verdict — Design

## Decisions

- **The dispatcher persists the verdict it already computes; the console never re-derives it** — no `gh` on `/api/board` (PR #79's failure mode), at the cost of a one-pass lag.
- **One file, replaced whole each pass that reaches `_claim_new`** — clearing needs no delete logic; a pass that skips `_claim_new` (`claims_paused`) leaves an old file behind, which the stamp check below neutralises.
- **The file carries `pass_started`, and the reader trusts it only when it equals `pass.json`'s `started_at`** — a stale or ahead-of-heartbeat file degrades to "no quarantine skipping" (the pre-change forecast), never to a wrong skip; the price is a window each pass, from `_claim_new` to the heartbeat write, where the forecast does not skip quarantined heads.
- **Written in a `finally:` of `_claim_new`** — every exit (`free <= 0`, `pick_project` None, slot exhaustion, loop end, an exception from a claim) writes what was collected; on an exception `pass.json` is not written, so the stamps differ and the reader ignores the partial file.
- **The writer lives in `failures` next to `quarantine_path` and swallows `OSError` with a `[warn]`** — same contract as `_write_heartbeat`: a full disk never breaks the pass; the console then sees a stale stamp and falls back.
- **One path helper for the file, in `failures` beside `quarantine_path`, used by both the writer and `Sources`** — `web/sources.py` already imports from `dispatcher`; one spelling of the filename means the two sides cannot drift apart.
- **The reader is a `Sources` method taking the heartbeat dict** — the route reads `pass.json` once and passes the same dict to `build_board(heartbeat=)` and the reader, so both judge the same pass.
- **`build_board` / `next_claim` / `_queue_heads` keep their `quarantined` parameter from `2237482`** — only the route's feed changes; the pure read model stays I/O-free.
- **Entries are validated one by one (`[str, int]`, bool rejected)** — one bad entry drops only itself, the rest still skip.
- **No schema version and no free-capacity field now** — the later same-pass-capacity ticket adds a key; the reader ignores unknown keys.

## Data model

No database. One new state file, `<state_dir>/quarantine-verdict.json`, written with `state.write_json_atomic` (temp file + rename, so a reader sees a whole old or whole new file).

| Table | Field | Constraint | Why |
|---|---|---|---|
| `quarantine-verdict.json` | (document) | JSON object; replaced whole, atomically | A reader never sees a half-written list; stale entries vanish with the next pass |
| `quarantine-verdict.json` | `pass_started` | string, byte-equal to the same pass's `pass.json` `started_at` | The reader's staleness check; mismatch means ignore the file |
| `quarantine-verdict.json` | `skipped` | list of `[repository_name: str, issue: int]`, sorted, no duplicates | Keyed on the pair as quarantine records are, so alpha#73 never hides beta#73 |

The reader enforces the shape (missing/torn/non-object/non-list → empty set; bad entries dropped); the writer is the only producer.

## Identities

None. No key is widened: the verdict uses the `(repository, issue)` pair that quarantine records (`quarantine/<repository>-<issue>.json`) and `next_claim`'s `quarantined` already use.

## Seams

- `dispatcher.main.run_pass(cfg, deps)` — existing. Dispatcher scenarios: the file after a pass, its `pass_started` against `pass.json`, claims-paused, unwritable file.
- `dispatcher.main._claim_new(cfg, deps, repositories, admit, order, dry_run, pass_started)` — existing (already driven directly by `tests/test_main.py`). Exit-path scenarios: `free <= 0`, slot exhaustion, `pick_project` None.
- `web.sources.Sources.quarantine_verdict(heartbeat) -> frozenset[tuple[str, int]]` — new. Reader scenarios.
- `web.read_model.next_claim(..., quarantined=...)` — existing. Pure forecast scenarios and the docstring.
- `GET /api/board` — existing. Route feed scenario (with `tests/webfakes.FakeSources`, which gains `quarantine_verdict`) and the offline degrade tests.
