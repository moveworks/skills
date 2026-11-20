#!/usr/bin/env python3
"""Sensitivity gate: turn sampled conversations into anonymized eval task candidates, or drop them.

    rsi.sh gate-test          gate 10 picks spread across the sample and show the rewrites (do this first)
    rsi.sh gate               full run

For each sampled conversation (sample.jsonl from sample.py):
  0. Pre-check, no LLM: a conversation with a <sensitive> tag anywhere is dropped unread.
  1. Gate (LLM, few-shot): drop when the request is about something sensitive (personal data,
     customer-confidential detail, private calendar or HR data, credentials, internal material) or
     is not a task; otherwise rewrite it as a standalone task with generic names and ids.
  2. Mechanical leak check on the rewrite: none of the terms the gate said it removed, no email,
     link, chat id, UUID or long number, and no run of the original wording (ten words in a row, or
     a whole short message) may appear in the task. One retry, then the conversation is dropped.
  3. Verify (second LLM call that sees only the rewrite, never the original): anything that still
     looks real or sensitive is dropped.
  4. A rewrite that matches one already kept (the same request with a different id) is dropped as a duplicate.
  Any error in any step drops the conversation (fail closed). Dropped picks are backfilled from the
  same cell's ranked reserves.

Writes, under <work>/sample/:
  tasks_candidates.jsonl  task_id, rewritten task, success criteria, placeholders, strata tags, weight.
                          No original text and no source ids.
  audit.jsonl             conversation_id, turn_id, decision, drop_reason, stage, backfill_round, task_id. Nothing else.
  strata.md               gains an "After the sensitivity gate" section: kept and dropped per cell and per reason.
  gate_test.md            (--limit runs) the test results, rewritten text only.
The original utterance is read from the pull, sent to the LLM once, and never written anywhere.

Few-shot examples come from templates/fewshot_gate.json, or from <work>/fewshot_gate.json when that
file exists (copy the template there to add corrected examples; every example must be invented).
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import llm  # noqa: E402
from common import read_jsonl, write_jsonl  # noqa: E402

DROP_REASONS = ["none", "pii", "customer_confidential", "sensitive_tagged", "private_calendar_hr", "credentials",
                "internal_confidential", "not_a_task"]
VERIFY_ISSUES = ["none", "real_identifier", "pii", "customer_confidential", "private_calendar_hr", "credentials"]
KINDS = ["person", "customer", "team", "issue_id", "project", "channel", "document", "date", "other"]

GATE_SYSTEM = """You are the sensitivity gate for an eval suite built from real traffic to an enterprise AI assistant.
You see one user request (plus earlier user turns for context). Decide whether it can become an eval task, and if so rewrite it. Nothing sensitive may reach the suite. When unsure, drop.

DROP (decision "drop") when the request itself is about, or cannot be stated without, any of these:
- pii: personal data beyond a work name: personal phone, personal email, home address, government id, health, family, compensation, date of birth.
- customer_confidential: confidential detail about a customer or deal: contract or renewal terms, pricing, revenue, churn risk, an incident or outage at a named customer, customer data, quotes from customer conversations, account strategy.
- sensitive_tagged: any content inside or marked with a <sensitive> tag.
- private_calendar_hr: the request CONTAINS private calendar or HR data: what was said in a meeting or 1:1, notes about a person, a performance review or self-review text, feedback about a named person, hiring, staffing, leave or compensation detail. A generic request that only asks the assistant to read the requester's own calendar, mail or messages ("what's on my calendar tomorrow", a daily brief routine) contains no such data: keep it and set runtime_data.
- credentials: passwords, tokens, API keys, secrets, cookies, session ids, or a request to use or reveal one.
- internal_confidential: the request pastes or depends on the substance of internal material that is not public (a memo, plan, pricing, reorg, financials, security detail, an unreleased launch) and would be meaningless with that substance replaced.
- not_a_task: greetings, thanks, tests, fragments that cannot be made into a standalone request even with the context, or text that is not asking the assistant to do anything.

