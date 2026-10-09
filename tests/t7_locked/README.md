# T7 isolated acceptance lock

Public contract base: `1afde4f9cbbe2f616e49c25d2e118b977b694e70`.
Only independently authored writer tests/helpers and the writer's own T6/provider
preparation are reused. Production bodies and other-owner fixtures were not writer
inputs. All 76 inherited protected paths remain byte exact.

[criterion-map.json](criterion-map.json) names all 155 collected cases: 146 product
criteria and nine infrastructure conformance cases. It covers the receipt contract's
16 groups plus the attachment-entry/ownership and atomic-bootstrap declarations.
Product baseline requires proper behavioral AssertionError RED; infrastructure GREEN
means fixture conformance only. No source implementation or full-product gate claim
is made. The callable/signature absence checks occur inside healthy public behavioral
setup. Cases continue through actual declared public behavior once those capabilities
exist; baseline absence does not claim the later assertions already executed.

Run from the repository with its installed Python/WebSocket dependency:

```sh
T7_LOCK_ARTIFACTS=/absolute/unique/evidence \
PYTHONPYCACHEPREFIX=/absolute/unique/pycache \
.venv/bin/python -m pytest --confcutdir=tests/t7_locked tests/t7_locked \
  --junitxml=/absolute/unique/result.xml
```

Each invocation needs a unique evidence/cache path. The default creates a disposable
private evidence root and deletes it only after matching ownership cleanup proofs.
Failure to prove group/directory absence preserves the ledger. Unix sockets/process
fixtures need ordinary local execution permission. They isolate HOME, Codex home,
state and worktree, create only generic content, and never invoke a real native CLI,
model/API or external service. Real listener, supervisor CLI, Gateway, RuntimeControl,
HTTP clients, Sessions and run_pass remain production public flows. Physical Tab,
Podman removal, GitHub, notifications, usage and inbound polling are disclosed external
dependency doubles. Layered dispatcher cases avoid an automatic competing sender.

The own native provider serializes native mutation, multiplexes correlated requests,
keeps held/lost ACK waits outside mutation locks, and implements no client-ID dedup.
Connection/client-specific faults distinguish preaccept nonacceptance from accepted
uncertainty. Opaque scope-limited cursors, missed notifications, completed history,
duplicate native items and explicit malformed wire faults are exercised independently.
No real active-history persistence guarantee is inferred from fixture visibility.

[schema manifest](schemas/manifest.json) vendors the exact 27 required official native
0.156.1 documents. The local strict evaluator supports every assertion keyword used
in those documents and audits all local references/7,636 schema nodes; draft-07 format
annotations are not asserted. It is not a general JSON Schema implementation. Retained
16 + 13 conformance checks preserve all 69 + 56 original assertion ASTs. Six further
provider cases and one HTTP adapter case exercise new capabilities. Explicit malformed
responses are separately marked expected negatives after validating the normal native
response; they never count as schema-conforming packets or known rejection evidence.

The production CLI and disclosed controller host capture actual argv, exact native
packets, public runtime phases and PID/group/socket/runtime-parent metadata before
strict owned teardown. All groups must be absent before owned temporary deletion.
Root readiness is correlated to durable first input; receipt-local settlement grants
no main Stop. Result fidelity decodes the declared standalone JSON CompletionRecord
list and compares typed identities/outcomes without fixing JSON formatting, item/list
order, wording or ID encoding. Ordinary text input keeps exact type/order/whitespace;
only omitted versus empty text_elements normalize for narrow receipt comparison.

Authoritative frozen hashes, baseline XML/log classification, capture hashes and
cleanup proof are recorded off-repository under the owned T7 intake and referenced by
the final immutable lock report. T8 real Podman/operator presentation and real-provider
recovery probes are outside this acceptance lock.

The declaration-only state observation supplement adds public read_session/has_waiting
and exact read-only OWN session/target/legacy-marker existence/content comparisons.
Receipt-local proof and idle admission preserve those artifacts; fresh initial missed
Stop cases prove actual absence, including malformed-file detection. Original freeze/
baseline files remain retained; the strengthened final lock has a separate freeze/run.
