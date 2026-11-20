#!/usr/bin/env python3
"""Failure analysis: pull traces, collect what failed, then check the analysis the agent wrote.

    rsi.sh traces [YYYY-MM-DD]           play back the production conversation behind each failing or poor task
    rsi.sh packets [YYYY-MM-DD]          write analysis/<date>.packets.json (what failed and its trace)
    rsi.sh analysis-check [YYYY-MM-DD]   validate analysis/<date>.json before it goes into the report

traces
  Pass/fail says that a task failed, not where. For every task with a failed check in the day's run
  and every poor-experience task in the suite, this plays back the real conversation it came from
  (`mw chat-eval playback`, conversation playback in Cockpit; set RSI_TRACE_CMD to use another tool) and
  finds where it went wrong: the turn, the step (the tool call, in order), the tool, and an outcome
  class (not_connected, no_permission, not_found, timeout, tool_error, no_reply, reasoning_only).
  The playback is real traffic, so it is saved under raw/traces/ and never read by the agent;
  analysis/<date>.traces.json holds only that structure, keyed by goal_id, no text and no source ids.
  Playback needs a signed-in Cockpit session. On the first 401 it stops and records
  status needs_cockpit_session; the packets and the report then say the traces are missing and why.

packets
  With an eval run for that date: one packet per task with a failed check. Each holds the task's
  strata, the failed checks (tag, judge line or data check) and the trace from the run: every tool
  call with its status, the connector mode it went through and any error, and the final reply, plus
  went_wrong (turn and step) from the run's trajectory and went_wrong_in_traffic from the traces step.
  These conversations are the suite's rewritten tasks against simulated systems. If a task called
  a real (as_is) connector, its replies and tool output are withheld from the packet.
  Without a run: one packet per poor-experience task in the suite (strata and the signals the
  sampler saw) and went_wrong_in_traffic, so the first analysis can start from the traffic sample.

analysis-check
  The analysis (templates/analysis.json shows the shape) groups failed tasks into failure modes
  and attaches one proposed change to each. The check fails when: a failed task is in no mode or
  in two; a mode has no proposal; a proposal lacks its target, before/after text or tags; a tag is
  not in the suite's vocabulary; any text holds an email, link, id or <sensitive> tag; or any text
  repeats wording from the pulled traffic: ten words in a row, or a whole short message (evidence
  must be paraphrased, never quoted); or a task whose packet names where it went wrong has no
  went_wrong line (task, turn, step, what) in its failure mode.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

KINDS = ["prompt", "tool", "documentation", "plugin", "skill", "access", "platform", "loop"]
CONFIDENCE = ["high", "medium", "low"]
STATUS = ["proposed", "approved", "skipped", "merged", "rejected", "superseded"]
TRACE_CMD = "mw chat-eval playback --org {org} --session-id {conversation_id} --format raw --all-messages"
COCKPIT_FIX = ("Trace playback needs a signed-in Cockpit session: open the playback page in Cockpit in Chrome, "
               "send the cookies with the Moveworks Tools extension, then run `rsi.sh traces` again.")
OUTCOMES = [("not_connected", re.compile(r"not connected|connect your|needs? (?:to be )?authori[sz]|\b401\b|unauthori[sz]ed|sign in", re.I)),
            ("no_permission", re.compile(r"\b403\b|forbidden|permission|access denied|not allowed", re.I)),
            ("not_found", re.compile(r"\b404\b|not found|does not exist|no such", re.I)),
            ("timeout", re.compile(r"timed? ?out|deadline exceeded", re.I))]
ERRORISH = re.compile(r'^\s*(?:\{\s*"?(?:error|errors)"?\s*:|error\b|exception\b|traceback\b)', re.I)


def load(work, date):
    manifest = {m["goal_id"]: m for m in common.read_jsonl(work / "suite" / "manifest.jsonl")}
    if not manifest:
        sys.exit("no suite manifest; run `rsi.sh build` first")
    sp = work / "runs" / date / "summary.json"
    summary = json.loads(sp.read_text()) if sp.exists() else None
    if summary and summary.get("status") != "ran":
        summary = None
    return manifest, summary


def required_tasks(manifest, summary):
    """The tasks an analysis has to account for, and why."""
    if summary:
        return sorted({f["goal_id"] for f in summary["failed_checks"]}), "eval_run"
    return sorted(g for g, m in manifest.items() if m["strata"]["experience"] == "poor"), "traffic_sample"


def outcome_of(text):
    """A class label for an error, never the error text itself."""
    for name, rx in OUTCOMES:
        if rx.search(text or ""):
            return name
    return "tool_error"


def went_wrong_playback(raw):
    """Where a played-back conversation went wrong: {turn, of, step, tool, outcome}. Structure only.
    raw is the playback response (tasks > sessions > turns > events / outputs)."""
    turns = [t for task in raw.get("tasks") or [] for s in task.get("sessions") or [] for t in s.get("turns") or []]
    turns.sort(key=lambda t: t.get("startedAt") or "")
    step, first_silent = 0, None
    for ti, turn in enumerate(turns, 1):
        names, current = {}, None
        for ev in turn.get("events") or []:
            item = ((ev.get("modelContextItemPayload") or {}).get("openaiItem")) or {}
            call, out, res = item.get("functionToolCall"), item.get("functionToolCallOutput"), ev.get("toolResultPayload")
            if call:
                step += 1
                current = (step, call.get("name"))
                names[call.get("callId")] = current
            elif out:
                text = str(out.get("output") or "")[:400]
                if ERRORISH.search(text) or re.search(r"\b40[134]\b|not connected|unauthori[sz]ed", text, re.I):
                    k, tool = names.get(out.get("callId"), current or (step, None))
                    return {"turn": ti, "of": len(turns), "step": k, "tool": tool, "outcome": outcome_of(text)}
            elif res and (res.get("success") is False or res.get("errorMessage")):
                k, tool = current or (step, res.get("toolId"))
                return {"turn": ti, "of": len(turns), "step": k, "tool": tool, "outcome": outcome_of(res.get("errorMessage"))}
            if ev.get("error"):
                k, tool = current or (step, None)
                return {"turn": ti, "of": len(turns), "step": k, "tool": tool, "outcome": outcome_of(ev["error"])}
        types = {str(o.get("messageType") or "").replace("MESSAGE_TYPE_", "") for o in turn.get("outputs") or [] if o.get("messageContent")}
        if "TEXT" not in types and first_silent is None:
            first_silent = {"turn": ti, "of": len(turns), "step": None, "tool": None,
                            "outcome": "reasoning_only" if "REASONING" in types else "no_reply"}
    return first_silent or {"turn": None, "of": len(turns), "step": None, "tool": None, "outcome": "ok"}


def went_wrong_eval(task):
    """The same, from an eval run's own trajectory (simulated systems)."""
    turns = (task.get("conversation") or {}).get("turns") or []
    step = 0
    for ti, turn in enumerate(turns, 1):
        for c in ((turn.get("trajectory") or {}).get("tool_calls")) or []:
            step += 1
            status = str(c.get("status") or "").lower()
            if c.get("error") or (status and status not in ("success", "succeeded", "ok", "completed")):
                return {"turn": ti, "of": len(turns), "step": step, "tool": (c.get("tool") or {}).get("name"),
                        "outcome": outcome_of(json.dumps(c.get("error")) if c.get("error") else status)}
        if not ((turn.get("assistant") or {}).get("messages")):
            return {"turn": ti, "of": len(turns), "step": None, "tool": None, "outcome": "no_reply"}
    return None