KEEP (decision "keep") when the job being asked for is ordinary and only its identifiers are specific. Then rewrite:
- Write one standalone request in the user's voice and register, same job, same level of detail, same length class. Fold in the earlier turns it depends on.
- Use your own words. Never reuse the original sentence, even when it holds nothing sensitive: say the same thing differently.
- Replace every real person with a generic name (Jordan Lee, Sam Rivera, Alex Chen, Taylor Morgan), every customer or company with a generic one (Acme Corp, Northwind, Contoso), every ticket, issue, escalation or record id with DEMO-101 style ids, every channel, document title and link with a generic description, every internal codename or unreleased project name with a plain description of what it is ("the search revamp project"). Exact dates become relative ("last week").
- The proper name of an internal project, initiative, program, team or unreleased feature is a codename: never carry it over, describe it instead, even when it sounds harmless.
- Pasted internal content (a status update, a message, notes, a list of work items) is never reproduced or paraphrased. Replace it with short invented content of the same shape, so the job is the same and none of the facts are.
- Keep names of public products and common tools (Slack, Notion, Linear, Salesforce, ServiceNow) and generic process words.
- Do not copy distinctive phrasing, numbers or lists from the original. Do not include anything the assistant replied.
- A customer or person name used only as a lookup key is fine to replace and keep. A request whose substance is the confidential detail is a drop, even if you could blur it.
- Machine-written requests (scheduled routines, orchestrator calls) are tasks too: keep what it instructs, reword and shorten the boilerplate, replace identifiers.

