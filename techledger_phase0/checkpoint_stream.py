from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

INPUT = Path("dev/analysis/output_rq2/rq2_repo_week_panel.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")

CANDIDATES = [
    "week",
    "repo",
    "repo_key",
    "total_commits",
    "total_changed_lines",
    "corrective_commits",
    "corrective_changed_lines",
    "corrective_rate",
    "corrective_line_rate",
    "living_tracked_lines_at_week_start",
    "tracked_line_churn_rate",
    "sonar_scan_count",
    "sonar_ncloc",
    "sonar_issue_count",
    "sonar_bug_count",
    "sonar_vulnerability_count",
    "sonar_code_smell_count",
    "sonar_complexity",
    "sonar_cognitive_complexity",
    "sonar_duplicated_lines_density",
    "sonar_sqale_index",
    "sonar_maintainability_effort",
    "ai_share",
]


def main() -> None:
    row_count = 0
    repos: set[str] = set()
    weeks: list[str] = []
    nonempty = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    sample_rows: list[dict[str, str]] = []

    with INPUT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        schema = reader.fieldnames or []
        present = [c for c in CANDIDATES if c in schema]
        for row in reader:
            row_count += 1
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            if repo:
                repos.add(repo)
            week = (row.get("week") or "").strip()
            if week:
                weeks.append(week)
            if len(sample_rows) < 4:
                sample_rows.append({c: row.get(c, "") for c in present})
            for col in present:
                value = (row.get(col) or "").strip()
                if value:
                    nonempty[col] += 1
                    if value not in examples[col] and len(examples[col]) < 5:
                        examples[col].append(value)

    summary = {
        "purpose": "Freeze a leakage-safe conventional baseline using repository context from the week before each feature admission.",
        "row_count": row_count,
        "unique_repositories": len(repos),
        "week_min_lexical": min(weeks) if weeks else None,
        "week_max_lexical": max(weeks) if weeks else None,
        "schema": schema,
        "candidate_fields": {
            col: {
                "nonempty_rows": nonempty[col],
                "coverage": nonempty[col] / row_count if row_count else None,
                "example_values": examples[col],
            }
            for col in present
        },
        "sample_rows": sample_rows,
        "baseline_rule": {
            "time_alignment": "For an admission in week W, use only repository-week values from W-1 or earlier.",
            "planned_core_predictors": [
                "log(asset tracked_line_count)",
                "prior-week total_commits",
                "prior-week total_changed_lines",
                "prior-week corrective_commits or corrective rate",
                "prior-week tracked_line_churn_rate",
                "prior-week living_tracked_lines_at_week_start",
            ],
            "planned_optional_predictors_if_coverage_allows": [
                "prior-week Sonar codebase complexity/issue/maintainability measures"
            ],
            "explicitly_excluded": [
                "admission-week activity",
                "post-admission termination/survival fields",
                "future corrective outcomes",
                "TechLedger-specific estate-context variables"
            ]
        }
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
