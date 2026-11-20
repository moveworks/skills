#!/usr/bin/env python3
"""Pull one assistant's traffic from the Moveworks Data API into the work folder.

    rsi.sh pull                      window and assistant come from <work>/loop.json
    rsi.sh pull --days 7             override the window length
    rsi.sh pull --since 2026-01-05 --until 2026-01-19

What it does
  1. Seed scan: pages every INTERACTION_TYPE_FREE_TEXT row in the window (hour slices, $top=500 +
     $skip) and keeps the rows whose detail.agent_id / detail.agent_name match the assistant.
     The filter is client-side because the API does not filter on detail.* fields.
  2. Detail pull: for the matched conversation ids, pulls interactions, plugin-calls,
     plugin-resources and conversations with `conversation_id in (...)` batches.
  3. Derive: writes turns.jsonl, one row per user turn with the experience signals.

Output: <work>/raw/<since>_<until>/ with interactions.jsonl, plugin_calls.jsonl,
plugin_resources.jsonl, conversations.jsonl, turns.jsonl and manifest.json. A per-day seed cache
in <work>/raw/seed/ makes the next day's pull fetch only the new hours.

The output holds real user utterances and assistant replies. It must stay on this machine: never
commit it, never copy rows into an eval suite, a report or a repo. The users table (names, emails)
is deliberately never pulled.

Access
  Default: a bearer token read from the file named by DATA_API_TOKEN_FILE
  (default ~/.moveworks/data_api/token), sent to DATA_API_HOST (default https://api.moveworks.ai).
  The token is read only here and is never printed.
  Alternative: set RSI_FETCH_CMD to a command that reads {"table", "params"} as JSON on stdin and
  prints the Data API response JSON ({"value": [...]}) on stdout, for orgs that reach the Data API
  through their own gateway.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

PAGE = 500  # Data API hard cap per page
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"Data API {status}: {str(body)[:300]}")
        self.status = status


# ───────────────────────────── transports ─────────────────────────────

class DirectTransport:
    """Bearer-token calls straight to the Data API. The token never leaves this process."""

    def __init__(self, host):
        self.base = host.rstrip("/") + "/export/v1/records"
        path = Path(os.environ.get("DATA_API_TOKEN_FILE") or "~/.moveworks/data_api/token").expanduser()
        if not path.is_file():
            sys.exit(f"no Data API token file at {path} (set DATA_API_TOKEN_FILE, or RSI_FETCH_CMD)")
        self._token = path.read_text().strip()
        if not self._token:
            sys.exit(f"the Data API token file at {path} is empty")

    def get(self, table, params):
        url = f"{self.base}/{table}?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace"))


class CommandTransport:
    """Hands each page request to the user's own command (RSI_FETCH_CMD)."""

    def __init__(self, cmd):
        self.cmd = shlex.split(cmd)

    def get(self, table, params):
        p = subprocess.run(self.cmd, input=json.dumps({"table": table, "params": params}),
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise ApiError(502, f"fetch command exited {p.returncode}")
        try:
            body = json.loads(p.stdout)
        except ValueError:
            raise ApiError(502, "fetch command did not print JSON")
        if isinstance(body, dict) and "value" not in body and "@odata.context" in body:
            body["value"] = []          # an empty page can come back with the context and no "value" key
        if not isinstance(body, dict) or "value" not in body:
            raise ApiError(body.get("status_code", 502) if isinstance(body, dict) else 502, "no 'value' in the response")
        return body


# ───────────────────────────── paging ─────────────────────────────

class Puller:
    def __init__(self, transport, workers, min_interval):
        self.t, self.workers = transport, workers
        self.min_interval, self._last, self._lock = min_interval, 0.0, threading.Lock()
        self.calls = 0

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1

    def page(self, table, filt, skip=0, orderby="created_time asc"):
        params = {"$filter": filt, "$orderby": orderby, "$top": PAGE, "$skip": skip}
        delay = 5
        for attempt in range(6):
            self._throttle()
            try:
                return self.t.get(table, params).get("value") or []
            except ApiError as e:
                if e.status in (400, 401, 403, 404) or attempt == 5:
                    raise
            except Exception:
                if attempt == 5:
                    raise
            time.sleep(delay)          # 429 / 5xx / network: back off and retry
            delay = min(delay * 2, 60)

    def all(self, table, filt, orderby="created_time asc"):
        rows, skip = [], 0
        while True:
            got = self.page(table, filt, skip, orderby)
            rows += got
            if len(got) < PAGE:
                return rows
            skip += PAGE

    def map(self, fn, items):
        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(fn, items))


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".")
        s = f"{head}.{(frac + '000000')[:6]}"
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def parse_when(s):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00Z"
    return parse_ts(s)


