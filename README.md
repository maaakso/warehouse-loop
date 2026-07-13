# The Warehouse Loop: a pattern for self-tuning pipelines

*A production pipeline that rebuilds itself twice a day, with the LLM kept out of the control path.*

> **Status: running in production** on one workspace (two loops live as of July 2026). This is an essay — a composition of existing approaches written down as a pattern, not a framework. The environment this loop maintains is described in the sequels: [environment, not memory](https://github.com/maaakso/environment-not-memory) and [the environment over the chaos](https://github.com/maaakso/environment-over-chaos).

**TL;DR**

- Most agent architectures add agents. This pattern exists to remove them: pipelines that improve themselves out of needing an LLM.
- The unit of architecture is not the pipeline — a pipeline is one revolution of an improvement loop. The LLM inside is secondary and replaceable.
- Five roles: a deterministic conveyor does the work; a temporary LLM worker handles only uncertain cases; a human director never pre-approves — his tool is a one-click complaint plus "why"; a manager LLM turns complaints into decisions; a builder ships them behind an eval gate with auto-revert.
- The law of decision gravity: every recurring decision must sink a level down — human → hypothesis → rule → code.
- Complaints outrank every internal metric; otherwise the system optimizes its own grades.

## The problem

Every ML pipeline ships imperfect. The usual fix is a human reviewing its output forever, or an LLM wired into the hot path forever. Both are permanent taxes. There is a third option: **a pipeline that gets measurably better every day, on a trajectory to needing neither.**

The unit of architecture here is not the pipeline — a pipeline is one revolution of the cycle. The unit is the **improvement loop** itself. A product, in this view, is a set of independent self-tuning loops, and the LLM inside each one is secondary and replaceable: today a cloud model, tomorrow a local one, eventually nothing. The architecture outlives them.

## The law of decision gravity

One rule holds it together: **every decision must sink one level down.** The human decides once → the manager turns it into a rule → the builder turns the rule into code → the conveyor executes it. A month later that category of decisions no longer reaches the human. What flows through the loop is not actions but **knowledge**: complaints and verdicts become rules, rules become code. Every role is technical debt that exists only while the process below it is immature; a loop is mature when its recurring decisions are fully expressed in code.

One asymmetry matters: the worker is temporary, **the director is not.** He stops making operational decisions but never disappears, because he is the only one who changes the goals.

## The analogy

A warehouse.

- A **conveyor** ships goods all day. Deterministic, cheap, local. It ships **everything, leaving nothing undecided**: every item in the window gets exactly one journaled fate, and the journal partitions add up — an item the conveyor "isn't sure about" is itself a fate (parked, visibly, until evidence arrives), never a silent leftover. Every action is reversible.
- An **acceptance worker** (an LLM, explicitly *temporary*) runs a **full post-shipment acceptance sweep**: an independent re-check of the entire delivery, outside the hot path, four questions per item. Was anything lost? Was it handled correctly? Is it on the right shelf? Is this a one-off, or a *class* of conveyor failure? He accepts the delivery like a real receiving clerk: **any conveyor mistake is his to fix** — reversibly, with an undo journal, and a wrongly-shelved item goes back through the conveyor rather than being hand-placed. Two seniorities bound him: the director's own verdicts are untouchable, and every new power he gains ships first in **would-mode behind a write-guard** — he journals what he *would* have fixed until the dry record earns the director's flag. Unsure ones go to the director's doubt queue; systematic patterns are **escalated straight to the manager**, who must triage each one — is the problem real, should it be fixed at all, should it be fixed differently, is there already a card for it. Verdict + reason, always journaled.
- The **director** (the human) does not approve shipments. Goods reach the shelves immediately. His tool is the **complaint**: one click to undo plus a free-text "why this was wrong." Silence is consent.
- An **optimization manager** (a stronger LLM) reads everything — journals, metrics, complaints, and the worker's escalations — and runs its own improvement loop on top of the conveyor: it turns problems into hypotheses, each with a declared metric target and a measurement window, and tests them one at a time. Product forks are **escalated** to the director; anything it can test itself, it decides and files as an FYI.
- A **builder** (the same strong LLM, agentic) takes the top decision and implements it: minimal diff, unit test added, docs updated in the same pass, then a mandatory **eval gate** — held-out metrics must not regress. Pass → the change is live for the next conveyor run. Fail → automatic revert and a post-mortem line.

The manager's objective is written into its prompt as an explicit function: minimize, simultaneously, the share of decisions made by an LLM, the number of complaints, the size of the doubt queue, processing cost, and decision latency. In one line: **make the conveyor good enough to retire the worker.** Every solution that replaces a cloud verdict with a local heuristic is a step toward the worker's own irrelevance. One complaint from the director outweighs any internal metric — without an external source of truth, a self-tuning system starts optimizing its own grades.

## The retirement metric

"Retire the worker" needs a number, and the acceptance sweep produces it: the **no-op rate** — the share of acceptance verdicts that change nothing, because the conveyor already put every item on the right shelf. At 95–98% no-op the worker is a formality; below that, every non-ok verdict is free training data — a case the conveyor demonstrably cannot handle yet, arriving pre-labeled with a reason.

This inverts a tempting failure mode. A manager told to minimize cost will start cutting the worker's caps and calling it progress — the queue shrinks, the metrics look better, and quality silently leaks out through the cases nobody reviews anymore. The rule is therefore explicit: **cost cuts trail confirmed quality, never lead it.** Budget is a consequence of the no-op rate rising, not a target of its own. Its hard half is the **denominator law**: a metric may never be improved by shrinking what it measures — cutting the sweep's sample, capping the reviewer's intake, or narrowing a rate's denominator to move a number is forbidden. If a role's participation metric must fall, the only legal mechanism is moving its decisions into deterministic code.

The same logic justifies a counterintuitive move: making the temporary role *more* expensive. Upgrading the worker to a stronger model raises the cost per verdict — and raises the quality of the findings that the loop converts into rules. A sharper inspector retires himself sooner. The expensive worker is not a drift back toward LLM dependence; it is an accelerator on the same trajectory, and the no-op rate is what keeps that claim honest.

## Why it doesn't run away

Self-modifying pipelines fail in known ways, so the rails are deliberate:

- **Whitelisted blast radius** — the builder can touch only its own pipeline files.
- **Structure is a view** — every action is reversible; facts are append-only.
- **A gate, not a judgment call** — no change survives a metric regression.
- **One label** — agents pick up only work explicitly tagged as theirs.
- **The seniority law** — the director's actions are terminal for every automated role. No worker, manager, or builder may revert, overwrite, or re-decide what the director decided — not in data (a sweep correction), not in code (a builder change that undoes his verdict), not in structure (recreating what he killed).
- **Two director controls** — complaints (ground truth that outweighs any internal metric) and a **directives file** the manager re-reads every cycle. It starts as one steering sentence and grows into a numbered two-way protocol: the manager escalates a fork, the director's answer is recorded as a directive, and that category of questions never comes back. Steering the whole system still costs one sentence at a time.
- **Autonomy graduated by numbers** — tiers of control are removed bottom-up only when live precision earns it.

## Day one

The director undid one wrong auto-action and left a one-line complaint explaining why that item didn't belong. The next cycle, the manager diagnosed it as a systematic error class. The builder shipped a deterministic scope gate, added a unit test, passed the gate with precision higher than baseline, and the exact case from the complaint is now blocked by code rather than by a model's judgment. A second revolution the same day ran unattended: a local message-type classifier, moving another cloud judgment on-prem.

Two turns of the loop, one human sentence each.

## Prior art

None of the parts are new. Recursive self-improvement, eval-gated code changes (AlphaEvolve, STOP), human-in-the-loop, OODA/PDCA, Toyota-style kaizen, CI/CD with canary and rollback are all well described. What is not yet established as a pattern is the composition: the loop as the base unit of product architecture; execution, review, optimization, and building as four separate entities; complaints ranked above benchmarks; and roles designed to disappear. Most agent architectures add agents. This one exists to remove them.

## The point

Don't ask an LLM to *be* the system. Ask it to *shrink itself out of* the system — and give it journals, a gate, and a complaints box so the shrinking shows up in numbers.

## License

Text and diagrams: [CC BY 4.0](LICENSE). If code appears in this repository later, it will carry its own permissive license (MIT or Apache-2.0).
