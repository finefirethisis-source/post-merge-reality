#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CSV_PATH="${CSV_PATH:-${1:-initial_sample_repos.csv}}"
PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
CODEBASE_AGENT_SHARE="${CODEBASE_AGENT_SHARE:-src/webscrape/codebase_agent_share.py}"
OUTPUT_DIR="${OUTPUT_DIR:-src/webscrape/output}"
CLONE_DIR="${CLONE_DIR:-src/webscrape/repos}"
LOG_DIR="${LOG_DIR:-src/webscrape/run_logs}"
MANIFEST_PATH="${MANIFEST_PATH:-$LOG_DIR/initial_sample_codebase_agent_share_manifest.csv}"

BRANCH="${BRANCH:-auto}"
SINCE="${SINCE:-}"
UNTIL="${UNTIL:-}"
SNAPSHOT_DATE="${SNAPSHOT_DATE:-}"
INTERVAL="${INTERVAL:-weekly}"
NO_FINAL_SNAPSHOT="${NO_FINAL_SNAPSHOT:-0}"
INCLUDE_EXTENSION="${INCLUDE_EXTENSION:-}"
EXCLUDE_TEST_FILES="${EXCLUDE_TEST_FILES:-0}"
MAX_SNAPSHOTS="${MAX_SNAPSHOTS:-}"
MAX_FILES_PER_SNAPSHOT="${MAX_FILES_PER_SNAPSHOT:-}"
MAX_REPOS="${MAX_REPOS:-}"
START_AT="${START_AT:-1}"
REPO_WORKERS="${REPO_WORKERS:-1}"
BLAME_WORKERS="${BLAME_WORKERS:-4}"
GIT_TIMEOUT="${GIT_TIMEOUT:-300}"
IGNORE_REVS_FILE="${IGNORE_REVS_FILE:-}"
SKIP_FETCH="${SKIP_FETCH:-1}"
KEEP_CLONE="${KEEP_CLONE:-0}"
FULL_CLONE="${FULL_CLONE:-0}"
USE_GITHUB_API="${USE_GITHUB_API:-0}"
TOKEN="${TOKEN:-${GITHUB_TOKEN:-}}"
GITHUB_API_SLEEP="${GITHUB_API_SLEEP:-0.05}"
NO_CACHE="${NO_CACHE:-0}"
PROGRESS="${PROGRESS:-auto}"
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

if [[ ! -f "$CODEBASE_AGENT_SHARE" ]]; then
  echo "Codebase agent-share script not found: $CODEBASE_AGENT_SHARE" >&2
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

if ! [[ "$BLAME_WORKERS" =~ ^[0-9]+$ ]] || (( BLAME_WORKERS < 1 )); then
  echo "BLAME_WORKERS must be a positive integer" >&2
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

for flag_name in NO_FINAL_SNAPSHOT EXCLUDE_TEST_FILES SKIP_FETCH KEEP_CLONE FULL_CLONE USE_GITHUB_API NO_CACHE DRY_RUN RESUME; do
  flag_value="${!flag_name}"
  if [[ "$flag_value" != "0" && "$flag_value" != "1" ]]; then
    echo "$flag_name must be 0 or 1" >&2
    exit 1
  fi
done

if [[ "$DRY_RUN" != "1" && ! -f "$MANIFEST_PATH" ]]; then
  printf '"index","repo","primary_language","branch","since","until","snapshot_date","interval","include_extensions","exclude_test_files","repo_workers","blame_workers","status","exit_code","started_at","finished_at","log_file","output_path"\n' > "$MANIFEST_PATH"
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

local_repo_path() {
  local repo="$1"
  printf '%s/%s\n' "$CLONE_DIR" "$(repo_key "$repo")"
}

default_branch_for_repo() {
  local repo="$1"
  local repo_dir
  local branch
  repo_dir="$(local_repo_path "$repo")"
  if [[ -d "$repo_dir/.git" ]]; then
    branch="$(git -C "$repo_dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    branch="${branch#origin/}"
    if [[ -n "$branch" ]]; then
      echo "$branch"
      return
    fi
    branch="$(git -C "$repo_dir" branch --show-current 2>/dev/null || true)"
    if [[ -n "$branch" ]]; then
      echo "$branch"
      return
    fi
  fi
  branch="$(
    git ls-remote --symref "https://github.com/${repo}.git" HEAD 2>/dev/null \
      | awk '/^ref:/ { sub("refs/heads/", "", $2); print $2; exit }'
  )"
  if [[ -n "$branch" ]]; then
    echo "$branch"
  else
    echo "main"
  fi
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
  local include_extensions="$9"
  local exclude_test_files="${10}"
  local repo_workers="${11}"
  local blame_workers="${12}"
  local status="${13}"
  local exit_code="${14}"
  local started_at="${15}"
  local finished_at="${16}"
  local log_file="${17}"
  local output_path="${18}"

  {
    csv_escape "$index"; printf ','
    csv_escape "$repo"; printf ','
    csv_escape "$language"; printf ','
    csv_escape "$branch"; printf ','
    csv_escape "$since"; printf ','
    csv_escape "$until"; printf ','
    csv_escape "$snapshot_date"; printf ','
    csv_escape "$interval"; printf ','
    csv_escape "$include_extensions"; printf ','
    csv_escape "$exclude_test_files"; printf ','
    csv_escape "$repo_workers"; printf ','
    csv_escape "$blame_workers"; printf ','
    csv_escape "$status"; printf ','
    csv_escape "$exit_code"; printf ','
    csv_escape "$started_at"; printf ','
    csv_escape "$finished_at"; printf ','
    csv_escape "$log_file"; printf ','
    csv_escape "$output_path"; printf '\n'
  } >> "$MANIFEST_PATH"
}