def matches(row, assistant):
    d = row.get("detail") or {}
    if UUID_RE.match(assistant):
        return (d.get("agent_id") or "").lower() == assistant.lower()
    return (d.get("agent_name") or "").strip().lower() == assistant.strip().lower()


def dedupe(rows):
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


# ───────────────────────────── steps ─────────────────────────────

def seed_scan(p, assistant, since, until, seed_dir, slice_minutes, refresh):
    """Find the assistant's user turns. Cached per UTC day in seed_dir, keyed by assistant."""
    seed_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    matched, scanned = [], 0
    day = since.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < until:
        nxt = day + dt.timedelta(days=1)
        settled = nxt < now - dt.timedelta(hours=6)      # a finished day is scanned whole, once, and cached
        lo, hi = (day, nxt) if settled else (max(day, since), min(nxt, until))
        cache = seed_dir / f"{common.short_hash(assistant.lower(), 8)}_{day:%Y%m%d}.json"
        if settled and cache.exists() and not refresh:
            c = json.loads(cache.read_text())
        else:
            slices, t = [], lo
            while t < hi:
                slices.append((t, min(t + dt.timedelta(minutes=slice_minutes), hi)))
                t += dt.timedelta(minutes=slice_minutes)

            def one(s):
                rows = p.all("interactions",
                             f"created_time ge '{iso(s[0])}' and created_time lt '{iso(s[1])}'"
                             " and type eq 'INTERACTION_TYPE_FREE_TEXT'")
                return len(rows), [r for r in rows if matches(r, assistant)]

            res = p.map(one, slices)
            c = {"scanned": sum(n for n, _ in res), "rows": [r for _, rs in res for r in rs]}
            if settled:
                cache.write_text(json.dumps(c, ensure_ascii=False))
        scanned += c["scanned"]
        matched += [r for r in c["rows"] if since <= parse_ts(r["created_time"]) < until]
        print(f"  seed {day.date()}  free-text rows scanned {c['scanned']:>6}  matched {len(c['rows']):>5}", flush=True)
        day += dt.timedelta(days=1)
    return dedupe(matched), scanned


def detail_pull(p, conv_ids, batch):
    chunks = [conv_ids[i:i + batch] for i in range(0, len(conv_ids), batch)]

    def in_list(ids):
        return "(" + ",".join("'" + i.replace("'", "''") + "'" for i in ids) + ")"

    out = {}
    for name, table, col in (("interactions", "interactions", "conversation_id"),
                             ("plugin_calls", "plugin-calls", "conversation_id"),
                             ("plugin_resources", "plugin-resources", "conversation_id"),
                             ("conversations", "conversations", "id")):
        res = p.map(lambda ids: p.all(table, f"{col} in {in_list(ids)}"), chunks)
        rows = dedupe([r for rs in res for r in rs])
        rows.sort(key=lambda r: (r.get("conversation_id") or r.get("id"), r.get("created_time") or ""))
        out[name] = rows
        print(f"  {table:<17} {len(rows):>7} rows", flush=True)
    return out


# Literal openers written by machines, not people (scheduled runs, integrations that call the
# assistant). A turn that starts with one is tagged source="automated:<label>"; everything else is
# "interactive". They come from loop.json "automation_prefixes": {"<opener text>": "<label>"}.
CONNECT_LINK_RE = re.compile(r"\[Connect [^\]]+\]\(")   # the assistant asking the user to authorize a connector


def source_of(utterance, prefixes):
    head = utterance.lstrip()
    for prefix, label in prefixes.items():
        if head.startswith(prefix):
            return f"automated:{label}"
    return "interactive"


def source_of(utterance, prefixes):
    head = utterance.lstrip()
    for prefix, label in prefixes.items():
        if head.startswith(prefix):
            return f"automated:{label}"
    return "interactive"


def similar(a, b):
    """How alike two consecutive messages are. Callers that wrap every message in the same header make
    the openings match, so compare the opening and the ending and keep the lower score."""
    a, b = a.lower(), b.lower()
    head = difflib.SequenceMatcher(None, a[:400], b[:400]).ratio()
    tail = difflib.SequenceMatcher(None, a[-400:], b[-400:]).ratio()
    return round(min(head, tail), 2)


ABANDON_NOTE = ("abandoned = last user turn of its conversation and no positive signal after it "
                "(no HELPFUL feedback, no link click)")


