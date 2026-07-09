#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CSV_PATH="${CSV_PATH:-${1:-initial_sample_repos.csv}}"
PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
SCRAPER="${SCRAPER:-src/webscrape/github_commit_scraper.py}"
OUTPUT_DIR="${OUTPUT_DIR:-src/webscrape/output}"
CLONE_DIR="${CLONE_DIR:-src/webscrape/repos}"
LOG_DIR="${LOG_DIR:-src/webscrape/run_logs}"
MANIFEST_PATH="${MANIFEST_PATH:-$LOG_DIR/initial_sample_scrape_manifest.csv}"

BRANCH="${BRANCH:-auto}"
SINCE="${SINCE:-}"
UNTIL="${UNTIL:-}"
MAX_COMMITS="${MAX_COMMITS:-}"
MAX_REPOS="${MAX_REPOS:-}"
START_AT="${START_AT:-1}"
REPO_WORKERS="${REPO_WORKERS:-1}"
BLAME_WORKERS="${BLAME_WORKERS:-4}"
PROGRESS="${PROGRESS:-auto}"
SKIP_GITHUB_API="${SKIP_GITHUB_API:-0}"
SKIP_FETCH="${SKIP_FETCH:-0}"
FULL_CLONE_FOR_BLAME="${FULL_CLONE_FOR_BLAME:-0}"
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

if [[ ! -f "$SCRAPER" ]]; then
  echo "Scraper not found: $SCRAPER" >&2
  exit 1
fi

if ! [[ "$REPO_WORKERS" =~ ^[0-9]+$ ]] || (( REPO_WORKERS < 1 )); then
  echo "REPO_WORKERS must be a positive integer" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "1" && ! -f "$MANIFEST_PATH" ]]; then
  printf '"index","repo","primary_language","include_extensions","branch","status","exit_code","started_at","finished_at","log_file"\n' > "$MANIFEST_PATH"
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

default_branch_for_repo() {
  local repo="$1"
  local branch
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

repo_output_path() {
  local repo="$1"
  printf '%s/%s\n' "$OUTPUT_DIR" "${repo//\//__}"
}

csv_escape() {
  local value="${1//\"/\"\"}"
  printf '"%s"' "$value"
}

append_manifest_row() {
  local index="$1"
  local repo="$2"
  local language="$3"
  local extensions="$4"
  local branch="$5"
  local status="$6"
  local exit_code="$7"
  local started_at="$8"
  local finished_at="$9"
  local log_file="${10}"

  {
    csv_escape "$index"; printf ','
    csv_escape "$repo"; printf ','
    csv_escape "$language"; printf ','
    csv_escape "$extensions"; printf ','
    csv_escape "$branch"; printf ','
    csv_escape "$status"; printf ','
    csv_escape "$exit_code"; printf ','
    csv_escape "$started_at"; printf ','
    csv_escape "$finished_at"; printf ','
    csv_escape "$log_file"; printf '\n'
  } >> "$MANIFEST_PATH"
}

write_manifest_row_file() {
  local path="$1"
  shift
  : > "$path"
  MANIFEST_PATH="$path" append_manifest_row "$@"
}

build_scraper_command() {
  local repo="$1"
  local branch="$2"
  local -n out_cmd="$3"

  out_cmd=(
    "$PYTHON_BIN" "$SCRAPER"
    --repo "$repo"
    --output-dir "$OUTPUT_DIR"
    --clone-dir "$CLONE_DIR"
    --branch "$branch"
    --blame-workers "$BLAME_WORKERS"
    --progress "$PROGRESS"
  )

  if [[ -n "$SINCE" ]]; then
    out_cmd+=(--since "$SINCE")
  fi
  if [[ -n "$UNTIL" ]]; then
    out_cmd+=(--until "$UNTIL")
  fi
  if [[ -n "$MAX_COMMITS" ]]; then
    out_cmd+=(--max-commits "$MAX_COMMITS")
  fi
  if [[ "$SKIP_GITHUB_API" == "1" ]]; then
    out_cmd+=(--skip-github-api)
  fi
  if [[ "$SKIP_FETCH" == "1" ]]; then
    out_cmd+=(--skip-fetch)
  fi
  if [[ "$FULL_CLONE_FOR_BLAME" == "1" ]]; then
    out_cmd+=(--full-clone-for-blame)
  fi
}

run_repo() {
  local index="$1"
  local repo="$2"
  local primary_language="$3"
  local extensions="$4"
  local selected_branch="$5"
  local output_path="$6"
  local log_file="$7"
  local row_file="$8"

  local cmd
  build_scraper_command "$repo" "$selected_branch" cmd

  {
    echo "repo=$repo"
    echo "index=$index"
    echo "primary_language=$primary_language"
    echo "include_extensions=all"
    echo "branch=$selected_branch"
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
    "$index" "$repo" "$primary_language" "$extensions" "$selected_branch" \
    "$status" "$exit_code" "$started_at" "$finished_at" "$log_file"
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
  RUN_ROWS_DIR="$(mktemp -d "$LOG_DIR/manifest_rows.XXXXXX")"
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

  extensions="all"
  selected_branch="$BRANCH"
  if [[ "$selected_branch" == "auto" ]]; then
    selected_branch="$(default_branch_for_repo "$repo")"
  fi

  output_path="$(repo_output_path "$repo")"
  log_file="$LOG_DIR/${repo//\//__}.log"

  if [[ "$DRY_RUN" != "1" && "$RESUME" == "1" && -s "$output_path/summary.json" ]]; then
    echo "[$index] skip repo=$repo primary_language=$primary_language output=$output_path"
    append_manifest_row \
      "$index" "$repo" "$primary_language" "$extensions" "$selected_branch" \
      "skipped_existing_output" "0" "" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$log_file"
    skipped=$((skipped + 1))
    processed=$((processed + 1))
    continue
  fi

  echo "[$index] repo=$repo primary_language=$primary_language include_extensions=all branch=$selected_branch"
  if [[ "$DRY_RUN" == "1" ]]; then
    build_scraper_command "$repo" "$selected_branch" cmd
    printf '  '
    printf '%q ' "${cmd[@]}"
    printf '\n'
    processed=$((processed + 1))
    continue
  fi

  wait_for_job_slot
  row_file="$RUN_ROWS_DIR/${index}_${repo//\//__}.csv"
  run_repo \
    "$index" "$repo" "$primary_language" "$extensions" "$selected_branch" \
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
