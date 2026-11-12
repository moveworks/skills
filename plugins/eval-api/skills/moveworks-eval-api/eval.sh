#!/usr/bin/env bash
# Moveworks Eval API (v1alpha1) helper. Needs bash, curl and jq.
#
#   eval.sh check                         is the token accepted and the API enabled?
#   eval.sh catalog [WORD]                simulated systems and the collection names each accepts
#   eval.sh dry-run CONFIG TASK_FILE...   upload the task files and validate everything; creates nothing
#   eval.sh create  CONFIG TASK_FILE...   upload, validate, and start the run; prints the run id
#   eval.sh status  RUN_ID [--wait]       where the run is; --wait polls every 30 s and fetches the report at the end
#   eval.sh report  RUN_ID                fetch the report and print a summary
#   eval.sh transcript RUN_ID [TASK_ID]   each conversation, turn by turn, with tool calls
#   eval.sh cancel  RUN_ID                stop a queued or running run
#   eval.sh list                          your recent runs
#
# Settings (environment variables):
#   EVAL_HOST        API host for your data center (default https://api.moveworks.ai)
#   EVAL_TOKEN_FILE  file holding your access token (default ~/.moveworks/eval_api/token)
#   OUT_DIR          where request and response files are saved (default ./eval-out/<timestamp>)
#
# The token is read from EVAL_TOKEN_FILE on every call and handed to curl on stdin. It is never
# put on a command line, printed, or written anywhere else. This script does not create tokens.
set -euo pipefail

EVAL_HOST=${EVAL_HOST:-https://api.moveworks.ai}
EVAL_TOKEN_FILE=${EVAL_TOKEN_FILE:-$HOME/.moveworks/eval_api/token}
BASE="$EVAL_HOST/rest/v1alpha1"
CATALOG_URL=https://marketplace.moveworks.com/api/purple-suite/instances/systems
STATUS="" BODY=""

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
log() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
for tool in curl jq; do command -v "$tool" > /dev/null || die "$tool is required"; done
[[ "$EVAL_HOST" == https://* ]] || die "EVAL_HOST must start with https://"

need_token() {
  [[ -s "$EVAL_TOKEN_FILE" ]] || die "no token at $EVAL_TOKEN_FILE. Save your access token there (or set EVAL_TOKEN_FILE). See the Prerequisites in SKILL.md."
}

# api METHOD URL [curl args...]: sets STATUS and BODY. The Authorization header goes in via stdin.
api() {
  local method=$1 url=$2 out
  shift 2
  need_token
  out=$(mktemp)
  STATUS=$(printf 'header = "Authorization: Bearer %s"\n' "$(tr -d '[:space:]' < "$EVAL_TOKEN_FILE")" |
    curl -sS -K - -X "$method" -o "$out" -w '%{http_code}' --connect-timeout 10 --max-time 300 "$@" "$url") || STATUS=000
  BODY=$(cat "$out"); rm -f -- "$out"
  if [[ "$STATUS" == 401 ]]; then
    die "401: the token was rejected or has expired. Get a fresh token the way you normally do, save it to $EVAL_TOKEN_FILE, and run the same command again (a run that already started keeps going: use 'status RUN_ID --wait')."
  fi
}

out_dir() { OUT_DIR=${OUT_DIR:-./eval-out/$(date +%Y%m%d-%H%M%S)}; mkdir -p "$OUT_DIR"; }

explain_access() {
  case "$STATUS" in
    404) echo "404: the Eval API is not enabled for this org (or EVAL_HOST is the wrong data center). Ask your Moveworks team to enable the Eval API and the Files API." ;;
    403) echo "403: the token's user is not allowed: it needs Creator Studio view access, Moveworks Setup edit access where enforced, and Conversations API provisioning. Message: $(jq -r '.error.message // .' <<< "$BODY" 2>/dev/null | head -c 400)" ;;
    *) echo "HTTP $STATUS: $(head -c 400 <<< "$BODY")" ;;
  esac
}

upload_all() {  # upload_all TASK_FILE... -> prints one file id per line
  local f
  for f in "$@"; do
    [[ -f "$f" ]] || die "no task file at $f"
    api POST "$BASE/files" -F "purpose=EVAL_RUNS_TASK" -F "file=@$f"
    [[ "$STATUS" == 201 ]] || die "upload of $f answered $STATUS: $(head -c 400 <<< "$BODY")"
    jq -c --arg file "$(basename "$f")" '{file: $file, response: .}' <<< "$BODY" >> "$OUT_DIR/uploads.jsonl"
    jq -r '.id' <<< "$BODY"
  done
}

