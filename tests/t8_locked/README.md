# T8 locked host acceptance

Source baseline: `d7c8d244b9b6c12e95335fb5e95b4daa0ebc1299`.
Verified merged runtime T7: `cf69f595bf9ecff454251efac8110e0c521d44e5`.
The baseline includes the final public T8 contract publication. Root activated
host execution; native execution is not authorized and all 18 N criteria remain UNRUN.

The unchanged 42 H groups have 98 independently selectable cases. Actual baseline:
87 behavioral RED, 11 existing-policy H42 GREEN, zero setup ERROR. Three focused
fixture checks PASS, including real controller attachment/replacement without
input, exact independent ACK/history confirmation and owned listener restart.
All 101 case cleanup audits PASS. See [baseline record](baseline-record.json),
[case map](host-case-map.md) and [native criteria](native-evidence-checklist.md).

Run from the repository root with its existing Python environment:

```sh
python tests/t8_locked/run_host.py --report /tmp/t8-host-results.json
python tests/t8_locked/run_host.py --report /tmp/t8-fixture-results.json fixture_sanity.FixtureSanity
```

Append a fully qualified `test_host_acceptance.HostAcceptance.test_H...` name to
select one case. The runner reports real error/failure types and frame locations
without printing product source bodies. Missing presentation capability is an
explicit behavior assertion after healthy owned setup, rather than an import error.

Fixtures own real listener processes, Unix sockets, current saved TaskState,
RuntimeClient events/views, eventlog, queue and recording notifier. Faults act at
owned transport/process/filesystem boundaries. Child HOME/XDG/provider/temp/herdr
paths and an allowlisted environment are isolated. Cleanup runs even on setup
failure, waits for recorded children/listener/proxy threads and removes owned paths.
Generic local artifact publishing uses only owned local Git origins.

The controller RPC peer and recording Sessions/GitHub objects are host stand-ins.
They never count as native launch, CLI/TUI, hook, provider or container evidence.
`dispatcher_process.py` executes the real public `run_pass`; profiling observes
only entry to the declared presentation callable. No product functions or runtime
view, claim, receipt or history responses are replaced. No product implementation
bodies, existing tests or private helper artifacts were read by this writer.

Root ruled that the 11 already-green H42 checks remain targeted regressions with
**no mutation credit**. The user waived exhaustive per-ticket review/mutation work;
no production mutation, independent full gate or native run is claimed here.

Public H42 fixture corrections approved before lock: pending GitHub `run_status`
returns `''`; completed runs return their conclusion. The saved default cap note
is exactly `(background work still running after 180m — cap reached)`; notifier
note adds existing `\n\n` plus nonempty capture-tail. Pre-expiry spec-review grace
retains the live session and operator artifact. Recording external `Sessions.end`
exercises the real public conditional forced-retirement fence before its record.
Root will append these existing-interface declarations to public07 before author work.

Portable public provenance:

- [Presentation contract](../../specs/codex-background-lifecycle/runtime-presentation-contract.md)
  and [public entrypoint](../../specs/codex-background-lifecycle/t8-public/presentation-entrypoint-public-declarations.md).
- [Existing host declarations](../../specs/codex-background-lifecycle/t8-public/host-fixture-public-declarations.md)
  and [production composition](../../specs/codex-background-lifecycle/t8-public/existing-interfaces/07-production-composition-public-declarations.md).
- [Pinned public native schema provenance](../../specs/codex-background-lifecycle/t8-public/existing-interfaces/native-schemas/manifest.json).
  The bounded host RPC peer uses pinned public InitializeResponse, ThreadResumeParams,
  ThreadReadResponse and list response shapes from that retained declaration set.
- Original public preparation retains the unchanged 42H/18N plan off-repository.
  The 449-file public supplement manifest digest was
  `abad48110162893dbf868958030cb983bfcdd45854a0cdc6ed1ca7633c671219`.
  No unused vendor bundle was copied into the tests.

`SHA256SUMS` records exact locked bytes, excluding itself. `baseline-record.json`
records original RED reasons and GREEN classifications, source binding, result-log
hashes and cleanup evidence. No actual GitHub, Telegram, authenticated provider,
model API, SSH or native operation was executed by this bundle.
