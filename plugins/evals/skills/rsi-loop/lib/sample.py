#!/usr/bin/env python3
"""Stratified sample of one assistant's conversations for an eval suite.

    rsi.sh sample              sample size comes from <work>/loop.json (sample_size)
    rsi.sh sample --n 150

Reads turns.jsonl from the latest pull and writes, under <work>/sample/:
  taxonomy.json    utterance types induced once from the traffic and then frozen, so per-type
                   numbers stay comparable run over run (--reinduce to rebuild). Invented examples only.
  labels.jsonl     per conversation: utterance_type + reply_outcome (ids and labels, no text).
  sample.jsonl     the picked conversations and ranked reserves: ids, strata tags, weight. No text.
  population.json  rates per stratum for the whole window (what the daily report shows). No text.
  strata.md        population vs sample per cell, and the marginals.

Three pivots
  capability   tool family (the system the turn called most) and utterance type (LLM, few-shot)
  experience   poor vs good, from thumbs down, no reply, retry, an auth wall, a hand-off, a reply
               that says it could not do the job (LLM), or a slow reply (>= p90 for its traffic class)
  audience     a person asking vs a machine caller (scheduled runs, integrations); channel is the finer cut

Allocation
  --people-share of the sample goes to people. Cells are audience x tool family x experience. Every
  non-empty cell gets --min-per-cell, poor cells share --poor-share of the rest in proportion to
  population, good cells share what is left. Inside a cell, picks are greedy toward the utterance
  types, channels and poor reasons still under --min-per-margin, with one conversation per distinct
  prompt and a cap per user. Each pick carries weight = cell population / cell sample, so pass rates
  can be weighted back to traffic.

Nothing written here holds utterance or reply text. user_id is hashed before it is written.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import llm  # noqa: E402
from common import read_jsonl, write_jsonl  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
POOR_ORDER = ["thumbs_down", "no_reply", "auth_wall", "retried", "reasoning_leak", "could_not_complete", "handoff", "slow"]
REPLY_OUTCOMES = ["answered", "partial", "could_not_complete", "reasoning_leak", "asked_clarification", "declined", "no_reply"]
POOR_OUTCOMES = ("could_not_complete", "reasoning_leak")


h = common.short_hash


def prompt_key(text):
    """Same prompt, different day or number -> same key. Used only to avoid sampling repeats."""
    t = re.sub(r"\d+", "0", (text or "").lower())
    return h(re.sub(r"\s+", " ", t).strip())


def excerpt(text, head, tail):
    """Machine-written prompts open with boilerplate and end with the ask: keep both ends."""
    text = text or ""
    return text if len(text) <= head + tail + 20 else text[:head] + "\n[...]\n" + text[-tail:]


WRITE_VERBS = ("create", "update", "save", "send", "add", "publish", "delete", "remove", "set", "move", "post",
               "append", "attach", "reassign", "reject", "run", "ensure", "transfer", "schedule")


def system_of(plugin):
    """mcp_<System>__<tool> -> System; a bare uuid -> 'unnamed plugin'; anything else is its own family."""
    if UUID_RE.match(plugin):
        return "unnamed plugin"
    m = re.match(r"mcp_(.+?)__(.+)$", plugin)
    return m.group(1).replace("_", " ") if m else plugin


def is_write(plugin):
    # slack_send_message, notion-create-pages, update_issue: the verb is in the first two words
    parts = re.split(r"[_-]", plugin.split("__", 1)[1]) if "__" in plugin else []
    return any(p in WRITE_VERBS for p in parts[:2])


def channel_of(t):
    if t["source"] != "interactive":
        return t["source"]
    return t.get("platform") or "unknown"


# ───────────────────────────── conversations ─────────────────────────────

def hard_signals(t):
    s = []
    if t.get("thumbs_down"): s.append("thumbs_down")
    if t.get("no_bot_reply") or t.get("empty_bot_reply"): s.append("no_reply")
    if t.get("retried"): s.append("retried")
    if t.get("connect_prompt"): s.append("auth_wall")
    if t.get("handoff") or t.get("file_ticket"): s.append("handoff")
    return s


def build_conversations(turns, settled_before):
    by = collections.defaultdict(list)
    for t in turns:
        by[t["conversation_id"]].append(t)
    # slow = at or over p90 of its own traffic class (people wait; routines do not)
    lat = collections.defaultdict(list)
    for t in turns:
        if t.get("latency_last_s") is not None:
            lat["interactive" if t["source"] == "interactive" else "automated"].append(t["latency_last_s"])
    p90 = {k: sorted(v)[int(0.9 * len(v))] for k, v in lat.items() if v}
    convs = []
    for cid, ts in by.items():
        ts.sort(key=lambda t: t["created_time"])
        for t in ts:
            t["_sig"] = hard_signals(t)
            k = "interactive" if t["source"] == "interactive" else "automated"
            if t.get("latency_last_s") is not None and k in p90 and t["latency_last_s"] >= p90[k]:
                t["_sig"].append("slow")
            # a recent turn may still be running, or its reply may not be exported yet
            if "no_reply" in t["_sig"] and t["created_time"][:19] >= settled_before:
                t["_sig"].remove("no_reply")
        # anchor = first turn with a hard signal, else the opening turn
        anchor = next((t for t in ts if any(s != "slow" for s in t["_sig"])), ts[0])
        convs.append({"conversation_id": cid, "anchor": anchor, "turns": ts})
    return convs, p90


def tool_families(convs, lump_below):
    """Family = the system the anchor turn called most (ties go to the less common system)."""
    freq = collections.Counter(n for c in convs for n in {system_of(p) for p in (c["anchor"].get("plugins") or [])})
    for c in convs:
        plugins = c["anchor"].get("plugins") or []
        k = collections.Counter(system_of(p) for p in plugins)
        c["tools"] = sorted(k, key=lambda n: -k[n])
        c["tool_family"] = max(k, key=lambda n: (k[n], -freq[n])) if k else "no tool"
        c["action"] = "write" if any(is_write(p) for p in plugins) else "read-only"
    fam = collections.Counter(c["tool_family"] for c in convs)
    for c in convs:
        if fam[c["tool_family"]] < lump_below and c["tool_family"] != "no tool":
            c["tool_family"] = "other tools"


# ───────────────────────────── LLM: taxonomy + labels ─────────────────────────────

TAXONOMY_SYSTEM = """You design an utterance-type taxonomy for one enterprise AI assistant, from a sample of what its users asked.
Group by what the user wants the assistant to DO (the job), not by topic or by which system is involved.
Return 6 to 10 types that are mutually exclusive and together cover the sample; include a catch-all only if needed.
Each type: a short kebab-case id, a one-sentence definition, and two example utterances.
The examples MUST be invented by you. Never copy or paraphrase closely any utterance you were shown, and never include
real person names, customer or company names, ticket ids, project codenames, links or numbers from the sample.
Type ids and definitions must be generic too (no names, no codenames)."""

TAXONOMY_SCHEMA = {"type": "object", "required": ["types"], "properties": {"types": {"type": "array", "items": {
    "type": "object", "required": ["id", "definition", "examples"], "properties": {
        "id": {"type": "string"}, "definition": {"type": "string"},
        "examples": {"type": "array", "items": {"type": "string"}}}}}}}

LABEL_SYSTEM = """You label conversations with an enterprise AI assistant. For each item you get the user's utterance and the start of the assistant's reply.

