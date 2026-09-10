from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INPUT = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
OBSERVATION_END = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)  # >=180 days observed


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


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def concentration(rows: list[dict], fraction: float) -> dict:
    if not rows:
        return {"asset_count": 0, "burden_share": None, "tracked_line_share": None, "burden_to_size_ratio": None}
    ranked = sorted(rows, key=lambda r: r["terminated"], reverse=True)
    n = max(1, round(len(ranked) * fraction))
    top = ranked[:n]
    total_burden = sum(r["terminated"] for r in ranked)
    total_size = sum(r["tracked"] for r in ranked)
    top_burden = sum(r["terminated"] for r in top)
    top_size = sum(r["tracked"] for r in top)
    burden_share = top_burden / total_burden if total_burden else 0.0
    size_share = top_size / total_size if total_size else 0.0
    return {
        "asset_count": n,
        "burden_share": burden_share,
        "tracked_line_share": size_share,
        "burden_to_size_ratio": burden_share / size_share if size_share else None,
    }


def stats(rows: list[dict]) -> dict:
    rates = [r["rate"] for r in rows]
    return {
        "asset_count": len(rows),
        "aggregate_termination_rate": (
            sum(r["terminated"] for r in rows) / sum(r["tracked"] for r in rows)
            if rows and sum(r["tracked"] for r in rows) else None
        ),
        "termination_rate_p50": quantile(rates, 0.50),
        "termination_rate_p90": quantile(rates, 0.90),
        "zero_burden_asset_share": (sum(1 for r in rows if r["terminated"] == 0) / len(rows)) if rows else None,
        "top_10pct_raw_burden_concentration": concentration(rows, 0.10),
    }


def size_band(n: int) -> str:
    if n < 50:
        return "20-49"
    if n < 100:
        return "50-99"
    if n < 250:
        return "100-249"
    if n < 500:
        return "250-499"
    if n < 1000:
        return "500-999"
    return "1000+"


def main() -> None:
    mature: list[dict] = []
    by_repo: dict[str, list[dict]] = defaultdict(list)
    by_band: dict[str, list[dict]] = defaultdict(list)

    with INPUT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            tracked = as_int(row.get("tracked_line_count"))
            if tracked < 20:
                continue
            dt = parse_dt(row.get("origin_commit_date"))
            if dt is None or dt > MATURE_CUTOFF:
                continue
            terminated = as_int(row.get("terminated_line_count"))
            repo = row.get("repo") or row.get("repo_key") or "unknown"
            item = {
                "repo": repo,
                "tracked": tracked,
                "terminated": terminated,
                "rate": as_float(row.get("termination_rate")),
            }
            mature.append(item)
            by_repo[repo].append(item)
            by_band[size_band(tracked)].append(item)

    repo_metrics = []
    for repo, rows in by_repo.items():
        if len(rows) < 20:
            continue
        c = concentration(rows, 0.10)
        repo_metrics.append({
            "repo": repo,
            "asset_count": len(rows),
            "top10_burden_share": c["burden_share"],
            "top10_size_share": c["tracked_line_share"],
            "top10_burden_to_size_ratio": c["burden_to_size_ratio"],
        })

    valid_burden = [r["top10_burden_share"] for r in repo_metrics if r["top10_burden_share"] is not None]
    valid_size = [r["top10_size_share"] for r in repo_metrics if r["top10_size_share"] is not None]
    valid_ratio = [r["top10_burden_to_size_ratio"] for r in repo_metrics if r["top10_burden_to_size_ratio"] is not None]

    band_order = ["20-49", "50-99", "100-249", "250-499", "500-999", "1000+"]
    summary = {
        "definition": {
            "asset": "origin commit classified as feature",
            "substantive_threshold": "at least 20 tracked lines",
            "maturity_rule": "origin date on or before 2025-12-02, giving at least 180 days before 2026-05-31 observation end",
            "burden_proxy": "count of original tracked lines later modified or deleted at least once",
            "important_limit": "lower-bound first-intervention proxy, not total lifetime maintenance effort",
        },
        "mature_substantive_features": stats(mature),
        "size_band_results": {band: stats(by_band[band]) for band in band_order if by_band[band]},
        "within_repository_results": {
            "eligible_repo_count": len(repo_metrics),
            "eligibility": "at least 20 mature substantive feature assets",
            "median_repo_top10_burden_share": quantile(valid_burden, 0.50),
            "median_repo_top10_tracked_line_share": quantile(valid_size, 0.50),
            "median_repo_top10_burden_to_size_ratio": quantile(valid_ratio, 0.50),
            "p25_repo_top10_burden_share": quantile(valid_burden, 0.25),
            "p75_repo_top10_burden_share": quantile(valid_burden, 0.75),
        },
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