def trace_targets(work, date):
    manifest, summary = load(work, date)
    want = {g for g, m in manifest.items() if m["strata"]["experience"] == "poor"}
    if summary:
        want |= {f["goal_id"] for f in summary["failed_checks"]}
    return manifest, sorted(want)


def traces(work, date, limit=80):
    """Play back the source conversation of each failing or poor task; keep only where it went wrong."""
    cfg = common.load_config(work)
    manifest, want = trace_targets(work, date)
    src = {a["task_id"]: a["conversation_id"] for a in common.read_jsonl(work / "sample" / "audit.jsonl") if a.get("task_id")}
    tpl = os.environ.get("RSI_TRACE_CMD") or TRACE_CMD
    org = cfg.get("trace_org") or cfg["org"]
    store = work / "raw" / "traces"
    store.mkdir(parents=True, exist_ok=True)
    rows, status, reason, failures = {}, "pulled", "", 0
    for g in want[:limit]:
        cid = src.get(manifest[g]["task_id"])
        if not cid:
            continue
        cache = store / f"{cid}.json"
        if not cache.exists():
            cmd = [x.format(org=org, conversation_id=cid) for x in shlex.split(tpl)]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            except FileNotFoundError:
                status, reason = "needs_mw_cli", "the trace tool is not installed (" + cmd[0] + ")"
                break
            except subprocess.TimeoutExpired:
                failures += 1
                continue
            if p.returncode != 0 or not p.stdout.strip().startswith("{"):
                if re.search(r"\b401\b|unauthori[sz]ed|cookies? (?:have |has )?expired|log ?in", p.stderr + p.stdout, re.I):
                    status, reason = "needs_cockpit_session", COCKPIT_FIX
                    break
                failures += 1
                if failures >= 3:
                    status, reason = "failed", "trace playback failed three times; see `mw chat-eval playback --help`"
                    break
                continue
            cache.write_text(p.stdout)                   # real traffic: stays under raw/
        try:
            rows[g] = went_wrong_playback(json.loads(cache.read_text()))
        except ValueError:
            failures += 1
    asked = len(want)
    if status == "pulled" and len(rows) < asked:
        status = "partial" if rows else "failed"
        reason = reason or f"{asked - len(rows)} of {asked} tasks have no trace"
    out = {"date": date, "status": status, "reason": reason, "tasks_wanted": asked, "tasks_traced": len(rows), "went_wrong": rows}
    (work / "analysis").mkdir(exist_ok=True)
    path = work / "analysis" / f"{date}.traces.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{path}: traced {len(rows)} of {asked} failing or poor tasks (status: {status})" + (f"\n{reason}" if reason else ""))
    return out


