"""panel.py — the quality panel of a self-tuning loop (skeleton).

Deterministic, read-only, no LLM. One run = one JSON line appended to
panel.jsonl plus a human-readable panel on stdout. The panel is the loop's
single source of numbers: the manager targets its metrics, verdicts compare
its records, and the sample audit cross-checks it against raw items.

Three layers, mirroring the essay:

  acceptance (BLOCKING) — did every fresh item get in and get a decision?
      Intake freshness, unsorted backlog, decision coverage, sweep losses.
      If any acceptance check is red, the gate is red and the manager may
      only repair acceptance — no tuning of shelves or flow.
  shelves (informing) — is what is already shelved still coherent?
      Shelf integrity, duplicates, items alien to their shelf.
  flow (informing) — how well does the conveyor decide, and at what cost?
      Precision of automatic placements, doubt-queue size, no-op rate of
      the acceptance sweep (the retirement metric).

Every metric is reported as {"value": ..., "threshold": ..., "ok": ...};
ok=None means informational (no threshold). The example providers below read
the generic journals so the skeleton runs end-to-end out of the box — REPLACE
THEIR BODIES with queries against your own domain's data.
"""
import json
import os
import random
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
import node  # noqa: E402

NODE = node.get_node()

PANEL_LOG = NODE["logs"]["panel_log"]
RUNS_LOG = NODE["logs"]["runs_log"]
FEEDBACK_LOG = NODE["logs"]["feedback_log"]
REVIEWS_LOG = NODE["logs"]["reviews_log"]
BUILDS_LOG = NODE["logs"]["builds_log"]
BACKLOG = NODE["logs"]["backlog"]
LOOP_STATE = NODE["logs"]["loop_state"]
SOLUTIONS_LOG = NODE["logs"]["solutions_log"]
SOLVE_REQUEST = NODE["logs"]["solve_request"]

PANEL_VERSION = NODE["caps"]["panel_version"]
THRESHOLDS = NODE["caps"]["thresholds"]   # adjustable; changing a formula bumps PANEL_VERSION
WINDOW_7D_MS = NODE["caps"]["window_7d_ms"]
SAMPLE_N = NODE["caps"]["sample_n"]
TEXT_CLIP = NODE["caps"]["text_clip"]
TREND_N = NODE["caps"]["trend_n"]
HYP_PREFIX = NODE["cards"]["hyp_prefix"]


def _now_ms():
    return int(time.time() * 1000)


def _tail_jsonl(path, limit=2000):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out[-limit:]


def _metric(value, threshold=None, higher_is_better=False):
    """Uniform metric shape. ok=None → informational. A missing value (no data
    yet) is reported as ok=None too — decide in your own provider whether
    'no data' should block the gate for a given metric."""
    if threshold is None or value is None:
        return {"value": value, "threshold": threshold, "ok": None}
    ok = value >= threshold if higher_is_better else value <= threshold
    return {"value": value, "threshold": threshold, "ok": bool(ok)}


# --------------------------------------------------------------------------- #
#  Layer providers — REPLACE THESE with your domain's queries.
#  Contract: each returns {metric_name: {"value", "threshold", "ok"}}.
# --------------------------------------------------------------------------- #
def layer_acceptance(now_ms):
    """BLOCKING layer: did every fresh item get in and get a decision?
    The example computes from the conveyor run journal (runs.jsonl); a real
    node queries its own intake stores directly."""
    runs = _tail_jsonl(RUNS_LOG)
    since = now_ms - WINDOW_7D_MS
    last_run_ts = max((r.get("ts", 0) for r in runs), default=None)
    intake_lag_hours = (round((now_ms - last_run_ts) / 3600000.0, 2)
                        if last_run_ts else None)
    week = [r for r in runs if r.get("ts", 0) >= since]
    unsorted_items = week[-1].get("unsorted") if week else None
    eligible = sum(r.get("items_seen", 0) for r in week)
    decided = sum(r.get("items_decided", 0) for r in week)
    coverage = round(decided / eligible, 3) if eligible else None
    sweep_fixes = sum(r.get("sweep_fixes", 0) for r in week)
    return {
        "intake_lag_hours": _metric(intake_lag_hours, THRESHOLDS["intake_lag_hours"]),
        "unsorted_items": _metric(unsorted_items, THRESHOLDS["unsorted_items"]),
        "decision_coverage_7d": _metric(coverage, THRESHOLDS["decision_coverage_7d"],
                                        higher_is_better=True),
        "sweep_fixes_7d": _metric(sweep_fixes, THRESHOLDS["sweep_fixes_7d"]),
    }


