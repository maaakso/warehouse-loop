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
