# T8 public declarations

These declarations bind to the verified merged T7 source revision
`cf69f595bf9ecff454251efac8110e0c521d44e5`; Root replaces this publication field with the
verified value. Each isolated native run records its fresh identity in
`ownership.json`; no native run has started at publication. T8 implementation
and genuine native actual-launch verification remain required; publication
alone makes no integration success claim.

The spec-root [runtime-presentation-contract.md](../runtime-presentation-contract.md)
contains the required behavior and criteria. Companion declarations are
[presentation entrypoint](presentation-entrypoint-public-declarations.md),
[host claims](host-presentation-public-declarations.md),
[operator surfaces](operator-surfaces-public-declarations.md) and
[existing host fixture interfaces](host-fixture-public-declarations.md).
The T8 block in spec-root `tasks.md` carries the task and public Touches inventory.
[Existing-interface supplements](existing-interfaces/README.md) declare the listener,
StageSignal, Sessions, mounts, hooks, native prerequisites and production composition.

User Tailscale sign-in is complete and the available box access/baseline is verified.
That establishes access/baseline only. Final T8 actual integration against the verified
merged T7 source remains required.

The public dispatcher entrypoint is `dispatcher.main.run_pass`; existing `_run_pass`
is a Touches location only, never another public/test seam. The sole new direct
presentation seam is `present_runtime_alerts`; isolated whole-pass execution is not
promised. The existing template interface/location is `telegram.templates.render`/
`_TEMPLATES`, with `runtime_alert` added there and no new registry/callable.
