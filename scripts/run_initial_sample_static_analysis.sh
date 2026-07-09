#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CSV_PATH="${CSV_PATH:-${1:-initial_sample_repos.csv}}"
PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
STATIC_ANALYZER="${STATIC_ANALYZER:-src/webscrape/run_static_analysis.py}"
INPUT_DIR="${INPUT_DIR:-src/webscrape/output}"
CLONE_DIR="${CLONE_DIR:-src/webscrape/repos}"
STATIC_OUTPUT_DIR="${STATIC_OUTPUT_DIR:-${OUTPUT_DIR:-}}"
LOG_DIR="${LOG_DIR:-src/webscrape/run_logs}"
MANIFEST_PATH="${MANIFEST_PATH:-$LOG_DIR/initial_sample_static_analysis_manifest.csv}"

COMMIT_SOURCE="${COMMIT_SOURCE:-all}"
CONFIG="${CONFIG:-p/default}"
SEMGREP_BIN="${SEMGREP_BIN:-semgrep}"
SEMGREP_TIMEOUT="${SEMGREP_TIMEOUT:-120}"
SEMGREP_JOBS="${SEMGREP_JOBS:-1}"
GIT_TIMEOUT="${GIT_TIMEOUT:-60}"
COMMIT_WORKERS="${COMMIT_WORKERS:-1}"
PROGRESS="${PROGRESS:-auto}"
FINDINGS_OUTPUT_NAME="${FINDINGS_OUTPUT_NAME:-commit_static_findings.jsonl}"
SUMMARY_OUTPUT_NAME="${SUMMARY_OUTPUT_NAME:-commit_static_analysis_summary.jsonl}"
MAX_COMMITS="${MAX_COMMITS:-}"
MAX_FILES_PER_COMMIT="${MAX_FILES_PER_COMMIT:-}"
MAX_REPOS="${MAX_REPOS:-}"
START_AT="${START_AT:-1}"
REPO_WORKERS="${REPO_WORKERS:-1}"
KEEP_TEMP="${KEEP_TEMP:-0}"
DRY_RUN="${DRY_RUN:-0}"
RESUME="${RESUME:-1}"

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

if [[ ! -f "$STATIC_ANALYZER" ]]; then
  echo "Static analyzer not found: $STATIC_ANALYZER" >&2
  exit 1
fi

if ! [[ "$REPO_WORKERS" =~ ^[0-9]+$ ]] || (( REPO_WORKERS < 1 )); then
  echo "REPO_WORKERS must be a positive integer" >&2
  exit 1
fi

if ! [[ "$COMMIT_WORKERS" =~ ^[0-9]+$ ]] || (( COMMIT_WORKERS < 1 )); then
  echo "COMMIT_WORKERS must be a positive integer" >&2
  exit 1
fi

if ! [[ "$SEMGREP_JOBS" =~ ^[0-9]+$ ]] || (( SEMGREP_JOBS < 1 )); then
  echo "SEMGREP_JOBS must be a positive integer" >&2
  exit 1
fi

case "$COMMIT_SOURCE" in
  all|observation) ;;
  *)
    echo "COMMIT_SOURCE must be one of: all, observation" >&2
    exit 1
    ;;
esac

if [[ "$KEEP_TEMP" != "0" && "$KEEP_TEMP" != "1" ]]; then
  echo "KEEP_TEMP must be 0 or 1" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "1" && ! -f "$MANIFEST_PATH" ]]; then
  printf '"index","repo","primary_language","commit_source","config","commit_workers","semgrep_jobs","status","exit_code","started_at","finished_at","log_file","input_path","output_path"\n' > "$MANIFEST_PATH"
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
  if [[ -n "$STATIC_OUTPUT_DIR" ]]; then
    printf '%s/%s\n' "$STATIC_OUTPUT_DIR" "$(repo_key "$repo")"
  else
    repo_input_path "$repo"
  fi
}

csv_escape() {
  local value="${1//\"/\"\"}"
  printf '"%s"' "$value"
}

append_manifest_row() {
  local index="$1"
  local repo="$2"
  local language="$3"
  local commit_source="$4"
  local config="$5"
  local commit_workers="$6"
  local semgrep_jobs="$7"
  local status="$8"
  local exit_code="$9"
  local started_at="${10}"
  local finished_at="${11}"
  local log_file="${12}"
  local input_path="${13}"
  local output_path="${14}"

  {
    csv_escape "$index"; printf ','
    csv_escape "$repo"; printf ','
    csv_escape "$language"; printf ','
    csv_escape "$commit_source"; printf ','
    csv_escape "$config"; printf ','
    csv_escape "$commit_workers"; printf ','
    csv_escape "$semgrep_jobs"; printf ','
    csv_escape "$status"; printf ','
    csv_escape "$exit_code"; printf ','
    csv_escape "$started_at"; printf ','
    csv_escape "$finished_at"; printf ','
    csv_escape "$log_file"; printf ','
    csv_escape "$input_path"; printf ','
    csv_escape "$output_path"; printf '\n'
  } >> "$MANIFEST_PATH"
}

