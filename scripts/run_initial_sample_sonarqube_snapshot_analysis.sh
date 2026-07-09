#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CSV_PATH="${CSV_PATH:-${1:-initial_sample_repos.csv}}"
PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
SONAR_ANALYZER="${SONAR_ANALYZER:-src/webscrape/run_sonarqube_snapshot_analysis.py}"
OUTPUT_DIR="${OUTPUT_DIR:-src/webscrape/output}"
CLONE_DIR="${CLONE_DIR:-src/webscrape/repos}"
LOG_DIR="${LOG_DIR:-src/webscrape/run_logs}"
MANIFEST_PATH="${MANIFEST_PATH:-$LOG_DIR/initial_sample_sonarqube_snapshot_analysis_manifest.csv}"

BRANCH="${BRANCH:-auto}"
SINCE="${SINCE:-}"
UNTIL="${UNTIL:-}"
SNAPSHOT_DATE="${SNAPSHOT_DATE:-}"
INTERVAL="${INTERVAL:-weekly}"
NO_FINAL_SNAPSHOT="${NO_FINAL_SNAPSHOT:-0}"
NO_FIRST_PARENT_SNAPSHOTS="${NO_FIRST_PARENT_SNAPSHOTS:-0}"
MAX_SNAPSHOTS="${MAX_SNAPSHOTS:-}"

SONAR_HOST_URL="${SONAR_HOST_URL:-${SONAR_HOST:-http://localhost:9000}}"
SONAR_SCANNER_BIN="${SONAR_SCANNER_BIN:-${SONAR_SCANNER_PATH:-sonar-scanner}}"
SONAR_JAVA_BINARIES="${SONAR_JAVA_BINARIES:-.}"
SONAR_PROPERTY="${SONAR_PROPERTY:-}"
SONAR_PROPERTIES="${SONAR_PROPERTIES:-}"
SONAR_PROPERTIES_FILE="${SONAR_PROPERTIES_FILE:-}"
PROJECT_KEY_PREFIX="${PROJECT_KEY_PREFIX:-${SONAR_PROJECT_KEY_PREFIX:-}}"
VERSION_PREFIX="${VERSION_PREFIX:-}"
METRIC="${METRIC:-}"
SKIP_ISSUES="${SKIP_ISSUES:-0}"
ISSUE_PAGE_SIZE="${ISSUE_PAGE_SIZE:-500}"
EXCLUDE_GENERATED_CODE="${EXCLUDE_GENERATED_CODE:-1}"
STREAM_SCANNER_OUTPUT="${STREAM_SCANNER_OUTPUT:-0}"

SUMMARY_OUTPUT_NAME="${SUMMARY_OUTPUT_NAME:-sonarqube_snapshot_scans.jsonl}"
SCANNER_TIMEOUT="${SCANNER_TIMEOUT:-1800}"
CE_TIMEOUT="${CE_TIMEOUT:-3600}"
CE_POLL_SECONDS="${CE_POLL_SECONDS:-2.0}"
GIT_TIMEOUT="${GIT_TIMEOUT:-300}"
REPO_WORKERS="${REPO_WORKERS:-1}"
SKIP_FETCH="${SKIP_FETCH:-1}"
KEEP_CLONE="${KEEP_CLONE:-0}"
KEEP_WORKTREES="${KEEP_WORKTREES:-0}"
PROGRESS="${PROGRESS:-auto}"
MAX_REPOS="${MAX_REPOS:-}"
START_AT="${START_AT:-1}"
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

if [[ ! -f "$SONAR_ANALYZER" ]]; then
  echo "SonarQube analyzer not found: $SONAR_ANALYZER" >&2
  exit 1
fi

if [[ -z "$SINCE" && -z "$SNAPSHOT_DATE" ]]; then
  echo "Set SINCE=YYYY-MM-DD or SNAPSHOT_DATE=YYYY-MM-DD before running." >&2
  exit 1
fi

if ! [[ "$REPO_WORKERS" =~ ^[0-9]+$ ]] || (( REPO_WORKERS < 1 )); then
  echo "REPO_WORKERS must be a positive integer" >&2
  exit 1
fi

