# Orchestration: waking the loop without daemons

No worker in the loop is a long-running process. Every stage is a short script
that runs, journals, and exits; what connects them is **trigger files** and a
scheduler you already have.

## Trigger files

Two zero-byte files act as wake-up calls:

- `solve_request` — touched by `panel.py --alert` when the acceptance gate goes
  red, when a fast measurement window closes, or when the WIP has been empty too
  long (anti-idle). Its watcher runs `manager.py`.
- `build_request` — written by the manager after filing a card (and by the
  builder itself to continue a chain). Its watcher runs `builder.py`.

Touch semantics matter: append-and-utime, never delete-and-recreate — some
watchers track the inode, and a replaced file silently detaches them.

## Watch options (provider-neutral)

Any of these works; pick what your platform gives you:

- **file-watch triggers** — a watcher service that runs a command when a path
  changes (launchd `WatchPaths` on macOS, `systemd.path` units on Linux,
  `inotifywait` in a supervisor loop, a Kubernetes sidecar on a shared volume);
- **cron / scheduled heartbeat** — a coarse fallback (e.g. every few hours) that
  runs the panel and the manager regardless of triggers; the manager is cheap to
  heartbeat because it exits early, without any LLM call, while the current
  increment's measurement window is open;
- **post-run hook** — the simplest of all: the conveyor's own run script calls
  `panel.py --alert` as its last step.

The recommended combination is event triggers **plus** a slow heartbeat: events
give latency, the heartbeat gives liveness when an event is lost.

One contract makes frequent wake-ups safe: **every worker in the loop — not
only the manager — must have a zero-cost empty path.** No LLM call and no
journal noise when there is no work; a watcher may fire on every run, because
an empty wake costs nothing.

## Editing the conveyor: edit cold, ship once

A conveyor is never edited while it can fire. Freeze the schedule (reversibly),
accumulate the changes offline, and ship them as one deploy behind a pre-flight
checklist: syntax, unit/e2e on copies, the rehearsal gate (replay against
recorded history — see README), and checksum identity across every environment
the engine lives in. Then unfreeze and watch one controlled run. A live edit
that collides with a tick produces a failure that looks exactly like a
scheduler outage — the cheapest way to debug that is to make it impossible.

## Liveness: stage markers and the next-wake clock

Workers are short scripts, but a conveyor run can take minutes — long enough
that a dashboard (or an operator) asks "is it running right now?". Two small
contracts answer that honestly.

**Stage marker.** A live run maintains one small JSON file in the node's
marker directory (`marker_dir` in the registry):

```json
{"stage": "sorting", "progress": {"done": 12, "total": 40},
 "started_ts": 0, "updated_ms": 0}
```

Rules:

- **atomic write** — write a tmp file, then rename; readers never see a
  half-written marker;
- **best-effort** — a marker failure must never fail the run;
- **dry runs do not write markers** — a rehearsal must not look alive;
- **removal in `try/finally`** — a crashed run cannot leave a phantom
  "in progress" behind;
- markers live in a dedicated **directory**, not a fixed file: if the loop
  runs in a container, bind-mount the directory — a mounted directory
  survives the ephemerality of the files inside it.

**Next-wake clock.** A node may expose a `next_wake` provider returning the
epoch of the conveyor's next scheduled run, so an idle dashboard can show
"next run at 12:05" instead of dead silence. Schedules come in two shapes —
calendar ("at :05 every hour") and interval ("last run + period"; intervals
restart from scheduler load, not from the wall clock) — so the computation is
per node. Caveat, stated once and honored everywhere: **the clock knows the
schedule, not the liveness of the scheduler.** If the watcher job is unloaded,
the clock still shows a time and the run will never come; liveness is what the
panel's intake-lag metric measures, the clock only renders expectation.

## Journals: append-only, and why

Every stage writes one JSON line per event to its own journal (`runs.jsonl`,
`panel.jsonl`, `reviews.jsonl`, `solutions.jsonl`, `builds.jsonl`,
`feedback.jsonl`). Rules:

- **append-only** — history is never rewritten; a correction is a new line, not
  an edit. This is what makes verdicts auditable: baseline and current values
  are both frozen lines, and any dispute replays from the journal.
- **state is minimal** — the only mutable state file is `loop_state.json`,
  holding the single current increment (WIP=1). Everything else can be
  reconstructed from journals.
- **the run journal doubles as the conveyor's clock** — every record carries
  `mode` and `aborted`, and only successful live runs move the intake window
  (see "Run journal contract" in README.md).
- **reversibility** — the conveyor journals every placement, so every action
  has an undo; the builder backs up every file before editing and reverts on a
  failed gate. Nothing in the loop is a one-way door except the director's
  explicit accepts.

Crash behavior falls out for free: a worker that dies mid-run leaves at worst a
stale trigger file, and the next heartbeat re-runs it idempotently — journals
make re-runs safe to reason about.