The reply shown is the assistant's final message for the turn.

utterance_type: pick exactly one id from the taxonomy below, by what the user wants the assistant to do.
{taxonomy}

reply_outcome: judge only what the reply text shows.
- answered: the reply does what was asked, or gives a direct, substantive answer.
- partial: it gives part of what was asked and says the rest is missing.
- could_not_complete: it says it could not do it: an error, no access, a tool failed, a system is not connected, or it only tells the user to go do it themselves. A search that ran fine and found nothing to report is NOT this; that is answered.
- reasoning_leak: the final message is itself the assistant's own planning notes or thinking out loud ("I need to make sure my final output...", "Let me check..."), with no answer addressed to the user.
- asked_clarification: it asks the user a question before doing anything.
- declined: it refuses on policy or scope grounds.
- no_reply: there is no reply text.

Examples (invented):
utterance "what shipped for search last week?" / reply "Three changes shipped: ..." -> answered
utterance "summarize open escalations for my area" / reply "I found 4 of them; I could not read the tracker for the other team, so those are missing." -> partial
utterance "update the status on that project to at risk" / reply "I wasn't able to update it. The request returned a permissions error. You can change it directly in the tool." -> could_not_complete
utterance "draft the weekly update" / reply "Which week and which team should this cover?" -> asked_clarification
utterance "pull the notes from my last 1:1" / reply "To continue, connect your account: Connect Notes" -> could_not_complete
utterance "[scheduled run] post the morning digest" / reply "" -> no_reply
utterance "[scheduled run] remind me of tickets due today" / reply "No tickets are due today, so there is nothing to send." -> answered (a correct nothing-to-send run)
utterance "[scheduled run] daily brief" / reply "Good morning. Calendar: not available, the calendar system is not connected. News: not available." -> could_not_complete
utterance "[scheduled run] weekly analytics recap" / reply "**Finalizing the summary** I need to keep the final output concise and make sure the links are formatted. I should double check the numbers before..." -> reasoning_leak
utterance "run the triage skill on this ticket and return only the JSON verdict" / reply "{"verdict":"needs_info","reason":"..."}" -> answered