def layer_shelves():
    """Informing layer: is what is already shelved still coherent? A real node
    replays its shelf-audit logic in dry mode (nothing applied) and reports
    integrity, duplicates, and items alien to their shelf."""
    return {
        "shelf_integrity": _metric(None),      # share of shelved items the audit would keep
        "duplicate_items": _metric(None),      # items placed on 2+ shelves
        "alien_share": _metric(None),          # shelved items that no longer match their shelf
    }


def layer_flow(now_ms):
    """Informing layer: how well does the conveyor decide, and at what cost?
    The example aggregates the run journal; the no-op rate of the acceptance
    sweep is THE retirement metric — the loop exists to push it toward 1.0."""
    runs = [r for r in _tail_jsonl(RUNS_LOG) if r.get("ts", 0) >= now_ms - WINDOW_7D_MS]
    auto = sum(r.get("auto_placed", 0) for r in runs)
    undone = sum(r.get("undone", 0) for r in runs)
    precision = round(1 - undone / auto, 3) if auto else None
    sweep_total = sum(r.get("sweep_total", 0) for r in runs)
    sweep_fixes = sum(r.get("sweep_fixes", 0) for r in runs)
    noop_rate = round(1 - sweep_fixes / sweep_total, 3) if sweep_total else None
    doubt_queue = runs[-1].get("doubt_queue") if runs else None
    return {
        "auto_precision_7d": _metric(precision),
        "sweep_noop_rate_7d": _metric(noop_rate),
        "doubt_queue_size": _metric(doubt_queue),
        "auto_actions_7d": _metric(auto),
    }


def collect_samples(now_ms):
    """Random samples of recent conveyor decisions, raw text included — the
    material for the manager's sample audit: if the samples disagree with the
    panel's numbers, the METRIC is what gets fixed, not the conveyor. The
    example samples the run journal's decision entries; a real node samples
    its own decision store."""
    decisions = []
    for r in _tail_jsonl(RUNS_LOG):
        if r.get("ts", 0) < now_ms - WINDOW_7D_MS:
            continue
        for d in (r.get("decisions") or []):
            decisions.append({"item_id": d.get("item_id"), "shelf_id": d.get("shelf_id"),
                              "verdict": d.get("verdict"),
                              "text": (d.get("text") or "")[:TEXT_CLIP]})
    return {"auto_actions": random.sample(decisions, min(SAMPLE_N, len(decisions)))}


# --------------------------------------------------------------------------- #
#  Context provider — the manager's input snapshot (all from journals).
# --------------------------------------------------------------------------- #
def _panel_trend(records):
    """Compact trend: for every numeric panel metric, the list of its last
    values (one per panel run, oldest first)."""
    trend = {}
    for rec in records[-TREND_N:]:
        flat = {"gate_acceptance_ok": rec.get("gate_acceptance_ok")}
        for layer_metrics in (rec.get("layers") or {}).values():
            for k, v in (layer_metrics or {}).items():
                if isinstance(v, dict):
                    v = v.get("value")
                if v is None or isinstance(v, (int, float)):
                    flat[k] = v
        for k, v in flat.items():
            trend.setdefault(k, []).append(v)
    return trend


