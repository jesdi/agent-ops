# Model tracks: which model runs which stage

## Problem

Model choice is a static table on the wrong inputs. `models.resolve` matches
`(stage, board effort, GitHub labels)` against an ordered rule list, first
match wins. The example config needs six near-identical rules to express
three ideas, and none of them can see the one thing that decides how hard a
task is: its scope, which only exists after the spec questionnaire is
answered.

The usage gate is a veto, not a chooser. When the chosen model has no
headroom the task waits, even when another model with headroom would do. The
console already computes those alternatives and offers them to a human; the
dispatcher never takes one itself.

Effort is not a routing input at all. Opus at high effort is expensive; Opus
at medium is the right default except for security work. An OpenAI model at
`xhigh` is close to a stronger one at `medium`. None of that can be said in
the config today.

CONTEXT.md and the pace-gate spec both defer "the router that picks among
admitted models by task type." This is that item.

## Scope

In scope:

- operator-defined **tracks** in `targets.yaml`, each with a prose `when`
  and, per stage, an ordered list of `provider/model@effort` entries;
- the **triage sweep** picks the track for the spec stage, recorded as a
  `track:<name>` label;
- the **spec session** picks the track for plan, implement and review, and
  writes it in its stage signal beside the committed spec;
- resolution stays a pure table: `(track, stage)` → first admitted entry;
- the pick is **sticky for the whole stage**;
- review prefers a provider other than the one that ran implement;
- `--effort` reaches the Claude CLI;
- deletion of `models.default`, `models.rules`, `triage_model`, and the
  label/effort matching engine;
- hard config cutover, ordered by hand against the infra repo.

Out of scope, deferred by name:

- the OpenAI/Codex runtime and usage adapter (tracks may name `openai/…`
  entries; admission fails closed for a provider with no adapter, so they are
  never picked until that work lands);
- any LLM call at spawn time;
- automatic fall-through to a model outside the track's list;
- console controls that write the track behind the session's back.

## Language

**Track**: an operator-named kind of work (`trivial`, `standard`,
`security`), defined in config by a prose `when` and a model list per stage.
The closed set of track names is the only thing a session may choose from.

**Entry**: one element of a track's stage list: `provider/model@effort`,
e.g. `anthropic/claude-opus-4-8@medium`. Effort is one of
`low | medium | high | xhigh | max` and is optional; unset means the CLI's
default. Not called a profile: that word already means something in Claude
Code.

**Pick**: the entry chosen when a task enters a stage. Recorded on the task
and reused by every session of that stage.

**Untracked**: a candidate with no `track:` label at claim time. Runs the
spec stage on the track named by `models.untracked`.

## Design

### Two decision points, both LLM sessions that already run

Triage sees a one-line idea; the spec session has read the code, asked the
questions, and knows the blast radius. So:

1. **Triage** picks the track for the **spec stage only** and records it as
   the label `track:<name>`. Cheap signal, cheap consequence.
2. **The spec session** picks the track for **plan, implement and review**
   and writes `"track": "<name>"` in its `awaiting-review` and `done`
   signals. It is committed with the spec, survives restarts, and is what the
   operator sees on the approval screen.

The spec session's track is final. Labels and effort are no longer routing
inputs. Security is expressed by a track whose `when` says "never below
this," carried in the prompt, not by a second matching engine.

Nothing at spawn time calls a model.

### Config — `models:` block

```yaml
models:
  triage:    [anthropic/claude-sonnet-4-6@medium]
  untracked: standard                # spec-stage track when no track: label
  tracks:
    trivial:
      when: Rote rename, typo, formatting, dependency bump, doc edit with no product decision.
      spec:      [anthropic/claude-haiku-4-5@low]
      plan:      [anthropic/claude-sonnet-4-6@medium]
      implement: [anthropic/claude-sonnet-4-6@medium, openai/gpt-luna@xhigh]
      review:    [openai/gpt-luna@xhigh, anthropic/claude-sonnet-4-6@medium]
    standard:
      when: Bounded change with a clear scope after the questionnaire.
      spec:      [anthropic/claude-opus-4-8@medium]
      plan:      [anthropic/claude-opus-4-8@medium, openai/gpt-sol@medium]
      implement: [openai/gpt-luna@xhigh, anthropic/claude-opus-4-8@medium]
      review:    [anthropic/claude-opus-4-8@medium, openai/gpt-sol@medium]
    security:
      when: Touches auth, secrets, permissions, or input at a trust boundary. Never below this track.
      spec:      [anthropic/claude-fable-5-1@high]
      plan:      [anthropic/claude-opus-4-8@high]
      implement: [anthropic/claude-opus-4-8@high]
      review:    [openai/gpt-sol@high, anthropic/claude-fable-5-1@high]
```

Validation at config load, all fatal:

- every track names all four stages (`spec`, `plan`, `implement`,
  `review`); a missing stage is an error, not a fallback;
- every list is non-empty; every entry parses as `provider/model[@effort]`
  with effort in the CLI's set;
- `untracked` names a defined track; `triage` is a non-empty list;
- the old keys `default`, `rules`, `triage_model` are rejected with a
  message naming the new keys.

Warning, not error: an entry names a provider with no usage adapter. It will
never be picked; say so once at load.

A target's own `models:` block replaces the global one wholesale, as today.
`referenced_providers` becomes the union of every provider in every list.

### Resolution — `dispatcher/models.py`

```python
@dataclass(frozen=True)
class Entry:
    provider: str
    model: str            # bare
    effort: str | None
    @property
    def model_id(self) -> str: ...   # provider/model, what admits() takes

def resolve(policy, track, stage, admit, avoid_provider=None) -> Entry | None
```

