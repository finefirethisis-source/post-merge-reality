from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
META = Path("filtered_full_sample_repos.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def is_test_repo(repo: str) -> bool:
    h = int(hashlib.sha256(repo.encode("utf-8")).hexdigest()[:8], 16)
    return h % 10 < 3


def main() -> None:
    counts = Counter()
    ai_counts = Counter()
    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            if as_int(row.get("tracked_line_count")) < 20:
                continue
            dt = parse_dt(row.get("origin_commit_date"))
            if dt is None or dt > MATURE_CUTOFF:
                continue
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            if not repo or not is_test_repo(repo):
                continue
            counts[repo] += 1
            if (row.get("origin_role") or "").strip() == "AI":
                ai_counts[repo] += 1

    metadata = {}
    with META.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo_slug") or "").strip()
            if repo:
                metadata[repo] = row

    candidates = []
    for repo, n in counts.items():
        m = metadata.get(repo, {})
        candidates.append({
            "repo": repo,
            "mature_feature_assets": n,
            "ai_feature_assets": ai_counts[repo],
            "primary_language": m.get("primary_language"),
            "stars": as_int(m.get("num_stars")),
            "forks": as_int(m.get("num_forks")),
            "min_monthly_commits_observed": as_int(m.get("min_monthly_commits_observed")),
            "prs": as_int(m.get("num_PRs")),
        })

    # Pilot-eligible: enough assets for within-repo comparisons, but avoid the very
    # largest cohorts on the first cloning pass. Sort toward ~100 assets and then
    # stable name order for reproducibility.
    eligible = [c for c in candidates if 40 <= c["mature_feature_assets"] <= 250]
    eligible.sort(key=lambda c: (abs(c["mature_feature_assets"] - 100), c["repo"].lower()))

    summary = {
        "purpose": "Select a small held-out-repository pilot for file-local pre-admission churn enrichment.",
        "test_repositories_total": len(counts),
        "pilot_eligibility": "deterministic held-out repo; 40-250 mature substantive feature assets",
        "eligible_repositories": len(eligible),
        "recommended_first_pilot": eligible[:10],
        "all_eligible": eligible,
        "next_step_if_approved_by_results": "Clone a small subset with git --filter=blob:none and compute 30/90-day pre-admission churn only for files touched by each origin commit.",
    }
    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