write_manifest_row_file() {
  local path="$1"
  shift
  : > "$path"
  MANIFEST_PATH="$path" append_manifest_row "$@"
}

build_codebase_share_command() {
  local repo="$1"
  local branch="$2"
  local -n out_cmd="$3"
  local values=()
  local value

  out_cmd=(
    "$PYTHON_BIN" "$CODEBASE_AGENT_SHARE"
    --repo "$repo"
    --output-dir "$OUTPUT_DIR"
    --clone-dir "$CLONE_DIR"
    --branch "$branch"
    --interval "$INTERVAL"
    --workers "$BLAME_WORKERS"
    --git-timeout "$GIT_TIMEOUT"
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
  if [[ -n "$INCLUDE_EXTENSION" ]]; then
    split_csv_values "$INCLUDE_EXTENSION" values
    for value in "${values[@]}"; do
      out_cmd+=(--include-extension "$value")
    done
  fi
  if [[ "$EXCLUDE_TEST_FILES" == "1" ]]; then
    out_cmd+=(--exclude-test-files)
  fi
  if [[ -n "$MAX_SNAPSHOTS" ]]; then
    out_cmd+=(--max-snapshots "$MAX_SNAPSHOTS")
  fi
  if [[ -n "$MAX_FILES_PER_SNAPSHOT" ]]; then
    out_cmd+=(--max-files-per-snapshot "$MAX_FILES_PER_SNAPSHOT")
  fi
  if [[ -n "$IGNORE_REVS_FILE" ]]; then
    out_cmd+=(--ignore-revs-file "$IGNORE_REVS_FILE")
  fi
  if [[ "$SKIP_FETCH" == "1" ]]; then
    out_cmd+=(--skip-fetch)
  fi
  if [[ "$KEEP_CLONE" == "1" ]]; then
    out_cmd+=(--keep-clone)
  fi
  if [[ "$FULL_CLONE" == "1" ]]; then
    out_cmd+=(--full-clone)
  fi
  if [[ "$USE_GITHUB_API" == "1" ]]; then
    out_cmd+=(--use-github-api --github-api-sleep "$GITHUB_API_SLEEP")
  fi
  if [[ "$NO_CACHE" == "1" ]]; then
    out_cmd+=(--no-cache)
  fi
}

run_repo() {
  local index="$1"
  local repo="$2"
  local primary_language="$3"
  local selected_branch="$4"
  local output_path="$5"
  local log_file="$6"
  local row_file="$7"

  local cmd
  build_codebase_share_command "$repo" "$selected_branch" cmd

  {
    echo "repo=$repo"
    echo "index=$index"
    echo "primary_language=$primary_language"
    echo "branch=$selected_branch"
    echo "since=$SINCE"
    echo "until=$UNTIL"
    echo "snapshot_date=$SNAPSHOT_DATE"
    echo "interval=$INTERVAL"
    echo "include_extensions=${INCLUDE_EXTENSION:-all}"
    echo "exclude_test_files=$EXCLUDE_TEST_FILES"
    echo "repo_workers=$REPO_WORKERS"
    echo "blame_workers=$BLAME_WORKERS"
    echo "keep_clone=$KEEP_CLONE"
    if [[ "$USE_GITHUB_API" == "1" && -n "$TOKEN" ]]; then
      echo "github_token_configured=1"
    else
      echo "github_token_configured=0"
    fi
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
  if [[ "$USE_GITHUB_API" == "1" && -n "$TOKEN" ]]; then
    GITHUB_TOKEN="$TOKEN" "${cmd[@]}" >> "$log_file" 2>&1
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
    "$index" "$repo" "$primary_language" "$selected_branch" \
    "$SINCE" "$UNTIL" "$SNAPSHOT_DATE" "$INTERVAL" "${INCLUDE_EXTENSION:-all}" \
    "$EXCLUDE_TEST_FILES" "$REPO_WORKERS" "$BLAME_WORKERS" \
    "$status" "$exit_code" "$started_at" "$finished_at" "$log_file" "$output_path"
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
  RUN_ROWS_DIR="$(mktemp -d "$LOG_DIR/codebase_agent_share_manifest_rows.XXXXXX")"
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

  selected_branch="$BRANCH"
  if [[ "$selected_branch" == "auto" ]]; then
    selected_branch="$(default_branch_for_repo "$repo")"
  fi

  output_path="$(repo_output_path "$repo")"
  log_file="$LOG_DIR/${repo//\//__}.codebase_agent_share.log"

  if [[ "$DRY_RUN" != "1" && "$RESUME" == "1" \
    && -s "$output_path/codebase_agent_share_snapshots.jsonl" \
    && -s "$output_path/codebase_agent_share_files.jsonl" ]]; then
    echo "[$index] skip repo=$repo primary_language=$primary_language existing codebase-share output=$output_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$selected_branch" \
      "$SINCE" "$UNTIL" "$SNAPSHOT_DATE" "$INTERVAL" "${INCLUDE_EXTENSION:-all}" \
      "$EXCLUDE_TEST_FILES" "$REPO_WORKERS" "$BLAME_WORKERS" \
      "skipped_existing_output" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
      "$log_file" "$output_path"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  echo "[$index] repo=$repo primary_language=$primary_language branch=$selected_branch interval=$INTERVAL include_extensions=${INCLUDE_EXTENSION:-all}"
  if [[ "$DRY_RUN" == "1" ]]; then
    build_codebase_share_command "$repo" "$selected_branch" cmd
    printf '  '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    processed=$((processed + 1))
    continue
  fi

  wait_for_job_slot
  row_file="$RUN_ROWS_DIR/${index}_${repo//\//__}.csv"
  run_repo \
    "$index" "$repo" "$primary_language" "$selected_branch" \
    "$output_path" "$log_file" "$row_file" &
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
