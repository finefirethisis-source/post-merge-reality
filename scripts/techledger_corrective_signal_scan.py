"""Scan all available admission-time signals for association with later corrective burden.

This is an analysis-only pass over ``corrective_metrics.csv``; repositories are not
rescanned. The primary question is not which markers merely identify large/hot code,
but which acceptance-time markers retain association with later corrective burden
after accounting for exposure.

Primary controlled view:
- work within each repository (removes between-repository scale/composition effects),
- rank-transform marker, outcome, and controls,
- residualize marker and outcome on admitted LOC plus generic prior activity,
- correlate the residuals.

Controls are intentionally limited to exposure variables, so complexity, ownership,
change shape, content, message, and provenance markers remain eligible signals.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr, rankdata, spearmanr

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "techledger_mvp" / "output" / "corrective_metrics.csv"
OUTPUT = ROOT / "techledger_mvp" / "output" / "corrective_signal_scan.json"

BASE_OUTCOMES = [
    "outcome_corrective_revisit_180d",
    "outcome_corrective_commit_count_180d",
    "outcome_corrective_changed_lines_180d",
]
DERIVED_OUTCOME = "outcome_corrective_changed_lines_per_admitted_loc_180d"
OUTCOMES = BASE_OUTCOMES + [DERIVED_OUTCOME]

ID_FIELDS = {"repo", "sha", "date", "author_email", "subject"}
CATEGORICAL_FIELDS = {"origin_role", "origin_intent", "origin_maintenance_class"}

# These are exposure controls or a family already tested in the immediately preceding
# persistence experiment, so they are not ranked as "other" candidate signals here.
EXCLUDED_FROM_PRIMARY = {
    "admitted_block_loc",
    "prior_block_commits_90d",
    "prior_block_changed_lines_90d",
    "prior_block_commits_30d",
    "prior_block_changed_lines_30d",
    "prior_corrective_block_commits_30d",
    "prior_corrective_block_commits_90d",
    "prior_corrective_block_changed_lines_30d",
    "prior_corrective_block_changed_lines_90d",
}

CONTROL_FIELDS = [
    "admitted_block_loc",
    "prior_block_commits_90d",
    "prior_block_changed_lines_90d",
]


def number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_rows():
    with INPUT.open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    for row in rows:
        lines = number(row.get("outcome_corrective_changed_lines_180d"))
        loc = number(row.get("admitted_block_loc"))
        row[DERIVED_OUTCOME] = (lines / loc) if lines is not None and loc and loc > 0 else None
    return rows


def family(marker: str) -> str:
    base = marker.split("=", 1)[0]
    if base.startswith("author_") or base.startswith("contributors_"):
        return "contributor_context"
    if base.startswith("message_"):
        return "commit_message"
    if base.startswith("changed_"):
        return "changed_content"
    if base.startswith("origin_"):
        return "origin_label"
    if base.startswith("prior_") or base.startswith("hot_block") or base.startswith("days_since") or base.startswith("touched_"):
        return "local_history"
    if base in {"weekday", "is_weekend", "author_hour_utc", "repo_age_days", "parent_count", "is_merge"}:
        return "commit_metadata"
    if any(base.startswith(prefix) for prefix in ("code_", "test_", "blocks_", "new_block", "file_extension", "function_attribution", "churn_per_block")):
        return "change_shape"
    if any(token in base for token in ("block_loc", "complexity", "comment_fraction", "change_density", "blocks_per_file", "mean_path_depth")):
        return "block_shape"
    return "other"


def raw_numeric(rows, marker: str, outcome: str):
    pairs = []
    for row in rows:
        x = number(row.get(marker))
        y = number(row.get(outcome))
        if x is not None and y is not None:
            pairs.append((x, y))
    if len(pairs) < 20:
        return None
    xs = np.asarray([p[0] for p in pairs], dtype=float)
    ys = np.asarray([p[1] for p in pairs], dtype=float)
    if np.all(xs == xs[0]) or np.all(ys == ys[0]):
        return None
    rho = float(spearmanr(xs, ys).statistic)
    if not math.isfinite(rho):
        return None
    return {"n": len(xs), "association": rho, "strength": abs(rho), "method": "Spearman rho"}


def residualize(values, controls):
    design = np.column_stack([np.ones(len(values)), *controls])
    beta, *_ = np.linalg.lstsq(design, values, rcond=None)
    return values - design @ beta


def controlled_numeric(rows, marker: str, outcome: str):
    by_repo = defaultdict(list)
    for row in rows:
        x = number(row.get(marker))
        y = number(row.get(outcome))
        controls = [number(row.get(field)) for field in CONTROL_FIELDS]
        if x is None or y is None or any(value is None for value in controls):
            continue
        by_repo[row["repo"]].append((x, y, *controls))

    pooled_x, pooled_y = [], []
    per_repo = {}
    for repo, values in by_repo.items():
        if len(values) < 20:
            continue
        arr = np.asarray(values, dtype=float)
        x, y = arr[:, 0], arr[:, 1]
        if np.all(x == x[0]) or np.all(y == y[0]):
            continue
        xr = rankdata(x, method="average")
        yr = rankdata(y, method="average")
        control_ranks = [rankdata(arr[:, i], method="average") for i in range(2, arr.shape[1])]
        xres = residualize(xr, control_ranks)
        yres = residualize(yr, control_ranks)
        if np.std(xres) == 0 or np.std(yres) == 0:
            continue
        r = float(pearsonr(xres, yres).statistic)
        per_repo[repo] = {"n": len(values), "association": r}
        pooled_x.extend(xres.tolist())
        pooled_y.extend(yres.tolist())

    if len(pooled_x) < 20 or len(per_repo) < 2:
        return None
    r = float(pearsonr(pooled_x, pooled_y).statistic)
    if not math.isfinite(r):
        return None
    repo_signs = [np.sign(item["association"]) for item in per_repo.values() if item["association"] != 0]
    pooled_sign = np.sign(r)
    consistency = sum(sign == pooled_sign for sign in repo_signs) / len(repo_signs) if repo_signs else 0.0
    return {
        "n": len(pooled_x),
        "repos_used": len(per_repo),
        "association": r,
        "strength": abs(r),
        "sign_consistency": consistency,
        "method": "Within-repo rank residual correlation",
        "controls": CONTROL_FIELDS,
        "per_repo": per_repo,
    }


def category_indicators(rows):
    indicators = {}
    for field in CATEGORICAL_FIELDS:
        known = [str(row.get(field, "") or "").strip() for row in rows]
        counts = Counter(value for value in known if value)
        for value, positive_count in counts.items():
            known_count = sum(bool(v) for v in known)
            negative_count = known_count - positive_count
            if positive_count < 10 or negative_count < 10:
                continue
            name = f"{field}={value}"
            indicators[name] = (field, value)
    return indicators


def category_rows(rows, field, value):
    transformed = []
    for row in rows:
        known = str(row.get(field, "") or "").strip()
        if not known:
            continue
        copy = dict(row)
        copy["__indicator__"] = 1.0 if known == value else 0.0
        transformed.append(copy)
    return transformed


def build_candidates(rows):
    fields = sorted(set().union(*(row.keys() for row in rows)))
    numeric_candidates = []
    for field in fields:
        if field in ID_FIELDS | CATEGORICAL_FIELDS | set(OUTCOMES) | EXCLUDED_FROM_PRIMARY:
            continue
        if field.startswith("outcome_"):
            continue
        valid = [number(row.get(field)) for row in rows]
        valid = [value for value in valid if value is not None]
        if len(valid) >= 20 and len(set(valid)) >= 2:
            numeric_candidates.append(field)
    return numeric_candidates, category_indicators(rows)


def cross_outcome_leaders(results):
    by_marker = defaultdict(list)
    for outcome, items in results.items():
        for item in items:
            by_marker[item["marker"]].append((outcome, item))
    leaders = []
    for marker, entries in by_marker.items():
        if len(entries) < 3:
            continue
        associations = [entry[1]["controlled"]["association"] for entry in entries if entry[1].get("controlled")]
        consistencies = [entry[1]["controlled"].get("sign_consistency", 0.0) for entry in entries if entry[1].get("controlled")]
        if len(associations) < 3:
            continue
        same_direction = all(value >= 0 for value in associations) or all(value <= 0 for value in associations)
        leaders.append({
            "marker": marker,
            "family": entries[0][1]["family"],
            "outcomes_available": len(associations),
            "median_abs_controlled_association": float(np.median(np.abs(associations))),
            "mean_abs_controlled_association": float(np.mean(np.abs(associations))),
            "same_direction_across_outcomes": same_direction,
            "mean_repo_sign_consistency": float(np.mean(consistencies)),
            "associations": {outcome: item["controlled"]["association"] for outcome, item in entries if item.get("controlled")},
        })
    leaders.sort(key=lambda item: (-item["median_abs_controlled_association"], -item["mean_repo_sign_consistency"], item["marker"]))
    return leaders[:30]


def main():
    rows = load_rows()
    numeric_candidates, categorical_candidates = build_candidates(rows)

    results = {}
    for outcome in OUTCOMES:
        items = []
        for marker in numeric_candidates:
            raw = raw_numeric(rows, marker, outcome)
            controlled = controlled_numeric(rows, marker, outcome)
            if raw is None and controlled is None:
                continue
            items.append({
                "marker": marker,
                "family": family(marker),
                "kind": "numeric",
                "raw": raw,
                "controlled": controlled,
            })

        for marker, (field, value) in categorical_candidates.items():
            subset = category_rows(rows, field, value)
            raw = raw_numeric(subset, "__indicator__", outcome)
            controlled = controlled_numeric(subset, "__indicator__", outcome)
            if raw is None and controlled is None:
                continue
            items.append({
                "marker": marker,
                "family": family(marker),
                "kind": "categorical_indicator_known_labels_only",
                "raw": raw,
                "controlled": controlled,
            })

        items.sort(key=lambda item: (-(item.get("controlled") or {"strength": -1})["strength"], item["marker"]))
        results[outcome] = items[:40]

    result = {
        "purpose": "Broad scan of all remaining admission-time signals for association with later corrective burden beyond size and generic prior activity.",
        "rows": len(rows),
        "outcomes": OUTCOMES,
        "derived_outcome": {
            DERIVED_OUTCOME: "corrective changed lines in 180d divided by admitted functional LOC"
        },
        "primary_controlled_method": {
            "method": "Within-repo rank residual correlation",
            "controls": CONTROL_FIELDS,
            "interpretation": "Association remaining after removing repo-local rank effects of admitted size and generic prior activity.",
        },
        "excluded_from_primary_scan": sorted(EXCLUDED_FROM_PRIMARY),
        "numeric_candidates_tested": numeric_candidates,
        "categorical_indicators_tested": sorted(categorical_candidates),
        "top_by_outcome": results,
        "cross_outcome_leaders": cross_outcome_leaders(results),
        "guardrails": [
            "Exploratory five-repository convenience sample only; this is signal discovery, not validation.",
            "Only admission-time fields are candidates; all fields beginning outcome_ are excluded from predictors.",
            "The prior-corrective-history family was already tested directly and is excluded from this 'other signals' ranking.",
            "Exposure controls are admitted LOC plus generic prior commit and changed-line activity over 90 days.",
            "Categorical origin labels are evaluated only among rows with a known label, not by treating missing labels as the opposite category.",
            "Contributor-context fields inherit the known limitation that first-parent Git author can be an integrator/merger rather than original PR author.",
            "repo_age_days is based on the scanner's shallow-history view and should not be treated as true repository age.",
            "Function attribution and complexity are heuristic; per-repository scanner coverage limitations still apply.",
            "No p-value or multiple-testing gate is used; strength, direction, and consistency across repos/outcomes are the screening criteria.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
