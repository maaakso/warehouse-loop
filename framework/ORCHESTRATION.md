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
- **reversibility** — the conveyor journals every placement, so every action
  has an undo; the builder backs up every file before editing and reverts on a
  failed gate. Nothing in the loop is a one-way door except the director's
  explicit accepts.

Crash behavior falls out for free: a worker that dies mid-run leaves at worst a
stale trigger file, and the next heartbeat re-runs it idempotently — journals
make re-runs safe to reason about.
