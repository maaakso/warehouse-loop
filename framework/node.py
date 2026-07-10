"""node.py — registry of self-tuning loop nodes.

One node = one improvement loop wrapped around one deterministic conveyor.
Everything domain-specific lives in this registry: journal paths, model names,
caps, the builder's sandbox, prompt template files, and the provider modules
that supply panel metrics and manager context. The workers (manager.py,
builder.py, panel.py) are domain-agnostic — they read only this config.

To add your own loop, copy the "example" node, point its providers at your
own modules, and select it with the LOOP_NODE environment variable.
"""
import importlib
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NODES = {
    "example": {
        # ── journals / state / trigger files (all append-only except state) ──
        "logs": {
            "panel_log":      os.path.join(BASE_DIR, "panel.jsonl"),      # written by panel.py
            "solutions_log":  os.path.join(BASE_DIR, "solutions.jsonl"),  # written by manager.py
            "builds_log":     os.path.join(BASE_DIR, "builds.jsonl"),     # written by builder.py
            "reviews_log":    os.path.join(BASE_DIR, "reviews.jsonl"),    # manager's problem layouts
            "runs_log":       os.path.join(BASE_DIR, "runs.jsonl"),       # conveyor run journal (your conveyor writes it; mode/aborted are mandatory — see "Run journal contract" in README.md)
            "feedback_log":   os.path.join(BASE_DIR, "feedback.jsonl"),   # director complaints / verdicts (ts per line)
            "loop_state":     os.path.join(BASE_DIR, "loop_state.json"),  # the single current increment (WIP=1)
            "backlog":        os.path.join(BASE_DIR, "backlog.json"),     # card queue shared with the director's board
            "directives":     os.path.join(BASE_DIR, "directives.md"),    # director's steering file, re-read every cycle
            "manager_lastfail": os.path.join(BASE_DIR, "manager_lastfail.txt"),  # raw LLM output on parse failure
            # trigger files: touching one wakes the corresponding worker (see ORCHESTRATION.md)
            "build_request":  os.path.join(BASE_DIR, "build_request"),
            "solve_request":  os.path.join(BASE_DIR, "solve_request"),
            # live stage markers — a DIRECTORY, not a file: markers are ephemeral,
            # the directory is what you bind-mount (see ORCHESTRATION.md, "Liveness")
            "marker_dir":     os.path.join(BASE_DIR, "markers"),
        },
        # ── measurement currencies for measure_after ──
        # A solution may declare its own window-closing condition,
        # measure_after: {currency: N, ...} — the window closes when ANY
        # currency reaches its count (OR), with the slow_window_days timeout
        # always in force as a safety net. "days" is built in; every other
        # currency must be declared here as {log, ts_field, pred}.
        # CHECKLIST when wiring a node: open the REAL journal and verify the
        # time field and the predicate for EVERY currency. Copied defaults
        # fail silently — a wrong ts_field or a predicate that matches no
        # line yields a counter that never counts, so the window only ever
        # closes by timeout and the increment measures nothing.
        "measure_events": {
            "runs": {"log": "runs_log", "ts_field": "ts",
                     "pred": lambda r: r.get("mode", "live") == "live"
                     and not r.get("aborted")},
            "director_verdicts": {"log": "feedback_log", "ts_field": "ts",
                                  "pred": lambda r: True},
        },
        # ── director verdicts: the slow window's currency ──
        # Verdicts of DIFFERENT nodes live in DIFFERENT places (a feedback
        # journal here, a moderation table elsewhere) with different time
        # fields and filters. A hardcoded shared source means one node's slow
        # window gets closed by ANOTHER node's events — declare it per node.
        "director_verdicts_source": {
            "log": "feedback_log",   # key into "logs" (or an absolute path)
            "ts_field": "ts",
            "pred": lambda r: True,  # which lines count as a director verdict
        },
        # ── worker models (placeholders — plug in whatever runner you use) ──
        "models": {
            "manager_model": "your-model",
            "builder_model": "your-model",
            "reviewer_model": "your-model",
        },
        # ── caps and loop parameters ──
        "caps": {
            "max_cards": 3,             # cards the manager may file per cycle
            "chain_max": 4,             # builder chain length per trigger
            "day_max": 8,               # builder runs per day
            "slow_window_verdicts": 20, # slow window closes after N director verdicts...
            "slow_window_days": 5,      # ...or after this many days
            "trend_n": 5,               # panel records included in the metric trend
            "sample_n": 5,              # random samples per panel for cross-checking
            "text_clip": 120,           # max chars of item text in samples
            "window_7d_ms": 7 * 86400 * 1000,
            "panel_version": "1",       # bump when panel formulas change
            # Acceptance-layer thresholds. Adjustable; changing a formula bumps panel_version.
            "thresholds": {
                "intake_lag_hours": 12.0,       # hours since the newest item arrived
                "unsorted_items": 50,           # items eligible for sorting but not yet sorted
                "decision_coverage_7d": 0.90,   # share of fresh items with a conveyor decision
                "sweep_fixes_7d": 20,           # acceptance-sweep corrections in the last 7 days
            },
        },
        # ── builder sandbox: file whitelist, gate commands, optional deploy ──
        "builder": {
            # Blast radius: the builder may edit ONLY these files.
            "allowed_files": ("conveyor.py", "panel.py"),
            "eval_cmd": "python3 conveyor.py --eval",       # unit tests + held-out metrics
            "smoke_cmd": "python3 conveyor.py --dry-run",   # cheap end-to-end smoke
            "baseline": "precision@1 = 80.0%",              # stated BEFORE any change ships
            "deploy_cmd": None,   # e.g. ["./deploy.sh", "conveyor"]; None = changes are live in place
            "doc_path": "ARCHITECTURE.md",                  # updated in the same pass (Definition of Done)
        },
        # ── prompt template files (rendered with str.format at runtime) ──
        "prompts": {
            "manager":    "prompts/example/manager.md",
            "builder_tz": "prompts/example/builder_tz.md",
            "reviewer":   "prompts/example/reviewer.md",
        },
        # ── backlog cards: labels, title prefix, panel module named in auto-cards ──
        "cards": {
            "builder_label": "loop-builder",     # the ONE label the builder picks up
            "escalation_label": "needs-director",
            "hyp_prefix": "[hyp]",               # marks manager-filed hypothesis cards
            "panel_module": "panel.py",
        },
        # ── domain providers: "module:function" strings resolved at runtime ──
        "providers": {
            "panel_provider":   "panel:run_panel",        # builds and journals one panel record
            "context_provider": "panel:collect_context",  # gathers the manager's input snapshot
            # optional: "next_wake": "yourmodule:next_wake_ts" — epoch ms of the
            # conveyor's next scheduled run, computed per node (calendar cron vs
            # fixed interval — see ORCHESTRATION.md, "Liveness"). The clock knows
            # the schedule, not the liveness of the scheduler.
        },
    },
}