def derive_turns(data, assistant, since, until, prefixes):
    """One row per user free-text turn handled by the assistant, with experience signals."""
    by_conv = collections.defaultdict(list)
    for r in data["interactions"]:
        by_conv[r["conversation_id"]].append(r)
    calls_by_ix = collections.defaultdict(list)
    for c in data["plugin_calls"]:
        calls_by_ix[c["interaction_id"]].append(c)
    res_by_call = collections.Counter(r["plugin_call_id"] for r in data["plugin_resources"])
    cited_by_call = collections.Counter(r["plugin_call_id"] for r in data["plugin_resources"] if r.get("cited"))
    conv_meta = {c["id"]: c for c in data["conversations"]}
    turns = []
    for cid, rows in by_conv.items():
        rows.sort(key=lambda r: r["created_time"])
        children = collections.defaultdict(list)
        for r in rows:
            if r.get("parent_interaction_id"):
                children[r["parent_interaction_id"]].append(r)
        user_turns = [r for r in rows if r["type"] == "INTERACTION_TYPE_FREE_TEXT"]
        for i, u in enumerate(user_turns):
            if not matches(u, assistant):
                continue
            t0 = parse_ts(u["created_time"])
            if not (since <= t0 < until):
                continue
            nxt = user_turns[i + 1] if i + 1 < len(user_turns) else None
            t_next = parse_ts(nxt["created_time"]) if nxt else None
            # everything hanging off this turn: direct children, and children of its bot replies
            kids = list(children.get(u["id"], []))
            bots = [k for k in kids if k["type"] == "INTERACTION_TYPE_BOT_MESSAGE"]
            for b in bots:
                kids += children.get(b["id"], [])
            # reactions also show up unparented, so take anything before the next user turn too
            window = [r for r in rows if r["actor"] == "user" and r["type"] != "INTERACTION_TYPE_FREE_TEXT"
                      and parse_ts(r["created_time"]) > t0 and (t_next is None or parse_ts(r["created_time"]) < t_next)]
            react = {r["id"]: r for r in kids + window if r["type"] != "INTERACTION_TYPE_BOT_MESSAGE"}.values()
            fb = [(r.get("detail") or {}).get("detail") for r in react
                  if r["type"] == "INTERACTION_TYPE_UIFORM_SUBMISSION" and r.get("label") == "feedback"]
            labels = [r.get("label") for r in react if r["type"] == "INTERACTION_TYPE_UIFORM_SUBMISSION"]
            calls = calls_by_ix.get(u["id"], [])
            bot_text = [((b.get("detail") or {}).get("content") or "") for b in bots]
            d = u.get("detail") or {}
            content = d.get("content") or ""
            next_content = ((nxt.get("detail") or {}).get("content") or "") if nxt else ""
            next_sim = similar(content, next_content) if nxt and content and next_content else None
            turns.append({
                "turn_id": u["id"], "conversation_id": cid, "user_id": u.get("user_id"),
                "created_time": u["created_time"], "platform": u.get("platform"),
                "platform_name": d.get("platform_name"), "agent_id": d.get("agent_id"),
                "agent_name": d.get("agent_name"), "domain": d.get("domain"), "entity": d.get("entity"),
                "route": (conv_meta.get(cid) or {}).get("route"),
                "primary_domain": (conv_meta.get(cid) or {}).get("primary_domain"),
                "turn_index": i, "turns_in_conversation": len(user_turns),
                "is_first_turn": i == 0, "is_last_turn": nxt is None,
                "source": source_of(content, prefixes),
                "utterance": content, "utterance_chars": len(content),
                "bot_messages": len(bots), "bot_chars": sum(len(t) for t in bot_text),
                "bot_reply": "\n\n".join(bot_text),
                # replies often arrive as progress or thinking messages followed by the answer: judge the last one
                "bot_final_reply": bot_text[-1] if bot_text else "",
                "no_bot_reply": not bots,
                "empty_bot_reply": bool(bots) and not any(t.strip() for t in bot_text),
                "latency_first_s": round((parse_ts(bots[0]["created_time"]) - t0).total_seconds(), 1) if bots else None,
                "latency_last_s": round((parse_ts(bots[-1]["created_time"]) - t0).total_seconds(), 1) if bots else None,
                "plugins": [c["plugin_name"] for c in calls],
                "plugins_unserved": [c["plugin_name"] for c in calls if not c.get("served")],
                "plugins_unused": [c["plugin_name"] for c in calls if not c.get("used")],
                "resources": sum(res_by_call[c["id"]] for c in calls),
                "resources_cited": sum(cited_by_call[c["id"]] for c in calls),
                "feedback": fb,
                "thumbs_up": "HELPFUL" in fb, "thumbs_down": "UNHELPFUL" in fb,
                "handoff": "handoff" in labels, "file_ticket": "file_ticket" in labels,
                "link_clicks": sum(1 for r in react if r["type"] == "INTERACTION_TYPE_LINK_CLICK"),
                "button_clicks": sum(1 for r in react if r["type"] == "INTERACTION_TYPE_BUTTON_CLICK"),
                "next_user_turn_gap_s": round((t_next - t0).total_seconds(), 1) if t_next else None,
                "next_utterance": next_content or None,
                "next_utterance_similarity": next_sim,
                "retried": bool(next_sim is not None and next_sim >= 0.6
                                and (t_next - t0).total_seconds() <= 1800),
                "connect_prompt": any(CONNECT_LINK_RE.search(t) for t in bot_text),
                "abandoned": nxt is None and "HELPFUL" not in fb
                             and not any(r["type"] == "INTERACTION_TYPE_LINK_CLICK" for r in react),
            })
    turns.sort(key=lambda t: t["created_time"])
    return turns