if ! [[ "$ISSUE_PAGE_SIZE" =~ ^[0-9]+$ ]] || (( ISSUE_PAGE_SIZE < 1 || ISSUE_PAGE_SIZE > 500 )); then
  echo "ISSUE_PAGE_SIZE must be an integer between 1 and 500" >&2
  exit 1
fi

case "$INTERVAL" in
  daily|weekly|monthly) ;;
  *)
    echo "INTERVAL must be one of: daily, weekly, monthly" >&2
    exit 1
    ;;
esac

case "$PROGRESS" in
  auto|always|never) ;;
  *)
    echo "PROGRESS must be one of: auto, always, never" >&2
    exit 1
    ;;
esac

for flag_name in NO_FINAL_SNAPSHOT NO_FIRST_PARENT_SNAPSHOTS SKIP_ISSUES EXCLUDE_GENERATED_CODE STREAM_SCANNER_OUTPUT SKIP_FETCH KEEP_CLONE KEEP_WORKTREES DRY_RUN RESUME; do
  flag_value="${!flag_name}"
  if [[ "$flag_value" != "0" && "$flag_value" != "1" ]]; then
    echo "$flag_name must be 0 or 1" >&2
    exit 1
  fi
done

if [[ "$DRY_RUN" != "1" && ! -f "$MANIFEST_PATH" ]]; then
  printf '"index","repo","primary_language","branch","since","until","snapshot_date","interval","max_snapshots","repo_workers","exclude_generated_code","stream_scanner_output","skip_issues","status","exit_code","started_at","finished_at","log_file","output_path","summary_path"\n' > "$MANIFEST_PATH"
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

repo_output_path() {
  local repo="$1"
  printf '%s/%s\n' "$OUTPUT_DIR" "$(repo_key "$repo")"
}

repo_summary_path() {
  local repo="$1"
  printf '%s/%s\n' "$(repo_output_path "$repo")" "$SUMMARY_OUTPUT_NAME"
}

split_csv_values() {
  local value="$1"
  local -n out_values="$2"
  local old_ifs="$IFS"
  local part
  out_values=()
  IFS=','
  for part in $value; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    if [[ -n "$part" ]]; then
      out_values+=("$part")
    fi
  done
  IFS="$old_ifs"
}

split_semicolon_values() {
  local value="$1"
  local -n out_values="$2"
  local old_ifs="$IFS"
  local part
  out_values=()
  IFS=';'
  for part in $value; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    if [[ -n "$part" ]]; then
      out_values+=("$part")
    fi
  done
  IFS="$old_ifs"
}

append_properties_file() {
  local path="$1"
  local -n out_cmd="$2"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    if [[ -n "$line" ]]; then
      out_cmd+=(--sonar-property "$line")
    fi
  done < "$path"
}

csv_escape() {
  local value="${1//\"/\"\"}"
  printf '"%s"' "$value"
}

append_manifest_row() {
  local index="$1"
  local repo="$2"
  local language="$3"
  local branch="$4"
  local since="$5"
  local until="$6"
  local snapshot_date="$7"
  local interval="$8"
  local max_snapshots="$9"
  local repo_workers="${10}"
  local exclude_generated_code="${11}"
  local stream_scanner_output="${12}"
  local skip_issues="${13}"
  local status="${14}"
  local exit_code="${15}"
  local started_at="${16}"
  local finished_at="${17}"
  local log_file="${18}"
  local output_path="${19}"
  local summary_path="${20}"

  {
    csv_escape "$index"; printf ','
    csv_escape "$repo"; printf ','
    csv_escape "$language"; printf ','
    csv_escape "$branch"; printf ','
    csv_escape "$since"; printf ','
    csv_escape "$until"; printf ','
    csv_escape "$snapshot_date"; printf ','
    csv_escape "$interval"; printf ','
    csv_escape "$max_snapshots"; printf ','
    csv_escape "$repo_workers"; printf ','
    csv_escape "$exclude_generated_code"; printf ','
    csv_escape "$stream_scanner_output"; printf ','
    csv_escape "$skip_issues"; printf ','
    csv_escape "$status"; printf ','
    csv_escape "$exit_code"; printf ','
    csv_escape "$started_at"; printf ','
    csv_escape "$finished_at"; printf ','
    csv_escape "$log_file"; printf ','
    csv_escape "$output_path"; printf ','
    csv_escape "$summary_path"; printf '\n'
  } >> "$MANIFEST_PATH"
}