build_body() {  # build_body CONFIG VALIDATE_ONLY FILE_ID...
  local config=$1 validate=$2
  shift 2
  [[ -f "$config" ]] || die "no config at $config"
  jq -n --rawfile content "$config" --argjson validate "$validate" \
    '{eval_config: {content: $content}, task_files: ($ARGS.positional | map({file_id: .})), on_invalid_task: "ON_INVALID_TASK_REJECT_RUN"}
     + (if $validate then {validate_only: true} else {} end)' --args "$@"
}

print_issues() {  # 400 VALIDATION_FAILED: one issue per line
  echo "Rejected (400). Each line names the file#task, the issue code, and where:"
  jq -r '.error.message // .' <<< "$BODY" | sed 's/^/  /'
}

submit() {  # submit dry|create CONFIG TASK_FILE...
  local mode=$1 config=$2
  shift 2
  (( $# > 0 )) || die "give the config file and at least one task file"
  local f left
  for f in "$config" "$@"; do  # placeholders in values (comments are ignored)
    left=$(sed 's/#.*//' "$f" | grep -o '<[A-Z][A-Z _-]*>' | sort -u | tr '\n' ' ' || true)
    [[ -z "$left" ]] || die "$f still has placeholders to fill in: $left"
  done
  out_dir
  log "uploading $# task file(s)"
  local ids; ids=$(upload_all "$@")
  local body; body=$(build_body "$config" "$([[ $mode == dry ]] && echo true || echo false)" $ids)
  printf '%s\n' "$body" > "$OUT_DIR/request.json"
  api POST "$BASE/eval_runs" -H 'Content-Type: application/json' --data-binary @"$OUT_DIR/request.json"
  printf '%s\n' "$BODY" > "$OUT_DIR/response.json"
  case "$STATUS" in
    200) echo "Dry run passed (200). Nothing was created."
         jq -r '"  tasks: \(.metadata.tasks.accepted) accepted, \(.metadata.tasks.skipped) skipped",
                (.warnings[]? | "  warning \(.code | sub("WARNING_CODE_"; "")): \(.message)")' <<< "$BODY" ;;
    202) local id; id=$(jq -r '.metadata.id' <<< "$BODY")
         echo "Run created (202): $id"
         echo "  follow it with: eval.sh status $id --wait"
         printf '%s\n' "$id" > "$OUT_DIR/run_id" ;;
    400) print_issues; exit 1 ;;
    *) explain_access; exit 1 ;;
  esac
  echo "  request and response saved in $OUT_DIR"
}

lifecycle() { jq -r '.lifecycle | "status \(.status | sub("EVAL_RUN_STATUS_"; ""))\(if .phase then " · \(.phase | sub("EVAL_RUN_PHASE_"; ""))" else "" end) · report \(.report.status | sub("REPORT_STATUS_"; "")) · cleanup \(.cleanup.status | sub("CLEANUP_STATUS_"; ""))\(if .queue_reason then " · queued: \(.queue_reason)" else "" end)\(if .error then " · error \(.error.code): \(.error.message // "")" else "" end)"' <<< "$BODY"; }

status() {
  local id=$1 wait=${2:-} last=""
  [[ -n "$id" ]] || die "give a run id"
  while :; do
    api GET "$BASE/eval_runs/$id"
    [[ "$STATUS" == 200 ]] || { explain_access; exit 1; }
    local line; line=$(lifecycle)
    [[ "$line" != "$last" ]] && log "$line" && last=$line
    [[ "$wait" == --wait ]] || return 0
    if jq -e '.lifecycle | ((.status | test("COMPLETED|FAILED|CANCELED")) and ((.cleanup.status // "") | test("SUCCEEDED|FAILED") or (.status | test("FAILED|CANCELED"))))' <<< "$BODY" > /dev/null; then
      jq -e '.lifecycle.report.status == "REPORT_STATUS_AVAILABLE"' <<< "$BODY" > /dev/null && report "$id"
      return 0
    fi
    sleep 30
  done
}

fetch_report() {  # fetch_report RUN_ID -> $OUT_DIR/report.json
  out_dir
  api GET "$BASE/eval_runs/$1/report" -H 'Accept-Encoding: gzip' --compressed
  [[ "$STATUS" == 200 ]] || { [[ "$STATUS" == 409 ]] && die "the report is not ready yet: check 'status $1'"; explain_access; exit 1; }
  printf '%s\n' "$BODY" > "$OUT_DIR/report.json"
}