def summarize(turns):
    n = len(turns) or 1
    pct = lambda k: round(100 * sum(1 for t in turns if t[k]) / n, 1)
    lat = sorted(t["latency_last_s"] for t in turns if t["latency_last_s"] is not None)
    q = lambda f: lat[min(len(lat) - 1, int(f * len(lat)))] if lat else None
    plug = collections.Counter(pl for t in turns for pl in set(t["plugins"]))
    return {
        "turns": len(turns),
        "conversations": len({t["conversation_id"] for t in turns}),
        "users": len({t["user_id"] for t in turns}),
        "by_platform": dict(collections.Counter(t["platform"] for t in turns)),
        "by_source": dict(collections.Counter(t["source"] for t in turns)),
        "pct_retried": pct("retried"), "pct_connect_prompt": pct("connect_prompt"),
        "pct_thumbs_up": pct("thumbs_up"), "pct_thumbs_down": pct("thumbs_down"),
        "pct_no_bot_reply": pct("no_bot_reply"), "pct_empty_bot_reply": pct("empty_bot_reply"),
        "pct_abandoned": pct("abandoned"), "pct_handoff": pct("handoff"),
        "pct_no_plugin": round(100 * sum(1 for t in turns if not t["plugins"]) / n, 1),
        "pct_plugin_unused": round(100 * sum(1 for t in turns if t["plugins_unused"]) / n, 1),
        "latency_last_s": {"p50": q(0.5), "p90": q(0.9), "p99": q(0.99)},
        "top_plugins": dict(plug.most_common(40)),
        "abandoned_definition": ABANDON_NOTE,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", help="work folder (default $RSI_WORK or ./rsi-loop)")
    ap.add_argument("--days", type=int, help="window length back from --until (default loop.json window_days)")
    ap.add_argument("--since", help="window start, ISO date or timestamp (UTC); overrides --days")
    ap.add_argument("--until", help="window end, ISO date or timestamp (UTC); default now")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-interval", type=float, default=0.35, help="seconds between calls across all workers")
    ap.add_argument("--slice-minutes", type=int, default=60)
    ap.add_argument("--batch", type=int, default=15, help="conversation ids per `in (...)` filter")
    ap.add_argument("--refresh", action="store_true", help="ignore the per-day seed cache")
    a = ap.parse_args()

    work = common.work_dir(a.work)
    cfg = common.load_config(work)
    assistant = cfg["assistant"]
    until = parse_when(a.until) if a.until else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    since = parse_when(a.since) if a.since else until - dt.timedelta(days=a.days or cfg["window_days"])
    name = f"{since:%Y%m%dT%H%M}_{until:%Y%m%dT%H%M}"
    out = work / "raw" / name
    out.mkdir(parents=True, exist_ok=True)

    fetch = os.environ.get("RSI_FETCH_CMD")
    transport = CommandTransport(fetch) if fetch else DirectTransport(os.environ.get("DATA_API_HOST") or "https://api.moveworks.ai")
    p = Puller(transport, a.workers, a.min_interval)
    started = time.time()
    print(f"window {iso(since)} .. {iso(until)}  org={cfg['org']}  assistant={assistant}\n-> {out}", flush=True)

    seeds, scanned = seed_scan(p, assistant, since, until, work / "raw" / "seed", a.slice_minutes, a.refresh)
    conv_ids = sorted({r["conversation_id"] for r in seeds})
    print(f"seed: {len(seeds)} user turns in {len(conv_ids)} conversations (of {scanned} free-text rows)", flush=True)

    data = detail_pull(p, conv_ids, a.batch) if conv_ids else {
        "interactions": [], "plugin_calls": [], "plugin_resources": [], "conversations": []}
    for key, rows in data.items():
        common.write_jsonl(out / f"{key}.jsonl", rows)
    turns = derive_turns(data, assistant, since, until, dict(cfg.get("automation_prefixes") or {}))
    common.write_jsonl(out / "turns.jsonl", turns)

    manifest = {
        "org": cfg["org"], "assistant": assistant, "since": iso(since), "until": iso(until),
        "pulled_at": iso(dt.datetime.now(dt.timezone.utc)), "transport": "command" if fetch else "direct",
        "api_calls": p.calls, "seconds": round(time.time() - started),
        "free_text_rows_scanned": scanned,
        "rows": {**{k: len(v) for k, v in data.items()}, "turns": len(turns)},
        "summary": summarize(turns),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (work / "raw" / "LATEST").write_text(name + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
