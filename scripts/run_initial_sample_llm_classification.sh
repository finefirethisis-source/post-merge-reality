#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CSV_PATH="${CSV_PATH:-${1:-initial_sample_repos.csv}}"
PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
LLM_CLASSIFIER="${LLM_CLASSIFIER:-src/llm/llm_classification.py}"
INPUT_DIR="${INPUT_DIR:-${OUTPUT_DIR:-src/webscrape/output}}"
LLM_OUTPUT_DIR="${LLM_OUTPUT_DIR:-}"
LOG_DIR="${LOG_DIR:-src/webscrape/run_logs}"
MANIFEST_PATH="${MANIFEST_PATH:-$LOG_DIR/initial_sample_llm_classification_manifest.csv}"

INPUT_NAME="${INPUT_NAME:-commit_contribution_classifications.jsonl}"
OUTPUT_NAME="${OUTPUT_NAME:-llm_commit_contribution_classifications.jsonl}"
REQUESTS_OUTPUT_NAME="${REQUESTS_OUTPUT_NAME:-llm_classification_requests.jsonl}"

PROVIDER="${PROVIDER:-openrouter}"
CLIENT="${CLIENT:-}"
MODEL="${MODEL:-minimax/minimax-m3}"
MODEL_TIMEOUT="${MODEL_TIMEOUT:-60}"
MODEL_RETRIES="${MODEL_RETRIES:-2}"
MODEL_RETRY_DELAY="${MODEL_RETRY_DELAY:-5}"
MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-}"
NO_RESPONSE_SCHEMA="${NO_RESPONSE_SCHEMA:-0}"
OPENROUTER_API_URL="${OPENROUTER_API_URL:-}"
OPENROUTER_SITE_URL="${OPENROUTER_SITE_URL:-}"
OPENROUTER_APP_NAME="${OPENROUTER_APP_NAME:-}"

MAX_FILES="${MAX_FILES:-5}"
MAX_PATCH_CHARS_PER_FILE="${MAX_PATCH_CHARS_PER_FILE:-500}"
INCLUDE_PATCH="${INCLUDE_PATCH:-0}"
MAX_COMMITS_PER_REPO="${MAX_COMMITS_PER_REPO:-}"
WRITE_REQUESTS="${WRITE_REQUESTS:-0}"
PRINT_PROMPT_SETUP="${PRINT_PROMPT_SETUP:-0}"
LLM_LOG_LEVEL="${LLM_LOG_LEVEL:-INFO}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-25}"

MAX_REPOS="${MAX_REPOS:-}"
START_AT="${START_AT:-1}"
REPO_WORKERS="${REPO_WORKERS:-1}"
DRY_RUN="${DRY_RUN:-0}"
RESUME="${RESUME:-1}"
RETRY_ERRORS="${RETRY_ERRORS:-1}"

if [[ "$DRY_RUN" != "1" ]]; then
  mkdir -p "$LOG_DIR"
fi

if [[ ! -f "$CSV_PATH" ]]; then
  echo "CSV not found: $CSV_PATH" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python executable not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -f "$LLM_CLASSIFIER" ]]; then
  echo "LLM classifier not found: $LLM_CLASSIFIER" >&2
  exit 1
fi

if ! [[ "$REPO_WORKERS" =~ ^[0-9]+$ ]] || (( REPO_WORKERS < 1 )); then
  echo "REPO_WORKERS must be a positive integer" >&2
  exit 1
fi

for value_name in DRY_RUN RESUME RETRY_ERRORS INCLUDE_PATCH WRITE_REQUESTS PRINT_PROMPT_SETUP NO_RESPONSE_SCHEMA; do
  value="${!value_name}"
  case "$value" in
    0|1) ;;
    *)
      echo "$value_name must be 0 or 1" >&2
      exit 1
      ;;
  esac
done

case "$PROVIDER" in
  none|openrouter) ;;
  *)
    echo "PROVIDER must be one of: none, openrouter" >&2
    exit 1
    ;;
