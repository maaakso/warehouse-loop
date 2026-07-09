#!/usr/bin/env python3
"""builder.py — the builder of a self-tuning loop (agentic LLM).

Takes the top hypothesis card carrying the builder's label, renders a strict
spec from a template, and runs an agentic LLM session against it. Safety rails:

  - file WHITELIST: the builder may edit only its own pipeline files;
  - backup before every edit; mandatory syntax check + eval gate; fail = revert;
  - a card ends at "Review" at most (only the director marks it Done);
  - one card per run; chain and daily caps; a failure STOPS the chain.

Escalation: a "blocked" result WITHOUT any file edits means a structural
obstacle (usually: the fix lives outside the sandbox) — the card loses the
builder's label and gains the escalation label, so the director must triage it
instead of the loop hammering the same wall.

Journal: builds.jsonl. Wake-up: the build_request trigger file.
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
BACKLOG = NODE["logs"]["backlog"]
BUILDS_LOG = NODE["logs"]["builds_log"]
BUILD_REQUEST = NODE["logs"]["build_request"]
MODEL = NODE["models"]["builder_model"]
WHITELIST = NODE["builder"]["allowed_files"]
DEPLOY_CMD = NODE["builder"]["deploy_cmd"]   # optional; None = changes are live in place
BUILDER_LABEL = NODE["cards"]["builder_label"]
ESCALATION_LABEL = NODE["cards"]["escalation_label"]
CHAIN_MAX = NODE["caps"]["chain_max"]
DAY_MAX = NODE["caps"]["day_max"]

TZ = node.load_prompt(NODE, "builder_tz")

RESULT_MARKER = "BUILD_RESULT"   # the agent must end its answer with this line


def run_agent(prompt, model=MODEL, timeout=3000):
    """Stub agent runner: shells out to a `claude -p` style agentic CLI with
    permissions confined to BASE_DIR. Replace this body with your own agent
    runner — the contract is prompt in, full transcript text out. Whatever you
    plug in, keep the sandbox: the agent works inside this directory only."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text",
         "--model", model, "--max-turns", "60", "--add-dir", BASE_DIR],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=BASE_DIR, timeout=timeout)
    return (proc.stdout or "").strip()


def pick_card(backlog):
    """The builder's zone of responsibility = ONLY cards carrying its label.
    Other backlog cards (the director's, other agents') are never touched,
    whatever their titles say. Priority order P0 > P1 > ..., then age."""
    cards = [c for c in backlog if BUILDER_LABEL in (c.get("labels") or [])
             and c.get("status") == "To Do"]
    cards.sort(key=lambda c: (c.get("priority") or "P3", c.get("created_at") or ""))
    return cards[0] if cards else None


