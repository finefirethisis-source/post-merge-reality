"""Rank admission-time markers against TechLedger MVP downstream rework outcomes."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pointbiserialr, spearmanr


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "techledger_mvp" / "output"
INPUT = OUTPUT / "commit_metrics.csv"
RESULT = OUTPUT / "marker_rankings.json"

OUTCOMES = [
    "outcome_rework_count_180d",
    "outcome_rework_volume_180d",
    "outcome_rework_intensity_180d",
]

ID_FIELDS = {"repo", "sha", "date", "author_email", "subject"}
CATEGORICAL_FIELDS = {"origin_role", "origin_intent", "origin_maintenance_class"}


def numeric(value: str):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_rows() -> list[dict[str, str]]:
    with INPUT.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def rank_numeric(rows: list[dict[str, str]], marker: str, outcome: str) -> dict | None:
    pairs = []
    for row in rows:
        x = numeric(row.get(marker, ""))
        y = numeric(row.get(outcome, ""))
        if x is not None and y is not None:
            pairs.append((x, y))
    if len(pairs) < 20:
        return None
    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    if np.all(xs == xs[0]) or np.all(ys == ys[0]):
        return None
    rho, p = spearmanr(xs, ys)
    if not math.isfinite(float(rho)):
        return None
    return {
        "marker": marker,
        "kind": "numeric",
        "n": len(pairs),
        "strength": abs(float(rho)),
        "association": float(rho),
        "p_value": float(p),
        "method": "Spearman rho",
    }


def rank_category(rows: list[dict[str, str]], field: str, outcome: str) -> list[dict]:
    values = Counter(row.get(field, "").strip() for row in rows if row.get(field, "").strip())
    result = []
    for value, count in values.items():
        if count < 10 or count > len(rows) - 10:
            continue
        xs = []
        ys = []
        for row in rows:
            y = numeric(row.get(outcome, ""))
            if y is None:
                continue
            xs.append(1 if row.get(field, "").strip() == value else 0)
            ys.append(y)
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            continue
        r, p = pointbiserialr(xs, ys)
        if not math.isfinite(float(r)):
            continue
        result.append({
            "marker": f"{field}={value}",
            "kind": "categorical_indicator",
            "n": len(xs),
            "strength": abs(float(r)),
            "association": float(r),
            "p_value": float(p),
            "method": "Point-biserial r",
        })
    return result


def repo_adjusted_rank(rows: list[dict[str, str]], marker: str, outcome: str) -> dict | None:
    """Spearman association after within-repo rank normalization.

    This keeps the illustration from being dominated solely by differences in scale
    between repositories. It is not a substitute for a multilevel model.
    """
    by_repo: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        x = numeric(row.get(marker, ""))
        y = numeric(row.get(outcome, ""))
        if x is not None and y is not None:
            by_repo[row["repo"]].append((x, y))
    xs = []
    ys = []
    for pairs in by_repo.values():
        if len(pairs) < 5:
            continue
        xv = np.array([p[0] for p in pairs])
        yv = np.array([p[1] for p in pairs])
        if np.all(xv == xv[0]) or np.all(yv == yv[0]):
            continue
        xrank = np.argsort(np.argsort(xv)).astype(float)
        yrank = np.argsort(np.argsort(yv)).astype(float)
        denom = max(1, len(pairs) - 1)
        xs.extend((xrank / denom).tolist())
        ys.extend((yrank / denom).tolist())
    if len(xs) < 20 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rho, p = spearmanr(xs, ys)
    if not math.isfinite(float(rho)):
        return None
    return {
        "marker": marker,
        "n": len(xs),
        "association": float(rho),
        "strength": abs(float(rho)),
        "p_value": float(p),
        "method": "Within-repo rank Spearman rho",
    }


def outcome_summary(rows, outcome):
    vals = np.array([numeric(r.get(outcome, "")) for r in rows if numeric(r.get(outcome, "")) is not None])
    return {
        "n": int(len(vals)),
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "p75": float(np.percentile(vals, 75)),
        "p90": float(np.percentile(vals, 90)),
        "p95": float(np.percentile(vals, 95)),
        "max": float(np.max(vals)),
        "zero_share": float(np.mean(vals == 0)),
    }


def main():
    rows = load_rows()
    fields = sorted(set().union(*(row.keys() for row in rows)))
    numeric_markers = [
        field for field in fields
        if field not in ID_FIELDS | CATEGORICAL_FIELDS | set(OUTCOMES)
        and sum(numeric(row.get(field, "")) is not None for row in rows) >= 20
    ]

    rankings = {}
    for outcome in OUTCOMES:
        raw = []
        adjusted = []
        for marker in numeric_markers:
            item = rank_numeric(rows, marker, outcome)
            if item:
                raw.append(item)
            adjusted_item = repo_adjusted_rank(rows, marker, outcome)
            if adjusted_item:
                adjusted.append(adjusted_item)
        for field in CATEGORICAL_FIELDS:
            raw.extend(rank_category(rows, field, outcome))
        raw.sort(key=lambda x: (-x["strength"], x["marker"]))
        adjusted.sort(key=lambda x: (-x["strength"], x["marker"]))
        rankings[outcome] = {
            "top_raw_associations": raw[:30],
            "top_within_repo_associations": adjusted[:30],
        }

    repo_counts = Counter(row["repo"] for row in rows)
    result = {
        "purpose": "Illustrative marker discovery for function/method-level downstream rework; not inferential validation.",
        "rows": len(rows),
        "repos": dict(repo_counts),
        "outcome_summaries": {outcome: outcome_summary(rows, outcome) for outcome in OUTCOMES},
        "numeric_markers_tested": numeric_markers,
        "categorical_marker_families": sorted(CATEGORICAL_FIELDS),
        "rankings": rankings,
        "interpretation_guardrails": [
            "Associations are descriptive and highly dependent on this five-repository convenience sample.",
            "Raw associations can reflect repository differences; within-repo rank associations are included as a second view.",
            "No multiple-testing correction is applied because this run is exploratory illustration, not hypothesis testing.",
            "Correlation is not causation and marker redundancy is expected.",
        ],
    }
    RESULT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