def load_traces(work, date):
    p = work / "analysis" / f"{date}.traces.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"date": date, "status": "not_pulled", "reason": "run `rsi.sh traces` first. " + COCKPIT_FIX, "went_wrong": {}}


def trace_of(task):
    real, calls, replies = False, [], []
    for turn in (task.get("conversation") or {}).get("turns") or []:
        msgs = (turn.get("assistant") or {}).get("messages") or []
        if msgs:
            replies.append((msgs[-1].get("content") or "")[:1500])
        for c in ((turn.get("trajectory") or {}).get("tool_calls")) or []:
            modes = [(k.get("mode") or "").lower() for k in c.get("connectors") or []]
            real = real or any(m.endswith("as_is") for m in modes)
            calls.append({"tool": (c.get("tool") or {}).get("name"), "status": c.get("status"), "modes": modes,
                          "argument_names": sorted((c.get("arguments") or {}).keys()) if isinstance(c.get("arguments"), dict) else [],
                          "error": (json.dumps(c.get("error"))[:300] if c.get("error") else None)})
    if real:        # a real connector answered: its data must not travel into the analysis
        return {"real_connector_called": True, "tool_calls": [{**c, "error": None} for c in calls], "replies": "withheld"}
    return {"real_connector_called": False, "tool_calls": calls, "replies": replies}