write_manifest_row_file() {
  local path="$1"
  shift
  : > "$path"
  MANIFEST_PATH="$path" append_manifest_row "$@"
}

build_static_analysis_command() {
  local repo="$1"
  local -n out_cmd="$2"

  out_cmd=(
    "$PYTHON_BIN" "$STATIC_ANALYZER"
    --input-dir "$INPUT_DIR"
    --clone-dir "$CLONE_DIR"
    --repo "$repo"
    --config "$CONFIG"
    --semgrep-bin "$SEMGREP_BIN"
    --semgrep-timeout "$SEMGREP_TIMEOUT"
    --semgrep-jobs "$SEMGREP_JOBS"
    --git-timeout "$GIT_TIMEOUT"
    --commit-source "$COMMIT_SOURCE"
    --workers "$COMMIT_WORKERS"
    --findings-output-name "$FINDINGS_OUTPUT_NAME"
    --summary-output-name "$SUMMARY_OUTPUT_NAME"
    --progress "$PROGRESS"
  )

  if [[ -n "$STATIC_OUTPUT_DIR" ]]; then
    out_cmd+=(--output-dir "$STATIC_OUTPUT_DIR")
  fi
  if [[ -n "$MAX_COMMITS" ]]; then
    out_cmd+=(--max-commits "$MAX_COMMITS")
  fi
  if [[ -n "$MAX_FILES_PER_COMMIT" ]]; then
    out_cmd+=(--max-files-per-commit "$MAX_FILES_PER_COMMIT")
  fi
  if [[ "$KEEP_TEMP" == "1" ]]; then
    out_cmd+=(--keep-temp)
  fi
}

run_repo() {
  local index="$1"
  local repo="$2"
  local primary_language="$3"
  local input_path="$4"
  local output_path="$5"
  local log_file="$6"
  local row_file="$7"

  local cmd
  build_static_analysis_command "$repo" cmd

  {
    echo "repo=$repo"
    echo "index=$index"
    echo "primary_language=$primary_language"
    echo "commit_source=$COMMIT_SOURCE"
    echo "config=$CONFIG"
    echo "commit_workers=$COMMIT_WORKERS"
    echo "semgrep_jobs=$SEMGREP_JOBS"
    echo "input_path=$input_path"
    echo "output_path=$output_path"
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
    "$index" "$repo" "$primary_language" "$COMMIT_SOURCE" "$CONFIG" \
    "$COMMIT_WORKERS" "$SEMGREP_JOBS" "$status" "$exit_code" \
    "$started_at" "$finished_at" "$log_file" "$input_path" "$output_path"
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
  RUN_ROWS_DIR="$(mktemp -d "$LOG_DIR/static_analysis_manifest_rows.XXXXXX")"
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

  input_path="$(repo_input_path "$repo")"
  output_path="$(repo_output_path "$repo")"
  log_file="$LOG_DIR/${repo//\//__}.static_analysis.log"

  if [[ "$DRY_RUN" != "1" && ! -s "$input_path/commit_file_changes.jsonl" ]]; then
    echo "[$index] skip repo=$repo primary_language=$primary_language missing commit_file_changes input=$input_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$COMMIT_SOURCE" "$CONFIG" \
      "$COMMIT_WORKERS" "$SEMGREP_JOBS" \
      "skipped_missing_output" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$log_file" "$input_path" "$output_path"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  if [[ "$DRY_RUN" != "1" && "$RESUME" == "1" && -e "$output_path/$FINDINGS_OUTPUT_NAME" && -s "$output_path/$SUMMARY_OUTPUT_NAME" ]]; then
    echo "[$index] skip repo=$repo primary_language=$primary_language existing static analysis output=$output_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$COMMIT_SOURCE" "$CONFIG" \
      "$COMMIT_WORKERS" "$SEMGREP_JOBS" \
      "skipped_existing_output" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$log_file" "$input_path" "$output_path"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  echo "[$index] repo=$repo primary_language=$primary_language commit_source=$COMMIT_SOURCE config=$CONFIG output=$output_path"
  if [[ "$DRY_RUN" == "1" ]]; then
    build_static_analysis_command "$repo" cmd
    printf '  '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    processed=$((processed + 1))
    continue
  fi

  wait_for_job_slot
  row_file="$RUN_ROWS_DIR/${index}_${repo//\//__}.csv"
  run_repo "$index" "$repo" "$primary_language" "$input_path" "$output_path" "$log_file" "$row_file" &
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
