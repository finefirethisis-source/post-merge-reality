from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

INPUT = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")


def as_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def main() -> None:
    row_count = 0
    assets: set[tuple[str, str]] = set()
    repos: set[str] = set()
    roles = Counter()
    intents = Counter()
    termination_rates: list[float] = []
    tracked_lines: list[float] = []
    first_termination_days: list[float] = []

    with INPUT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        for row in reader:
            row_count += 1
            repo = row.get("repo") or row.get("repo_key") or ""
            sha = row.get("origin_commit_sha") or ""
            if repo and sha:
                assets.add((repo, sha))
            if repo:
                repos.add(repo)
            roles[row.get("origin_role") or "unknown"] += 1
            intents[row.get("origin_primary_operational_intent") or "unknown"] += 1
            for key, target in (
                ("termination_rate", termination_rates),
                ("tracked_line_count", tracked_lines),
                ("first_termination_days", first_termination_days),
            ):
                value = as_float(row.get(key))
                if value is not None:
                    target.append(value)

    def dist(values: list[float]) -> dict[str, float | int | None]:
        return {
            "n": len(values),
            "p50": quantile(values, 0.50),
            "p75": quantile(values, 0.75),
            "p90": quantile(values, 0.90),
            "p95": quantile(values, 0.95),
            "p99": quantile(values, 0.99),
            "max": max(values) if values else None,
        }

    summary = {
        "input": str(INPUT),
        "schema": fields,
        "row_count": row_count,
        "unique_origin_assets": len(assets),
        "unique_repositories": len(repos),
        "origin_roles": dict(roles),
        "top_origin_intents": intents.most_common(20),
        "termination_rate_distribution": dist(termination_rates),
        "tracked_line_count_distribution": dist(tracked_lines),
        "first_termination_days_distribution": dist(first_termination_days),
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
