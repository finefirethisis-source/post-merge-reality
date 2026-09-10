from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
PANEL = Path("dev/analysis/output_rq2/rq2_repo_week_panel.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
OBS_END = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)

MARKERS = [
    "asset_tracked_lines",
    "is_ai_origin",
    "prior_total_commits",
    "prior_total_changed_lines",
    "prior_corrective_commits",
    "prior_corrective_changed_lines",
    "prior_corrective_rate",
    "prior_corrective_line_rate",
    "prior_tracked_line_churn_rate",
    "prior_living_tracked_lines",
    "prior_ai_share",
    "prior_sonar_issue_density_per_kloc",
    "prior_sonar_bug_density_per_kloc",
    "prior_sonar_vulnerability_density_per_kloc",
    "prior_sonar_code_smell_density_per_kloc",
    "prior_sonar_complexity_per_kloc",
    "prior_sonar_cognitive_complexity_per_kloc",
    "prior_sonar_duplicated_lines_density",
    "prior_sonar_maintainability_effort_per_kloc",
]

PANEL_MAP = {
    "prior_total_commits": "total_commits",
    "prior_total_changed_lines": "total_changed_lines",
    "prior_corrective_commits": "corrective_commits",
    "prior_corrective_changed_lines": "corrective_changed_lines",
    "prior_corrective_rate": "corrective_rate",
    "prior_corrective_line_rate": "corrective_line_rate",
    "prior_tracked_line_churn_rate": "tracked_line_churn_rate",
    "prior_living_tracked_lines": "living_tracked_lines_at_week_start",
    "prior_ai_share": "ai_share",
    "prior_sonar_issue_density_per_kloc": "sonar_issue_density_per_kloc",
    "prior_sonar_bug_density_per_kloc": "sonar_bug_density_per_kloc",
    "prior_sonar_vulnerability_density_per_kloc": "sonar_vulnerability_density_per_kloc",
    "prior_sonar_code_smell_density_per_kloc": "sonar_code_smell_density_per_kloc",
    "prior_sonar_complexity_per_kloc": "sonar_complexity_per_kloc",
    "prior_sonar_cognitive_complexity_per_kloc": "sonar_cognitive_complexity_per_kloc",
    "prior_sonar_duplicated_lines_density": "sonar_duplicated_lines_density",
    "prior_sonar_maintainability_effort_per_kloc": "sonar_maintainability_effort_per_kloc",
}


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None) -> int:
    v = parse_float(value)
    return int(v) if v is not None else 0


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


def prior_week_key(dt: datetime) -> str:
    monday = dt - timedelta(days=dt.weekday())
    prior = (monday - timedelta(days=7)).date()
    return f"{prior.isoformat()}T00:00:00+00:00"


def is_test_repo(repo: str) -> bool:
    h = int(hashlib.sha256(repo.encode("utf-8")).hexdigest()[:8], 16)
    return h % 10 < 3


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda idx: values[idx])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and values[order[end]] == values[order[pos]]:
            end += 1
        avg_rank = (pos + 1 + end) / 2.0
        for j in range(pos, end):
            ranks[order[j]] = avg_rank
        pos = end
    return ranks


def pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(a) != len(b):
        return None
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return pearson(rankdata(xs), rankdata(ys))


def pairs_for(rows: list[dict], marker: str, outcome: str) -> list[tuple[float, float]]:
    out = []
    for r in rows:
        x = r.get(marker)
        y = r.get(outcome)
        if x is None or y is None:
            continue
        out.append((float(x), float(y)))
    return out


def global_capture(rows: list[dict], marker: str, direction: str, frac: float = 0.10) -> float | None:
    eligible = [r for r in rows if r.get(marker) is not None]
    if not eligible:
        return None
    total = sum(float(r["corrective_lines"]) for r in eligible)
    if total <= 0:
        return None
    reverse = direction == "high"
    ranked = sorted(eligible, key=lambda r: float(r[marker]), reverse=reverse)
    n = max(1, round(len(ranked) * frac))
    return sum(float(r["corrective_lines"]) for r in ranked[:n]) / total


def within_repo_capture(rows: list[dict], marker: str, direction: str, frac: float = 0.10) -> float | None:
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get(marker) is not None:
            by_repo[r["repo"]].append(r)
    shares: list[float] = []
    for repo_rows in by_repo.values():
        if len(repo_rows) < 20:
            continue
        total = sum(float(r["corrective_lines"]) for r in repo_rows)
        if total <= 0:
            continue
        reverse = direction == "high"
        ranked = sorted(repo_rows, key=lambda r: float(r[marker]), reverse=reverse)
        n = max(1, round(len(ranked) * frac))
        shares.append(sum(float(r["corrective_lines"]) for r in ranked[:n]) / total)
    return median(shares)


def median_within_repo_spearman(rows: list[dict], marker: str, outcome: str) -> float | None:
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get(marker) is not None and r.get(outcome) is not None:
            by_repo[r["repo"]].append(r)
    corrs: list[float] = []
    for repo_rows in by_repo.values():
        if len(repo_rows) < 20:
            continue
        corr = spearman([(float(r[marker]), float(r[outcome])) for r in repo_rows])
        if corr is not None:
            corrs.append(corr)
    return median(corrs)


