# T8 existing-interface supplement

This supplement declares existing signatures, record/artifact formats and external
prerequisites for the [presentation contract](../../runtime-presentation-contract.md).
The verified merged-T7 source and owned native run identity are recorded in the
[public declarations index](../README.md). T8 implementation and genuine native
actual-launch verification remain required; no integration success is claimed.

| Interface | Public declaration |
| --- | --- |
| Listener lifetime and readiness | [Listener](01-listener-public-declarations.md) |
| StageSignal | [StageSignal](02-stage-signal-public-declarations.md) |
| Sessions, configuration and handles | [Sessions and Config](03-sessions-config-public-declarations.md) |
| Mount builders and result artifacts | [Builders and artifacts](04-mount-builders-public-declarations.md) |
| Installed hooks and native inputs | [Installed hooks](05-installed-hook-public-declarations.md) |
| Native/provider baseline | [Native prerequisites](06-native-prerequisites-public-declarations.md) |
| Production composition and policies | [Composition](07-production-composition-public-declarations.md) |
| Local provider and owned herdr interfaces | [External prerequisites](public-external-prerequisites.md) |

The [schema subset manifest](native-schemas/manifest.json) retains 20 directly
referenced generated schema documents and their upstream provenance. Four native
tool projections are copied byte-for-byte. The upstream 436-file generated bundle
remains off-repository. Every signature preserves the existing annotation/default
shape; `...` denotes omitted implementation. Unannotated existing arguments and
returns remain unannotated. These declarations add no mutation route, callback,
helper or test-only seam.