def run_build():
    try:
        backlog = json.load(open(BACKLOG))
    except FileNotFoundError:
        backlog = []
    card = pick_card(backlog)
    if not card:
        print(json.dumps({"ok": False, "err": "no builder-labeled cards in To Do"}))
        return None
    card["status"] = "In Progress"
    json.dump(backlog, open(BACKLOG, "w"), ensure_ascii=False, indent=1)

    prompt = TZ.format(base=BASE_DIR, whitelist=", ".join(WHITELIST),
                       eval_cmd=NODE["builder"]["eval_cmd"],
                       smoke_cmd=NODE["builder"]["smoke_cmd"],
                       baseline=NODE["builder"]["baseline"],
                       doc_path=NODE["builder"]["doc_path"],
                       card=json.dumps({"id": card["id"], "title": card["title"],
                                        "note": card.get("description")
                                                or card.get("review_note", "")},
                                       indent=1))
    t0 = time.time()
    try:
        out = run_agent(prompt)
    except subprocess.TimeoutExpired:
        out = ""
    m = re.search(RESULT_MARKER + r":\s*(\{.*\})", out, re.S)
    try:
        result = json.loads(m.group(1)) if m else {
            "status": "blocked", "summary": f"no {RESULT_MARKER} (timeout/crash)"}
    except Exception:
        result = {"status": "blocked", "summary": f"{RESULT_MARKER} did not parse"}
    result.update({"ts": int(time.time() * 1000), "card_id": card["id"],
                   "card_title": card["title"], "model": MODEL,
                   "duration_s": round(time.time() - t0, 1)})

    # ── card status machine ──
    backlog = json.load(open(BACKLOG))
    for c in backlog:
        if c.get("id") == card["id"]:
            gate = result.get("gate") or {}
            note = (f" | BUILDER {time.strftime('%Y-%m-%d %H:%M')}: {result.get('status')}. "
                    f"{result.get('summary', '')} | Gate: before={gate.get('before', '?')} "
                    f"after={gate.get('after', '?')} "
                    f"| Files: {', '.join(result.get('files') or [])}")
            c["review_note"] = (c.get("review_note") or "") + note
            c["status"] = "Review" if result.get("status") == "applied" else "To Do"
            # Escalation: blocked WITHOUT edits = structural obstacle (usually
            # files outside the sandbox) — do not hammer the card, hand it to
            # the director. Exception: a missing result marker (timeout / the
            # final report never printed) is NOT structural — the work may have
            # been done — so it is retried, not escalated. blocked WITH edits
            # is an ordinary gate revert; no escalation.
            if (result.get("status") == "blocked" and not result.get("files")
                    and RESULT_MARKER not in (result.get("summary") or "")):
                c["labels"] = ([l for l in (c.get("labels") or []) if l != BUILDER_LABEL]
                               + [ESCALATION_LABEL])
                c["description"] = ((c.get("description") or "") +
                    f" | BUILDER ESCALATION {time.strftime('%Y-%m-%d %H:%M')}: blocked with "
                    f"no edits (diagnosis in {os.path.basename(BUILDS_LOG)} ts={result['ts']}) "
                    f"— likely outside the sandbox; needs the director.")
                result["escalated"] = True
            break
    json.dump(backlog, open(BACKLOG, "w"), ensure_ascii=False, indent=1)

    # ── optional deploy: if the conveyor executes deployed copies elsewhere,
    # applied edits must be shipped, or increments get measured WITHOUT the
    # change. The deploy command should validate syntax and abort on error. ──
    if DEPLOY_CMD and result.get("status") == "applied" and result.get("files"):
        try:
            dp = subprocess.run(DEPLOY_CMD, cwd=BASE_DIR,
                                capture_output=True, text=True, timeout=120)
            result["deployed"] = (dp.returncode == 0)
            if dp.returncode != 0:
                result["deploy_error"] = (dp.stdout + dp.stderr)[-300:]
        except Exception as e:
            result["deployed"] = False
            result["deploy_error"] = str(e)[:300]

    with open(BUILDS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")
    print(json.dumps({"ok": True, "status": result.get("status"),
                      "card": card["title"][:60],
                      "duration_s": result["duration_s"]}))
    _maybe_chain(result)
    return result


# --------------------------------------------------------------------------- #
#  Autonomous chain: after a successful "applied", the builder takes the next
#  high-priority card itself. Rails: at most CHAIN_MAX builds per trigger, at
#  most DAY_MAX builds per day, and any failure (blocked / reverted) STOPS the
#  chain — a systemic problem is not something to hammer.
# --------------------------------------------------------------------------- #
def _builds_today():
    n, day = 0, time.strftime("%Y-%m-%d")
    try:
        for ln in open(BUILDS_LOG, encoding="utf-8"):
            try:
                r = json.loads(ln)
                if time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0) / 1000)) == day:
                    n += 1
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return n


def _maybe_chain(result):
    if result.get("status") != "applied":
        print(json.dumps({"chain": "stop", "reason": f"build {result.get('status')}"}))
        return
    try:
        req = json.load(open(BUILD_REQUEST))
    except Exception:
        req = {}
    chain = int(req.get("chain") or 0) + 1
    if chain >= CHAIN_MAX:
        print(json.dumps({"chain": "stop", "reason": f"chain cap {CHAIN_MAX}"}))
        return
    if _builds_today() >= DAY_MAX:
        print(json.dumps({"chain": "stop", "reason": f"daily cap {DAY_MAX}"}))
        return
    backlog = json.load(open(BACKLOG))
    nxt = [c for c in backlog if BUILDER_LABEL in (c.get("labels") or [])
           and c.get("status") == "To Do" and (c.get("priority") or "P3") in ("P0", "P1")]
    if not nxt:
        print(json.dumps({"chain": "stop", "reason": "no P0/P1 in To Do"}))
        return
    with open(BUILD_REQUEST, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": int(time.time() * 1000), "from": "builder_chain",
                            "chain": chain}) + "\n")
    print(json.dumps({"chain": "next", "n": chain, "queue_p01": len(nxt)}))


if __name__ == "__main__":
    run_build()