def screen_marker(train: list[dict], test: list[dict], marker: str) -> dict:
    train_high = within_repo_capture(train, marker, "high")
    train_low = within_repo_capture(train, marker, "low")
    if train_high is None and train_low is None:
        direction = "high"
    elif train_low is None or (train_high is not None and train_high >= train_low):
        direction = "high"
    else:
        direction = "low"

    return {
        "direction_selected_on_train": direction,
        "train_pairs": len(pairs_for(train, marker, "log_corrective_lines")),
        "test_pairs": len(pairs_for(test, marker, "log_corrective_lines")),
        "train_spearman_log_raw_burden": spearman(pairs_for(train, marker, "log_corrective_lines")),
        "test_spearman_log_raw_burden": spearman(pairs_for(test, marker, "log_corrective_lines")),
        "train_spearman_corrective_fraction": spearman(pairs_for(train, marker, "corrective_fraction")),
        "test_spearman_corrective_fraction": spearman(pairs_for(test, marker, "corrective_fraction")),
        "train_spearman_corrective_per_kloc_year": spearman(pairs_for(train, marker, "corrective_per_kloc_year")),
        "test_spearman_corrective_per_kloc_year": spearman(pairs_for(test, marker, "corrective_per_kloc_year")),
        "train_global_top10_burden_capture": global_capture(train, marker, direction),
        "test_global_top10_burden_capture": global_capture(test, marker, direction),
        "train_median_within_repo_top10_burden_capture": within_repo_capture(train, marker, direction),
        "test_median_within_repo_top10_burden_capture": within_repo_capture(test, marker, direction),
        "train_median_within_repo_spearman_log_raw_burden": median_within_repo_spearman(train, marker, "log_corrective_lines"),
        "test_median_within_repo_spearman_log_raw_burden": median_within_repo_spearman(test, marker, "log_corrective_lines"),
    }


def main() -> None:
    panel: dict[tuple[str, str], dict[str, float | None]] = {}
    panel_fields = list(PANEL_MAP.values())
    with PANEL.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            week = (row.get("week") or "").strip()
            if not repo or not week:
                continue
            panel[(repo, week)] = {field: parse_float(row.get(field)) for field in panel_fields}

    cohort: dict[tuple[str, str], dict] = {}
    missing_panel = 0
    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            tracked = parse_int(row.get("tracked_line_count"))
            if tracked < 20:
                continue
            dt = parse_dt(row.get("origin_commit_date"))
            if dt is None or dt > MATURE_CUTOFF:
                continue
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            prior = panel.get((repo, prior_week_key(dt)))
            if not repo or not sha or prior is None:
                missing_panel += 1
                continue
            observed_days = max(1.0, (OBS_END - dt).total_seconds() / 86400.0)
            item = {
                "repo": repo,
                "sha": sha,
                "asset_tracked_lines": float(tracked),
                "is_ai_origin": 1.0 if (row.get("origin_role") or "").strip() == "AI" else 0.0,
                "observed_days": observed_days,
                "corrective_lines": 0.0,
            }
            for marker, source in PANEL_MAP.items():
                item[marker] = prior.get(source)
            cohort[(repo, sha)] = item

    matched = 0
    with RQ1B.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            key = (repo, sha)
            if key not in cohort:
                continue
            corrective = float(parse_int(row.get("terminal_corrective_line_count")))
            r = cohort[key]
            r["corrective_lines"] = corrective
            r["log_corrective_lines"] = math.log1p(corrective)
            tracked = float(r["asset_tracked_lines"])
            r["corrective_fraction"] = corrective / tracked if tracked else 0.0
            exposure_kloc_years = (tracked / 1000.0) * (float(r["observed_days"]) / 365.25)
            r["corrective_per_kloc_year"] = corrective / exposure_kloc_years if exposure_kloc_years > 0 else 0.0
            matched += 1

    rows = [r for r in cohort.values() if "log_corrective_lines" in r]
    train = [r for r in rows if not is_test_repo(r["repo"])]
    test = [r for r in rows if is_test_repo(r["repo"])]

    marker_results = {marker: screen_marker(train, test, marker) for marker in MARKERS}

    # Rank markers for convenience using discovery data only. This is not a score.
    def discovery_strength(item: tuple[str, dict]) -> float:
        result = item[1]
        capture = result.get("train_median_within_repo_top10_burden_capture")
        corr = result.get("train_median_within_repo_spearman_log_raw_burden")
        return (capture or 0.0) + abs(corr or 0.0)

    discovery_order = [name for name, _ in sorted(marker_results.items(), key=discovery_strength, reverse=True)]

    summary = {
        "purpose": "Screen existing leakage-safe admission-time markers individually before constructing any composite TechLedger vector.",
        "asset_definition": "feature-classified origin commit with >=20 tracked lines and >=180 days observation",
        "outcome": "terminal corrective first-intervention lines; lower bound on downstream corrective burden",
        "important_conceptual_note": "Pre-admission activity/churn is treated here as a candidate risk marker, not merely as nuisance to regress away.",
        "time_alignment": "Repository-week markers come from W-1 for an admission in week W.",
        "validation": "Marker direction is selected on training repositories only; the same direction is then evaluated on wholly held-out repositories.",
        "cohort": {
            "assets": len(rows),
            "matched_outcomes": matched,
            "missing_prior_week_panel": missing_panel,
            "train_assets": len(train),
            "test_assets": len(test),
            "train_repositories": len({r['repo'] for r in train}),
            "test_repositories": len({r['repo'] for r in test}),
            "repo_overlap": len({r['repo'] for r in train} & {r['repo'] for r in test}),
        },
        "outcome_views": {
            "log_raw_burden": "economic carrying burden proxy; preserves the fact that larger admissions can cost more",
            "corrective_fraction": "corrective original lines / original tracked lines",
            "corrective_per_kloc_year": "size- and observation-age-normalized intensity diagnostic",
        },
        "discovery_order_not_a_score": discovery_order,
        "markers": marker_results,
        "parked_marker": "file/subsystem-local 30/90-day pre-admission churn; requires selective repository enrichment and will be added later as a candidate vector component",
        "guardrail": "This checkpoint is exploratory marker screening. It does not establish causality and does not justify a composite risk score.",
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
