"""Add admission-time contributor standing and local ownership markers.

Every marker uses only function-attributed repository events strictly before the
admission commit and within a fixed 180-day lookback. This avoids future leakage
and avoids giving later admissions a longer history window.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict, deque
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from techledger_mvp import block_churn_scan as scan  # noqa: E402

INPUT = scan.OUTPUT / "commit_metrics.csv"
SUMMARY = scan.OUTPUT / "scan_summary.json"
LOOKBACK_DAYS = 180

NEW_FIELDS = [
    "author_180d_function_commits",
    "author_180d_function_commit_share",
    "author_180d_contributor_percentile",
    "author_180d_contributor_rank_from_top",
    "contributors_active_180d",
    "author_block_commit_share_180d",
    "author_block_line_share_180d",
    "author_existing_block_experience_fraction_180d",
    "author_majority_owner_block_fraction_180d",
    "author_last_toucher_block_fraction_180d",
    "touched_blocks_with_history_fraction_180d",
    "author_file_commit_share_180d",
    "author_file_line_share_180d",
    "author_existing_file_experience_fraction_180d",
    "author_majority_owner_file_fraction_180d",
    "author_last_toucher_file_fraction_180d",
    "touched_files_with_history_fraction_180d",
]


def load_rows() -> list[dict[str, object]]:
    with INPUT.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_rows(rows: list[dict[str, object]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with INPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def contributor_standing(counts: Counter[str], author: str) -> tuple[int, float, float, int, int]:
    total = sum(counts.values())
    author_count = counts.get(author, 0)
    active = len(counts)
    if not active:
        return 0, 0.0, 0.0, 1, 0
    values = list(counts.values())
    lower = sum(value < author_count for value in values)
    equal = sum(value == author_count for value in values)
    percentile = (lower + 0.5 * equal) / active
    rank_from_top = 1 + sum(value > author_count for value in values)
    return author_count, author_count / total, percentile, rank_from_top, active


def local_stats(histories, author: str, date) -> dict[str, float]:
    cutoff = date - timedelta(days=LOOKBACK_DAYS)
    existing = experienced = majority = last_toucher = 0
    total_commits = author_commits = total_lines = author_lines = 0

    for history in histories:
        prior = [item for item in history if cutoff <= item[0] < date]
        if not prior:
            continue
        existing += 1
        total_commits += len(prior)
        own_commits = sum(item[1] == author for item in prior)
        author_commits += own_commits
        lines = sum(item[2] for item in prior)
        own_lines = sum(item[2] for item in prior if item[1] == author)
        total_lines += lines
        author_lines += own_lines
        if own_commits:
            experienced += 1
        if lines and own_lines / lines >= 0.5:
            majority += 1
        if prior[-1][1] == author:
            last_toucher += 1

    return {
        "commit_share": author_commits / total_commits if total_commits else math.nan,
        "line_share": author_lines / total_lines if total_lines else math.nan,
        "experience_fraction": experienced / existing if existing else math.nan,
        "majority_fraction": majority / existing if existing else math.nan,
        "last_toucher_fraction": last_toucher / existing if existing else math.nan,
        "existing": existing,
    }


def enrich_repo(slug: str, rows_by_key: dict[tuple[str, str], dict[str, object]], packaged_labels) -> None:
    events, _ = scan.stream_repo(slug, packaged_labels)
    recent_events = deque()
    recent_counts: Counter[str] = Counter()
    block_history = defaultdict(list)
    file_history = defaultdict(list)

    for event in events:
        cutoff = event.date - timedelta(days=LOOKBACK_DAYS)
        while recent_events and recent_events[0][0] < cutoff:
            _, old_author = recent_events.popleft()
            recent_counts[old_author] -= 1
            if recent_counts[old_author] <= 0:
                del recent_counts[old_author]

        row = rows_by_key.get((slug, event.sha))
        file_lines: Counter[str] = Counter()
        for touch in event.blocks.values():
            file_lines[touch.path] += touch.changed_lines

        if row is not None:
            count, share, percentile, rank, active = contributor_standing(recent_counts, event.author_email)
            row["author_180d_function_commits"] = count
            row["author_180d_function_commit_share"] = share
            row["author_180d_contributor_percentile"] = percentile
            row["author_180d_contributor_rank_from_top"] = rank
            row["contributors_active_180d"] = active

            block_stats = local_stats([block_history[key] for key in event.blocks], event.author_email, event.date)
            row["author_block_commit_share_180d"] = block_stats["commit_share"]
            row["author_block_line_share_180d"] = block_stats["line_share"]
            row["author_existing_block_experience_fraction_180d"] = block_stats["experience_fraction"]
            row["author_majority_owner_block_fraction_180d"] = block_stats["majority_fraction"]
            row["author_last_toucher_block_fraction_180d"] = block_stats["last_toucher_fraction"]
            row["touched_blocks_with_history_fraction_180d"] = block_stats["existing"] / max(1, len(event.blocks))

            file_stats = local_stats([file_history[path] for path in file_lines], event.author_email, event.date)
            row["author_file_commit_share_180d"] = file_stats["commit_share"]
            row["author_file_line_share_180d"] = file_stats["line_share"]
            row["author_existing_file_experience_fraction_180d"] = file_stats["experience_fraction"]
            row["author_majority_owner_file_fraction_180d"] = file_stats["majority_fraction"]
            row["author_last_toucher_file_fraction_180d"] = file_stats["last_toucher_fraction"]
            row["touched_files_with_history_fraction_180d"] = file_stats["existing"] / max(1, len(file_lines))

        for key, touch in event.blocks.items():
            block_history[key].append((event.date, event.author_email, touch.changed_lines))
        for path, lines in file_lines.items():
            file_history[path].append((event.date, event.author_email, lines))
        recent_events.append((event.date, event.author_email))
        recent_counts[event.author_email] += 1


def main() -> None:
    rows = load_rows()
    rows_by_key = {(str(row["repo"]), str(row["sha"])): row for row in rows}
    packaged_labels = scan.load_packaged_labels()

    for slug in scan.TARGET_REPOS:
        print(f"Enriching {slug}", flush=True)
        enrich_repo(slug, rows_by_key, packaged_labels)

    enriched = list(rows_by_key.values())
    write_rows(enriched)

    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    summary["contributor_context_enrichment"] = {
        "lookback_days": LOOKBACK_DAYS,
        "temporal_rule": "Only function-attributed events strictly before the admission commit in a fixed 180-day lookback are used.",
        "identity": "Lower-cased Git author email on the landed default-branch commit; identities are not unified across multiple emails. On merge commits this may represent the merger/integrator rather than every original code author.",
        "rank_basis": "Prior function-attributed default-branch events in the repository.",
        "local_ownership_basis": "Prior touches to the exact canonical function blocks and files used by the churn outcome.",
        "fields": NEW_FIELDS,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["contributor_context_enrichment"], indent=2), flush=True)


if __name__ == "__main__":
    main()
