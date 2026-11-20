#!/usr/bin/env bash
# rsi-loop helper: sample an assistant's traffic, build a clean eval suite from it, run it, and report.
# Needs bash, python3 (standard library only), and for the eval steps curl and jq (see ../moveworks-eval-api).
#
# Usage: rsi.sh <command> [args]
#   init --org ORG --assistant ID_OR_NAME [--name DISPLAY_NAME] [--days 14] [--n 100]
#                         create the work folder, loop.json and the eval config templates
#   pull [--days N | --since D --until D]    Data API -> <work>/raw/ (real traffic; never leaves this machine)
#   sample [--n N]        stratified sample -> <work>/sample/strata.md, sample.jsonl, population.json
#   gate-test [N]         sensitivity gate on N picks (default 10); shows the rewrites only
#   gate                  sensitivity gate on the whole sample -> sample/tasks_candidates.jsonl
#   build [--replace]     task suite -> <work>/suite/ (append-only; fails on any leak)
#   check                 is the Eval API reachable with the saved token?
#   dry-run               server-side validation of every task file; creates nothing
#   run pilot|all         start the eval run(s). Needs the user's explicit yes first; this script does not ask.
#   summarize [DATE]      per-tag and per-stratum pass rates -> runs/<date>/summary.json
#   packets [DATE]        what failed, with traces -> analysis/<date>.packets.json
#   analysis-check [DATE] validate analysis/<date>.json (coverage, required fields, no leaks)
#   report [DATE]         the daily report -> reports/<date>.html and .md
#   daily                 pull, sample, gate, build, dry-run, then run + summarize if a standing yes is on file,
#                         then packets and a report
#   leak-scan FILE...     scan any file for emails, links, ids and quoted traffic before it leaves the machine
#   status                what exists in the work folder
#
# Settings (environment variables):
#   RSI_WORK              work folder (default ./rsi-loop). It ignores itself in git.
#   DATA_API_TOKEN_FILE   file holding the Data API token (default ~/.moveworks/data_api/token). Read only by lib/pull.py.
#   DATA_API_HOST         default https://api.moveworks.ai
#   RSI_FETCH_CMD         optional: your own command for Data API pages (see lib/pull.py)
#   RSI_LLM_CMD           optional: your own command for the labelling and gate model calls (see lib/llm.py)
#   EVAL_TOKEN_FILE, EVAL_HOST   passed through to the Eval API helper
#   EVAL_CONFIG           eval config to run with (default <work>/suite/config.yaml)
#   RUN_DATE              date folder for runs (default today)
#   RSI_PYTHON            python interpreter (default python3)
set -euo pipefail

KIT=$(cd "$(dirname "$0")" && pwd)
EVAL_SH=${EVAL_SH:-$KIT/../moveworks-eval-api/eval.sh}
PY=${RSI_PYTHON:-python3}
WORK=${RSI_WORK:-$PWD/rsi-loop}
export RSI_WORK=$WORK
RUN_DATE=${RUN_DATE:-$(date +%F)}
CONFIG=${EVAL_CONFIG:-$WORK/suite/config.yaml}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
py() { local script=$1; shift; "$PY" "$KIT/lib/$script" "$@"; }
need_eval() { [[ -f $EVAL_SH ]] || die "Eval API helper not found at $EVAL_SH (set EVAL_SH)"; }
need_config() {
  [[ -f $CONFIG ]] || die "no eval config at $CONFIG: copy one of $WORK/suite/config.*.yaml to it and fill it in"
}
task_files() { ls "$WORK"/suite/tasks/ready-*.yaml 2> /dev/null || true; }

