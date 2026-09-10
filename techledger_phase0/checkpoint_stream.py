from __future__ import annotations

import csv
import json
from pathlib import Path

INPUT = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")


def as_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def as_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def concentration(rows: list[dict[str, float | int]], fraction: float) -> dict[str, float | int | None]:
    if not rows:
        return {"asset_count": 0, "burden_share": None, "tracked_line_share": None, "burden_to_size_ratio": None}
    ranked = sorted(rows, key=lambda r: int(r["terminated"]), reverse=True)
    n = max(1, round(len(ranked) * fraction))
    top = ranked[:n]
    total_burden = sum(int(r["terminated"]) for r in ranked)
    total_size = sum(int(r["tracked"]) for r in ranked)
    top_burden = sum(int(r["terminated"]) for r in top)
    top_size = sum(int(r["tracked"]) for r in top)
    burden_share = top_burden / total_burden if total_burden else 0.0
    size_share = top_size / total_size if total_size else 0.0
    return {
        "asset_count": n,
        "burden_share": burden_share,
        "tracked_line_share": size_share,
        "burden_to_size_ratio": burden_share / size_share if size_share else None,
    }


def cohort_stats(rows: list[dict[str, float | int]]) -> dict:
    rates = [float(r["rate"]) for r in rows]
    tracked = [float(r["tracked"]) for r in rows]
    terminated = [float(r["terminated"]) for r in rows]
    total_tracked = sum(int(r["tracked"]) for r in rows)
    total_terminated = sum(int(r["terminated"]) for r in rows)
    return {
        "asset_count": len(rows),
        "zero_burden_asset_share": (sum(1 for r in rows if int(r["terminated"]) == 0) / len(rows)) if rows else None,
        "total_tracked_lines": total_tracked,
        "total_terminated_lines": total_terminated,
        "aggregate_termination_rate": total_terminated / total_tracked if total_tracked else None,
        "termination_rate_distribution": {
            "p50": quantile(rates, 0.50),
            "p75": quantile(rates, 0.75),
            "p90": quantile(rates, 0.90),
            "p95": quantile(rates, 0.95),
            "p99": quantile(rates, 0.99),
        },
        "tracked_line_distribution": {
            "p50": quantile(tracked, 0.50),
            "p90": quantile(tracked, 0.90),
            "p99": quantile(tracked, 0.99),
        },
        "terminated_line_distribution": {
            "p50": quantile(terminated, 0.50),
            "p90": quantile(terminated, 0.90),
            "p99": quantile(terminated, 0.99),
        },
        "burden_concentration": {
            "top_1pct": concentration(rows, 0.01),
            "top_5pct": concentration(rows, 0.05),
            "top_10pct": concentration(rows, 0.10),
            "top_20pct": concentration(rows, 0.20),
        },
    }


def main() -> None:
    features: list[dict[str, float | int]] = []

    with INPUT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            tracked = as_int(row.get("tracked_line_count"))
            terminated = as_int(row.get("terminated_line_count"))
            features.append({
                "tracked": tracked,
                "terminated": terminated,
                "rate": as_float(row.get("termination_rate")),
            })

    substantive = [r for r in features if int(r["tracked"]) >= 20]

    summary = {
        "definition": {
            "asset": "origin commit classified as feature",
            "raw_burden_proxy": "count of original tracked lines later modified or deleted at least once",
            "normalized_burden_proxy": "terminated original lines / original tracked lines",
            "important_limit": "This is a lower-bound first-intervention proxy, not total lifetime maintenance effort.",
            "substantive_asset_threshold": "at least 20 tracked lines",
        },
        "all_feature_assets": cohort_stats(features),
        "substantive_feature_assets": cohort_stats(substantive),
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
