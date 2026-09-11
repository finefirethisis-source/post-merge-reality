"""Test whether corrective history carries signal beyond size and generic activity.

This is deliberately a cheap analysis-only gate over the existing corrective_metrics.csv.
No repository rescan is performed.

Primary admission-time signals:
- prior corrective commit share over 90d = corrective commits / all commits in touched blocks
- prior corrective line share over 90d = corrective changed lines / all changed lines in touched blocks

Primary test:
Within each repository, rank-transform signal, outcome, admitted LOC, and matching
activity denominator; residualize both signal and outcome on LOC + activity, then
correlate residuals across repositories. This is a descriptive partial-Spearman-style
check of whether the *composition* of prior work matters beyond how large and active
the area already was.

Sensitivity test additionally controls max complexity proxy.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pearsonr, rankdata, spearmanr

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "techledger_mvp" / "output" / "corrective_metrics.csv"
OUTPUT = ROOT / "techledger_mvp" / "output" / "corrective_persistence_test.json"

OUTCOMES = [
    "outcome_corrective_revisit_180d",
    "outcome_corrective_commit_count_180d",
    "outcome_corrective_changed_lines_180d",
]

SIGNALS = {
    "prior_corrective_commit_share_90d": {
        "numerator": "prior_corrective_block_commits_90d",
        "denominator": "prior_block_commits_90d",
    },
    "prior_corrective_line_share_90d": {
        "numerator": "prior_corrective_block_changed_lines_90d",
        "denominator": "prior_block_changed_lines_90d",
    },
}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def load_rows():
    with INPUT.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def add_shares(rows):
    for row in rows:
        for signal, spec in SIGNALS.items():
            numerator = number(row.get(spec["numerator"]))
            denominator = number(row.get(spec["denominator"]))
            if numerator is None or denominator is None or denominator <= 0:
                row[signal] = None
            else:
                row[signal] = numerator / denominator


def raw_spearman(rows, signal, outcome):
    pairs = []
    for row in rows:
        x = number(row.get(signal))
        y = number(row.get(outcome))
        if x is not None and y is not None:
            pairs.append((x, y))
    if len(pairs) < 20:
        return None
    xs = np.asarray([pair[0] for pair in pairs], dtype=float)
    ys = np.asarray([pair[1] for pair in pairs], dtype=float)
    if np.all(xs == xs[0]) or np.all(ys == ys[0]):
        return None
    rho = float(spearmanr(xs, ys).statistic)
    return {"n": len(xs), "association": rho, "method": "Spearman rho"}


def residualize(values, controls):
    design = np.column_stack([np.ones(len(values)), *controls])
    beta, *_ = np.linalg.lstsq(design, values, rcond=None)
    return values - design @ beta


def partial_rank(rows, signal, outcome, activity_field, include_complexity=False):
    """Repo-local rank residualization, then pooled residual correlation."""
    by_repo = defaultdict(list)
    for row in rows:
        x = number(row.get(signal))
        y = number(row.get(outcome))
        size = number(row.get("admitted_block_loc"))
        activity = number(row.get(activity_field))
        complexity = number(row.get("max_complexity_proxy"))
        if x is None or y is None or size is None or activity is None:
            continue
        if include_complexity and complexity is None:
            continue
        by_repo[row["repo"]].append((x, y, size, activity, complexity))

    pooled_x = []
    pooled_y = []
    per_repo = {}

    for repo, values in by_repo.items():
        if len(values) < 20:
            continue
        arr = np.asarray(values, dtype=float)
        x, y, size, activity = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
        complexity = arr[:, 4]
        if np.all(x == x[0]) or np.all(y == y[0]):
            continue

        xr = rankdata(x, method="average")
        yr = rankdata(y, method="average")
        sr = rankdata(size, method="average")
        ar = rankdata(activity, method="average")
        controls = [sr, ar]
        if include_complexity:
            controls.append(rankdata(complexity, method="average"))

        xres = residualize(xr, controls)
        yres = residualize(yr, controls)
        if np.std(xres) == 0 or np.std(yres) == 0:
            continue
        r = float(pearsonr(xres, yres).statistic)
        per_repo[repo] = {"n": len(values), "association": r}
        pooled_x.extend(xres.tolist())
        pooled_y.extend(yres.tolist())

    if len(pooled_x) < 20 or len(per_repo) < 2:
        return None
    association = float(pearsonr(pooled_x, pooled_y).statistic)
    return {
        "n": len(pooled_x),
        "repos_used": len(per_repo),
        "association": association,
        "method": "Within-repo rank residual correlation",
        "controls": ["admitted_block_loc", activity_field] + (["max_complexity_proxy"] if include_complexity else []),
        "per_repo": per_repo,
    }


def quartile_summary(rows, signal, outcome):
    """Descriptive outcome means by within-repo corrective-share quartile."""
    grouped = defaultdict(list)
    for repo in sorted({row["repo"] for row in rows}):
        repo_rows = []
        for row in rows:
            if row["repo"] != repo:
                continue
            x = number(row.get(signal))
            y = number(row.get(outcome))
            if x is not None and y is not None:
                repo_rows.append((row, x, y))
        if len(repo_rows) < 20:
            continue
        xs = np.asarray([item[1] for item in repo_rows])
        ranks = rankdata(xs, method="average") / len(xs)
        for (row, x, y), rank in zip(repo_rows, ranks):
            quartile = min(4, max(1, int(math.ceil(rank * 4))))
            grouped[quartile].append(y)

    result = {}
    for quartile in range(1, 5):
        vals = grouped.get(quartile, [])
        result[f"Q{quartile}"] = {
            "n": len(vals),
            "mean_outcome": float(np.mean(vals)) if vals else None,
        }
    return result


def main():
    rows = load_rows()
    add_shares(rows)

    eligible = {}
    for signal, spec in SIGNALS.items():
        eligible[signal] = sum(number(row.get(signal)) is not None for row in rows)

    tests = {}
    for signal, spec in SIGNALS.items():
        activity = spec["denominator"]
        tests[signal] = {}
        for outcome in OUTCOMES:
            tests[signal][outcome] = {
                "raw": raw_spearman(rows, signal, outcome),
                "controlled_size_activity": partial_rank(rows, signal, outcome, activity, include_complexity=False),
                "controlled_size_activity_complexity": partial_rank(rows, signal, outcome, activity, include_complexity=True),
                "within_repo_share_quartiles": quartile_summary(rows, signal, outcome),
            }

    result = {
        "purpose": "Does the fraction of recent work that was corrective predict later corrective burden beyond asset size and generic activity?",
        "rows_total": len(rows),
        "eligible_rows_with_prior_activity": eligible,
        "signals": SIGNALS,
        "tests": tests,
        "interpretation_rule": "A positive controlled association means two areas of similar size and recent activity differ in later corrective burden according to how corrective their recent history was. This is still descriptive evidence, not causal validation.",
        "guardrails": [
            "Five-repository convenience sample; repositories with fewer than 20 eligible rows are excluded from repo-local residual tests.",
            "Corrective labels are the conservative proxy produced by the preceding corrective-burden gate.",
            "Function attribution inherits the existing MVP scanner's heuristic limitations.",
            "The quartile summaries are descriptive only and do not themselves control for size/activity; the residual tests are the primary result.",
            "No p-value gate is used for the go/no-go interpretation.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