def collect_context():
    """Everything the manager reads, in one compact dict: recent conveyor runs,
    director feedback (complaints are the highest-ranked signal — each one is a
    direct statement of HOW the system is wrong), the builder's recent work,
    past reviews (continuity: do not re-diagnose), open hypothesis cards
    (dedup), and the panel with its trend. Extend with whatever raw evidence
    your domain can offer — undone actions with the director's comments are
    the most valuable single input."""
    runs = _tail_jsonl(RUNS_LOG, 10)
    feedback = _tail_jsonl(FEEDBACK_LOG, 60)
    builds = _tail_jsonl(BUILDS_LOG, 6)
    prev_reviews = [{"ts": r.get("ts"), "summary": (r.get("summary_md") or "")[:400],
                     "problems": [p.get("title") for p in (r.get("problems") or [])]}
                    for r in _tail_jsonl(REVIEWS_LOG, 3)]
    try:
        open_cards = [{"id": c["id"], "title": c["title"]} for c in json.load(open(BACKLOG))
                      if c.get("origin") == "agent" and HYP_PREFIX in (c.get("title") or "")
                      and c.get("status") not in ("Done", "Cancelled")]
    except Exception:
        open_cards = []
    panel_records = _tail_jsonl(PANEL_LOG, TREND_N)
    return {
        "runs": runs,
        "director_feedback": feedback,
        "builds_by_builder": builds,
        "prev_reviews": prev_reviews,
        "open_hypothesis_cards": open_cards,
        "panel": panel_records[-1] if panel_records else None,
        "panel_trend": _panel_trend(panel_records),
    }


# --------------------------------------------------------------------------- #
#  The panel run
# --------------------------------------------------------------------------- #
def _print_panel(rec):
    lines = [f"=== QUALITY PANEL v{rec['panel_version']} "
             f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['ts'] / 1000))}) ==="]
    for layer_name, metrics in rec["layers"].items():
        for k, m in metrics.items():
            mark = {True: "OK  ", False: "FAIL", None: "info"}[m["ok"]]
            thr = "" if m["threshold"] is None else f"  (threshold {m['threshold']})"
            lines.append(f"  {layer_name:<10} | {k:<24} = {m['value']}{thr}  [{mark}]")
    if rec["gate_acceptance_ok"]:
        lines.append("ACCEPTANCE GATE: OK")
    else:
        lines.append(f"ACCEPTANCE GATE: BLOCKED ({', '.join(rec['red_checks'])})")
    print("\n".join(lines))


def run_panel():
    """Build one panel record, append it to the journal, print it. Read-only
    to all domain data; the journal is the only thing it writes."""
    now_ms = _now_ms()
    layers = {
        "acceptance": layer_acceptance(now_ms),
        "shelves": layer_shelves(),
        "flow": layer_flow(now_ms),
    }
    # the gate: every thresholded acceptance metric must be ok
    red = [k for k, m in layers["acceptance"].items() if m["ok"] is False]
    rec = {
        "ts": now_ms,
        "panel_version": PANEL_VERSION,
        "thresholds": dict(THRESHOLDS),
        "layers": layers,
        "gate_acceptance_ok": not red,
        "red_checks": red,
        "samples": collect_samples(now_ms),
    }
    with open(PANEL_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    _print_panel(rec)
    return rec


def alert_after_panel(rec, idle_hours=3):
    """Event-driven wake-up of the manager after a fresh panel: touch the
    solve_request trigger (its watcher runs the manager) when
      (a) the acceptance gate is red — repair cannot wait for a schedule;
      (b) the current increment has a fast window — this panel just closed it;
      (c) anti-idle — a verdict was issued but no new increment was assigned,
          and the manager has been silent longer than idle_hours; without this
          an empty WIP waits for the next heartbeat."""
    trigger = SOLVE_REQUEST

    def _touch():
        with open(trigger, "a", encoding="utf-8"):
            pass
        os.utime(trigger, None)

    if not rec.get("gate_acceptance_ok", True):
        _touch()
        print(f"[alert] acceptance gate red -> touch {os.path.basename(trigger)}")
        return
    try:
        st = json.load(open(LOOP_STATE))
        inc = st.get("increment") or {}
        if inc and (inc.get("window") or "slow") == "fast":
            _touch()
            print(f"[alert] fast window closed by fresh panel -> touch {os.path.basename(trigger)}")
            return
        if not inc:
            sol = _tail_jsonl(SOLUTIONS_LOG, 1)
            last_run = sol[-1].get("ts", 0) if sol else 0
            if time.time() * 1000 - last_run > idle_hours * 3600 * 1000:
                _touch()
                print(f"[alert] WIP empty, manager silent >{idle_hours}h -> "
                      f"touch {os.path.basename(trigger)} (anti-idle)")
    except (FileNotFoundError, ValueError):
        pass


if __name__ == "__main__":
    import sys as _sys
    _rec = run_panel()
    if "--alert" in _sys.argv:
        alert_after_panel(_rec)
