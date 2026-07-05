# The Warehouse Loop: a pattern for self-tuning pipelines

*How we let an LLM crew rebuild a production pipeline twice a day — without letting it near the steering wheel.*

## The problem

Every ML-ish pipeline ships imperfect. The usual fix is a human reviewing its output forever, or an LLM glued into the hot path forever. Both are permanent taxes. We wanted a third option: **a pipeline that gets measurably better every day, on a trajectory to needing neither.**

The unit of architecture here is not the pipeline — a pipeline is just one revolution of the cycle. The unit is the **improvement loop** itself. A product, in this view, is a set of independent self-tuning loops, and the LLM inside each one is secondary and replaceable: today a cloud model, tomorrow a local one, eventually nothing. The architecture outlives them all.

## The law of decision gravity

One rule holds the whole thing together: **every decision must sink one level down.** The human decides once → the manager turns it into a rule → the builder turns the rule into code → the conveyor just does it. A month later the human has forgotten that category of decisions exists. What actually flows through the loop is not actions — it's **knowledge**: complaints and verdicts become rules, rules become code. Every role is technical debt that exists only while the process below it is immature; a loop is mature when its recurring decisions are fully expressed in code.

One asymmetry matters: the worker is temporary, **the director is not.** He stops making operational decisions — but he never disappears, because he is the only one who changes the goals.

## The analogy

Picture a warehouse.

- A **conveyor** ships goods all day. Deterministic, cheap, local. It journals every decision and every action is reversible.
- A **floor worker** (an LLM, explicitly *temporary*) handles only what the conveyor isn't sure about — and spot-audits the confident lane. Verdict + reason, always journaled. Flags, never overrides.
- The **director** (the human) doesn't approve shipments. Goods hit the shelves immediately. His tool is the **complaint**: one click to undo + a free-text "why this was wrong." Silence means consent.
- An **optimization manager** (a stronger LLM, twice a day) reads everything — journals, metrics, complaints — and does three things: lays out problems with evidence, keeps the worker honest with numbers, and turns problems into concrete engineering decisions. Product forks get **escalated** to the director; everything it can test itself, it decides itself and files an FYI.
- A **builder** (the same strong LLM, agentic) picks the top decision and implements it: minimal diff, unit test added, docs updated in the same pass, then a mandatory **eval gate** — held-out metrics must not regress. Pass → the change is live for the next conveyor run. Fail → automatic revert and an honest post-mortem line.

The manager's objective is written into its prompt as an explicit function — minimize, simultaneously: the share of decisions made by an LLM, the number of complaints, the size of the doubt queue, processing cost, and decision latency. In one line: **make the conveyor good enough to fire the worker.** Every solution that replaces a cloud verdict with a local heuristic is a step toward its own irrelevance. And one complaint from the director outweighs any internal metric — without an external source of truth, a self-tuning system quietly starts optimizing its own grades.

## Why it doesn't run away

Self-modifying pipelines fail in known ways, so the rails are boring on purpose:

- **Whitelisted blast radius** — the builder can only touch its own pipeline files.
- **Structure is a view** — every action is reversible; facts are append-only.
- **Gate, not vibes** — no change survives a metric regression. Ever.
- **One label** — agents only pick up work explicitly tagged as theirs.
- **The director keeps two controls**: complaints (ground truth that outweighs any internal metric) and a one-line **directives file** the manager must re-read every cycle. Steering the whole system costs one sentence.
- **Autonomy is graduated by numbers** — tiers of control are removed bottom-up only when live precision earns it, never on faith.

## What happened on day one

The director undid one wrong auto-action and left a one-line complaint explaining why that item didn't belong. Next cycle, the manager diagnosed it as a systematic error class. The builder shipped a deterministic scope gate, added a unit test, passed the gate with precision *up*, and the exact case from the complaint is now blocked by code — not by a model's opinion. Second revolution, same day, fully unattended: a local message-type classifier, another cloud judgment moved on-prem.

Two turns of the loop, one human sentence each.

## Prior art, honestly

None of the parts are new. Recursive self-improvement, eval-gated code changes (AlphaEvolve, STOP), human-in-the-loop, OODA/PDCA, Toyota-style kaizen, CI/CD with canary and rollback — all well described. What we haven't seen as an established pattern is the composition: the loop as the base unit of product architecture, execution / review / optimization / building as four separate entities, complaints ranked above benchmarks, and — most of all — roles designed to disappear. Most agent architectures add agents. This one exists to remove them.

## The point

Don't ask an LLM to *be* the system. Ask it to *shrink itself out of* the system — and give it journals, a gate, and a complaints box so you can watch it happen in numbers.
