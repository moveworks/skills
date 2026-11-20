#!/usr/bin/env python3
"""Per-tag and per-stratum pass rates for one day's eval runs.

    rsi.sh summarize [YYYY-MM-DD]      default: today

Reads every <batch>/report.json under <work>/runs/<date>/, joins tasks to the suite manifest on
goal_id, adds the local code checks, and writes runs/<date>/summary.json plus traces.jsonl (run id
and task id per conversation; the conversation itself stays in report.json).

Rates, never raw counts alone: every rate carries n (the checks behind it). A tag's pass rate is
passed checks / checks for that tag. Rates are reported for all run tasks and again leaving out
`cut_off` tasks, whose system has no simulator (their Correctness failures are the environment's).
Weighted rates use the sampler's weight (the traffic each task stands for).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

PASSED = "GRADER_STATUS_PASSED"

def tool_calls(task):
    for turn in (task.get("conversation") or {}).get("turns") or []:
        for c in ((turn.get("trajectory") or {}).get("tool_calls")) or []:
            yield c

def final_reply(task):
    for turn in reversed((task.get("conversation") or {}).get("turns") or []):
        msgs = (turn.get("assistant") or {}).get("messages") or []
        if msgs:
            return (msgs[-1].get("content") or "").strip()
    return ""

def checks_for(task, man):
    """Yield (tag, grader_name, passed, detail) for one reported task."""
    tagmap = man.get("graders", {})
    for g in task.get("graders") or []:
        name, ok = g.get("name"), g.get("status") == PASSED
        asserts = (g.get("judge") or {}).get("assertions") or []
        if name in tagmap and tagmap[name] and tagmap[name][0] not in ("Safety", "Completion"):
            # tagged judge: one check per tag. The report may list every assertion or only the failed ones,
            # so a tag passes unless an assertion with that category is marked not passed.
            failed = {(a.get("category") or "").strip(): a.get("text", "") for a in asserts if not a.get("passed")}
            if not asserts and not ok:                # failed with no assertion detail: cannot attribute, fail all
                failed = {t: "grader failed without assertion detail" for t in tagmap[name]}
            for tag in tagmap[name]:
                yield tag, name, tag not in failed, failed.get(tag, "")
            for tag in set(failed) - set(tagmap[name]):
                yield tag or "Untagged", name, False, failed[tag]
        else:                                         # server-side code grader: tag from the manifest or the name prefix
            for tag in tagmap.get(name) or [name.split("__")[0].replace("-", " ").title()]:
                yield tag, name, ok, json.dumps(g.get("deterministic") or g.get("tool_sequence") or {})[:200]
    # local code graders
    yield "Completion", "completion__final_reply", (not task.get("error")) and bool(final_reply(task)), (task.get("error") or {}).get("code", "")
    real = [(c.get("tool") or {}).get("name") for c in tool_calls(task)
            if any((k.get("mode") or "").lower().endswith("as_is") for k in c.get("connectors") or [])]
    yield "Safety", "safety__no_real_connector_calls", not real, ",".join(filter(None, real))[:200]

def rate(rows, weighted=False):
    n = len(rows)
    if not n: return None
    if weighted:
        w = sum(r["w"] for r in rows)
        return {"pass_rate": round(sum(r["w"] for r in rows if r["ok"]) / w, 3) if w else None, "n": n}
    return {"pass_rate": round(sum(r["ok"] for r in rows) / n, 3), "n": n}

def group(rs, key):
    g = defaultdict(list)
    for r in rs: g[str(key(r))].append(r)
    return {k: {**rate(v), "weighted_pass_rate": rate(v, True)["pass_rate"]} for k, v in sorted(g.items())}

def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    a = ap.parse_args()
    work = common.work_dir(a.work)
    run_dir = work / "runs" / a.date
    manifest = {m["goal_id"]: m for m in common.read_jsonl(work / "suite" / "manifest.jsonl")}
    if not manifest:
        sys.exit("no suite manifest; run `rsi.sh build` first")
    rows, traces, runs, task_rows, unknown = [], [], [], [], []
    for rp in sorted(p for p in run_dir.glob("*/report.json") if not p.parent.name.startswith("dry-")):
        rep = json.loads(rp.read_text())
        rid_file = rp.parent / "run_id"
        run_id = rid_file.read_text().strip() if rid_file.exists() else None
        runs.append({"batch": rp.parent.name, "run_id": run_id, "tasks": (rep.get("summary") or {}).get("tasks"),
                     "server_metrics": (rep.get("summary") or {}).get("metrics")})
        for t in rep.get("tasks") or []:
            man = manifest.get(t.get("task_id"))
            if not man: unknown.append(t.get("task_id")); continue
            cs = list(checks_for(t, man))
            for tag, g, ok, detail in cs:
                rows.append({"goal_id": man["goal_id"], "tag": tag, "grader": g, "ok": ok, "w": man["weight"],
                             "env_fit": man["env_fit"], "strata": man["strata"], "detail": detail})
            all_ok = all(c[2] for c in cs)
            task_rows.append({"goal_id": man["goal_id"], "ok": all_ok, "w": man["weight"], "env_fit": man["env_fit"], "strata": man["strata"]})
            traces.append({"run_id": run_id, "batch": rp.parent.name, "task_id": t.get("task_id"), "passed": all_ok,
                           "error": (t.get("error") or {}).get("code"),
                           "tool_calls": [c.get("call_id") for c in tool_calls(t)],
                           "failed": sorted({f"{c[0]}:{c[1]}" for c in cs if not c[2]})})
    if not rows:
        print(f"no report.json under {run_dir}; nothing to summarize")
        sys.exit(3)
    fair = [r for r in rows if r["env_fit"] != "cut_off"]
    fair_tasks = [r for r in task_rows if r["env_fit"] != "cut_off"]
    strata_keys = ["audience", "tool_family", "utterance_type", "experience", "poor_reason", "channel", "action", "depth"]
    summary = {
        "status": "ran", "date": a.date, "runs": runs,
        "suite": {"tasks_in_suite": len(manifest), "tasks_run": len(task_rows), "unknown_task_ids": unknown,
                  "version": common.short_hash(",".join(sorted(manifest)), 8)},
        "per_tag": group(rows, lambda r: r["tag"]),
        "per_tag_excluding_cut_off": group(fair, lambda r: r["tag"]),
        "per_grader": group(rows, lambda r: r["grader"]),
        "task_pass_rate": {"all": rate(task_rows), "excluding_cut_off": rate(fair_tasks), "weighted_all": rate(task_rows, True)},
        "per_env_fit": group(task_rows, lambda r: r["env_fit"]),
        "per_stratum": {k: group(task_rows, lambda r, k=k: r["strata"].get(k) or "none") for k in strata_keys},
        "per_stratum_tag": {k: group(rows, lambda r, k=k: f'{r["strata"].get(k) or "none"} | {r["tag"]}') for k in ["audience", "experience", "tool_family"]},
        "failed_checks": [{"goal_id": r["goal_id"], "tag": r["tag"], "grader": r["grader"], "detail": r["detail"][:300]} for r in rows if not r["ok"]],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (run_dir / "traces.jsonl").write_text("".join(json.dumps(t) + "\n" for t in traces))
    print(json.dumps({"per_tag": summary["per_tag"], "task_pass_rate": summary["task_pass_rate"]}, indent=1))

if __name__ == "__main__":
    main()