report() {
  fetch_report "$1"
  local r="$OUT_DIR/report.json"
  echo; echo "Report for run $1 (saved to $r)"
  jq -r '"Conversations: \(.summary.tasks.completed) of \(.summary.tasks.total) finished, \(.summary.tasks.failed) failed to run"' "$r"
  echo "Scores:"
  jq -r '.summary.metrics[]? | "  \(.name): \(.value * 100 | round / 100) (n=\(.sample_count))"' "$r"
  echo "Per task:"
  jq -r '.tasks[] | "  \(if (.graders | length) > 0 and all(.graders[]; .status == "GRADER_STATUS_PASSED") then "PASS" else "FAIL" end)  \(.task_id)  " + ([.graders[] | "\(.name)=\(.status | sub("GRADER_STATUS_"; "") | ascii_downcase)"] | join(" "))' "$r"
  echo "Failed checks:"
  jq -r '.tasks[] | . as $t | .graders[] | select(.status != "GRADER_STATUS_PASSED") |
    if .judge then (.judge.assertions[]? | select(.passed != true) | "  \($t.task_id): \(.category // "check"): \(.text)")
    elif .tool_sequence then "  \($t.task_id): missing tool call(s): \((.tool_sequence.missing // []) | join(", "))"
    elif .deterministic then "  \($t.task_id): data check got \(.deterministic.actual), expected \(.deterministic.expected)"
    else "  \($t.task_id): \(.name) errored" end' "$r" | sed '/^$/d' | { grep . || echo "  none"; }
  echo "Task errors:"
  jq -r '.tasks[] | select(.error) | "  \(.task_id): \(.error.code) \(.error.message // "")"' "$r" | { grep . || echo "  none"; }
}

transcript() {
  fetch_report "$1"
  jq -r --arg t "${2:-}" '.tasks[] | select($t == "" or .task_id == $t) |
    "\n=== \(.task_id) ===",
    ((.conversation.turns // [])[] |
      (if .user.content then "USER: \(.user.content)" else empty end),
      ((.trajectory.tool_calls // []) | group_by(.call_id) | map(last)[] | select(.status != "TOOL_CALL_STATUS_INCOMPLETE") |
        "  TOOL \(.tool.name) [\((.connectors[0].mode // "") | sub("CONNECTOR_MODE_"; "") | ascii_downcase)] args=\(.arguments | tostring | .[0:300])\n    -> \((.error // .output) | tostring | .[0:500])"),
      ((.assistant.messages // [])[] | "ASSISTANT: \(.content)"))' "$OUT_DIR/report.json"
}

cmd=${1:-}; [[ $# -gt 0 ]] && shift
case "$cmd" in
  check)
    api GET "$BASE/eval_runs?limit=1"
    if [[ "$STATUS" == 200 ]]; then echo "OK (200): the token is accepted and the Eval API is enabled for your org."; else explain_access; exit 1; fi ;;
  catalog)
    curl -sS --max-time 60 "$CATALOG_URL" | jq -r --arg w "${1:-}" '.systems[] | select($w == "" or (.id | test($w)) or ([.collections[].name] | join(" ") | test($w))) |
      "\(.id): \([.collections[].name] | join(", "))"' ;;
  dry-run) submit dry "$@" ;;
  create) submit create "$@" ;;
  status) status "${1:-}" "${2:-}" ;;
  report) report "${1:?give a run id}" ;;
  transcript) transcript "${1:?give a run id}" "${2:-}" ;;
  cancel)
    api POST "$BASE/eval_runs/${1:?give a run id}/cancel"
    case "$STATUS" in 200) echo "Canceled; cleanup follows. $(lifecycle)" ;; 409) echo "409: the run already finished." ;; *) explain_access; exit 1 ;; esac ;;
  list)
    api GET "$BASE/eval_runs?limit=10"
    [[ "$STATUS" == 200 ]] || { explain_access; exit 1; }
    jq -r '(.eval_runs // [])[] | "\(.metadata.id)  \(.lifecycle.created_at)  \(.lifecycle.status | sub("EVAL_RUN_STATUS_"; ""))  \(.metadata.title // "")"' <<< "$BODY" ;;
  -h|--help|"") sed -n '2,21p' "$0" ;;
  *) die "unknown command '$cmd' (see eval.sh --help)" ;;
esac