esac

case "$LLM_LOG_LEVEL" in
  DEBUG|INFO|WARNING|ERROR) ;;
  *)
    echo "LLM_LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR" >&2
    exit 1
    ;;
esac

if [[ -n "$CLIENT" && "$PROVIDER" != "none" ]]; then
  echo "CLIENT cannot be combined with PROVIDER=$PROVIDER; set PROVIDER=none when CLIENT is set" >&2
  exit 1
fi

if [[ "$PRINT_PROMPT_SETUP" == "1" && ( -n "$CLIENT" || "$PROVIDER" != "none" ) ]]; then
  echo "PRINT_PROMPT_SETUP=1 only works without a model client; set PROVIDER=none and leave CLIENT empty" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "1" && -z "$CLIENT" && "$PROVIDER" == "openrouter" && -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "OPENROUTER_API_KEY must be set for PROVIDER=openrouter" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "1" && ! -f "$MANIFEST_PATH" ]]; then
  printf '"index","repo","primary_language","provider","client","model","max_files","include_patch","max_commits_per_repo","status","exit_code","started_at","finished_at","log_file","input_path","output_path","requests_output_path"\n' > "$MANIFEST_PATH"
fi

csv_rows() {
  "$PYTHON_BIN" - "$CSV_PATH" <<'PY'
import csv
import sys

path = sys.argv[1]
with open(path, newline="", encoding="utf-8") as handle:
    for index, row in enumerate(csv.DictReader(handle), start=1):
        repo = (row.get("repo_slug") or "").strip()
        language = (row.get("primary_language") or "NA").strip() or "NA"
        if repo:
            print(f"{index}\t{repo}\t{language}")
PY
}

repo_key() {
  local repo="$1"
  printf '%s\n' "${repo//\//__}"
}

repo_input_path() {
  local repo="$1"
  printf '%s/%s\n' "$INPUT_DIR" "$(repo_key "$repo")"
}

repo_output_path() {
  local repo="$1"
  if [[ -n "$LLM_OUTPUT_DIR" ]]; then
    printf '%s/%s\n' "$LLM_OUTPUT_DIR" "$(repo_key "$repo")"
  else
    repo_input_path "$repo"
  fi
}

should_write_requests() {
  [[ "$WRITE_REQUESTS" == "1" ]] || [[ -z "$CLIENT" && "$PROVIDER" == "none" ]]
}

csv_escape() {
  local value="${1//\"/\"\"}"
  printf '"%s"' "$value"
}

append_manifest_row() {
  local index="$1"
  local repo="$2"
  local language="$3"
  local provider="$4"
  local client="$5"
  local model="$6"
  local max_files="$7"
  local include_patch="$8"
  local max_commits_per_repo="$9"
  local status="${10}"
  local exit_code="${11}"
  local started_at="${12}"
  local finished_at="${13}"
  local log_file="${14}"
  local input_path="${15}"
  local output_path="${16}"
  local requests_output_path="${17}"

  {
    csv_escape "$index"; printf ','
    csv_escape "$repo"; printf ','
    csv_escape "$language"; printf ','
    csv_escape "$provider"; printf ','
    csv_escape "$client"; printf ','
    csv_escape "$model"; printf ','
    csv_escape "$max_files"; printf ','
    csv_escape "$include_patch"; printf ','
    csv_escape "$max_commits_per_repo"; printf ','
    csv_escape "$status"; printf ','
    csv_escape "$exit_code"; printf ','
    csv_escape "$started_at"; printf ','
    csv_escape "$finished_at"; printf ','
    csv_escape "$log_file"; printf ','
    csv_escape "$input_path"; printf ','
    csv_escape "$output_path"; printf ','
    csv_escape "$requests_output_path"; printf '\n'
  } >> "$MANIFEST_PATH"
}

write_manifest_row_file() {
  local path="$1"
  shift
  : > "$path"
  MANIFEST_PATH="$path" append_manifest_row "$@"
}

