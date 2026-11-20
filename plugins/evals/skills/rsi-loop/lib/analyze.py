#!/usr/bin/env python3
"""Failure analysis: collect what failed, then check the analysis the agent wrote.

    rsi.sh packets [YYYY-MM-DD]          write analysis/<date>.packets.json (what failed and its trace)
    rsi.sh analysis-check [YYYY-MM-DD]   validate analysis/<date>.json before it goes into the report

packets
  With an eval run for that date: one packet per task with a failed check. Each holds the task's
  strata, the failed checks (tag, judge line or data check) and the trace from the run: every tool
  call with its status, the connector mode it went through and any error, and the final reply.
  These conversations are the suite's rewritten tasks against simulated systems. If a task called
  a real (as_is) connector, its replies and tool output are withheld from the packet.
  Without a run: one packet per poor-experience task in the suite (strata and the signals the
  sampler saw), so the first analysis can start from the traffic sample.

analysis-check
  The analysis (templates/analysis.json shows the shape) groups failed tasks into failure modes
  and attaches one proposed change to each. The check fails when: a failed task is in no mode or
  in two; a mode has no proposal; a proposal lacks its target, before/after text or tags; a tag is
  not in the suite's vocabulary; any text holds an email, link, id or <sensitive> tag; or any text
  repeats wording from the pulled traffic: ten words in a row, or a whole short message (evidence
  must be paraphrased, never quoted).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

KINDS = ["prompt", "tool", "documentation", "skill", "access", "platform", "loop"]
CONFIDENCE = ["high", "medium", "low"]
STATUS = ["proposed", "merged", "rejected", "superseded"]


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
    out = {"date": date, "basis": basis, "tasks_to_explain": len(need), "packets": []}
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
                                   "success_criteria": r.get("success_criteria")})
    (work / "analysis").mkdir(exist_ok=True)
    path = work / "analysis" / f"{date}.packets.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{path}: {len(need)} tasks to explain (basis: {basis})")


def problems(doc, manifest, summary, tags, raw_sh):
    """Every reason the analysis cannot go into the report. Never quotes the offending text."""
    bad = []
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
    bad = problems(doc, manifest, summary, vocabulary(work), raw_shingles(work))
    if bad:
        print(f"analysis-check: {len(bad)} problem(s) in {path}")
        for b in bad:
            print("  - " + b)
        sys.exit(1)
    print(f"analysis-check: ok ({len(doc['failure_modes'])} failure modes, {len(doc['proposals'])} proposals)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["packets", "check"])
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    a = ap.parse_args()
    work = common.work_dir(a.work)
    (packets if a.cmd == "packets" else check)(work, a.date)


if __name__ == "__main__":
    main()