Output:
- removed_terms: every real name, id, codename, channel, link or number you replaced or left out (used for a leak check, never stored).
- task: the rewritten request ("" on drop).
- success_criteria: one sentence on what a good answer does, written from the request alone, generic ("" on drop).
- placeholders: each generic value you put in the task and its kind, so fixtures can be bound to it later.
- runtime_data: what private data the assistant would read if this task were run for a real user: "calendar_mail" (the requester's calendar, mail, messages or meeting notes), "hr" (people or org data beyond a directory lookup), "customer" (customer or account records), else "none". Tasks that are not "none" must only ever be run against simulated systems or a test user.
- If you are told a previous rewrite leaked, rewrite again with none of the removed terms and no link, email, or raw id.

Examples (all invented):
{examples}"""

VERIFY_SYSTEM = """You are the last check before an eval task enters a shareable suite. You see only a rewritten task, not its source.
Say whether it is clean. It is NOT clean if it contains or is about any of:
- real_identifier: anything that looks like a real person, customer, company, internal codename, private channel, ticket id, link or email. These generic stand-ins are expected and fine: Jordan Lee, Sam Rivera, Alex Chen, Taylor Morgan, Acme Corp, Northwind, Contoso, DEMO-123 style ids, role or team descriptions, and public product names (Slack, Notion, Linear, Salesforce, ServiceNow, Moveworks).
- pii: personal data beyond a work name.
- customer_confidential: contract, pricing, revenue, churn, incident or account-strategy detail about a customer.
- private_calendar_hr: actual calendar or meeting contents, 1:1 notes, performance or feedback about a person, hiring, staffing, compensation. A generic request to read the requester's own calendar or mail, with no such content in it, is fine.
- credentials: any secret or a request to use one.
You may also get carried_phrases: quoted or capitalized phrases that were copied verbatim from the original. Mark the task not clean (real_identifier) if any of them is the proper name of an internal project, initiative, program, codename, team, customer or person. Status words (At Risk, In Progress), public product and company names, field labels, and generic routine titles like 'Daily Brief' are fine.
When unsure, say not clean.

Examples:
{examples}"""

RUNTIME = ["none", "calendar_mail", "hr", "customer"]
GATE_SCHEMA = {"type": "object", "required": ["decision", "drop_reason", "removed_terms", "task", "success_criteria", "placeholders", "runtime_data"],
               "properties": {"decision": {"type": "string", "enum": ["keep", "drop"]},
                              "drop_reason": {"type": "string", "enum": DROP_REASONS},
                              "removed_terms": {"type": "array", "items": {"type": "string"}},
                              "task": {"type": "string"}, "success_criteria": {"type": "string"},
                              "runtime_data": {"type": "string", "enum": RUNTIME},
                              "placeholders": {"type": "array", "items": {"type": "object", "required": ["value", "kind"], "properties": {
                                  "value": {"type": "string"}, "kind": {"type": "string", "enum": KINDS}}}}}}
VERIFY_SCHEMA = {"type": "object", "required": ["clean", "issue"], "properties": {
    "clean": {"type": "boolean"}, "issue": {"type": "string", "enum": VERIFY_ISSUES}}}

# Shapes that are never acceptable in a stored task, whatever the model said.
STRUCTURAL = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "link": re.compile(r"https?://|www\."),
    "chat_id": re.compile(r"\b[UCDGW][A-Z0-9]{8,11}\b"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-", re.I),
    "long_number": re.compile(r"\d{6,}"),
}


def leak(task_text, removed_terms, source_sh):
    low = task_text.lower()
    if common.quotes_traffic(task_text, source_sh):
        return "source_wording"
    for name, rx in STRUCTURAL.items():
        if rx.search(task_text):
            return name
    for term in removed_terms:
        t = (term or "").strip().lower()
        if len(t) >= 3 and t in low:
            return "removed_term"
    return None


def is_duplicate(text, kept_texts):
    """The same request with a different id is one eval task."""
    n = re.sub(r"\s+", " ", text.lower())
    return any(difflib.SequenceMatcher(None, n, k).ratio() >= 0.8 for k in kept_texts)


def carried_phrases(task_text, raw):
    """Quoted or Title Case phrases in the rewrite that also appear verbatim in the original."""
    c = set(re.findall(r"['\"]([^'\"\n]{3,60})['\"]", task_text))
    c |= {m.group(1) for m in re.finditer(r"\b([A-Z][A-Za-z0-9]+(?:[ -][A-Z][A-Za-z0-9]+)+)", task_text)}
    return sorted(x for x in c if x in raw)[:25]


def gate_one(row, conv_turns, systems, models):
    """Returns (audit_row, task_or_None). Never raises; any failure is a drop."""
    cid, tid = row["conversation_id"], row["turn_id"]
    task_id = "t-" + common.short_hash("rsi:" + tid, 10)
    audit = {"conversation_id": cid, "turn_id": tid, "decision": "drop", "drop_reason": None, "stage": None, "task_id": None}

    def drop(reason, stage):
        audit.update(drop_reason=reason, stage=stage)
        return audit, None

    turns = sorted(conv_turns, key=lambda t: t["created_time"])
    anchor = next((t for t in turns if t["turn_id"] == tid), None)
    if anchor is None:
        return drop("gate_error", "precheck")
    if any("<sensitive" in ((t.get("utterance") or "") + (t.get("bot_reply") or "")).lower() for t in turns):
        return drop("sensitive_tagged", "precheck")
    context = [t["utterance"][:600] for t in turns if t["created_time"] < anchor["created_time"]][-3:]
    u = anchor["utterance"]
    source_sh = common.message_shingles([t.get("utterance") for t in turns])
    req = {"context": context, "utterance": u if len(u) <= 5000 else u[:2500] + "\n[...]\n" + u[-2500:]}
    fail = None
    for attempt in (1, 2):
        try:
            g = llm.call(systems["gate"], json.dumps(req, ensure_ascii=False), GATE_SCHEMA, model=models["gate"])
        except llm.LlmError:
            return drop("gate_error", "gate")
        if g["decision"] != "keep":
            return drop(g["drop_reason"] if g["drop_reason"] != "none" else "not_a_task", "gate")
        task_text = (g.get("task") or "").strip()
        if g["drop_reason"] != "none" or len(task_text) < 8:
            return drop(g["drop_reason"] if g["drop_reason"] != "none" else "not_a_task", "gate")
        found = leak(task_text + " " + g.get("success_criteria", ""), g.get("removed_terms") or [], source_sh)
        if found:
            fail = f"leak_check:{found}"
            req["previous_rewrite_leaked"] = (
                "Your last rewrite reused the original wording. Write it again in different words: same job, same detail."
                if found == "source_wording" else
                f"Your last rewrite still contained a {found.replace('_', ' ')}. Rewrite it again.")
            continue
        try:
            v = llm.call(systems["verify"], json.dumps({"task": task_text, "success_criteria": g.get("success_criteria", ""),
                                                        "carried_phrases": carried_phrases(task_text, u)}),
                         VERIFY_SCHEMA, model=models["verify"])
        except llm.LlmError:
            return drop("gate_error", "verify")
        if v["clean"] and v["issue"] == "none":
            fail = None
            break
        fail = "verify"
        req["previous_rewrite_leaked"] = (f"A reviewer flagged your last rewrite ({v['issue']}): it still carried a real name or "
                                          "real content from the original. Rewrite it again with every proper name described "
                                          "generically and any pasted content replaced by invented content.")
    if fail:
        return drop("residual_sensitive", fail)
    audit.update(decision="keep", drop_reason=None, stage="verify", task_id=task_id)
    task = {"task_id": task_id, "task": task_text, "success_criteria": g.get("success_criteria", "").strip(),
            "placeholders": g.get("placeholders") or [], "runtime_data": g.get("runtime_data", "none"), "strata": row["strata"],
            "poor_signals": row.get("poor_signals") or [], "tools_observed": row.get("tools") or [],
            "_cell_population": row.get("cell_population")}
    return audit, task


MARK = "## After the sensitivity gate"


def append_summary(sdir, rows, audits, tasks):
    """Add (or replace) the post-gate section of strata.md: counts and labels only."""
    cell = lambda r: (r["strata"].get("audience"), r["strata"]["tool_family"], r["strata"]["experience"])
    by_id = {r["conversation_id"]: r for r in rows}
    reasons = collections.Counter(a["drop_reason"] for a in audits if a["decision"] == "drop")
    pop, sampled, gated, kept = {}, collections.Counter(), collections.Counter(), collections.Counter(cell(t) for t in tasks)
    for r in rows:
        pop[cell(r)] = r.get("cell_population")
        sampled[cell(r)] += bool(r["selected"])
    for a in audits:
        gated[cell(by_id[a["conversation_id"]])] += 1
    explain = {"not_a_task": "greeting, thanks, test ping or a fragment with no standalone request",
               "sensitive_tagged": "a <sensitive> tag anywhere in the conversation; dropped unread",
               "duplicate_task": "same request as a kept task once ids were replaced",
               "residual_sensitive": "the rewrite still held a removed term or the original wording, or the second reviewer flagged it",
               "customer_confidential": "confidential customer or deal detail",
               "internal_confidential": "depends on pasted internal material that cannot be replaced", "pii": "personal data beyond a work name",
               "private_calendar_hr": "private calendar or HR content in the request", "credentials": "a secret in the request",
               "gate_error": "the model call failed; dropped to be safe"}
    L = [MARK, "", f"Gated {len(audits)} conversations (the sample plus reserves used as backfill): kept {len(tasks)}, dropped {len(audits) - len(tasks)}.", "",
         "| Drop reason | Dropped | What it means |", "|---|---|---|"]
    L += [f"| {k} | {n} | {explain.get(k, '')} |" for k, n in reasons.most_common()]
    rd = collections.Counter(t.get("runtime_data", "none") for t in tasks)
    L += ["", "Kept tasks that would read private data when run (run these only against simulated systems or a test user): " +
          ", ".join(f"{k} {n}" for k, n in rd.most_common() if k != "none") + ".", "",
          "| Audience | Tool family | Experience | Population | Sampled | Gated | Kept |", "|---|---|---|---|---|---|---|"]
    for c in sorted(pop, key=lambda c: (c[0] != "people", -(pop[c] or 0))):
        L.append(f"| {c[0]} | {c[1]} | {c[2]} | {pop[c]} | {sampled[c]} | {gated[c]} | {kept[c]} |")
    L.append(f"| **Total** | | | | {sum(sampled.values())} | {len(audits)} | {len(tasks)} |")
    path = sdir / "strata.md"
    head = path.read_text().split(MARK)[0].rstrip() if path.exists() else ""
    path.write_text(head + "\n\n" + "\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    ap.add_argument("--raw", help="pull folder (default: the latest pull)")
    ap.add_argument("--limit", type=int, help="test mode: gate this many picks spread across the sample, print them, write gate_test.md only")
    ap.add_argument("--gate-model", default="sonnet")
    ap.add_argument("--verify-model", default="sonnet")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--backfill-rounds", type=int, default=2)
    ap.add_argument("--summary-only", action="store_true", help="rebuild the post-gate section of strata.md from the files on disk")
    a = ap.parse_args()

    work = common.work_dir(a.work)
    common.load_config(work)
    sdir = work / "sample"
    rows = read_jsonl(sdir / "sample.jsonl")
    if not rows:
        sys.exit(f"no sample in {sdir}/sample.jsonl; run `rsi.sh sample` first")
    if a.summary_only:
        append_summary(sdir, rows, read_jsonl(sdir / "audit.jsonl"), read_jsonl(sdir / "tasks_candidates.jsonl"))
        return
    need = {r["conversation_id"] for r in rows}
    by_conv = collections.defaultdict(list)
    for t in read_jsonl(common.latest_raw(work, a.raw) / "turns.jsonl"):
        if t["conversation_id"] in need:
            by_conv[t["conversation_id"]].append(t)
    own = work / "fewshot_gate.json"
    shots = json.loads((own if own.exists() else common.KIT / "templates" / "fewshot_gate.json").read_text())
    ex = "\n\n".join("INPUT " + json.dumps({"context": e["context"], "utterance": e["utterance"]}) +
                     "\nOUTPUT " + json.dumps(e["output"]) for e in shots["examples"])
    vex = "\n".join(json.dumps({k: e[k] for k in ("task", "carried_phrases") if k in e}) + " -> " + json.dumps({"clean": e["clean"], "issue": e["issue"]})
                    for e in shots["verify_examples"])
    systems = {"gate": GATE_SYSTEM.replace("{examples}", ex), "verify": VERIFY_SYSTEM.replace("{examples}", vex)}
    models = {"gate": a.gate_model, "verify": a.verify_model}
    run = lambda r: gate_one(r, by_conv.get(r["conversation_id"], []), systems, models)
    cell = lambda r: (r["strata"].get("audience"), r["strata"]["tool_family"], r["strata"]["experience"])

    primary = [r for r in rows if r["selected"]]
    if a.limit:
        step = max(1, len(primary) // a.limit)
        test = primary[::step][:a.limit]
        res = llm.map_calls(run, test, a.workers)
        lines = [f"# Sensitivity gate: test on {len(test)}", "",
                 "Rewritten text only. The originals are not shown or stored.", "",
                 "| # | Audience | Tool family | Utterance type | Experience | Decision | Reason | Rewritten task |", "|---|---|---|---|---|---|---|---|"]
        for i, (r, (au, t)) in enumerate(zip(test, res), 1):
            s = r["strata"]
            lines.append(f"| {i} | {s.get('audience')} | {s['tool_family']} | {s['utterance_type']} | {s['experience']} | {au['decision']} | "
                         f"{au['drop_reason'] or ''} ({au['stage']}) | {(t['task'] if t else '').replace('|', '/').replace(chr(10), ' ')} |")
        (sdir / "gate_test.md").write_text("\n".join(lines) + "\n")
        print("\n".join(lines))
        return

    audits, tasks = [], []
    target = collections.Counter(cell(r) for r in primary)
    kept = collections.Counter()
    reserves = collections.defaultdict(list)
    for r in sorted((r for r in rows if not r["selected"]), key=lambda r: r["reserve_rank"] or 0):
        reserves[cell(r)].append(r)
    batch, seen_texts, dup_cells = primary, [], set()
    for rnd in range(a.backfill_rounds + 1):
        print(f"round {rnd}: gating {len(batch)}", flush=True)
        for r, (au, t) in zip(batch, llm.map_calls(run, batch, a.workers)):
            au["backfill_round"] = rnd
            audits.append(au)
            if t and is_duplicate(t["task"], seen_texts):
                au.update(decision="drop", drop_reason="duplicate_task", stage="dedupe", task_id=None)
                dup_cells.add(cell(r))
            elif t:
                tasks.append(t); kept[cell(r)] += 1
                seen_texts.append(re.sub(r"\s+", " ", t["task"].lower()))
        batch = []
        for c, want in target.items():
            if c in dup_cells:          # its reserves are more copies of the same request
                continue
            for _ in range(max(0, want - kept[c])):
                if reserves[c]:
                    batch.append(reserves[c].pop(0))
        if not batch:
            break
    per_cell = collections.Counter(cell(t) for t in tasks)
    for t in tasks:
        pop = t.pop("_cell_population")
        t["weight"] = round(pop / per_cell[cell(t)], 2) if pop else None
    tasks.sort(key=lambda t: (cell(t), t["task_id"]))
    write_jsonl(sdir / "tasks_candidates.jsonl", tasks)
    write_jsonl(sdir / "audit.jsonl", audits)
    append_summary(sdir, rows, audits, tasks)
    reasons = collections.Counter((x["drop_reason"], x["stage"]) for x in audits if x["decision"] == "drop")
    print(f"gated {len(audits)}: kept {len(tasks)}, dropped {len(audits) - len(tasks)}")
    for (reason, stage), n in reasons.most_common():
        print(f"  dropped {n:3d}  {reason}  (at {stage})")
    short = {c: (kept[c], want) for c, want in target.items() if kept[c] < want}
    if short:
        print("cells still short after backfill (kept/target): " + "; ".join(f"{' x '.join(map(str, c))} {k}/{w}" for c, (k, w) in short.items()))


if __name__ == "__main__":
    main()
