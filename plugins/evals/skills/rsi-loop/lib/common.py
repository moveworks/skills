"""Helpers shared by the rsi-loop scripts: the work folder, the loop config and the leak checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9 _-]*>")

DEFAULTS = {
    "org": "", "assistant": "", "assistant_name": "", "window_days": 14, "sample_size": 100, "id_prefix": "rsi",
    "persona": "an employee using a work assistant",
    "automation_prefixes": {}, "human_sources": ["interactive"],
    "simulators": {}, "no_system_families": ["no tool"], "max_suite_tasks": 300, "run_approval": "ask",
    "trace_org": "",
}


def read_jsonl(p):
    p = Path(p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def write_jsonl(p, rows):
    Path(p).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def short_hash(s, n=12):
    return hashlib.sha256(str(s).encode()).hexdigest()[:n]


def work_dir(arg=None):
    """The work folder holds real traffic. It ignores itself in git so nothing in it can be committed."""
    w = Path(arg or os.environ.get("RSI_WORK") or "rsi-loop").expanduser().resolve()
    if KIT == w or KIT in w.parents:
        sys.exit("the work folder cannot be inside the skill's own directory; pick another folder")
    w.mkdir(parents=True, exist_ok=True)
    gi = w / ".gitignore"
    if not gi.exists():
        gi.write_text("# rsi-loop work folder: sampled traffic, suites and reports stay local\n*\n")
    return w


def load_config(work, need=("org", "assistant")):
    path = work / "loop.json"
    if not path.exists():
        sys.exit(f"no loop.json in {work}; run `rsi.sh init` first")
    cfg = {**DEFAULTS, **json.loads(path.read_text())}
    for k in need:
        if not str(cfg.get(k) or "").strip() or PLACEHOLDER.search(str(cfg[k])):
            sys.exit(f"loop.json: '{k}' is not filled in")
    return cfg


def latest_raw(work, arg=None):
    """Folder of the most recent pull (raw/LATEST names it)."""
    if arg:
        return Path(arg)
    ptr = work / "raw" / "LATEST"
    if not ptr.exists():
        sys.exit(f"no pull found under {work}/raw; run `rsi.sh pull` first")
    return work / "raw" / ptr.read_text().strip()


# Shapes that must never appear in anything the loop writes for sharing (suite, analysis, report).
LEAK_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@(?!example\.(?:com|org)\b)[\w-]+\.[\w.-]+"),
    "link": re.compile(r"(?:https?://|www\.)(?!marketplace\.moveworks\.com/api/purple-suite|docs\.moveworks\.com|placeholder\.invalid)\S+"),
    "chat_id": re.compile(r"\b[UCDGW][A-Z0-9]{8,11}\b"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    "long_number": re.compile(r"\b\d{9,}\b"),
    "sensitive_tag": re.compile(r"</?sensitive", re.I),
    "credential": re.compile(r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY)"),
}


def leak_kinds(text, skip=()):
    """Names of the leak shapes found in text (never the matched text itself)."""
    return sorted(k for k, rx in LEAK_PATTERNS.items() if k not in skip and rx.search(text))


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def shingles(text, k=10):
    w = _words(text)
    return {" ".join(w[i:i + k]) for i in range(len(w) - k + 1)}


def message_shingles(texts, k=10):
    """What counts as quoting these messages: every k-word run, plus every whole message of
    5 to k-1 words (most chat messages are short)."""
    out = set()
    for text in texts:
        w = _words(text)
        if 5 <= len(w) < k:
            out.add(" ".join(w))
        out |= shingles(text, k)
    return out


def raw_shingles(raw_dir, k=10):
    """The quote index for the pulled traffic. Used to prove a file repeats none of it."""
    turns = read_jsonl(Path(raw_dir) / "turns.jsonl")
    return message_shingles([t.get(f) for t in turns for f in ("utterance", "bot_reply")], k)


def quotes_traffic(text, raw_sh, k=10):
    """True when text repeats a k-word run, or a whole short message, from real traffic."""
    if not raw_sh:
        return False
    return any(not shingles(text, n).isdisjoint(raw_sh) for n in range(5, k + 1))


# ───────────────────────────── the wording check (a hard step of the gate) ─────────────────────────────
# A task keeps the user's intent, never the user's words. A gated task may not share
#   * a run of RUN_CONV (8) or more words with its own source conversation (user and assistant turns), or
#   * a run of RUN_ANY (10) or more words with any message in the pull, or
#   * its whole text with a message of its source conversation (short tasks).

RUN_CONV, RUN_ANY = 8, 10
TURN_TEXT_FIELDS = ("utterance", "bot_reply", "bot_final_reply", "next_utterance")


def turn_texts(turns):
    return [t.get(f) or "" for t in turns for f in TURN_TEXT_FIELDS]


class WordingIndex:
    """Built once per gate run from the pull: per-conversation texts and the 10-word runs of every message."""

    def __init__(self, turns):
        by = {}
        for t in turns:
            by.setdefault(t.get("conversation_id"), []).append(t)
        self.conv = {cid: turn_texts(ts) for cid, ts in by.items()}
        self.any_runs = set()
        for texts in self.conv.values():
            for x in texts:
                self.any_runs |= shingles(x, RUN_ANY)
        self._conv_runs = {}

    def conv_runs(self, cid):
        if cid not in self._conv_runs:
            runs = set()
            for x in self.conv.get(cid, []):
                runs |= shingles(x, RUN_CONV)
            self._conv_runs[cid] = runs
        return self._conv_runs[cid]


def shared_runs(text, runs, n):
    """The maximal stretches of text whose every n-word window is in runs (the reused wording)."""
    w, out, i = _words(text), [], 0
    while i <= len(w) - n:
        if " ".join(w[i:i + n]) in runs:
            j = i + n
            while j < len(w) and " ".join(w[j - n + 1:j + 1]) in runs:
                j += 1
            out.append(" ".join(w[i:j]))
            i = j
        else:
            i += 1
    return out


def wording_violations(index, cid, *texts):
    """Reused source wording in any of texts, longest first. Empty means the wording check passes.
    The phrases go back to the rewording model only; never print or store them."""
    conv_runs, found = index.conv_runs(cid), []
    for text in texts:
        found += shared_runs(text, conv_runs, RUN_CONV) + shared_runs(text, index.any_runs, RUN_ANY)
    whole = " ".join(_words(texts[0])) if texts else ""
    if 3 <= len(whole.split()) < RUN_CONV and any(f" {whole} " in f" {' '.join(_words(x))} " for x in index.conv.get(cid, [])):
        found.append(whole)                          # a whole short message, reused
    return sorted(set(found), key=len, reverse=True)


def display_name(cfg):
    """A name for reports: never the assistant's id."""
    name = str(cfg.get("assistant_name") or "").strip()
    if name:
        return name
    a = str(cfg["assistant"])
    return "Assistant" if LEAK_PATTERNS["uuid"].search(a) else a