write_manifest_row_file() {
  local path="$1"
  shift
  : > "$path"
  MANIFEST_PATH="$path" append_manifest_row "$@"
}

build_sonarqube_command() {
  local repo="$1"
  local -n out_cmd="$2"
  local values=()
  local value

  out_cmd=(
    "$PYTHON_BIN" "$SONAR_ANALYZER"
    --repo "$repo"
    --output-dir "$OUTPUT_DIR"
    --clone-dir "$CLONE_DIR"
    --branch "$BRANCH"
    --interval "$INTERVAL"
    --sonar-host-url "$SONAR_HOST_URL"
    --sonar-scanner-bin "$SONAR_SCANNER_BIN"
    --sonar-java-binaries "$SONAR_JAVA_BINARIES"
    --summary-output-name "$SUMMARY_OUTPUT_NAME"
    --scanner-timeout "$SCANNER_TIMEOUT"
    --ce-timeout "$CE_TIMEOUT"
    --ce-poll-seconds "$CE_POLL_SECONDS"
    --git-timeout "$GIT_TIMEOUT"
    --repo-workers 1
    --issue-page-size "$ISSUE_PAGE_SIZE"
    --progress "$PROGRESS"
  )

  if [[ -n "$SINCE" ]]; then
    out_cmd+=(--since "$SINCE")
  fi
  if [[ -n "$UNTIL" ]]; then
    out_cmd+=(--until "$UNTIL")
  fi
  if [[ -n "$SNAPSHOT_DATE" ]]; then
    split_csv_values "$SNAPSHOT_DATE" values
    for value in "${values[@]}"; do
      out_cmd+=(--snapshot-date "$value")
    done
  fi
  if [[ "$NO_FINAL_SNAPSHOT" == "1" ]]; then
    out_cmd+=(--no-final-snapshot)
  fi
  if [[ "$NO_FIRST_PARENT_SNAPSHOTS" == "1" ]]; then
    out_cmd+=(--no-first-parent-snapshots)
  fi
  if [[ -n "$MAX_SNAPSHOTS" ]]; then
    out_cmd+=(--max-snapshots "$MAX_SNAPSHOTS")
  fi
  if [[ -n "$PROJECT_KEY_PREFIX" ]]; then
    out_cmd+=(--project-key-prefix "$PROJECT_KEY_PREFIX")
  fi
  if [[ -n "$VERSION_PREFIX" ]]; then
    out_cmd+=(--version-prefix "$VERSION_PREFIX")
  fi
  if [[ -n "$METRIC" ]]; then
    split_csv_values "$METRIC" values
    for value in "${values[@]}"; do
      out_cmd+=(--metric "$value")
    done
  fi
  if [[ -n "$SONAR_PROPERTY" ]]; then
    out_cmd+=(--sonar-property "$SONAR_PROPERTY")
  fi
  if [[ -n "$SONAR_PROPERTIES" ]]; then
    split_semicolon_values "$SONAR_PROPERTIES" values
    for value in "${values[@]}"; do
      out_cmd+=(--sonar-property "$value")
    done
  fi
  if [[ -n "$SONAR_PROPERTIES_FILE" ]]; then
    if [[ ! -f "$SONAR_PROPERTIES_FILE" ]]; then
      echo "SONAR_PROPERTIES_FILE not found: $SONAR_PROPERTIES_FILE" >&2
      exit 1
    fi
    append_properties_file "$SONAR_PROPERTIES_FILE" out_cmd
  fi
  if [[ "$EXCLUDE_GENERATED_CODE" == "1" ]]; then
    out_cmd+=(--exclude-generated-code)
  fi
  if [[ "$STREAM_SCANNER_OUTPUT" == "1" ]]; then
    out_cmd+=(--stream-scanner-output)
  fi
  if [[ "$SKIP_ISSUES" == "1" ]]; then
    out_cmd+=(--skip-issues)
  fi
  if [[ "$SKIP_FETCH" == "1" ]]; then
    out_cmd+=(--skip-fetch)
  fi
  if [[ "$KEEP_CLONE" == "1" ]]; then
    out_cmd+=(--keep-clone)
  fi
  if [[ "$KEEP_WORKTREES" == "1" ]]; then
    out_cmd+=(--keep-worktrees)
  fi
  if [[ "$RESUME" == "1" ]]; then
    out_cmd+=(--resume)
  else
    out_cmd+=(--no-resume)
  fi
}

