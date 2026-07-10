# Warehouse Loop — reference implementation

A minimal, generic implementation of the pattern described in [the essay at the
root of this repository](../README.md): a deterministic **conveyor** wrapped in a
self-tuning improvement loop, with the LLM kept out of the control path. The code
here is a template — the `example` node runs end-to-end on synthetic journals, and
every domain-specific piece is an explicit extension point.

## The unit: a node

One **node** = one improvement loop around one conveyor. A node is nothing more
than an entry in the registry (`node.py`):

- **config** — journal paths, caps, thresholds, the builder's sandbox
  (`allowed_files`, `eval_cmd`, `baseline`, optional `deploy_cmd`), card labels;
- **domain providers** — two `module:function` hooks: a *panel provider* that
  measures the conveyor, and a *context provider* that assembles the manager's
  input snapshot;
- **prompts** — three template files rendered with `str.format()` at runtime.

The workers — `manager.py`, `builder.py`, `panel.py` — are domain-agnostic and
read only the registry. Select a node with the `LOOP_NODE` environment variable.

## The cycle

```
conveyor run ──▶ panel (deterministic, journals one record)
                    │
                    ▼  event: gate red / fast window closed / anti-idle
              manager (LLM, WIP=1)
                    │  verdict on the closed increment,
                    │  then at most ONE new metric-targeted decision → card
                    ▼
              builder (agentic LLM, whitelist + eval gate, auto-revert)
                    │  applied change is live for the next conveyor run
                    ▼
          measurement window (fast: next panel | slow: N director verdicts)
                    │
                    ▼
              verdict: accept / rollback / inconclusive → next increment
```

Files, not daemons: each arrow is a trigger file (`solve_request`,
`build_request`) touched by the previous stage and watched by the next — see
[ORCHESTRATION.md](ORCHESTRATION.md).

Once a day the loop also reports up: one scheduled digest per node — verdicts
closed, escalations, FYIs — so the director reads one artifact, not journals.

## Plugging in your domain

1. **Panel providers** (`panel.py`) — replace the bodies of the three layer
   functions; each returns `{metric: {"value", "threshold", "ok"}}`:
   - `layer_acceptance` — *blocking*: did every fresh item get in and get a
     decision? Any red check here blocks all other tuning.
   - `layer_shelves` — informing: is what is already shelved still coherent?
   - `layer_flow` — informing: how well does the conveyor decide, and at what
     cost? This layer carries the **no-op rate** — the retirement metric.
   Also replace `collect_samples` (random raw items for the sample audit).
2. **Context provider** (`collect_context`) — everything the manager reads:
   recent runs, director complaints (the highest-ranked signal), the builder's
   work, past reviews, open cards, the panel with its trend.
3. **Prompts** (`prompts/<node>/`) — copy the `example` set and put your
   conveyor's actual tiers and vocabulary into the preambles. Keep the objective
   function and the laws; they are the pattern.
4. **Builder sandbox** — list the conveyor files the builder may touch
   (`allowed_files`), provide a real `eval_cmd` (unit tests + held-out metrics)
   and a stated `baseline`.
5. **LLM runners** — `manager.call_llm` and `builder.run_agent` are stubs that
   shell out to a CLI; replace their bodies with your own runner. Set
   `models.*` in the registry (placeholder: `"your-model"`).

## The rules the code enforces

- **WIP=1** — one increment in measurement at a time; the manager exits early
  (no LLM call) while the window is open.
- **Metric target declared before the change ships** — a solution without
  `metric_target` is deterministically dropped (`validate_solutions`), and the
  baseline is snapshotted from the panel at filing time.
- **Non-degradation gate** — the builder's eval gate reverts any change that
  regresses held-out metrics; the manager must not worsen other layers.
- **The rehearsal gate** — before a loop is switched on, and before any change
  that alters how the conveyor decides, the new engine is replayed over a
  recorded window of history and scored against what actually happened: live
  placements are the ground truth, agreement is the acceptance criterion, and
  the threshold is declared up front. The eval gate proves the change doesn't
  regress its metrics; the rehearsal proves the whole engine still behaves like
  reality. A loop that cannot be rehearsed against its own journals is not
  ready to be trusted with writes.
- **The seniority law** — the director's actions are terminal for every
  automated role: no worker, manager, or builder may revert, overwrite, or
  re-decide them — not in data, not in code, not in structure.
- **The denominator law** — a metric may never be improved by shrinking what
  it measures: cutting the sweep's sample or a rate's denominator to move a
  number is forbidden; coverage cuts follow confirmed quality (a sustained
  no-op rate), they never produce it.
- **Acceptance blocks** — while the acceptance gate is red, the only admissible
  decision is repairing acceptance.
- **No hardcoded entity names** — decisions and implementations are general
  rules (properties, thresholds, signal classes), never spot fixes for one item
  or shelf; a card feasible only as a hardcode is `blocked`, not a workaround.
- **Cost cuts trail quality** — budget is a consequence of the no-op rate
  rising, never a target of its own.