def get_node(name=None):
    """Node config by name → env LOOP_NODE → 'example'."""
    if name is None:
        name = os.environ.get("LOOP_NODE") or "example"
    return NODES[name]


def load_prompt(node, name):
    """Read a node's prompt template. node — a config dict (from get_node) or a
    node name; name — a key of the "prompts" section. The file is read verbatim
    (no strip): str.format() is applied by the worker at runtime, so literal
    braces in templates must be doubled ({{ }})."""
    cfg = node if isinstance(node, dict) else get_node(node)
    prompts = cfg.get("prompts") or {}
    if name not in prompts:
        raise KeyError(f"node.load_prompt: prompt '{name}' is not declared in the "
                       f"node's 'prompts' section (have: {sorted(prompts)})")
    path = os.path.join(BASE_DIR, prompts[name])
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"node.load_prompt: prompt file '{name}' not found: {path} — "
            f"every node must ship its template files (see prompts/<node>/)")
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def resolve_provider(node, name):
    """Resolve a "module:function" provider string from the node's "providers"
    section into a callable. Providers are the ONLY domain hook the generic
    workers call — write your own module and point the registry at it."""
    cfg = node if isinstance(node, dict) else get_node(node)
    spec = (cfg.get("providers") or {}).get(name)
    if not spec:
        raise KeyError(f"node.resolve_provider: provider '{name}' is not declared")
    mod_name, _, fn_name = spec.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, fn_name)
