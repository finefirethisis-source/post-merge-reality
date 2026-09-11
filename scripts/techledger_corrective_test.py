"""Test the narrow TechLedger hypothesis: can admission-time markers predict corrective burden?

This deliberately leaves the existing broad rework experiment untouched. It reuses
its function-attributed event stream and contributor-enriched admission rows, then
adds a conservative corrective-only outcome.

A later event is counted as corrective when:
1. a packaged maintenance label explicitly says ``corrective``; otherwise
2. if no packaged maintenance/intent label is available, the commit subject has an
   explicit fix/bug/regression/hotfix/rollback signal already recognized by the MVP.

Everything else is treated as non-corrective/unknown. Therefore the outcome is best
read as a lower-bound, high-confidence corrective burden proxy, not exhaustive bugs.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import pointbiserialr, rankdata, spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from techledger_mvp import block_churn_scan as scan  # noqa: E402

INPUT = scan.OUTPUT / "commit_metrics.csv"
METRICS = scan.OUTPUT / "corrective_metrics.csv"
RESULT = scan.OUTPUT / "corrective_rankings.json"

OUTCOMES = [
    "outcome_corrective_revisit_180d",
    "outcome_corrective_commit_count_180d",
    "outcome_corrective_changed_lines_180d",
]

PRIOR_MARKERS = [
    "prior_corrective_block_commits_30d",
    "prior_corrective_block_commits_90d",
    "prior_corrective_block_changed_lines_30d",
    "prior_corrective_block_changed_lines_90d",
]

ID_FIELDS = {"repo", "sha", "date", "author_email", "subject"}
CATEGORICAL_FIELDS = {"origin_role", "origin_intent", "origin_maintenance_class"}


def numeric(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_rows() -> list[dict[str, object]]:
    with INPUT.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_rows(rows: list[dict[str, object]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with METRICS.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def corrective_evidence(event: scan.Event) -> tuple[bool, str]:
    maintenance = str(event.markers.get("origin_maintenance_class", "") or "").strip().lower()
    if maintenance:
        return maintenance == "corrective", f"packaged_maintenance:{maintenance}"

    intent = str(event.markers.get("origin_intent", "") or "").strip().lower()
    if intent:
        return intent == "bug_fix", f"packaged_intent:{intent}"

    if int(event.markers.get("message_fix", 0) or 0) == 1:
        return True, "message_fallback:fix"
    if int(event.markers.get("message_revert", 0) or 0) == 1:
        return True, "message_fallback:revert"
    return False, "unknown_or_noncorrective"


def enrich_repo(
    slug: str,
    events: list[scan.Event],
    rows_by_key: dict[tuple[str, str], dict[str, object]],
) -> Counter[str]:
    block_index: dict[str, list[scan.Event]] = defaultdict(list)
    evidence_counts: Counter[str] = Counter()
    corrective_by_sha: dict[str, bool] = {}

    for event in events:
        is_corrective, source = corrective_evidence(event)
        corrective_by_sha[event.sha] = is_corrective
        evidence_counts[source] += 1
        for block_key in event.blocks:
            block_index[block_key].append(event)

    for event in events:
        row = rows_by_key.get((slug, event.sha))
        if row is None:
            continue

        prior_commits_30: set[str] = set()
        prior_commits_90: set[str] = set()
        prior_lines_30 = 0
        prior_lines_90 = 0
        future_commits: set[str] = set()
        future_lines = 0

        for block_key in event.blocks:
            for other in block_index[block_key]:
                delta_days = (other.date - event.date).total_seconds() / 86400.0
                if delta_days < 0:
                    age = -delta_days
                    if age <= 90 and corrective_by_sha.get(other.sha, False):
                        prior_commits_90.add(other.sha)
                        prior_lines_90 += other.blocks[block_key].changed_lines
                        if age <= 30:
                            prior_commits_30.add(other.sha)
                            prior_lines_30 += other.blocks[block_key].changed_lines
                    continue
                if delta_days == 0:
                    continue
                if delta_days > scan.HORIZON_DAYS:
                    break
                if corrective_by_sha.get(other.sha, False):
                    future_commits.add(other.sha)
                    future_lines += other.blocks[block_key].changed_lines

        row["prior_corrective_block_commits_30d"] = len(prior_commits_30)
        row["prior_corrective_block_commits_90d"] = len(prior_commits_90)
        row["prior_corrective_block_changed_lines_30d"] = prior_lines_30
        row["prior_corrective_block_changed_lines_90d"] = prior_lines_90
        row["outcome_corrective_revisit_180d"] = int(bool(future_commits))
        row["outcome_corrective_commit_count_180d"] = len(future_commits)
        row["outcome_corrective_changed_lines_180d"] = future_lines

    return evidence_counts


def rank_numeric(rows, marker: str, outcome: str):
    pairs = []
    for row in rows:
        x = numeric(row.get(marker, ""))
        y = numeric(row.get(outcome, ""))
        if x is not None and y is not None:
            pairs.append((x, y))
    if len(pairs) < 20:
        return None
    xs = np.asarray([pair[0] for pair in pairs], dtype=float)
    ys = np.asarray([pair[1] for pair in pairs], dtype=float)
    if np.all(xs == xs[0]) or np.all(ys == ys[0]):
        return None
    rho = float(spearmanr(xs, ys).statistic)
    if not math.isfinite(rho):
        return None
    return {
        "marker": marker,
        "n": len(pairs),
        "association": rho,
        "strength": abs(rho),
        "method": "Spearman rho",
    }


def rank_category(rows, field: str, outcome: str):
    values = Counter(str(row.get(field, "") or "").strip() for row in rows)
    values.pop("", None)
    result = []
    for value, count in values.items():
        if count < 10 or count > len(rows) - 10:
            continue
        xs, ys = [], []
        for row in rows:
            y = numeric(row.get(outcome, ""))
            if y is None:
                continue
            xs.append(1 if str(row.get(field, "") or "").strip() == value else 0)
            ys.append(y)
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            continue
        r = float(pointbiserialr(xs, ys).statistic)
        if math.isfinite(r):
            result.append({
                "marker": f"{field}={value}",
                "n": len(xs),
                "association": r,
                "strength": abs(r),
                "method": "Point-biserial r",
            })
    return result


def within_repo_rank(rows, marker: str, outcome: str):
    by_repo: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        x = numeric(row.get(marker, ""))
        y = numeric(row.get(outcome, ""))
        if x is not None and y is not None:
            by_repo[str(row["repo"])].append((x, y))

    xs, ys = [], []
    repos_used = 0
    for pairs in by_repo.values():
        if len(pairs) < 5:
            continue
        xv = np.asarray([pair[0] for pair in pairs], dtype=float)
        yv = np.asarray([pair[1] for pair in pairs], dtype=float)
        if np.all(xv == xv[0]) or np.all(yv == yv[0]):
            continue
        xr = rankdata(xv, method="average") / len(xv)
        yr = rankdata(yv, method="average") / len(yv)
        xs.extend(xr.tolist())
        ys.extend(yr.tolist())
        repos_used += 1

    if len(xs) < 20 or repos_used < 2:
        return None
    rho = float(spearmanr(xs, ys).statistic)
    if not math.isfinite(rho):
        return None
    return {
        "marker": marker,
        "n": len(xs),
        "repos_used": repos_used,
        "association": rho,
        "strength": abs(rho),
        "method": "Pooled within-repo average-rank Spearman rho",
    }


def outcome_summary(rows, outcome: str):
    values = [numeric(row.get(outcome, "")) for row in rows]
    vals = np.asarray([value for value in values if value is not None], dtype=float)
    return {
        "n": int(len(vals)),
        "mean": float(np.mean(vals)),
        "median": float(np.median(vals)),
        "p90": float(np.percentile(vals, 90)),
        "p95": float(np.percentile(vals, 95)),
        "max": float(np.max(vals)),
        "zero_share": float(np.mean(vals == 0)),
    }


def main() -> None:
    rows = load_rows()
    rows_by_key = {(str(row["repo"]), str(row["sha"])): row for row in rows}
    packaged_labels = scan.load_packaged_labels()

    # The preceding enrichment step leaves all five clones in /tmp. Reuse them so
    # this test needs only another local git-log pass, not another network clone.
    original_clone = scan.clone_repo

    def reuse_clone(slug: str) -> Path:
        target = scan.WORK / slug.replace("/", "__")
        if (target / ".git").exists():
            return target
        return original_clone(slug)

    scan.clone_repo = reuse_clone

    classification_counts: Counter[str] = Counter()
    repo_event_counts = {}
    for slug in scan.TARGET_REPOS:
        print(f"Corrective-burden pass: {slug}", flush=True)
        events, _ = scan.stream_repo(slug, packaged_labels)
        repo_event_counts[slug] = len(events)
        classification_counts.update(enrich_repo(slug, events, rows_by_key))

    enriched_rows = list(rows_by_key.values())
    missing = [row for row in enriched_rows if "outcome_corrective_revisit_180d" not in row]
    if missing:
        raise RuntimeError(f"Corrective pass failed to match {len(missing)} admission rows")
    write_rows(enriched_rows)

    fields = sorted(set().union(*(row.keys() for row in enriched_rows)))
    numeric_markers = [
        field for field in fields
        if field not in ID_FIELDS | CATEGORICAL_FIELDS
        and not field.startswith("outcome_")
        and sum(numeric(row.get(field, "")) is not None for row in enriched_rows) >= 20
    ]

    rankings = {}
    for outcome in OUTCOMES:
        raw = []
        adjusted = []
        for marker in numeric_markers:
            item = rank_numeric(enriched_rows, marker, outcome)
            if item:
                raw.append(item)
            adjusted_item = within_repo_rank(enriched_rows, marker, outcome)
            if adjusted_item:
                adjusted.append(adjusted_item)
        for field in CATEGORICAL_FIELDS:
            raw.extend(rank_category(enriched_rows, field, outcome))
        raw.sort(key=lambda item: (-item["strength"], item["marker"]))
        adjusted.sort(key=lambda item: (-item["strength"], item["marker"]))
        rankings[outcome] = {
            "top_raw_associations": raw[:30],
            "top_within_repo_associations": adjusted[:30],
        }

    by_repo = {}
    for slug in scan.TARGET_REPOS:
        repo_rows = [row for row in enriched_rows if row["repo"] == slug]
        positives = sum(int(row["outcome_corrective_revisit_180d"]) for row in repo_rows)
        by_repo[slug] = {
            "admissions": len(repo_rows),
            "with_corrective_revisit": positives,
            "corrective_revisit_share": positives / len(repo_rows) if repo_rows else 0.0,
        }

    result = {
        "purpose": "Cheapest next-gate test: whether admission-time repository signals predict conservative 180-day corrective burden rather than generic future activity.",
        "rows": len(enriched_rows),
        "repos": by_repo,
        "outcomes": OUTCOMES,
        "outcome_summaries": {outcome: outcome_summary(enriched_rows, outcome) for outcome in OUTCOMES},
        "classification": {
            "rule": "Use packaged maintenance/intent labels when present. Only when absent, explicit fix/bug/regression/hotfix or revert/rollback language is a positive fallback. Everything else is non-corrective/unknown.",
            "event_evidence_counts": dict(classification_counts),
            "repo_event_counts": repo_event_counts,
            "interpretation": "This is intentionally conservative and should be read as a lower-bound corrective-burden proxy.",
        },
        "prior_corrective_markers_added": PRIOR_MARKERS,
        "numeric_markers_tested": numeric_markers,
        "rankings": rankings,
        "guardrails": [
            "Five-repository convenience sample only; this is a gate/smell test, not validation.",
            "Function attribution remains the same heuristic used by the existing MVP scanner.",
            "Unknown downstream changes are not guessed to be bugs, so false negatives are expected by design.",
            "Raw correlations can reflect repository differences; within-repo average-rank results are shown separately.",
            "No p-value gating or multiple-testing claims are used; association strength and consistency are the decision inputs.",
        ],
    }
    RESULT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
