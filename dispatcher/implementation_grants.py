"""Bind implement-spec's role choices to this launch's gated runtime grants."""
from __future__ import annotations

import json

from dispatcher.models import Entry
from dispatcher.workspace import write_worktree_file

MODEL_FILE = 'runtime-models.json'
ROLES = ('test-writer', 'implementer', 'reviewer',
         'fixer', 'final-reviewer', 'conflict', 'pr-writer')
REVIEW_ROLES = ('reviewer', 'final-reviewer')


def role_models(primary: Entry, secondary: Entry | None) -> dict[str, list[str]]:
    """The driver requires explicit efforts and two implementer rounds.

    An unspecified CLI effort becomes medium, supported by both runtimes.
    Repeating the grant for fix round two does not invent a stronger model
    or effort. The role contract targets the driver with isolated same-provider
    final-review lenses (older drivers must be upgraded, not bypassed).
    """
    own = str(primary) if primary.effort else f'{primary}@medium'
    review = secondary or primary
    other = str(review) if review.effort else f'{review}@medium'
    models = {role: [own] for role in ROLES}
    models.update({role: [other] for role in REVIEW_ROLES})
    models['implementer'] = [own, own]
    return models


def prepare(worktree: str, stage: str, primary: Entry, secondary: Entry | None,
            prompt: str) -> str:
    """Refresh only run inputs, leaving the driver's checkpoint and ledger alone."""
    if stage != 'implement':
        return prompt
    models = role_models(primary, secondary)
    write_worktree_file(worktree, '.agent', MODEL_FILE, json.dumps(models, indent=2) + '\n')
    grant = (f'Secondary runtime granted: {secondary}.' if secondary else
             'No secondary runtime is granted for this launch. Its absence is intentional '
             'policy (not configured, ineligible, or withheld by the usage gate), not an '
             'installation failure. Use supported same-provider review in separate read-only '
             'contexts and disclose that coverage with the actual entries in the ledger and PR.')
    return f'''{prompt}

## Current implementation runtime grants
Primary runtime granted: {primary}. {grant}
Only the entries in `.agent/{MODEL_FILE}` are granted for driver roles in this
launch. Run implement-spec with `--models .agent/{MODEL_FILE}`, including on
resume; this replaces any stale role configuration from an earlier launch.
Keep the existing driver state, spec, tickets, gate settings and retry question.
Do not reset progress or skip ordinary role preflight, isolated review contexts,
review findings, gates, or ledger/PR coverage disclosure. If the installed driver
cannot support this configuration, report that specific skill incompatibility.
Do not install, mount, authenticate or invoke an ungranted provider, alter usage
limits, or edit global/user model preferences. This file is run-scoped and must
not be committed. Both implementer fix rounds use the admitted primary entry.
'''
