#!/usr/bin/env python3
"""manager.py — the optimization manager of a self-tuning loop.

One run does three things: lays out problems with evidence, issues a verdict on
the increment whose measurement window just closed, and turns at most ONE new
problem into a metric-targeted engineering decision (WIP=1). Decisions become
cards in the backlog; filing a card wakes the builder via a trigger file.

Event-driven, not scheduled: the run is cheap to invoke because it exits early
(without any LLM call) while the current increment's window is still open.

The LLM is behind one function — call_llm(prompt) -> str. Replace its body
with your own runner; everything else is deterministic.
"""
import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import node  # noqa: E402

NODE = node.get_node()

SOLUTIONS_LOG = NODE["logs"]["solutions_log"]
REVIEWS_LOG = NODE["logs"]["reviews_log"]
BACKLOG = NODE["logs"]["backlog"]
LOOP_STATE_PATH = NODE["logs"]["loop_state"]
PANEL_LOG = NODE["logs"]["panel_log"]
DIRECTIVES = NODE["logs"]["directives"]
MANAGER_LASTFAIL = NODE["logs"]["manager_lastfail"]
BUILD_REQUEST = NODE["logs"]["build_request"]

MODEL = NODE["models"]["manager_model"]
MAX_CARDS = NODE["caps"]["max_cards"]
SLOW_WINDOW_VERDICTS = NODE["caps"]["slow_window_verdicts"]
SLOW_WINDOW_DAYS = NODE["caps"]["slow_window_days"]

BUILDER_LABEL = NODE["cards"]["builder_label"]
HYP_PREFIX = NODE["cards"]["hyp_prefix"]
PANEL_MODULE = NODE["cards"]["panel_module"]

PROMPT = node.load_prompt(NODE, "manager")