def packets(work, date):
    manifest, summary = load(work, date)
    need, basis = required_tasks(manifest, summary)
    tr = load_traces(work, date)
    out = {"date": date, "basis": basis, "tasks_to_explain": len(need),
           "traces": {k: tr.get(k) for k in ("status", "reason", "tasks_wanted", "tasks_traced")}, "packets": []}
    in_traffic = tr.get("went_wrong") or {}
    if summary:
        failed = collections.defaultdict(list)
        for f in summary["failed_checks"]:
            failed[f["goal_id"]].append({k: f[k] for k in ("tag", "grader", "detail")})
        reports = {}
        for rp in (work / "runs" / date).glob("*/report.json"):
            if rp.parent.name.startswith("dry-"):
                continue
            for t in json.loads(rp.read_text()).get("tasks") or []:
                reports[t.get("task_id")] = t
        for g in need:
            m, t = manifest[g], reports.get(g) or {}
            out["packets"].append({"goal_id": g, "strata": m["strata"], "env_fit": m["env_fit"], "weight": m["weight"],
                                   "failed_checks": failed[g], "task_error": (t.get("error") or {}).get("code"),
                                   "went_wrong": went_wrong_eval(t), "went_wrong_in_traffic": in_traffic.get(g),
                                   "trace": trace_of(t)})
        out["per_tag"] = summary["per_tag"]
        seeds = collections.Counter((f["tag"], manifest[f["goal_id"]]["strata"]["utterance_type"],
                                     manifest[f["goal_id"]]["strata"]["tool_family"]) for f in summary["failed_checks"])
        out["where_failures_cluster"] = [{"tag": a, "utterance_type": b, "tool_family": c, "failed_checks": n}
                                         for (a, b, c), n in seeds.most_common(25)]
    else:
        store = {r["task_id"]: r for r in common.read_jsonl(work / "suite" / "tasks.jsonl")}
        for g in need:
            m = manifest[g]
            r = store.get(m["task_id"]) or {}
            out["packets"].append({"goal_id": g, "strata": m["strata"], "env_fit": m["env_fit"], "weight": m["weight"],
                                   "run_tier": m["run_tier"], "poor_signals": r.get("poor_signals") or [],
                                   "tools_observed": r.get("tools_observed") or [], "task": r.get("task"),
                                   "success_criteria": r.get("success_criteria"), "went_wrong_in_traffic": in_traffic.get(g)})
    (work / "analysis").mkdir(exist_ok=True)
    path = work / "analysis" / f"{date}.packets.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{path}: {len(need)} tasks to explain (basis: {basis})")
    if tr.get("status") != "pulled":
        print(f"traces: {tr.get('status')}. Turn and step are missing where there is no trace. {tr.get('reason', '')}")


def problems(doc, manifest, summary, tags, raw_sh, pk=None):
    """Every reason the analysis cannot go into the report. Never quotes the offending text."""
    bad = []
    located = {p["goal_id"] for p in (pk or {}).get("packets") or []
               if any((p.get(k) or {}).get("outcome") not in (None, "ok") for k in ("went_wrong", "went_wrong_in_traffic"))}
    need, basis = required_tasks(manifest, summary)
    if doc.get("basis") != basis:
        bad.append(f"basis must be '{basis}' for this date")
    modes, props = doc.get("failure_modes") or [], doc.get("proposals") or []
    pid = {p.get("id") for p in props}
    seen = collections.Counter()
    for i, m in enumerate(modes):
        where = f"failure_modes[{i}] ({m.get('id')})"
        for k in ("id", "title", "cause"):
            if not str(m.get(k) or "").strip():
                bad.append(f"{where}: '{k}' is empty")
        if not m.get("tasks"):
            bad.append(f"{where}: no tasks")
        if not m.get("evidence"):
            bad.append(f"{where}: no evidence lines")
        if m.get("confidence") not in CONFIDENCE:
            bad.append(f"{where}: confidence must be one of {CONFIDENCE}")
        if m.get("proposal") not in pid and not str(m.get("no_fix_reason") or "").strip():
            bad.append(f"{where}: needs a proposal id that exists, or a no_fix_reason")
        ww = {w.get("task"): w for w in m.get("went_wrong") or []}
        for g in set(m.get("tasks") or []) & located:
            w = ww.get(g)
            if not w or not isinstance(w.get("turn"), int) or not str(w.get("what") or "").strip():
                bad.append(f"{where}: task {g} has a trace; add a went_wrong line naming its turn, step and what happened")
        seen.update(m.get("tasks") or [])
    for i, n in enumerate(doc.get("not_failures") or []):
        if not str(n.get("why") or "").strip():
            bad.append(f"not_failures[{i}]: 'why' is empty")
        seen.update([n.get("task")])
    for g in need:
        if seen[g] == 0:
            bad.append(f"task {g} is in no failure mode (or list it under not_failures with a reason)")
    for g, n in seen.items():
        if n > 1:
            bad.append(f"task {g} is listed {n} times; each task belongs to one failure mode")
        if g not in manifest:
            bad.append(f"task {g} is not in the suite")
    for i, p in enumerate(props):
        where = f"proposals[{i}] ({p.get('id')})"
        for k in ("id", "title", "target", "before", "after", "expected"):
            if not str(p.get(k) or "").strip():
                bad.append(f"{where}: '{k}' is empty")
        if p.get("kind") not in KINDS:
            bad.append(f"{where}: kind must be one of {KINDS}")
        if p.get("confidence") not in CONFIDENCE:
            bad.append(f"{where}: confidence must be one of {CONFIDENCE}")
        if p.get("status", "proposed") not in STATUS:
            bad.append(f"{where}: status must be one of {STATUS}")
        if not p.get("tags") or any(t not in tags for t in p["tags"]):
            bad.append(f"{where}: tags must come from the suite's vocabulary: {sorted(tags)}")
        if not any(m.get("proposal") == p.get("id") for m in modes):
            bad.append(f"{where}: no failure mode points at it")
    if len(pid) != len(props):
        bad.append("two proposals share an id")

    def walk(o, path):
        if isinstance(o, str):
            for kind in common.leak_kinds(o):
                bad.append(f"{path}: contains something shaped like a {kind.replace('_', ' ')}; describe it in words instead")
            if common.quotes_traffic(o, raw_sh):
                bad.append(f"{path}: repeats wording from real traffic (ten words in a row, or a whole short message); paraphrase it")
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(doc, "")
    return bad


