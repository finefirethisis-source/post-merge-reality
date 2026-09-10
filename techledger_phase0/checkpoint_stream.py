from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)


def as_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


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


def concentration(rows: list[dict], metric: str, fraction: float) -> dict:
    if not rows:
        return {"asset_count": 0, "burden_share": None, "tracked_line_share": None, "burden_to_size_ratio": None}
    ranked = sorted(rows, key=lambda r: r[metric], reverse=True)
    n = max(1, round(len(ranked) * fraction))
    top = ranked[:n]
    total_burden = sum(r[metric] for r in ranked)
    total_size = sum(r["tracked"] for r in ranked)
    top_burden = sum(r[metric] for r in top)
    top_size = sum(r["tracked"] for r in top)
    burden_share = top_burden / total_burden if total_burden else 0.0
    size_share = top_size / total_size if total_size else 0.0
    return {
        "asset_count": n,
        "burden_share": burden_share,
        "tracked_line_share": size_share,
        "burden_to_size_ratio": burden_share / size_share if size_share else None,
    }


def concentration_set(rows: list[dict], metric: str) -> dict:
    return {
        "top_1pct": concentration(rows, metric, 0.01),
        "top_5pct": concentration(rows, metric, 0.05),
        "top_10pct": concentration(rows, metric, 0.10),
        "top_20pct": concentration(rows, metric, 0.20),
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


def metric_stats(rows: list[dict], metric: str) -> dict:
    values = [float(r[metric]) for r in rows]
    nonzero = [v for v in values if v > 0]
    return {
        "total": sum(values),
        "zero_asset_share": (sum(1 for v in values if v == 0) / len(values)) if values else None,
        "p50_all_assets": quantile(values, 0.50),
        "p90_all_assets": quantile(values, 0.90),
        "p99_all_assets": quantile(values, 0.99),
        "p50_nonzero_assets": quantile(nonzero, 0.50),
        "p90_nonzero_assets": quantile(nonzero, 0.90),
        "p99_nonzero_assets": quantile(nonzero, 0.99),
        "concentration": concentration_set(rows, metric),
    }


def main() -> None:
    # Build the admission cohort from RQ1a: feature intent, >=20 tracked lines,
    # and >=180 days of observation by the common 2026-05-31 endpoint.
    cohort: dict[tuple[str, str], dict] = {}
    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
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
            repo = row.get("repo") or row.get("repo_key") or ""
            sha = row.get("origin_commit_sha") or ""
            if not repo or not sha:
                continue
            cohort[(repo, sha)] = {
                "repo": repo,
                "sha": sha,
                "tracked": tracked,
                "corrective_lines": 0,
                "corrective_commits": 0,
            }

    matched = 0
    duplicate_keys = 0
    seen: set[tuple[str, str]] = set()
    with RQ1B.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = row.get("repo") or row.get("repo_key") or ""
            sha = row.get("origin_commit_sha") or ""
            key = (repo, sha)
            if key not in cohort:
                continue
            if key in seen:
                duplicate_keys += 1
                continue
            seen.add(key)
            matched += 1
            cohort[key]["corrective_lines"] = as_int(row.get("terminal_corrective_line_count"))
            cohort[key]["corrective_commits"] = as_int(row.get("terminal_corrective_commit_count"))

    rows = list(cohort.values())
    by_repo: dict[str, list[dict]] = defaultdict(list)
    by_band: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
        by_band[size_band(row["tracked"])].append(row)

    repo_line_shares: list[float] = []
    repo_commit_shares: list[float] = []
    repo_line_ratios: list[float] = []
    repo_commit_ratios: list[float] = []
    eligible_repo_count = 0
    for repo_rows in by_repo.values():
        if len(repo_rows) < 20:
            continue
        eligible_repo_count += 1
        c_lines = concentration(repo_rows, "corrective_lines", 0.10)
        c_commits = concentration(repo_rows, "corrective_commits", 0.10)
        if c_lines["burden_share"] is not None:
            repo_line_shares.append(float(c_lines["burden_share"]))
        if c_commits["burden_share"] is not None:
            repo_commit_shares.append(float(c_commits["burden_share"]))
        if c_lines["burden_to_size_ratio"] is not None:
            repo_line_ratios.append(float(c_lines["burden_to_size_ratio"]))
        if c_commits["burden_to_size_ratio"] is not None:
            repo_commit_ratios.append(float(c_commits["burden_to_size_ratio"]))

    band_order = ["20-49", "50-99", "100-249", "250-499", "500-999", "1000+"]
    size_band_results = {}
    for band in band_order:
        band_rows = by_band.get(band, [])
        if not band_rows:
            continue
        size_band_results[band] = {
            "asset_count": len(band_rows),
            "corrective_lines_top10": concentration(band_rows, "corrective_lines", 0.10),
            "corrective_commits_top10": concentration(band_rows, "corrective_commits", 0.10),
        }

    summary = {
        "definition": {
            "asset": "origin commit classified as feature, at least 20 tracked lines, at least 180 days observed",
            "corrective_line_proxy": "original lines whose first terminal intervention was classified corrective",
            "corrective_event_proxy": "distinct terminal corrective commits represented among first interventions of original lines",
            "important_limit": "Both are lower-bound first-intervention measures; they do not count every later maintenance touch to the asset.",
        },
        "join_quality": {
            "cohort_assets": len(cohort),
            "matched_in_rq1b": matched,
            "unmatched": len(cohort) - matched,
            "duplicate_join_keys_in_rq1b": duplicate_keys,
        },
        "corrective_lines": metric_stats(rows, "corrective_lines"),
        "corrective_commits": metric_stats(rows, "corrective_commits"),
        "within_repository_top10": {
            "eligible_repo_count": eligible_repo_count,
            "eligibility": "at least 20 mature substantive feature assets",
            "median_corrective_line_burden_share": quantile(repo_line_shares, 0.50),
            "p25_corrective_line_burden_share": quantile(repo_line_shares, 0.25),
            "p75_corrective_line_burden_share": quantile(repo_line_shares, 0.75),
            "median_corrective_line_burden_to_size_ratio": quantile(repo_line_ratios, 0.50),
            "median_corrective_commit_burden_share": quantile(repo_commit_shares, 0.50),
            "p25_corrective_commit_burden_share": quantile(repo_commit_shares, 0.25),
            "p75_corrective_commit_burden_share": quantile(repo_commit_shares, 0.75),
            "median_corrective_commit_burden_to_size_ratio": quantile(repo_commit_ratios, 0.50),
        },
        "size_band_top10": size_band_results,
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
