#!/usr/bin/env python3
"""The daily report: the negative experiences seen in traffic, and what to merge to fix them.

    rsi.sh report [YYYY-MM-DD]        default: today

Reads (all under <work>): sample/population.json (traffic rates per stratum), sample/audit.jsonl
(sensitivity gate counts), suite/manifest.jsonl and tags.json, every runs/<date>/summary.json up
to the report date (per-tag pass rates over time), and analysis/<date>.json (failure modes and
proposed changes; the most recent earlier analysis is used, and labelled, if today's is missing).

Also reads analysis/<date>.traces.json (where each failing or poor task went wrong, structure only).

Writes reports/<date>.md and reports/<date>.html (one self-contained file, no external assets). Each
proposal carries Approve / Skip: in the HTML the buttons copy `rsi.sh decide <date> <id> approve|skip`,
which records the verdict on the proposal. The report ends with Design notes on how the loop works.

The report shows rates with the number of checks or conversations behind them, never a bare
count. It holds no utterance or reply text: before anything is written, the whole report is
scanned for emails, links, ids and <sensitive> tags, and for wording that also appears in
the pulled traffic. If the scan finds something, nothing is written and the command exits 1.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

REASONS = {"thumbs_down": "Thumbs down", "no_reply": "No reply", "auth_wall": "Asked the user to connect a system",
           "retried": "The user asked again", "reasoning_leak": "The reply was the model's thinking notes",
           "could_not_complete": "Said it could not do it", "handoff": "Handed off to a person", "slow": "Slow reply"}
MAX_RUNS = 10


def pct(x):
    return "-" if x is None else f"{100 * x:.0f}%"


def load_json(p):
    return json.loads(p.read_text()) if p.exists() else None


def gather(work, date):
    cfg = common.load_config(work)
    manifest = common.read_jsonl(work / "suite" / "manifest.jsonl")
    tags = load_json(work / "suite" / "tags.json") or {"judge_tags": {}, "code_check_tags": {}}
    runs = []
    for d in sorted(p.name for p in (work / "runs").glob("*") if p.is_dir()) if (work / "runs").exists() else []:
        s = load_json(work / "runs" / d / "summary.json")
        if s and s.get("status") == "ran" and d <= date:
            runs.append((d, s))
    analysis, analysis_date = None, None
    if (work / "analysis").exists():
        for p in sorted((work / "analysis").glob("????-??-??.json"), reverse=True):
            if p.stem <= date:
                analysis, analysis_date = json.loads(p.read_text()), p.stem
                break
    audit = common.read_jsonl(work / "sample" / "audit.jsonl")
    tr = None
    if (work / "analysis").exists():
        for p in sorted((work / "analysis").glob("????-??-??.traces.json"), reverse=True):
            if p.name[:10] <= date:
                tr = json.loads(p.read_text())
                break
    return {"cfg": cfg, "date": date, "manifest": manifest, "tags": tags, "runs": runs[-MAX_RUNS:],
            "analysis": analysis, "analysis_date": analysis_date, "audit": audit, "traces": tr,
            "pop": load_json(work / "sample" / "population.json")}


# ───────────────────────────── sections, as (title, intro lines, tables, extra blocks) ─────────────────────────────

def traffic_section(pop):
    if not pop:
        return [("p", "No traffic sample yet. Run the pull and sample steps.")]
    b = [("p", f"{pct(pop['poor_rate']['rate'])} of conversations in the window showed a poor-experience signal "
               f"(n = {pop['poor_rate']['n']} conversations, {pop['since']} to {pop['until']}). "
               "A conversation counts once, under the first signal that applies.")]
    rows = [[REASONS.get(r, r), pct(v["rate"]), pct(v["share_of_poor"])] for r, v in pop["poor_reasons"].items()]
    b.append(("table", ["What went wrong", "Share of all conversations", "Share of poor conversations"], rows))
    cells = sorted(pop["audience_x_tool_family"], key=lambda c: -c["poor_rate"] * c["n"])[:12]
    b.append(("h3", "Where it happens"))
    b.append(("table", ["Asked by", "Tool family", "Poor rate", "Conversations behind the rate"],
              [[c["audience"], c["tool_family"], pct(c["poor_rate"]), f"n = {c['n']}"] for c in cells]))
    ut = pop["by"]["utterance_type"]
    b.append(("table", ["Utterance type", "Share of traffic", "Poor rate", "Conversations behind the rate"],
              [[k, pct(v["share"]), pct(v["poor_rate"]), f"n = {v['n']}"] for k, v in ut.items()]))
    jg = pop.get("experience_judge") or {}
    if jg.get("judged"):
        b.append(("h3", "Experience judge vs hard signals"))
        b.append(("p", f"Good or poor is an LLM judge's verdict; the hard signals are its features. It agrees with them on "
                       f"{pct(jg['agreement'])} of conversations (n = {jg['judged']} judged, {jg['fallback_to_signals']} fell back to the signals)."))
        b.append(("table", ["Judge", "Signals say poor", "Signals say good"],
                  [["poor", jg["both_poor"], jg["judge_poor_signals_good"]], ["good", jg["judge_good_signals_poor"], jg["both_good"]]]))
        b.append(("table", ["Hard signal", "Judge also says poor", "Conversations behind the rate"],
                  [[REASONS.get(k, k), pct(v["judge_poor_rate"]), f"n = {v['n']}"] for k, v in jg["per_signal"].items()]))
    days = pop.get("by_day") or {}
    if days:
        b.append(("h3", "Poor rate by day"))
        b.append(("trend", [(d, v["poor_rate"], v["n"]) for d, v in days.items()], False))
    return b


def tag_order(tags, runs):
    known = list(tags["judge_tags"]) + list(tags["code_check_tags"])
    extra = sorted({t for _, s in runs for t in s["per_tag"]} - set(known))
    return known + extra


def eval_section(g):
    runs, man = g["runs"], g["manifest"]
    ready = sum(1 for m in man if m["run_tier"] == "ready")
    b = [("p", f"Task suite: {len(man)} tasks, {ready} ready to run and {len(man) - ready} held "
               "(they would read private data or change a real system). Every task carries the same judges; "
               "each judge check carries one tag, and a tag's pass rate is passed checks over checks with that tag.")]
    if not runs:
        b.append(("p", "No eval run yet, so there is no pass rate to show. The first run sets the baseline."))
        return b
    latest_d, latest = runs[-1]
    prev = runs[-2][1] if len(runs) > 1 else None
    head = ["Tag", f"Pass rate, {latest_d}", "Checks behind it", "Change since the previous run", "Leaving out tasks with no simulator"]
    rows = []
    for t in tag_order(g["tags"], runs):
        cur = latest["per_tag"].get(t)
        if not cur:
            continue
        was = (prev or {}).get("per_tag", {}).get(t)
        delta = "first run" if not prev else ("-" if not was else f"{100 * (cur['pass_rate'] - was['pass_rate']):+.0f} pts")
        fair = latest["per_tag_excluding_cut_off"].get(t)
        rows.append([t, pct(cur["pass_rate"]), f"n = {cur['n']}", delta, f"{pct(fair['pass_rate'])} (n = {fair['n']})" if fair else "-"])
    b.append(("table", head, rows))
    b.append(("h3", "Per-tag pass rate over time"))
    if len(runs) == 1:
        b.append(("p", "One run so far: this is the baseline, not a trend."))
    versions = [s["suite"].get("version") for _, s in runs]
    if len(set(versions)) > 1:
        b.append(("p", "The suite grew between some of these runs (marked *), so compare those points with care."))
    head = ["Tag"] + [d[5:] + ("*" if i and versions[i] != versions[i - 1] else "") for i, (d, _) in enumerate(runs)]
    rows = []
    for t in tag_order(g["tags"], runs):
        cells = [s["per_tag"].get(t) for _, s in runs]
        if any(cells):
            rows.append([t] + [f"{pct(c['pass_rate'])} (n = {c['n']})" if c else "-" for c in cells])
    b.append(("table", head, rows))
    for t in tag_order(g["tags"], runs):
        pts = [(d, s["per_tag"][t]["pass_rate"], s["per_tag"][t]["n"]) for d, s in runs if t in s["per_tag"]]
        if len(pts) > 1:
            b.append(("trend", pts, True, t))
    b.append(("h3", f"Where the suite fails, {latest_d}"))
    for key, label in (("audience", "Asked by"), ("tool_family", "Tool family"), ("utterance_type", "Utterance type"), ("experience", "Experience in traffic")):
        rows = [[k, pct(v["pass_rate"]), pct(v["weighted_pass_rate"]), f"n = {v['n']}"]
                for k, v in sorted(latest["per_stratum"][key].items(), key=lambda kv: kv[1]["pass_rate"])]
        b.append(("table", [label, "Tasks passing every check", "Weighted to traffic", "Tasks behind the rate"], rows))
    return b


def traces_note(g):
    tr = g.get("traces") or {}
    if tr.get("status") == "pulled":
        return []
    if tr.get("status") == "needs_cockpit_session":
        head = "Needs Cockpit session."
    elif tr.get("status") == "needs_mw_cli":
        head = "Needs the trace tool (mw)."
    else:
        head = "Traces are missing."
    have = f"{tr.get('tasks_traced', 0)} of {tr['tasks_wanted']} failing or poor tasks have a trace" if tr.get("tasks_wanted") else "No task has a trace"
    return [("note", f"{head} {have}, so their turn and step are unknown and the causes below rest on reply text and tool names. "
                     + (tr.get("reason") or "Run `rsi.sh traces`; it needs a signed-in Cockpit session."))]


def merge_section(g):
    a = g["analysis"]
    if not a:
        return traces_note(g) + [("p", "No failure analysis yet for this date. Run the analysis step, then build the report again.")]
    b = traces_note(g)
    if g["analysis_date"] != g["date"]:
        b.append(("p", f"Today's analysis is not written yet. This is the analysis from {g['analysis_date']}."))
    if a.get("basis") == "traffic_sample":
        b.append(("p", "No eval has run, so these come from the traffic sample alone. Every expected effect is a direction, not a measurement."))
    man = {m["goal_id"]: m for m in g["manifest"]}
    convs = (g["pop"] or {}).get("conversations")
    latest = g["runs"][-1][1] if g["runs"] else None
    failed_tags = collections.defaultdict(set)
    for f in (latest or {}).get("failed_checks", []):
        failed_tags[f["goal_id"]].add(f["tag"])
    modes_by_p = collections.defaultdict(list)
    for m in a["failure_modes"]:
        modes_by_p[m.get("proposal")].append(m)
    order = {"proposed": 0, "approved": 1, "merged": 2, "skipped": 3, "superseded": 4, "rejected": 5}
    for p in sorted(a["proposals"], key=lambda p: order.get(p.get("status", "proposed"), 9)):
        ms = modes_by_p.get(p["id"], [])
        tasks = [t for m in ms for t in m["tasks"] if t in man]
        weight = sum(man[t]["weight"] for t in tasks)
        share = f"its tasks stand for about {pct(min(1.0, weight / convs))} of conversations in the window" if convs else "share unknown (no traffic sample)"
        now = "; ".join(f"{t} {pct(latest['per_tag'][t]['pass_rate'])} (n = {latest['per_tag'][t]['n']})"
                        for t in p["tags"] if latest and t in latest["per_tag"]) or "no run yet"
        b.append(("h3", f"{p['id']}. {p['title']}"))
        b.append(("kv", [("Status", p.get("status", "proposed")), ("Kind of change", p["kind"]), ("Where it merges", p["target"]),
                         ("Fixes", "; ".join(m["title"] for m in ms) or "-"),
                         ("How much traffic", share),
                         ("Tags it should move", ", ".join(p["tags"])), ("Those tags today", now),
                         ("Expected", p["expected"]), ("Confidence", p["confidence"])]))
        b.append(("diff", p["before"], p["after"]))
        b.append(("decide", g["analysis_date"], p["id"], p.get("status", "proposed")))
        for m in ms:
            hit = collections.Counter(t for g_ in m["tasks"] for t in failed_tags.get(g_, ()))
            b.append(("p", f"{m['id']}. {m['title']}. Likely cause ({m['confidence']} confidence): {m['cause']}"
                           + (" Failed tags: " + ", ".join(f"{t} on {pct(n / len(m['tasks']))} of its tasks" for t, n in hit.most_common()) + "." if hit else "")))
            b.append(("ul", m["evidence"]))
            ww = [f"Turn {w['turn']}" + (f" of {w['of']}" if w.get("of") else "") + (f", step {w['step']}" if w.get("step") else "")
                  + f": {w['what']}" for w in m.get("went_wrong") or [] if w.get("turn")]
            if ww:
                b.append(("p", "Where it went wrong, from the traces:"))
                b.append(("ul", ww))
    loose = [m for m in a["failure_modes"] if not m.get("proposal")]
    if loose:
        b.append(("h3", "Failure modes with no change proposed"))
        b.append(("ul", [f"{m['id']}. {m['title']}: {m.get('no_fix_reason', '')}" for m in loose]))
    return b


def gate_section(g):
    au = g["audit"]
    if not au:
        return [("p", "The sensitivity gate has not run.")]
    n = len(au)
    kept = [x for x in au if x["decision"] == "keep"]
    drops = collections.Counter(x["drop_reason"] for x in au if x["decision"] == "drop")
    at = collections.Counter(x.get("stage") for x in au if x["decision"] == "drop")
    reworded = sum(1 for x in kept if x.get("reworded"))
    b = [("p", f"Latest sample: the gate kept {pct(len(kept) / n)} of the conversations it read (n = {n}) as invented tasks with "
               "the same intent and dropped the rest. No original wording, name or id is stored in the suite or in this report.")]
    steps = [("Pre-check", "a sensitive-content tag drops the conversation unread", at["precheck"], None),
             ("Gate", "drops personal, customer, HR, credential, internal and unreleased roadmap content; rewrites the rest", at["gate"], None),
             ("Leak check", "removed names, ids, links and emails may not survive", sum(v for k, v in at.items() if str(k).startswith("leak_check")), None),
             ("Wording check", "no 8 words in a row from the source conversation, no 10 from any pulled message", at["wording"] + at["final_wording"], reworded),
             ("Adversarial audit", "a second model that sees only the rewrite hunts for anything real", at["verify"], None),
             ("Duplicates", "the same request with another id", at["dedupe"], None)]
    b.append(("table", ["Step", "What it does", "Dropped (share of gated)", "Reworded and kept"],
              [[s_, what, f"{pct(d / n)} (n = {d})", "-" if r is None else f"{pct(r / max(1, len(kept)))} of kept (n = {r})"] for s_, what, d, r in steps]))
    b.append(("table", ["Dropped because", "Share of gated conversations"],
              [[k.replace("_", " "), f"{pct(v / n)} (n = {v})"] for k, v in drops.most_common()]))
    return b


DESIGN_NOTES = [
    ("Where a conversation went wrong, not just pass or fail",
     "The analyze step reads the agent trace of every failing and poor task: the eval run's own trajectory, and a playback of "
     "the production conversation behind it (`mw chat-eval playback`). Each failure mode names the turn and the step (the tool "
     "call) where it went wrong. Playback needs a signed-in Cockpit session; when it is missing, this report says so at the top "
     "of What to merge."),
    ("Good versus poor experiences",
     "An explicit LLM judge, with a small rubric and invented few-shots, labels every sampled conversation good or poor from the "
     "request, the final reply and the user's next message. Thumbs down, no reply, retry, connect prompts, hand-offs and slow "
     "replies stay as its features, and its agreement with them is shown under Negative experiences."),
    ("Names, ids and personal data in utterances",
     "Every sampled conversation passes the sensitivity gate before anything is stored: it is dropped, or rewritten as an "
     "invented task with the same intent. A wording check refuses any task that shares 8 words in a row with its conversation "
     "or 10 with any pulled message, and a second model audits each rewrite adversarially. The counts per step are in the "
     "Sensitivity gate section."),
    ("What comes out",
     "Proposals. Each is a before and after diff against the assistant's instructions, an MCP tool's description or schema, "
     "documentation, or a plugin, with Approve / Skip in this report. The loop applies nothing; the next run shows whether the "
     "tags an approved change named moved."),
]


def design_section():
    b = []
    for q, a in DESIGN_NOTES:
        b += [("h3", q), ("p", a)]
    return b


# ───────────────────────────── renderers ─────────────────────────────

def to_md(title, sections):
    L = [f"# {title}", ""]
    for name, blocks in sections:
        L += [f"## {name}", ""]
        for blk in blocks:
            kind = blk[0]
            if kind == "p":
                L += [blk[1], ""]
            elif kind == "h3":
                L += [f"### {blk[1]}", ""]
            elif kind == "ul":
                L += [f"- {x}" for x in blk[1]] + [""]
            elif kind == "kv":
                L += [f"- **{k}:** {v}" for k, v in blk[1]] + [""]
            elif kind == "table":
                cell = lambda x: str(x).replace("|", "/").replace("\n", " ")
                L += ["| " + " | ".join(map(cell, blk[1])) + " |", "|" + "---|" * len(blk[1])]
                L += ["| " + " | ".join(map(cell, r)) + " |" for r in blk[2]] + [""]
            elif kind == "diff":
                L += ["```diff"] + [f"- {x}" for x in blk[1].split("\n")] + [f"+ {x}" for x in blk[2].split("\n")] + ["```", ""]
            elif kind == "note":
                L += [f"> **{blk[1].split('. ')[0]}.** {blk[1].split('. ', 1)[1] if '. ' in blk[1] else ''}", ""]
            elif kind == "decide":
                L += [f"Approve: `rsi.sh decide {blk[1]} {blk[2]} approve` · Skip: `rsi.sh decide {blk[1]} {blk[2]} skip` (now: {blk[3]})", ""]
            elif kind == "trend" and len(blk) == 3:     # per-tag trends are already a table in Markdown
                L += [", ".join(f"{d[5:]} {pct(r)} (n = {n})" for d, r, n in blk[1]), ""]
    return "\n".join(L).rstrip() + "\n"


def spark(points, higher_is_better):
    w, h, pad = 420, 70, 8
    n = len(points)
    xs = [pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    ys = [h - pad - (h - 2 * pad) * r for _, r, _ in points]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3"><title>{html.escape(d)}: {pct(r)} (n = {k})</title></circle>'
                   for x, y, (d, r, k) in zip(xs, ys, points))
    cls = "up" if higher_is_better else "down"
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" role="img" aria-label="trend">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" class="axis"/>'
            f'<polyline points="{line}" fill="none"/>{dots}</svg>'
            f'<div class="range">{html.escape(points[0][0])}: {pct(points[0][1])} &rarr; {html.escape(points[-1][0])}: {pct(points[-1][1])}</div>')


CSS = """
:root{color-scheme:light dark;--fg:#1c1e21;--muted:#5f6670;--line:#d9dce1;--bg:#fff;--soft:#f5f6f8;--bad:#b3261e;--good:#1b6e3c;--accent:#2f5fd0}
@media (prefers-color-scheme:dark){:root{--fg:#e8eaed;--muted:#a0a6ae;--line:#3a3f46;--bg:#16181b;--soft:#1f2226;--bad:#f2867e;--good:#7fd6a0;--accent:#8fb0ff}}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--fg);background:var(--bg);max-width:1040px;margin:32px auto;padding:0 20px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 10px;padding-top:14px;border-top:1px solid var(--line)}h3{font-size:16px;margin:22px 0 8px}
p{margin:8px 0}table{border-collapse:collapse;margin:10px 0 16px;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:13px}td:not(:first-child){font-variant-numeric:tabular-nums}
dl{display:grid;grid-template-columns:190px 1fr;gap:4px 14px;margin:8px 0 12px}dt{color:var(--muted)}dd{margin:0}
pre{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:10px 12px;white-space:pre-wrap;font:13px/1.45 ui-monospace,Menlo,monospace;margin:8px 0 14px}
.del{color:var(--bad)}.add{color:var(--good)}.fig{margin:6px 0 14px}.fig b{font-size:13px}
.spark{width:420px;max-width:100%;height:70px;display:block}.spark polyline{stroke:var(--accent);stroke-width:2}.spark circle{fill:var(--accent)}.spark .axis{stroke:var(--line)}
.range{color:var(--muted);font-size:13px}.sub{color:var(--muted);margin:0 0 8px}
.note{border:1px solid var(--line);border-left:4px solid var(--bad);background:var(--soft);border-radius:6px;padding:10px 14px;margin:10px 0}
.decide{display:flex;gap:8px;align-items:center;margin:4px 0 14px}.decide button{font:600 13px system-ui,sans-serif;padding:6px 14px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
.decide button.ok{background:var(--good);border-color:var(--good);color:var(--bg)}.decide span{color:var(--muted);font-size:13px}
"""
COPY_JS = """<script>document.addEventListener('click',function(e){var b=e.target.closest('button[data-cmd]');if(!b)return;
navigator.clipboard.writeText(b.dataset.cmd).then(function(){b.parentNode.querySelector('span').textContent='Copied: '+b.dataset.cmd+'. Run it to record the choice.';});});</script>"""


def to_html(title, sub, sections):
    e = html.escape
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>{e(title)}</title><style>{CSS}</style></head><body><h1>{e(title)}</h1><p class='sub'>{e(sub)}</p>"]
    for name, blocks in sections:
        out.append(f"<h2>{e(name)}</h2>")
        for blk in blocks:
            kind = blk[0]
            if kind == "p":
                out.append(f"<p>{e(blk[1])}</p>")
            elif kind == "h3":
                out.append(f"<h3>{e(blk[1])}</h3>")
            elif kind == "ul":
                out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in blk[1]) + "</ul>")
            elif kind == "kv":
                out.append("<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in blk[1]) + "</dl>")
            elif kind == "table":
                out.append("<table><thead><tr>" + "".join(f"<th>{e(str(h))}</th>" for h in blk[1]) + "</tr></thead><tbody>" +
                           "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in blk[2]) + "</tbody></table>")
            elif kind == "diff":
                out.append("<pre>" + "".join(f"<span class='del'>- {e(x)}</span>\n" for x in blk[1].split("\n")) +
                           "".join(f"<span class='add'>+ {e(x)}</span>\n" for x in blk[2].split("\n")) + "</pre>")
            elif kind == "note":
                head, _, rest = blk[1].partition(". ")
                out.append(f"<div class='note'><b>{e(head)}.</b> {e(rest)}</div>")
            elif kind == "decide":
                cmd = f"rsi.sh decide {blk[1]} {blk[2]}"
                out.append(f"<div class='decide'><button class='ok' data-cmd='{e(cmd)} approve'>Approve</button>"
                           f"<button data-cmd='{e(cmd)} skip'>Skip</button><span>Now: {e(blk[3])}</span></div>")
            elif kind == "trend":
                label = f"<b>{e(blk[3])}</b>" if len(blk) > 3 else ""
                out.append(f"<div class='fig'>{label}{spark(blk[1], blk[2])}</div>")
    out.append(COPY_JS + "</body></html>")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    a = ap.parse_args()
    work = common.work_dir(a.work)
    g = gather(work, a.date)
    cfg = g["cfg"]
    title = f"{common.display_name(cfg)}: daily improvement report, {a.date}"
    sub = f"Org: {cfg['org']}. Nothing in this report has been applied; each change below is a proposal to review and merge."
    sections = [("Negative experiences in traffic", traffic_section(g["pop"])),
                ("What to merge", merge_section(g)),
                ("Eval suite: pass rate per tag", eval_section(g)),
                ("Sensitivity gate", gate_section(g)),
                ("Design notes", design_section())]
    md = to_md(title, [("Summary", [("p", sub)])] + sections)

    # last gate: the report itself must be clean
    kinds = common.leak_kinds(md)
    ptr = work / "raw" / "LATEST"
    raw_sh = common.raw_shingles(work / "raw" / ptr.read_text().strip()) if ptr.exists() else set()
    quoted = [name for name, blocks in sections if common.quotes_traffic(to_md("", [(name, blocks)]), raw_sh)]
    if kinds or quoted:
        why = [f"something shaped like a {k.replace('_', ' ')}" for k in kinds] + [f"wording from real traffic in '{q}'" for q in quoted]
        sys.exit("report not written: it contains " + "; ".join(why) + ". Fix the analysis (paraphrase, describe ids in words) and run again.")
    out = work / "reports"
    out.mkdir(exist_ok=True)
    (out / f"{a.date}.md").write_text(md)
    (out / f"{a.date}.html").write_text(to_html(title, sub, sections))
    print(f"wrote {out / (a.date + '.html')} and {a.date}.md")


if __name__ == "__main__":
    main()