Walk `policy.tracks[track][stage]`. If `avoid_provider` is set (review
stages), stably move entries from that provider to the back first. Return the
first entry `admit(entry.model_id)` admits; `None` if nothing is admitted.
Pure: `admit` is the same `Admit` callable the pass already builds.

`resolve_for_stage` keeps its runtime-to-policy stage mapping (`queued` and
`awaiting-spec-review` → spec, `address-review` → implement).

`model_ids()` returns every entry's model id across all tracks; `/run` and
`/resume` keep validating operator overrides against it.

### Sticky pick — `TaskState`

New field `stage_pick: str` holding the entry as written
(`provider/model@effort`), set when a stage is entered and cleared on stage
change. Every session of that stage (each implement ticket, each review fix
round, address-review, every resume) launches with it. If the pick is denied
mid-stage the task waits; the list is not walked again. Review's
`avoid_provider` is the provider of the implement pick recorded on the task,
so it is exactly one provider.

`Launch(stage, model)` becomes `Launch(stage, entry)`; the gate asks about
`entry.model_id`, the spawner passes `--model` and `--effort`.

### Track on the task — `TaskState` and signals

New field `track: str`. At claim time it is the `track:` label if present,
else `models.untracked`. When a spec signal with `status` in
`awaiting-review | done` arrives, its `track` is validated against the
config's track names and stored; missing or unknown means the spec is not
accepted and the session is told to re-emit, the same bounce used today for
an unchecked criterion. A spec never reaches review without a valid track.

Spec revision sessions after a rejection keep running on the triage-chosen
track; they are still the spec stage.

### Operator override

At the spec gate, through the existing approval reply: "approved, but run it
as security." The spec session re-emits its signal with the new track and it
is validated as above. The session stays the single writer of the field.
The one-shot `/run` model override stays as the per-launch emergency lever.

### Prompts

- `prompts/triage.md`: the track list with each `when`, rendered from
  config; emit exactly one `track:<name>` in `add_labels`. `triage_apply`
  accepts `track:*` labels from the inventory, at most one per issue,
  replacing any existing one.
- `prompts/spec.md`: the same list; "after the questionnaire is answered and
  the spec is written, pick the track for the remaining stages and write it
  in the `awaiting-review` and `done` signals. Security-tagged work is never
  below the security track. If the operator's approval names a track, use
  that one."
- Both prompts get the list as a rendered `$tracks` variable so the prompt
  never hard-codes track names.

### Launcher — `dispatcher/containers.py`

`session_cmd` and `triage_cmd` take an `Entry`; add `--effort <level>` when
set. Only the Anthropic runtime exists; a non-Anthropic entry never reaches
the launcher because admission fails closed for it.

### Console

- Task card and approval screen show the track and its `when`, and the
  current pick.
- Admission warnings list the track's entries in order with each verdict,
  replacing today's "every model in the policy" alternatives.
- `GateView` (the header verdict) keys on `tracks[untracked].spec[0]`, "what
  an idle box would spawn next." `_budget_edge` keys on the same entry.
- Backlog labels: `track:*` labels must exist in each target repo; the
  backlog skill's setup verb creates them from the config's track names.

### CONTEXT.md

Add **Track**, **Entry**, **Pick**, **Untracked** to the glossary. Move "the
router that picks among admitted models by task type" from deferred to
shipped; keep runtime adapters and cross-runtime continuation deferred.
Record that labels and effort are not routing inputs.

## Edge cases

- **No entry admitted**: the task parks `stalled-on-budget` as today, with
  the entry list and verdicts on the console. Never falls through to a model
  outside the list.
- **Pick denied mid-stage**: wait for that pick. Ticket 3 never runs on a
  different provider than ticket 1.
- **Every implement entry and every review entry share one provider**: the
  reorder is a no-op; review runs on the same provider. Preference, not a
  hard rule.
- **Spec session omits `track` or invents one**: bounced, not accepted, not
  defaulted.
- **Two `track:` labels on an issue**: `triage_apply` keeps the newest, the
  dispatcher takes whichever it finds and logs the conflict.
- **Track renamed in config while a task is in flight**: the stored track no
  longer resolves; the task parks `blocked` with the reason, the operator
  re-runs with an override or renames back. Not silently remapped.
- **Old config shape on the box**: fatal at load with the new key names.
  Deployment order below.
- **Unset effort**: no `--effort` flag; the CLI's default.

## Testing

- `models.py`: parse round-trip of the example config; every validation
  error above; `resolve` walks in order, honours `admit`, returns `None`
  when nothing is admitted, stable reorder for `avoid_provider`, no-op when
  all entries share the provider; `resolve_for_stage` mapping unchanged.
- `main.py`: pick recorded on stage entry and reused across tickets, review
  rounds and a resume; denied pick waits without re-walk; review's
  `avoid_provider` is the implement pick's provider; spec signal without a
  valid track is bounced; claim uses `track:` label else `untracked`.
- `containers.py`: argv contains `--effort` iff the entry sets it.
- `triage_apply.py`: at most one `track:*`, replaces the old one, rejects a
  name not in the inventory.
- `web`: approval screen shows track and `when`; admission alternatives are
  the track's ordered entries; `/run` rejects a model outside `model_ids()`.
- Config: the old shape fails with a message naming `tracks`, `untracked`,
  `triage`.

## Mechanical follow-through

Deployment order, as plan steps, because main auto-bumps the app revision
after CI (PR #118) and the September crash loop was a config/code mismatch:

1. Rewrite `targets.yaml` in `agent-ops-infra` to the tracks shape.
2. In a worktree on the feature branch, load that file with the new parser.
3. Create `track:*` labels in every target repo.
4. Merge. The revision bump deploys code and config that already agree.

Also: `targets.example.yaml` rewritten to the shape above; README's model
paragraph and CONTEXT.md updated; `models.log` gains the effort and track.