- **One label** — the builder picks up only cards explicitly tagged as its own.
- **Escalation path** — a `blocked` build with no edits sheds the builder's
  label and gains the escalation label: the director triages it, the loop does
  not hammer the same wall. Chain (`chain_max`) and daily (`day_max`) caps
  bound autonomy; any failure stops the chain.
- **Append-only journals** — every stage writes one JSON line per event; state
  files hold only the current increment. History is never rewritten.

## Run journal contract

The conveyor's run journal (`runs_log`) is also the conveyor's clock: the
intake window of the next run starts where the last run left off. Two fields
are therefore mandatory on every record:

- `mode`: `"live"` | `"dry"`. Dry runs — the builder's eval rehearsals, manual
  tests — write to the SAME journal, but must NOT advance the window:
  otherwise every rehearsal eats the next live run's window, and the material
  in between gets processed by a rehearsal that has no right to act.
- `aborted`: bool. A crashed run leaves an emergency record carrying its error
  — the trace is mandatory — but must NOT advance the window either: the
  window waits for a successful run.

Keep the whole state semantics ("which record moves the window") in ONE
function with unit tests. Two call sites with two filters is how the dry hole
reopens.

## Conveyor reliability rules

Found the hard way; cheap to follow, expensive to rediscover:

- **One write connection per run, commit early.** Opening a second connection
  to the same store while the run's transaction is open deadlocks against the
  UI and other workers — and note that writing into a temp table opens a
  transaction too. One connection per live run; commit as soon as a batch is
  complete.
- **Every run leaves a trace.** Write the run record in `finally`: a crash
  produces an emergency record (`aborted: true` + the error) instead of a
  silent gap — which looks identical to "the scheduler never fired".
- **Rate metrics: numerator and denominator share one window.** An all-time
  count divided by a windowed total is confident nonsense.
- **Cross-journal id checks: verify id length and format on both sides.** One
  journal writing truncated ids while the comparison expects full ones loses
  every match silently — verdicts vanish with no error. Audit every
  `id in set` between different journals.
- **Instruments are code too.** The panel ships with a deterministic
  self-test, and a disagreement between an instrument and reality is repaired
  in the instrument as its own class of work — a loop that tunes the conveyor
  against a lying gauge optimizes noise.
- **One writer at a time.** Deploys are serialized behind a mutex, and any
  store or module shared by two loops has exactly one writer at a time.

## The acceptance sweep contour

The acceptance sweep — the LLM pass that re-checks the conveyor's placements
after every shipment — accumulates powers over time. Four contract rules:

- **New powers start in would-mode under a write guard.** Without the flag the
  sweep only journals what it *would* have corrected; nothing is touched. The
  director turns the flag on after the would-records have been vetted against
  live traffic. The guard is what separates diagnosis from intervention.
- **Director feedback on conveyor decisions is its own ground-truth channel**:
  a table in the node's shared store — verdict, the decision's key, an
  `applied` flag. The conveyor applies pending rows at the START of every live
  run; a director verdict outranks any sweep verdict (the override is written
  over it, not alongside); rows are never deleted — they are input to the
  panel and the manager.
- **The sweep's systemic escalations are a mandatory manager input**, not an
  option. A pattern the sweep reports — a class of items the conveyor
  systematically mishandles — goes into the context provider, or it rots in
  the journal.
- **Suggestion caps are prioritized.** When per-run proposals hit a cap,
  routine repeats must not crowd out substantive ones: sort the pool before
  applying the cap, or exempt repeats from it.
- **The sweep is bounded by default.** An acceptance worker can damage a
  delivery as easily as fix it — a sweep that "finds" connections manufactures
  them. Two permanent guards: under doubt the verdict is no-op (the default is
  *leave it*, never *improve it*), and corrections per run are hard-capped,
  with the cap sized so a misfiring sweep cannot redo the shipment before the
  next panel catches it.

## Shelves are born and die

Some nodes don't just sort onto shelves — they create them. Three rules keep
that from running away:

- **Birth needs an evidence bar.** A new shelf is created only above a
  declared threshold of independent evidence; below the bar the candidate is
  parked, visibly, awaiting evidence — a fate, not a rejection.
- **The dead don't crowd the living.** Dedup runs only against live shelves; a
  retired shelf leaves an immutable tombstone that never blocks a new birth.
- **Rebirth is a road, not a resurrection.** A wrongly killed shelf comes back
  through the normal birth path — never by editing the tombstone.

## Files

| file | role |
| --- | --- |
| `node.py` | registry: config + providers + prompts per node |
| `panel.py` | deterministic quality panel, sample collection, event alerts |
| `manager.py` | optimization manager: layout → verdict → one decision → card |
| `builder.py` | agentic builder: card → spec → change → eval gate → revert/apply |
| `prompts/example/` | prompt templates (manager, builder spec, reviewer) |
| `ORCHESTRATION.md` | waking the workers without daemons |

For the reasoning behind the pattern — the roles, the law of decision gravity,
the retirement metric — see the essay in the repository root.