Return one label object per item, with the same i."""


def final_reply(t):
    """The last bot message is the answer; earlier ones are progress or thinking notes."""
    return t["bot_final_reply"] if "bot_final_reply" in t else (t.get("bot_reply") or "")


def induce_taxonomy(convs, model, rng, k=140):
    by_ch = collections.defaultdict(list)
    for c in convs:
        by_ch[c["channel"]].append(c)
    pick, seen = [], set()
    per = max(8, k // max(1, len(by_ch)))
    for ch, cs in by_ch.items():
        rng.shuffle(cs)
        for c in cs:
            if c["prompt_key"] not in seen and len([p for p in pick if p["channel"] == ch]) < per:
                seen.add(c["prompt_key"]); pick.append(c)
    body = "\n".join(f"- [{c['channel']}] {excerpt(c['anchor']['utterance'], 150, 250).replace(chr(10), ' ')}" for c in pick[:k])
    out = llm.call(TAXONOMY_SYSTEM, "Sample of user utterances:\n" + body, TAXONOMY_SCHEMA, model=model, timeout=400)
    return out["types"]


def label_all(convs, taxonomy, model, cache_path, workers, batch=20):
    cache = {r["conversation_id"]: r for r in read_jsonl(cache_path)}
    ids = [t["id"] for t in taxonomy]
    todo = [c for c in convs if c["conversation_id"] not in cache or cache[c["conversation_id"]].get("turn_id") != c["anchor"]["turn_id"]
            or cache[c["conversation_id"]]["utterance_type"] not in ids]
    tax = "\n".join(f"- {t['id']}: {t['definition']} e.g. \"{t['examples'][0]}\"" for t in taxonomy)
    schema = {"type": "object", "required": ["labels"], "properties": {"labels": {"type": "array", "items": {
        "type": "object", "required": ["i", "utterance_type", "reply_outcome"], "properties": {
            "i": {"type": "integer"}, "utterance_type": {"type": "string", "enum": ids},
            "reply_outcome": {"type": "string", "enum": REPLY_OUTCOMES}}}}}}
    system = LABEL_SYSTEM.replace("{taxonomy}", tax)

    def run(chunk):
        items = [{"i": i, "utterance": excerpt(c["anchor"]["utterance"], 400, 500),
                  "reply": excerpt(final_reply(c["anchor"]), 500, 300)} for i, c in enumerate(chunk)]
        try:
            out = llm.call(system, json.dumps(items, ensure_ascii=False), schema, model=model)
        except llm.LlmError:
            return []
        rows = []
        for l in out.get("labels", []):
            if isinstance(l.get("i"), int) and 0 <= l["i"] < len(chunk):
                c = chunk[l["i"]]
                rows.append({"conversation_id": c["conversation_id"], "turn_id": c["anchor"]["turn_id"],
                             "utterance_type": l["utterance_type"], "reply_outcome": l["reply_outcome"]})
        return rows

    chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
    print(f"labelling {len(todo)} conversations in {len(chunks)} calls ({len(cache)} cached)", flush=True)
    for rows in llm.map_calls(run, chunks, workers):
        for r in rows:
            cache[r["conversation_id"]] = r
    live = {c["conversation_id"] for c in convs}
    write_jsonl(cache_path, [r for k, r in cache.items() if k in live])
    return cache


# ───────────────────────────── allocation ─────────────────────────────

def largest_remainder(budget, sizes, caps):
    """Split budget across keys in proportion to sizes, never over caps."""
    alloc = {k: 0 for k in sizes}
    budget = min(budget, sum(caps.values()))
    while budget > 0:
        open_ = {k: sizes[k] for k in sizes if alloc[k] < caps[k]}
        if not open_:
            break
        tot = sum(open_.values())
        quota = {k: budget * v / tot for k, v in open_.items()}
        step = {k: min(int(q), caps[k] - alloc[k]) for k, q in quota.items()}
        if sum(step.values()) == 0:
            for k in sorted(open_, key=lambda k: -(quota[k] - int(quota[k])))[:budget]:
                step[k] = 1
        for k, v in step.items():
            alloc[k] += v
        budget -= sum(step.values())
    return alloc


def allocate(cells, n, min_per_cell, poor_share, people_share):
    """Cell key = (audience, tool_family, experience). Budget splits by audience first, then by experience."""
    caps = {k: len({c["prompt_key"] for c in v}) for k, v in cells.items()}
    alloc = {k: min(min_per_cell, caps[k]) for k in cells}
    for aud, a_share in (("people", people_share), ("machine", 1 - people_share)):
        mine = [k for k in cells if k[0] == aud]
        rest = max(0, round(n * a_share) - sum(alloc[k] for k in mine))
        for exp, share in (("poor", poor_share), ("good", 1 - poor_share)):
            keys = [k for k in mine if k[2] == exp]
            extra = largest_remainder(round(rest * share), {k: len(cells[k]) for k in keys},
                                      {k: caps[k] - alloc[k] for k in keys})
            for k, v in extra.items():
                alloc[k] += v
    return alloc


def select(cells, alloc, rng, min_per_margin, user_cap, reserves):
    margins = ("utterance_type", "channel", "poor_reason", "action")
    pop = {m: collections.Counter(c[m] for cs in cells.values() for c in cs if c[m]) for m in margins}
    need = {m: {v: min(min_per_margin, n) for v, n in pop[m].items()} for m in margins}
    got = {m: collections.Counter() for m in margins}
    per_user, used_prompts, picked = collections.Counter(), set(), []

    def gain(c):
        return sum(max(0, need[m].get(c[m], 0) - got[m][c[m]]) for m in margins if c[m])

    def take(c, why):
        c["selected"], c["pick_reason"] = True, why
        picked.append(c); used_prompts.add(c["prompt_key"]); per_user[c["user"]] += 1
        for m in margins:
            if c[m]: got[m][c[m]] += 1

    # rarest cells first, so they get first call on the scarce margins
    for key in sorted(cells, key=lambda k: len(cells[k])):
        pool = cells[key][:]
        rng.shuffle(pool)
        for _ in range(alloc[key]):
            ok = [c for c in pool if not c.get("selected") and c["prompt_key"] not in used_prompts]
            capped = [c for c in ok if per_user[c["user"]] < user_cap] or ok
            if not capped:
                break
            take(max(capped, key=gain), "cell")       # max is stable, pool is shuffled: ties are random
    # top up any margin still short
    everyone = [c for cs in cells.values() for c in cs]
    rng.shuffle(everyone)
    for m in margins:
        for v, want in need[m].items():
            for c in everyone:
                if got[m][v] >= want:
                    break
                if c[m] == v and not c.get("selected") and c["prompt_key"] not in used_prompts:
                    take(c, f"margin:{m}")
    # ranked reserves per cell, for backfilling what the sensitivity gate drops
    for key, cs in cells.items():
        rank = 0
        for c in sorted(cs, key=lambda c: rng.random()):
            if not c.get("selected") and c["prompt_key"] not in used_prompts and rank < reserves:
                rank += 1
                c["reserve_rank"] = rank; used_prompts.add(c["prompt_key"])
    return picked


# ───────────────────────────── report ─────────────────────────────

def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def strata_md(convs, picked, taxonomy, meta):
    L = []
    N, n = len(convs), len(picked)
    poor_pop = sum(1 for c in convs if c["experience"] == "poor")
    poor_s = sum(1 for c in picked if c["experience"] == "poor")
    L += [f"# Strata: {meta['assistant']} ({meta['org']}), {meta['window']}", "",
          f"Population: {N} conversations ({meta['turns']} user turns, {meta['users']} users). Sample: {n} conversations.",
          f"Poor experience is {pct(poor_pop, N)} of traffic and {pct(poor_s, n)} of the sample, on purpose: the loop is looking for what to fix. "
          "Each sampled conversation carries a weight (cell population / cell sample) so pass rates can be weighted back to real traffic.", "",
          "No utterance or reply text is in this file, only labels and counts.", "",
          "## Audience x capability (tool family) x experience", "",
          "Audience is the third pivot: a person asking versus a machine caller (scheduled runs and integrations that call the assistant). "
          "Tool family is the system the turn called most. Rows are sorted by traffic within each audience.", "",
          "| Audience | Tool family | Share of audience | Poor rate | Good: population | Good: sample | Poor: population | Poor: sample |",
          "|---|---|---|---|---|---|---|---|"]
    def cnt(rows, **kw):
        return sum(1 for c in rows if all(c[k] == v for k, v in kw.items()))
    for aud in ("people", "machine"):
        A = [c for c in convs if c["audience"] == aud]
        if not A:
            continue
        for f, tot in collections.Counter(c["tool_family"] for c in A).most_common():
            kw = dict(audience=aud, tool_family=f)
            L.append(f"| {aud} | {f} | {pct(tot, len(A))} | {pct(cnt(convs, experience='poor', **kw), tot)} | "
                     f"{cnt(convs, experience='good', **kw)} | {cnt(picked, experience='good', **kw)} | "
                     f"{cnt(convs, experience='poor', **kw)} | {cnt(picked, experience='poor', **kw)} |")
        ap_, as_ = cnt(A, experience="poor"), cnt(picked, audience=aud, experience="poor")
        ns = cnt(picked, audience=aud)
        L.append(f"| **{aud}** | **all ({pct(len(A), N)} of traffic)** | 100% | {pct(ap_, len(A))} | {len(A) - ap_} | {ns - as_} | {ap_} | {as_} |")
    L.append(f"| **Total** | | | {pct(poor_pop, N)} | {N - poor_pop} | {n - poor_s} | {poor_pop} | {poor_s} |")

    def margin(title, key, note, rows_filter=lambda c: True, defs=None, share=False):
        P = [c for c in convs if rows_filter(c)]; S = [c for c in picked if rows_filter(c)]
        L.extend(["", f"## {title}", "", note, "",
                  "| " + title.split(" (")[0].split(",")[0] + " | Population | " + ("Share" if share else "Poor rate") + " | Sample |" + (" Definition |" if defs else ""),
                  "|---|---|---|---|" + ("---|" if defs else "")])
        for v, tot in collections.Counter(c[key] for c in P).most_common():
            poor = sum(1 for c in P if c[key] == v and c["experience"] == "poor")
            mid = pct(tot, len(P)) if share else pct(poor, tot)
            L.append(f"| {v} | {tot} | {mid} | {sum(1 for c in S if c[key] == v)} |" + (f" {defs.get(v, '')} |" if defs else ""))

    margin("Capability (utterance type)", "utterance_type",
           "What the user wanted done. Types were induced from the traffic by an LLM, then frozen in `taxonomy.json`; each conversation is labelled few-shot.",
           defs={t["id"]: t["definition"] for t in taxonomy})
    margin("Channel", "channel", "Who is asking and where: people on each chat surface, versus each kind of machine caller.")
    margin("Poor experience, by first signal", "poor_reason",
           "One reason per poor conversation, the first that applies in this order: " + ", ".join(POOR_ORDER) +
           f". Slow means at or over the 90th percentile for its traffic class ({meta['p90']}).",
           rows_filter=lambda c: c["experience"] == "poor", share=True)
    margin("Tool action", "action", "Whether the turn called a tool that changes something (create, update, send, publish) or only read.")
    margin("Conversation depth", "depth", "Single question versus a back-and-forth.")
    L += ["", "## How the sample was drawn", "",
          f"- {int(meta['people_share'] * 100)}% of the sample goes to conversations a person started, the rest to machine callers.",
          f"- Cells are audience x tool family x experience. Every non-empty cell gets at least {meta['min_per_cell']}; within an audience, poor cells then share {int(meta['poor_share'] * 100)}% of the rest in proportion to traffic, good cells the remainder.",
          f"- Inside a cell, picks lean toward utterance types, channels and poor reasons that are still under {meta['min_per_margin']} in the sample.",
          f"- One conversation per distinct prompt (scheduled runs repeat the same prompt daily) and at most {meta['user_cap']} conversations per user.",
          "- Because a repeated prompt is one eval task, a cell can show 0 sampled when its prompts were already picked through another cell.",
          "- The eval task is anchored on the first turn that shows a poor signal, or the opening turn when none does.",
          f"- Seed {meta['seed']}. Up to {meta['reserves']} ranked reserves per cell backfill whatever the sensitivity gate drops."]
    return "\n".join(L) + "\n"


def population(convs, meta):
    """Rates per stratum over every conversation in the window. Labels and numbers only."""
    def block(rows, key):
        out = {}
        for v, n in collections.Counter(c[key] for c in rows).most_common():
            poor = sum(1 for c in rows if c[key] == v and c["experience"] == "poor")
            out[str(v)] = {"n": n, "share": round(n / len(rows), 4), "poor_rate": round(poor / n, 4)}
        return out
    poor = [c for c in convs if c["experience"] == "poor"]
    by_day = collections.defaultdict(list)
    for c in convs:
        by_day[c["anchor"]["created_time"][:10]].append(c)
    cells = collections.Counter((c["audience"], c["tool_family"]) for c in convs)
    return {
        **meta, "conversations": len(convs),
        "poor_rate": {"rate": round(len(poor) / len(convs), 4), "n": len(convs)},
        "by": {k: block(convs, k) for k in ("audience", "tool_family", "utterance_type", "channel", "action", "depth")},
        "audience_x_tool_family": [
            {"audience": a, "tool_family": f, "n": n,
             "poor_rate": round(sum(1 for c in convs if (c["audience"], c["tool_family"]) == (a, f) and c["experience"] == "poor") / n, 4)}
            for (a, f), n in cells.most_common()],
        "poor_reasons": {r: {"n": n, "share_of_poor": round(n / len(poor), 4), "rate": round(n / len(convs), 4)}
                         for r, n in collections.Counter(c["poor_reason"] for c in poor).most_common()},
        "by_day": {d: {"n": len(cs), "poor_rate": round(sum(1 for c in cs if c["experience"] == "poor") / len(cs), 4)}
                   for d, cs in sorted(by_day.items())},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    ap.add_argument("--raw", help="pull folder (default: the latest pull)")
    ap.add_argument("--n", type=int, help="sample size (default loop.json sample_size)")
    ap.add_argument("--min-per-cell", type=int, default=2)
    ap.add_argument("--min-per-margin", type=int, default=4)
    ap.add_argument("--poor-share", type=float, default=0.55)
    ap.add_argument("--people-share", type=float, default=0.65, help="share of the sample for conversations a person started")
    ap.add_argument("--user-cap", type=int, default=6)
    ap.add_argument("--reserves", type=int, default=6)
    ap.add_argument("--lump-below", type=int, default=0, help="tool families under this many conversations fold into 'other tools' (default 1%% of traffic, min 8)")
    ap.add_argument("--settle-hours", type=float, default=24, help="a turn newer than this at pull time is not counted as 'no reply'")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--label-model", default="sonnet")
    ap.add_argument("--relabel", action="store_true", help="ignore labels.jsonl and label every conversation again")
    ap.add_argument("--taxonomy-model", default="sonnet")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--reinduce", action="store_true", help="rebuild taxonomy.json (breaks comparability with earlier runs)")
    a = ap.parse_args()

    work = common.work_dir(a.work)
    cfg = common.load_config(work)
    raw, out = common.latest_raw(work, a.raw), work / "sample"
    a.n = a.n or int(cfg["sample_size"])
    out.mkdir(parents=True, exist_ok=True)
    turns = read_jsonl(raw / "turns.jsonl")
    turns = [t for t in turns if (t.get("utterance") or "").strip()]
    if not turns:
        sys.exit(f"no turns in {raw}/turns.jsonl; run `rsi.sh pull` first")
    man = json.loads((raw / "manifest.json").read_text()) if (raw / "manifest.json").exists() else {}
    pulled = man.get("pulled_at") or max(t["created_time"] for t in turns)
    settled_before = (dt.datetime.strptime(pulled[:19], "%Y-%m-%dT%H:%M:%S") - dt.timedelta(hours=a.settle_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rng = random.Random(a.seed)
    convs, p90 = build_conversations(turns, settled_before)
    tool_families(convs, a.lump_below or max(8, len(convs) // 100))
    humans = set(cfg.get("human_sources") or ["interactive"])
    for c in convs:
        an = c["anchor"]
        c["channel"] = channel_of(an)
        c["audience"] = "people" if an["source"] in humans else "machine"
        c["prompt_key"] = prompt_key(an["utterance"])
        c["user"] = h(an.get("user_id"))
        c["depth"] = "single turn" if len(c["turns"]) == 1 else "multi-turn"

    tax_path = out / "taxonomy.json"
    if tax_path.exists() and not a.reinduce:
        taxonomy = json.loads(tax_path.read_text())["types"]
    else:
        taxonomy = induce_taxonomy(convs, a.taxonomy_model, rng)
        if common.leak_kinds(json.dumps(taxonomy)):
            sys.exit("the induced taxonomy contains something that looks like a real identifier; run again with --reinduce")
        tax_path.write_text(json.dumps({"types": taxonomy}, indent=2) + "\n")
        print(f"taxonomy: {[t['id'] for t in taxonomy]}")
    if a.relabel and (out / "labels.jsonl").exists():
        (out / "labels.jsonl").unlink()
    labels = label_all(convs, taxonomy, a.label_model, out / "labels.jsonl", a.workers)
    missing = 0
    for c in convs:
        l = labels.get(c["conversation_id"])
        missing += l is None
        c["utterance_type"] = l["utterance_type"] if l else "unlabelled"
        c["reply_outcome"] = l["reply_outcome"] if l else "unknown"
        sig = set(c["anchor"]["_sig"])
        if c["reply_outcome"] in POOR_OUTCOMES:
            sig.add(c["reply_outcome"])
        c["poor_signals"] = [s for s in POOR_ORDER if s in sig]
        c["experience"] = "poor" if sig else "good"
        c["poor_reason"] = c["poor_signals"][0] if sig else None
    if missing:
        print(f"warning: {missing} conversations could not be labelled", file=sys.stderr)

    cells = collections.defaultdict(list)
    for c in convs:
        cells[(c["audience"], c["tool_family"], c["experience"])].append(c)
    alloc = allocate(cells, a.n, a.min_per_cell, a.poor_share, a.people_share)
    picked = select(cells, alloc, rng, a.min_per_margin, a.user_cap, a.reserves)
    cell_n = collections.Counter((c["audience"], c["tool_family"], c["experience"]) for c in picked)

    rows = []
    for c in sorted(convs, key=lambda c: (not c.get("selected"), c["audience"], c["tool_family"], c["experience"], c.get("reserve_rank", 0))):
        if not (c.get("selected") or c.get("reserve_rank")):
            continue
        key = (c["audience"], c["tool_family"], c["experience"])
        rows.append({"conversation_id": c["conversation_id"], "turn_id": c["anchor"]["turn_id"],
                     "selected": bool(c.get("selected")), "reserve_rank": c.get("reserve_rank"),
                     "pick_reason": c.get("pick_reason"),
                     "strata": {"audience": c["audience"], "tool_family": c["tool_family"], "utterance_type": c["utterance_type"],
                                "experience": c["experience"], "poor_reason": c["poor_reason"],
                                "channel": c["channel"], "action": c["action"], "depth": c["depth"]},
                     "poor_signals": c["poor_signals"], "reply_outcome": c["reply_outcome"], "tools": c["tools"],
                     "user": c["user"], "day": c["anchor"]["created_time"][:10],
                     "cell_population": len(cells[key]),
                     "weight": round(len(cells[key]) / cell_n[key], 2) if cell_n[key] else None})
    write_jsonl(out / "sample.jsonl", rows)

    days = sorted({t["created_time"][:10] for t in turns})
    meta = {"assistant": common.display_name(cfg), "org": cfg["org"],
            "window": f"{(man.get('since') or days[0])[:10]} to {(man.get('until') or days[-1])[:10]} (traffic present {days[0]} to {days[-1]})",
            "turns": len(turns), "users": len({t.get("user_id") for t in turns}),
            "p90": ", ".join(f"{k} {v:.0f} s" for k, v in p90.items()), "min_per_cell": a.min_per_cell,
            "min_per_margin": a.min_per_margin, "poor_share": a.poor_share, "user_cap": a.user_cap,
            "seed": a.seed, "reserves": a.reserves, "people_share": a.people_share}
    (out / "strata.md").write_text(strata_md(convs, picked, taxonomy, meta))
    pop = population(convs, {"assistant": common.display_name(cfg), "org": cfg["org"], "since": (man.get("since") or days[0])[:10],
                             "until": (man.get("until") or days[-1])[:10], "turns": len(turns), "p90_latency_s": p90})
    (out / "population.json").write_text(json.dumps(pop, indent=1) + "\n")
    print(f"population {len(convs)} conversations, sample {len(picked)}, reserves {sum(1 for r in rows if not r['selected'])}")
    print(f"wrote {out}/strata.md, sample.jsonl, population.json, labels.jsonl, taxonomy.json")


if __name__ == "__main__":
    main()