run_repo() {
  local index="$1"
  local repo="$2"
  local primary_language="$3"
  local output_path="$4"
  local summary_path="$5"
  local log_file="$6"
  local row_file="$7"

  local cmd
  build_sonarqube_command "$repo" cmd

  {
    echo "repo=$repo"
    echo "index=$index"
    echo "primary_language=$primary_language"
    echo "branch=$BRANCH"
    echo "since=$SINCE"
    echo "until=$UNTIL"
    echo "snapshot_date=$SNAPSHOT_DATE"
    echo "interval=$INTERVAL"
    echo "max_snapshots=$MAX_SNAPSHOTS"
    echo "repo_workers=$REPO_WORKERS"
    echo "exclude_generated_code=$EXCLUDE_GENERATED_CODE"
    echo "stream_scanner_output=$STREAM_SCANNER_OUTPUT"
    echo "skip_issues=$SKIP_ISSUES"
    echo "keep_clone=$KEEP_CLONE"
    echo "sonar_host_url=$SONAR_HOST_URL"
    echo "sonar_token_configured=$([[ -n "${SONAR_TOKEN:-}" ]] && echo 1 || echo 0)"
    echo "output_path=$output_path"
    echo "summary_path=$summary_path"
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
  if [[ -n "${SONAR_TOKEN:-}" ]]; then
    SONAR_TOKEN="$SONAR_TOKEN" "${cmd[@]}" >> "$log_file" 2>&1
    exit_code=$?
  else
    "${cmd[@]}" >> "$log_file" 2>&1
    exit_code=$?
  fi
  if (( exit_code == 0 )); then
    status="ok"
  else
    status="failed"
  fi
  finished_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  write_manifest_row_file \
    "$row_file" \
    "$index" "$repo" "$primary_language" "$BRANCH" \
    "$SINCE" "$UNTIL" "$SNAPSHOT_DATE" "$INTERVAL" "$MAX_SNAPSHOTS" \
    "$REPO_WORKERS" "$EXCLUDE_GENERATED_CODE" "$STREAM_SCANNER_OUTPUT" "$SKIP_ISSUES" \
    "$status" "$exit_code" "$started_at" "$finished_at" \
    "$log_file" "$output_path" "$summary_path"
  return "$exit_code"
}

processed=0
failed=0
running=0
declare -a job_pids=()
declare -A job_rows=()
declare -A job_repos=()
declare -A job_logs=()
RUN_ROWS_DIR=""
if [[ "$DRY_RUN" != "1" ]]; then
  RUN_ROWS_DIR="$(mktemp -d "$LOG_DIR/sonarqube_snapshot_manifest_rows.XXXXXX")"
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

  output_path="$(repo_output_path "$repo")"
  summary_path="$(repo_summary_path "$repo")"
  log_file="$LOG_DIR/${repo//\//__}.sonarqube_snapshot.log"

  echo "[$index] repo=$repo primary_language=$primary_language branch=$BRANCH interval=$INTERVAL"
  if [[ "$DRY_RUN" == "1" ]]; then
    build_sonarqube_command "$repo" cmd
    printf '  '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    processed=$((processed + 1))
    continue
  fi

  wait_for_job_slot
  row_file="$RUN_ROWS_DIR/${index}_${repo//\//__}.csv"
  run_repo \
    "$index" "$repo" "$primary_language" \
    "$output_path" "$summary_path" "$log_file" "$row_file" &
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
  echo "done: processed=$processed failed=$failed dry_run=1"
else
  echo "done: processed=$processed failed=$failed manifest=$MANIFEST_PATH"
fi
if (( failed > 0 )); then
  exit 1
fi