cmd_init() {
  local org="" assistant="" name="" days=14 n=100
  while [[ $# -gt 0 ]]; do
    case $1 in
      --org) org=$2; shift 2 ;;
      --assistant) assistant=$2; shift 2 ;;
      --name) name=$2; shift 2 ;;
      --days) days=$2; shift 2 ;;
      --n) n=$2; shift 2 ;;
      *) die "init: unknown option $1" ;;
    esac
  done
  [[ -n $org && -n $assistant ]] || die "init needs --org and --assistant"
  case $WORK/ in "$KIT"/*) die "the work folder cannot be inside the skill's own directory" ;; esac
  mkdir -p "$WORK/suite"
  [[ -f $WORK/.gitignore ]] || printf '# rsi-loop work folder: sampled traffic, suites and reports stay local\n*\n' > "$WORK/.gitignore"
  if [[ -f $WORK/loop.json ]]; then
    echo "kept the existing $WORK/loop.json"
  else
    ORG=$org ASSISTANT=$assistant NAME=$name DAYS=$days N=$n "$PY" - "$KIT/templates/loop.json" "$WORK/loop.json" << 'PYEOF'
import json, os, sys
cfg = json.load(open(sys.argv[1]))
cfg.update(org=os.environ["ORG"], assistant=os.environ["ASSISTANT"], assistant_name=os.environ["NAME"],
           window_days=int(os.environ["DAYS"]), sample_size=int(os.environ["N"]))
with open(sys.argv[2], "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PYEOF
    echo "wrote $WORK/loop.json"
  fi
  local f
  for f in config.mocked.yaml config.talk-only.yaml; do
    [[ -f $WORK/suite/$f ]] || cp "$KIT/templates/$f" "$WORK/suite/$f"
  done
  echo "work folder ready: $WORK (git-ignored). Next: rsi.sh pull"
}

run_batch() { # run_batch FILE
  local f=$1 b out
  b=$(basename "$f" .yaml)
  out=$WORK/runs/$RUN_DATE/$b
  if [[ -s $out/report.json ]]; then echo "skip $b: report already saved"; return 0; fi
  if [[ -s $out/run_id ]]; then
    echo "resume $b: run $(cat "$out/run_id")"
  else
    OUT_DIR=$out bash "$EVAL_SH" create "$CONFIG" "$f"
  fi
  [[ -s $out/run_id ]] || die "no run id for $b; stopping"
  OUT_DIR=$out bash "$EVAL_SH" status "$(cat "$out/run_id")" --wait
}

cmd_dry_run() {
  need_eval; need_config
  local f files; files=$(task_files)
  [[ -n $files ]] || die "no task files in $WORK/suite/tasks; run: rsi.sh build"
  for f in $files; do
    OUT_DIR=$WORK/runs/$RUN_DATE/dry-$(basename "$f" .yaml) bash "$EVAL_SH" dry-run "$CONFIG" "$f"
  done
}

cmd_run() {
  need_eval; need_config
  local f files; files=$(task_files)
  [[ -n $files ]] || die "no task files in $WORK/suite/tasks; run: rsi.sh build"
  mkdir -p "$WORK/runs/$RUN_DATE"
  case ${1:-} in
    pilot) run_batch "$WORK/suite/tasks/ready-00-pilot.yaml" ;;
    all) for f in $files; do run_batch "$f"; done ;;
    *) die "run needs 'pilot' or 'all'" ;;
  esac
  py summarize.py "$RUN_DATE"
}

cmd_daily() {
  py pull.py
  py sample.py
  py gate.py
  py build_suite.py
  local approval
  approval=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("run_approval", "ask"))' "$WORK/loop.json")
  if [[ -f $CONFIG && -f $EVAL_SH ]]; then
    cmd_dry_run
    if [[ $approval == standing ]]; then
      cmd_run all
    else
      echo "not starting the eval run: loop.json run_approval is '$approval'. After a yes, run: rsi.sh run all"
    fi
  else
    echo "no eval config at $CONFIG yet: skipping the dry run and the eval run"
  fi
  py analyze.py packets "$RUN_DATE"
  py report.py "$RUN_DATE"
  echo "next: write $WORK/analysis/$RUN_DATE.json from the packets, then: rsi.sh analysis-check && rsi.sh report"
}

cmd_leak_scan() {
  [[ $# -gt 0 ]] || die "leak-scan needs at least one file"
  "$PY" - "$KIT/lib" "$WORK" "$@" << 'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import common
work = Path(sys.argv[2])
ptr = work / "raw" / "LATEST"
raw = common.raw_shingles(work / "raw" / ptr.read_text().strip()) if ptr.exists() else set()
bad = 0
for f in sys.argv[3:]:
    text = Path(f).read_text(errors="replace")
    found = common.leak_kinds(text) + (["quoted_traffic"] if common.quotes_traffic(text, raw) else [])
    print(f"{'LEAK' if found else 'clean'}  {f}  {' '.join(found)}")
    bad += bool(found)
if not raw:
    print("note: no pulled traffic found, so the quoted-traffic check did not run")
sys.exit(1 if bad else 0)
PYEOF
}

cmd_status() {
  echo "work folder: $WORK"
  [[ -f $WORK/loop.json ]] || { echo "  not initialised (rsi.sh init)"; return 0; }
  "$PY" - "$WORK" << 'PYEOF'
import json, sys
from pathlib import Path
w = Path(sys.argv[1])
cfg = json.load(open(w / "loop.json"))
print(f"  org {cfg.get('org')}  assistant {cfg.get('assistant')}  window {cfg.get('window_days')} days  sample {cfg.get('sample_size')}  run approval: {cfg.get('run_approval', 'ask')}")
latest = (w / "raw" / "LATEST").read_text().strip() if (w / "raw" / "LATEST").exists() else None
print(f"  latest pull: {latest or 'none'}")
lines = lambda p: sum(1 for l in open(p) if l.strip()) if p.exists() else 0
print(f"  sample: {lines(w / 'sample' / 'sample.jsonl')} rows; gated task candidates: {lines(w / 'sample' / 'tasks_candidates.jsonl')}")
print(f"  suite: {lines(w / 'suite' / 'manifest.jsonl')} tasks; config.yaml {'present' if (w / 'suite' / 'config.yaml').exists() else 'missing'}")
for name in ("runs", "analysis", "reports"):
    d = w / name
    items = sorted(p.name for p in d.iterdir()) if d.exists() else []
    print(f"  {name}: {', '.join(items[-5:]) or 'none'}")
PYEOF
}

cmd=${1:-}
[[ $# -gt 0 ]] && shift
case $cmd in
  init) cmd_init "$@" ;;
  pull) py pull.py "$@" ;;
  sample) py sample.py "$@" ;;
  gate-test) py gate.py --limit "${1:-10}" ;;
  gate) py gate.py "$@" ;;
  build) py build_suite.py "$@" ;;
  check) need_eval; bash "$EVAL_SH" check ;;
  dry-run) cmd_dry_run ;;
  run) cmd_run "$@" ;;
  summarize) py summarize.py "$@" ;;
  packets) py analyze.py packets "$@" ;;
  analysis-check) py analyze.py check "$@" ;;
  report) py report.py "$@" ;;
  daily) cmd_daily ;;
  leak-scan) cmd_leak_scan "$@" ;;
  status) cmd_status ;;
  -h | --help | help | "") sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$cmd' (rsi.sh --help)" ;;
esac