def raw_shingles(work):
    ptr = work / "raw" / "LATEST"
    return common.raw_shingles(work / "raw" / ptr.read_text().strip()) if ptr.exists() else set()


def vocabulary(work):
    t = json.loads((work / "suite" / "tags.json").read_text())
    return set(t["judge_tags"]) | set(t["code_check_tags"])


def check(work, date):
    manifest, summary = load(work, date)
    path = work / "analysis" / f"{date}.json"
    if not path.exists():
        sys.exit(f"no analysis at {path}; copy templates/analysis.json there and fill it in from the packets")
    try:
        doc = json.loads(path.read_text())
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    pp = work / "analysis" / f"{date}.packets.json"
    bad = problems(doc, manifest, summary, vocabulary(work), raw_shingles(work), json.loads(pp.read_text()) if pp.exists() else None)
    if bad:
        print(f"analysis-check: {len(bad)} problem(s) in {path}")
        for b in bad:
            print("  - " + b)
        sys.exit(1)
    print(f"analysis-check: ok ({len(doc['failure_modes'])} failure modes, {len(doc['proposals'])} proposals)")


def decide(work, date, pid, verdict):
    """Record the reviewer's Approve / Skip from the report on the proposal itself."""
    if not pid or not verdict:
        sys.exit("decide needs a proposal id and approve or skip: rsi.sh decide <date> P-1 approve")
    path = work / "analysis" / f"{date}.json"
    if not path.exists():
        sys.exit(f"no analysis at {path}")
    doc = json.loads(path.read_text())
    hit = [p for p in doc.get("proposals") or [] if p.get("id") == pid]
    if not hit:
        sys.exit(f"no proposal {pid} in {path}")
    hit[0]["status"] = "approved" if verdict == "approve" else "skipped"
    hit[0]["decided_at"] = dt.datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"{pid}: {hit[0]['status']}. The loop does not apply it; merge it where it says, then mark it merged.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["traces", "packets", "check", "decide"])
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("proposal", nargs="?", help="decide: the proposal id (P-1)")
    ap.add_argument("verdict", nargs="?", choices=["approve", "skip"], help="decide: approve or skip")
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    a = ap.parse_args()
    work = common.work_dir(a.work)
    if a.cmd == "decide":
        decide(work, a.date, a.proposal, a.verdict)
    else:
        {"traces": traces, "packets": packets, "check": check}[a.cmd](work, a.date)


if __name__ == "__main__":
    main()