# --------------------------------------------------------------------------- #
#  LLM runner — the ONE integration point. Replace with your own.
# --------------------------------------------------------------------------- #
def call_llm(prompt, model=MODEL, timeout=480):
    """Stub runner: shells out to a `claude -p` style CLI. Replace this body
    with your own LLM runner (API client, local model, another CLI) — the
    contract is simply prompt in, raw text out."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text", "--model", model],
        capture_output=True, text=True, timeout=timeout)
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------- #
#  Loop state: the single current increment (WIP=1)
# --------------------------------------------------------------------------- #
def load_loop_state(path=LOOP_STATE_PATH):
    try:
        st = json.load(open(path, encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("increment", None)
            return st
    except Exception:
        pass
    return {"increment": None}


def save_loop_state(state, path=LOOP_STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def last_panel(path=PANEL_LOG):
    """The most recent valid panel record (or None if no panel yet)."""
    rec = None
    try:
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rec


def panel_metric_values(panel, metric_target):
    """Values of the panel metrics mentioned in metric_target, flattened across
    all layers. Tokens in metric_target are matched against REAL panel keys —
    nothing is parsed 'by format'."""
    if not panel or not metric_target:
        return {}
    flat = {}
    for layer_metrics in (panel.get("layers") or {}).values():
        for k, v in (layer_metrics or {}).items():
            if isinstance(v, dict):
                v = v.get("value")
            if v is None or isinstance(v, (int, float)):
                flat[k] = v
    names = re.findall(r"[A-Za-z][A-Za-z0-9_]*", metric_target)
    return {n: flat[n] for n in names if n in flat}


def count_journal_events(spec, since_ms=0):
    """Count journal lines matching a measurement-currency spec
    {log, ts_field, pred} after since_ms; 'log' is a key of the node's "logs"
    section (or a path). Used for the slow window (director_verdicts_source)
    and for measure_after currencies (measure_events). When wiring a node,
    verify ts_field and pred against the REAL journal — a wrong field name
    counts nothing, silently, forever."""
    path = NODE["logs"].get(spec.get("log"), spec.get("log"))
    ts_field = spec.get("ts_field") or "ts"
    pred = spec.get("pred") or (lambda r: True)
    n = 0
    try:
        for ln in open(path, encoding="utf-8"):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get(ts_field, 0) > since_ms and pred(r):
                n += 1
    except FileNotFoundError:
        pass
    return n


def count_director_verdicts(since_ms=0):
    """Director verdicts (complaints / confirmations) journaled after since_ms,
    counted from the node's director_verdicts_source. Different nodes keep
    their verdicts in different stores — a shared hardcoded source would close
    one node's slow window with another node's events."""
    return count_journal_events(NODE.get("director_verdicts_source")
                                or {"log": "feedback_log"}, since_ms)


def is_window_closed(increment, panel, director_verdicts_since=None, now_ms=None):
    """(closed, reason) for the current increment.
    fast: the panel has a record with ts > started_ts (recomputed after the change).
    slow: >= SLOW_WINDOW_VERDICTS director verdicts after started_ts, OR more
    than SLOW_WINDOW_DAYS days have passed.
    measure_after: the solution may declare its own closing condition, e.g.
    {"runs": 30, "director_verdicts": 10, "days": 3} — the window closes when
    ANY currency reaches its count (OR across currencies). Currencies are the
    node's measure_events ("days" is built in); an undeclared currency never
    counts, and the SLOW_WINDOW_DAYS safety timeout applies regardless.
    Without measure_after the fast/slow default above is unchanged.
    director_verdicts_since is an injection point for tests (defaults to the
    node's director_verdicts_source)."""
    started = increment.get("started_ts") or 0
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    measure_after = increment.get("measure_after")
    if isinstance(measure_after, dict) and measure_after:
        if now_ms - started > SLOW_WINDOW_DAYS * 86400 * 1000:
            return True, f"timeout>{SLOW_WINDOW_DAYS}d"
        events = NODE.get("measure_events") or {}
        for currency, target in measure_after.items():
            try:
                target = int(target)
            except (TypeError, ValueError):
                continue
            if currency == "days":
                if now_ms - started > target * 86400 * 1000:
                    return True, f"measure_after days>={target}"
            elif currency == "director_verdicts":
                got = (director_verdicts_since if director_verdicts_since is not None
                       else count_director_verdicts(started))
                if got >= target:
                    return True, f"measure_after director_verdicts>={target}"
            elif currency in events:
                if count_journal_events(events[currency], started) >= target:
                    return True, f"measure_after {currency}>={target}"
        return False, "measure_after_open"
    if (increment.get("window") or "slow") == "fast":
        if panel and (panel.get("ts") or 0) > started:
            return True, "panel_after_start"
        return False, "no_panel_after_start"
    if now_ms - started > SLOW_WINDOW_DAYS * 86400 * 1000:
        return True, f"timeout>{SLOW_WINDOW_DAYS}d"
    if director_verdicts_since is None:
        director_verdicts_since = count_director_verdicts(started)
    if director_verdicts_since >= SLOW_WINDOW_VERDICTS:
        return True, f"director_verdicts>={SLOW_WINDOW_VERDICTS}"
    return False, "slow_window_open"


def validate_solutions(result):
    """Deterministic gate on the LLM's output: a solution with file_card=true
    but WITHOUT a non-empty metric_target is not filed — file_card is flipped
    to false and a warning is returned for the review record. The rule 'a
    metric target is declared before the change ships' is enforced in code,
    not trusted to the prompt."""
    invalid = []
    for s in (result.get("solutions") or []):
        if not isinstance(s, dict):
            continue
        if s.get("file_card") and not str(s.get("metric_target") or "").strip():
            s["file_card"] = False
            invalid.append({"title": s.get("title") or "",
                            "reason": "file_card=true without metric_target — not filed"})
    return invalid


def parse_llm_json(stdout):
    """Extract and parse the JSON object from raw LLM output, repairing the
    typical defects (code fences, trailing commas, control chars in strings).
    On failure the raw output is dumped to MANAGER_LASTFAIL for diagnosis."""
    stdout = re.sub(r"```(?:json)?", "", stdout or "")
    m = re.search(r"\{.*\}", stdout, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        repaired = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        repaired = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", repaired)
        try:
            return json.loads(repaired)
        except Exception:
            with open(MANAGER_LASTFAIL, "w", encoding="utf-8") as f:
                f.write(stdout)
            return None


# --------------------------------------------------------------------------- #
#  Card filing
# --------------------------------------------------------------------------- #
def file_cards(result, backlog_path=BACKLOG, max_cards=MAX_CARDS):
    """File hypothesis cards from the manager's answer: solutions with
    file_card=true, plus an auto-card 'fix the metric' if the sample audit
    found mismatches (a broken ruler outranks the next improvement — it is
    filed FIRST). Dedup by normalized title; at most max_cards total.
    Returns [{"id","title"}] — the id is needed for the increment state."""
    filed = []
    try:
        backlog = json.load(open(backlog_path))
    except FileNotFoundError:
        backlog = []
    except Exception as e:
        print(f"[manager] backlog read fail: {e}", file=sys.stderr)
        return filed
    existing = {re.sub(r"\W+", "", (c.get("title") or "").lower()) for c in backlog}

    def _push(card):
        title = card["title"]
        key = re.sub(r"\W+", "", title.lower())
        if key in existing or len(filed) >= max_cards:
            return
        existing.add(key)
        backlog.insert(0, card)
        filed.append({"id": card["id"], "title": title})

    mismatches = (result.get("sample_audit") or {}).get("mismatches") or []
    if mismatches:
        suspects = sorted({(m.get("metric_suspect") or "").strip()
                           for m in mismatches if (m.get("metric_suspect") or "").strip()})
        details = "\n".join(f"- {m.get('kind', '?')} {m.get('id', '?')}: {m.get('issue', '')} "
                            f"(metric: {m.get('metric_suspect', '?')})" for m in mismatches[:10])
        _push({
            "id": f"hyp-{int(time.time())}-{len(filed)}",
            "title": f"{HYP_PREFIX} Fix a panel metric: samples disagree with the numbers",
            "status": "To Do", "labels": [BUILDER_LABEL],
            "priority": "P1", "origin": "agent",
            "created_at": time.strftime("%Y-%m-%d"),
            "description": (f"PROBLEM: the panel's random samples disagree with its metrics — "
                            f"suspects: {', '.join(suspects) or 'see mismatches'}.\n"
                            f"MISMATCHES:\n{details}\n"
                            f"WHAT TO CHANGE: the formula / denominator / data source of the "
                            f"suspect metric in {PANEL_MODULE} (NOT the conveyor's behavior).\n"
                            f"EVAL GATE: recomputing the metric on the same sample matches a "
                            f"manual assessment."),
            "review_note": "Auto-card from the panel sample audit (manager).",
        })

    for s in (result.get("solutions") or [])[:max_cards]:
        if not s.get("file_card"):
            continue
        _push({
            "id": f"hyp-{int(time.time())}-{len(filed)}",
            "title": f"{HYP_PREFIX} {s.get('title', '')}".strip(), "status": "To Do",
            "labels": [BUILDER_LABEL],   # the builder's zone: only its own cards
            "priority": s.get("priority") or "P2", "origin": "agent",
            "created_at": time.strftime("%Y-%m-%d"),
            # description = the spec (visible on the board AND handed to the builder)
            "description": (f"PROBLEM: {s.get('problem', '')}\n"
                            f"WHAT TO CHANGE: {s.get('change', '')}\n"
                            f"EXPECTED EFFECT: {s.get('expected_effect', '')}\n"
                            f"METRIC TARGET: {s.get('metric_target', '')}\n"
                            f"MEASUREMENT WINDOW: {s.get('window', '')}\n"
                            f"EVAL GATE: {s.get('eval', '')}\n"
                            f"Apply ONLY through the eval gate."),
            "review_note": "Decision by the optimization manager (auto).",
        })
    if filed:
        json.dump(backlog, open(backlog_path, "w"), ensure_ascii=False, indent=1)
    return filed


# --------------------------------------------------------------------------- #
#  The cycle
# --------------------------------------------------------------------------- #
def run_manager(dry=False):
    # ── event loop, WIP=1: state and panel BEFORE any context assembly or LLM ──
    state = load_loop_state()
    panel = last_panel()
    gate_ok = bool((panel or {}).get("gate_acceptance_ok", True))
    acceptance_blocked = panel is not None and not gate_ok
    inc = state.get("increment")
    verdict_request = None
    if acceptance_blocked:
        # acceptance is red: this run is an acceptance-repair run, the current
        # increment is frozen (state untouched — no verdict, no new increment)
        print("[manager] ACCEPTANCE_BLOCKED: acceptance gate is red — repair mode, "
              "increment frozen", file=sys.stderr)
    elif inc:
        closed, reason = is_window_closed(inc, panel)
        if not closed:
            # measurement window still open → early exit WITHOUT an LLM call
            print(json.dumps({"ts": int(time.time() * 1000), "skipped": "window_open",
                              "increment": inc.get("title"), "reason": reason}))
            return None
        verdict_request = dict(inc)
        verdict_request["current"] = panel_metric_values(panel, inc.get("metric_target"))

    extra = ""
    if acceptance_blocked:
        red = panel.get("red_checks") or []
        extra += ("\nACCEPTANCE_BLOCKED: the latest panel's acceptance gate is red "
                  f"(failing checks: {', '.join(red) or 'see panel'}). The ONLY admissible "
                  "decision this run is repairing acceptance; do not tune shelves or flow.\n")
    if verdict_request:
        extra += ("\nVERDICT_REQUEST — the current increment's measurement window is CLOSED; "
                  "issue a verdict.\nIncrement: " + json.dumps(verdict_request) + "\n"
                  "Compare baseline and current on metric_target and add the key "
                  '"increment_verdict": {"verdict": "accept"|"rollback"|"inconclusive", '
                  '"reasoning": "<grounded in the numbers>"} to your JSON answer. '
                  "Verdict first, then (if warranted) ONE new solution.\n")

    collect_context = node.resolve_provider(NODE, "context_provider")
    ctx = collect_context()
    prev_reviews = []
    try:
        with open(REVIEWS_LOG, encoding="utf-8") as f:
            prev_reviews = [json.loads(x) for x in f.readlines()[-3:] if x.strip()]
        prev_reviews.reverse()
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    rev_brief = [{"ts": r.get("ts"), "summary": r.get("summary_md"),
                  "problems": r.get("problems") or []} for r in prev_reviews]
    try:
        directives = open(DIRECTIVES, encoding="utf-8").read()[:8000]
    except FileNotFoundError:
        directives = "(no directives yet)"
    prompt = PROMPT.format(max_cards=MAX_CARDS, directives=directives,
                           reviews=json.dumps(rev_brief, indent=1),
                           data=json.dumps(ctx, indent=1),
                           extra=extra)
    stdout = call_llm(prompt)
    result = parse_llm_json(stdout)
    if result is None:
        print(f"[manager] bad LLM json (raw saved to "
              f"{os.path.basename(MANAGER_LASTFAIL)})", file=sys.stderr)
        return None
    now = int(time.time() * 1000)

    # ── deterministic validation: file_card without metric_target is dropped ──
    invalid_solutions = validate_solutions(result)

    # ── verdict on the closed increment: parse it and clear the state ──
    increment_verdict = None
    if verdict_request:
        iv = result.get("increment_verdict")
        if isinstance(iv, dict) and iv.get("verdict") in ("accept", "rollback", "inconclusive"):
            increment_verdict = {"verdict": iv["verdict"],
                                 "reasoning": iv.get("reasoning") or ""}
        else:
            increment_verdict = {"verdict": "inconclusive",
                                 "reasoning": "LLM did not return a valid increment_verdict"}
        increment_verdict["increment"] = {k: inc.get(k) for k in
                                          ("title", "card_id", "metric_target", "window",
                                           "measure_after", "started_ts", "baseline")}
        increment_verdict["current"] = verdict_request.get("current")
        state["increment"] = None
        if not dry:
            save_loop_state(state)

    # part 1 (the layout) → reviews journal
    review = {"ts": now, "model": MODEL, "kind": "review",
              "summary_md": result.get("review_summary") or "",
              "problems": result.get("problems") or [],
              "anomalies": result.get("anomalies") or [],
              "sample_audit": result.get("sample_audit")}
    if invalid_solutions:
        review["invalid_solutions"] = invalid_solutions
    if increment_verdict:
        review["increment_verdict"] = increment_verdict
    if not dry:
        with open(REVIEWS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(review) + "\n")

    # part 2 (the decisions) → solutions journal + cards
    result.update({"ts": now, "model": MODEL, "kind": "solution"})
    if invalid_solutions:
        result["invalid_solutions"] = invalid_solutions
    if increment_verdict:
        result["increment_verdict"] = increment_verdict

    filed = []
    if not dry:
        filed = file_cards(result)
    result["filed_cards"] = [f["title"] for f in filed]

    # ── the first filed solution with a metric_target becomes the new increment.
    # An increment = a decision accepted for measurement; the builder ships it,
    # the state merely records it. Frozen while acceptance is blocked.
    new_increment = None
    if not dry and not acceptance_blocked and not state.get("increment"):
        for s in (result.get("solutions") or []):
            if not (isinstance(s, dict) and s.get("file_card")
                    and str(s.get("metric_target") or "").strip()):
                continue
            title = f"{HYP_PREFIX} {s.get('title', '')}".strip()
            card = next((f for f in filed if f["title"] == title), None)
            if card is None:
                continue   # not filed (dedup / max_cards overflow)
            new_increment = {
                "title": s.get("title") or "",
                "card_id": card["id"],
                "metric_target": s.get("metric_target"),
                "window": s.get("window") if s.get("window") in ("fast", "slow") else "slow",
                "started_ts": now,
                "baseline": panel_metric_values(panel, s.get("metric_target")),
            }
            # optional manager-declared closing condition (see is_window_closed)
            if isinstance(s.get("measure_after"), dict) and s.get("measure_after"):
                new_increment["measure_after"] = s["measure_after"]
            state["increment"] = new_increment
            save_loop_state(state)
            result["new_increment"] = new_increment
            break
    if not dry:
        with open(SOLUTIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        # close the loop: wake the builder — it takes the top hypothesis card
        # and implements it through the eval gate (see ORCHESTRATION.md)
        with open(BUILD_REQUEST, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time() * 1000), "from": "manager"}) + "\n")
    print(json.dumps({"ok": True, "solutions": len(result.get("solutions") or []),
                      "filed_cards": result["filed_cards"],
                      "acceptance_blocked": acceptance_blocked,
                      "increment_verdict": (increment_verdict or {}).get("verdict"),
                      "new_increment": (new_increment or {}).get("title"),
                      "invalid_solutions": len(invalid_solutions)}))
    return result


if __name__ == "__main__":
    run_manager(dry="--dry" in sys.argv)