llm_output_complete() {
  local classification_path="$1"
  local output_path="$2"
  local max_commits_per_repo="$3"
  local client="$4"
  local provider="$5"
  local retry_errors="$6"

  [[ -e "$output_path" ]] || return 1
  "$PYTHON_BIN" - "$classification_path" "$output_path" "$max_commits_per_repo" "$client" "$provider" "$retry_errors" <<'PY'
import json
import sys
from pathlib import Path

classification_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
limit_text = sys.argv[3].strip()
client = sys.argv[4].strip()
provider = sys.argv[5].strip()
retry_errors = sys.argv[6].strip() == "1"
client_available = bool(client) or provider != "none"


def row_key(row):
    sha = row.get("sha")
    if isinstance(sha, str) and sha.strip():
        return f"sha:{sha.strip()}"
    repo = row.get("repo") or row.get("repo_key")
    subject = row.get("message_subject")
    repo_text = str(repo).strip() if repo is not None else ""
    subject_text = str(subject).strip() if subject is not None else ""
    if repo_text or subject_text:
        return f"fallback:{repo_text}\0{subject_text}"
    return None


def row_complete(row):
    status = row.get("classification_status")
    if status == "classified":
        return True
    if status == "pending_model":
        return not client_available
    if status == "model_error":
        return not retry_errors
    return False

limit = None
if limit_text:
    try:
        limit = max(0, int(limit_text))
    except ValueError:
        sys.exit(1)

selected_keys = []
with classification_path.open(encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("primary_operational_intent") in {"mixed", "unknown"}:
            key = row_key(row)
            if key is None:
                sys.exit(1)
            selected_keys.append(key)
            if limit is not None and len(selected_keys) >= limit:
                break

latest_rows = {}
with output_path.open(encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            sys.exit(1)
        key = row_key(row)
        if key is not None:
            latest_rows[key] = row

for key in selected_keys:
    row = latest_rows.get(key)
    if row is None or not row_complete(row):
        sys.exit(1)

sys.exit(0)
PY
}

build_llm_command() {
  local classification_path="$1"
  local output_path="$2"
  local requests_output_path="$3"
  local -n out_cmd="$4"

  out_cmd=(
    "$PYTHON_BIN" "$LLM_CLASSIFIER"
    "$classification_path"
    --output "$output_path"
    --max-files "$MAX_FILES"
    --max-patch-chars-per-file "$MAX_PATCH_CHARS_PER_FILE"
    --log-level "$LLM_LOG_LEVEL"
    --progress-interval "$PROGRESS_INTERVAL"
    --model-retries "$MODEL_RETRIES"
    --model-retry-delay "$MODEL_RETRY_DELAY"
  )

  if [[ -n "$CLIENT" ]]; then
    out_cmd+=(--client "$CLIENT")
  else
    out_cmd+=(--provider "$PROVIDER")
    if [[ -n "$MODEL" ]]; then
      out_cmd+=(--model "$MODEL")
    fi
    if [[ "$PROVIDER" == "openrouter" ]]; then
      out_cmd+=(--model-timeout "$MODEL_TIMEOUT")
      if [[ -n "$OPENROUTER_API_URL" ]]; then
        out_cmd+=(--openrouter-api-url "$OPENROUTER_API_URL")
      fi
      if [[ -n "$OPENROUTER_SITE_URL" ]]; then
        out_cmd+=(--openrouter-site-url "$OPENROUTER_SITE_URL")
      fi
      if [[ -n "$OPENROUTER_APP_NAME" ]]; then
        out_cmd+=(--openrouter-app-name "$OPENROUTER_APP_NAME")
      fi
      if [[ -n "$MAX_OUTPUT_TOKENS" ]]; then
        out_cmd+=(--max-output-tokens "$MAX_OUTPUT_TOKENS")
      fi
      if [[ "$NO_RESPONSE_SCHEMA" == "1" ]]; then
        out_cmd+=(--no-response-schema)
      fi
    fi
  fi

  if [[ "$INCLUDE_PATCH" == "1" ]]; then
    out_cmd+=(--include-patch)
  fi
  if [[ -n "$MAX_COMMITS_PER_REPO" ]]; then
    out_cmd+=(--limit "$MAX_COMMITS_PER_REPO")
  fi
  if [[ -n "$requests_output_path" ]]; then
    out_cmd+=(--requests-output "$requests_output_path")
  fi
  if [[ "$WRITE_REQUESTS" == "1" ]]; then
    out_cmd+=(--write-requests)
  fi
  if [[ "$PRINT_PROMPT_SETUP" == "1" ]]; then
    out_cmd+=(--print-prompt-setup)
  fi
  if [[ "$RESUME" == "0" ]]; then
    out_cmd+=(--no-resume)
  fi
  if [[ "$RETRY_ERRORS" == "0" ]]; then
    out_cmd+=(--no-retry-errors)
  fi
}

run_repo() {
  local index="$1"
  local repo="$2"
  local primary_language="$3"
  local input_path="$4"
  local output_path="$5"
  local requests_output_path="$6"
  local log_file="$7"
  local row_file="$8"

  local cmd
  build_llm_command "$input_path" "$output_path" "$requests_output_path" cmd
  mkdir -p "$(dirname "$output_path")"
  if [[ -n "$requests_output_path" ]]; then
    mkdir -p "$(dirname "$requests_output_path")"
  fi

  {
    echo "repo=$repo"
    echo "index=$index"
    echo "primary_language=$primary_language"
    echo "provider=$PROVIDER"
    echo "client=$CLIENT"
    echo "model=$MODEL"
    echo "model_retries=$MODEL_RETRIES"
    echo "model_retry_delay=$MODEL_RETRY_DELAY"
    echo "max_files=$MAX_FILES"
    echo "include_patch=$INCLUDE_PATCH"
    echo "max_commits_per_repo=$MAX_COMMITS_PER_REPO"
    echo "resume=$RESUME"
    echo "retry_errors=$RETRY_ERRORS"
    echo "input_path=$input_path"
    echo "output_path=$output_path"
    echo "requests_output_path=$requests_output_path"
    echo "started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    printf 'command='
    printf '%q ' "${cmd[@]}"
    printf '\n'
    echo
  } > "$log_file"

  local started_at
  local finished_at
  local status
  local exit_code
  started_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  exit_code=0
  if "${cmd[@]}" >> "$log_file" 2>&1; then
    status="ok"
  else
    exit_code=$?
    status="failed"
  fi
  finished_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  write_manifest_row_file \
    "$row_file" \
    "$index" "$repo" "$primary_language" "$PROVIDER" "$CLIENT" "$MODEL" \
    "$MAX_FILES" "$INCLUDE_PATCH" "$MAX_COMMITS_PER_REPO" \
    "$status" "$exit_code" "$started_at" "$finished_at" \
    "$log_file" "$input_path" "$output_path" "$requests_output_path"
  return "$exit_code"
}

processed=0
failed=0
skipped=0
running=0
declare -a job_pids=()
declare -A job_rows=()
declare -A job_repos=()
declare -A job_logs=()
RUN_ROWS_DIR=""
if [[ "$DRY_RUN" != "1" ]]; then
  RUN_ROWS_DIR="$(mktemp -d "$LOG_DIR/llm_classification_manifest_rows.XXXXXX")"
fi

collect_finished_jobs() {
  local mode="${1:-nonblock}"
  local pid
  local remaining=()
  local should_wait
  for pid in "${job_pids[@]}"; do
    should_wait=0
    if [[ "$mode" == "all" ]]; then
      should_wait=1
    elif ! kill -0 "$pid" 2>/dev/null; then
      should_wait=1
    fi

    if [[ "$should_wait" == "1" ]]; then
      if wait "$pid"; then
        echo "  completed repo=${job_repos[$pid]}"
      else
        exit_code=$?
        failed=$((failed + 1))
        echo "  failed repo=${job_repos[$pid]} exit_code=$exit_code; see ${job_logs[$pid]}" >&2
      fi
      cat "${job_rows[$pid]}" >> "$MANIFEST_PATH"
      rm -f "${job_rows[$pid]}"
      unset "job_rows[$pid]" "job_repos[$pid]" "job_logs[$pid]"
      running=$((running - 1))
    else
      remaining+=("$pid")
    fi
  done
  job_pids=("${remaining[@]}")
}

wait_for_job_slot() {
  while (( running >= REPO_WORKERS )); do
    collect_finished_jobs nonblock
    if (( running >= REPO_WORKERS )); then
      sleep 2
    fi
  done
}

while IFS=$'\t' read -r index repo primary_language; do
  if (( index < START_AT )); then
    continue
  fi
  if [[ -n "$MAX_REPOS" && "$processed" -ge "$MAX_REPOS" ]]; then
    break
  fi

  repo_input_dir="$(repo_input_path "$repo")"
  repo_output_dir="$(repo_output_path "$repo")"
  input_path="$repo_input_dir/$INPUT_NAME"
  output_path="$repo_output_dir/$OUTPUT_NAME"
  requests_output_path=""
  if should_write_requests; then
    requests_output_path="$repo_output_dir/$REQUESTS_OUTPUT_NAME"
  fi
  log_file="$LOG_DIR/${repo//\//__}.llm_classification.log"

  if [[ "$DRY_RUN" != "1" && ! -s "$input_path" ]]; then
    echo "[$index] skip repo=$repo primary_language=$primary_language missing classification input=$input_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$PROVIDER" "$CLIENT" "$MODEL" \
      "$MAX_FILES" "$INCLUDE_PATCH" "$MAX_COMMITS_PER_REPO" \
      "skipped_missing_input" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$log_file" "$input_path" "$output_path" "$requests_output_path"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  if [[ "$DRY_RUN" != "1" && "$RESUME" == "1" ]] && llm_output_complete "$input_path" "$output_path" "$MAX_COMMITS_PER_REPO" "$CLIENT" "$PROVIDER" "$RETRY_ERRORS"; then
    echo "[$index] skip repo=$repo primary_language=$primary_language existing complete LLM output=$output_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$PROVIDER" "$CLIENT" "$MODEL" \
      "$MAX_FILES" "$INCLUDE_PATCH" "$MAX_COMMITS_PER_REPO" \
      "skipped_existing_output" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$log_file" "$input_path" "$output_path" "$requests_output_path"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  echo "[$index] repo=$repo primary_language=$primary_language provider=$PROVIDER model=$MODEL input=$input_path output=$output_path"
  if [[ "$DRY_RUN" == "1" ]]; then
    build_llm_command "$input_path" "$output_path" "$requests_output_path" cmd
    printf '  '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    processed=$((processed + 1))
    continue
  fi

  wait_for_job_slot
  row_file="$RUN_ROWS_DIR/${index}_${repo//\//__}.csv"
  run_repo "$index" "$repo" "$primary_language" "$input_path" "$output_path" "$requests_output_path" "$log_file" "$row_file" &
  pid=$!
  job_pids+=("$pid")
  job_rows[$pid]="$row_file"
  job_repos[$pid]="$repo"
  job_logs[$pid]="$log_file"
  running=$((running + 1))
  processed=$((processed + 1))
done < <(csv_rows)

if [[ "$DRY_RUN" != "1" ]]; then
  collect_finished_jobs all
  rmdir "$RUN_ROWS_DIR" 2>/dev/null || true
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "done: processed=$processed skipped=$skipped failed=$failed dry_run=1"
else
  echo "done: processed=$processed skipped=$skipped failed=$failed manifest=$MANIFEST_PATH"
fi
if (( failed > 0 )); then
  exit 1
fi
