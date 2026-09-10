from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
PANEL = Path("dev/analysis/output_rq2/rq2_repo_week_panel.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)

CORE_FIELDS = [
    "total_commits",
    "total_changed_lines",
    "corrective_commits",
    "corrective_rate",
    "tracked_line_churn_rate",
    "living_tracked_lines_at_week_start",
]
SONAR_FIELDS = [
    "sonar_ncloc",
    "sonar_issue_count",
    "sonar_bug_count",
    "sonar_vulnerability_count",
    "sonar_code_smell_count",
    "sonar_complexity",
    "sonar_cognitive_complexity",
    "sonar_duplicated_lines_density",
    "sonar_maintainability_effort",
]


def f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def i(value: str | None) -> int:
    value_f = f(value)
    return int(value_f) if value_f is not None else 0


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


def week_start(dt: datetime) -> str:
    d = (dt - timedelta(days=dt.weekday())).date()
    return f"{d.isoformat()}T00:00:00+00:00"


def prior_week_key(dt: datetime) -> str:
    monday = dt - timedelta(days=dt.weekday())
    prior = (monday - timedelta(days=7)).date()
    return f"{prior.isoformat()}T00:00:00+00:00"


def is_test_repo(repo: str) -> bool:
    h = int(hashlib.sha256(repo.encode("utf-8")).hexdigest()[:8], 16)
    return h % 10 < 3


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    aug = [row[:] + [b[idx]] for idx, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[pivot][col] = 1e-12
        aug[col], aug[pivot] = aug[pivot], aug[col]
        div = aug[col][col]
        aug[col] = [v / div for v in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor == 0:
                continue
            aug[r] = [aug[r][c] - factor * aug[col][c] for c in range(n + 1)]
    return [aug[r][-1] for r in range(n)]


def transform_feature(name: str, value: float) -> float:
    if name in {
        "asset_tracked_lines",
        "total_commits",
        "total_changed_lines",
        "corrective_commits",
        "living_tracked_lines_at_week_start",
        "sonar_ncloc",
        "sonar_issue_count",
        "sonar_bug_count",
        "sonar_vulnerability_count",
        "sonar_code_smell_count",
        "sonar_complexity",
        "sonar_cognitive_complexity",
        "sonar_maintainability_effort",
    }:
        return math.log1p(max(0.0, value))
    return value


def prep_matrix(train: list[dict], test: list[dict], features: list[str]):
    medians: dict[str, float] = {}
    means: dict[str, float] = {}
    sds: dict[str, float] = {}
    for name in features:
        vals = [r[name] for r in train if r.get(name) is not None]
        med = median([float(v) for v in vals])
        medians[name] = med
        transformed = [transform_feature(name, float(r[name] if r.get(name) is not None else med)) for r in train]
        mean = sum(transformed) / len(transformed)
        var = sum((v - mean) ** 2 for v in transformed) / max(1, len(transformed) - 1)
        means[name] = mean
        sds[name] = math.sqrt(var) if var > 1e-12 else 1.0

    def build(rows: list[dict]) -> list[list[float]]:
        out = []
        for r in rows:
            x = [1.0]
            for name in features:
                raw = float(r[name] if r.get(name) is not None else medians[name])
                val = transform_feature(name, raw)
                x.append((val - means[name]) / sds[name])
            out.append(x)
        return out

    return build(train), build(test), medians


def poisson_fit(x: list[list[float]], y: list[float], ridge: float = 1.0, max_iter: int = 40) -> list[float]:
    p = len(x[0])
    mean_y = max(sum(y) / len(y), 1e-4)
    beta = [math.log(mean_y)] + [0.0] * (p - 1)
    for _ in range(max_iter):
        xtwx = [[0.0] * p for _ in range(p)]
        xtwz = [0.0] * p
        for row, target in zip(x, y):
            eta = sum(row[j] * beta[j] for j in range(p))
            eta = max(-15.0, min(15.0, eta))
            mu = max(math.exp(eta), 1e-8)
            z = eta + (target - mu) / mu
            w = mu
            for a in range(p):
                xtwz[a] += row[a] * w * z
                for b in range(p):
                    xtwx[a][b] += row[a] * w * row[b]
        for j in range(1, p):
            xtwx[j][j] += ridge
        new_beta = solve_linear(xtwx, xtwz)
        delta = max(abs(new_beta[j] - beta[j]) for j in range(p))
        beta = new_beta
        if delta < 1e-7:
            break
    return beta


def predict(x: list[list[float]], beta: list[float], tracked: list[int]) -> list[float]:
    out = []
    for row, cap in zip(x, tracked):
        eta = sum(row[j] * beta[j] for j in range(len(beta)))
        mu = math.exp(max(-15.0, min(15.0, eta)))
        out.append(min(float(cap), mu))
    return out


def poisson_deviance(y: list[float], mu: list[float]) -> float:
    total = 0.0
    for obs, pred in zip(y, mu):
        pred = max(pred, 1e-12)
        if obs > 0:
            total += 2.0 * (obs * math.log(obs / pred) - (obs - pred))
        else:
            total += 2.0 * pred
    return total / len(y)


def pearson_log(y: list[float], mu: list[float]) -> float | None:
    a = [math.log1p(v) for v in y]
    b = [math.log1p(v) for v in mu]
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    va = sum((v - ma) ** 2 for v in a)
    vb = sum((v - mb) ** 2 for v in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((u - ma) * (v - mb) for u, v in zip(a, b)) / math.sqrt(va * vb)


def r2_log(y: list[float], mu: list[float]) -> float | None:
    a = [math.log1p(v) for v in y]
    b = [math.log1p(v) for v in mu]
    mean = sum(a) / len(a)
    sst = sum((v - mean) ** 2 for v in a)
    if sst <= 0:
        return None
    sse = sum((u - v) ** 2 for u, v in zip(a, b))
    return 1.0 - sse / sst


def capture_share(y: list[float], score: list[float], fraction: float) -> float | None:
    total = sum(y)
    if total <= 0:
        return None
    n = max(1, round(len(y) * fraction))
    idx = sorted(range(len(y)), key=lambda k: score[k], reverse=True)[:n]
    return sum(y[k] for k in idx) / total


def residual_concentration(y: list[float], mu: list[float], fraction: float) -> float | None:
    residual = [max(0.0, obs - pred) for obs, pred in zip(y, mu)]
    total = sum(residual)
    if total <= 0:
        return None
    n = max(1, round(len(residual) * fraction))
    top = sorted(residual, reverse=True)[:n]
    return sum(top) / total


def evaluate(y: list[float], mu: list[float]) -> dict:
    total_y = sum(y)
    total_mu = sum(mu)
    positive_excess = sum(max(0.0, obs - pred) for obs, pred in zip(y, mu))
    return {
        "test_assets": len(y),
        "observed_corrective_lines": total_y,
        "predicted_corrective_lines": total_mu,
        "prediction_to_observed_ratio": total_mu / total_y if total_y else None,
        "mean_poisson_deviance": poisson_deviance(y, mu),
        "log_count_r2": r2_log(y, mu),
        "log_count_pearson": pearson_log(y, mu),
        "actual_burden_captured_by_top10pct_predicted": capture_share(y, mu, 0.10),
        "actual_burden_captured_by_top20pct_predicted": capture_share(y, mu, 0.20),
        "positive_excess_lines": positive_excess,
        "positive_excess_share_of_observed": positive_excess / total_y if total_y else None,
        "top10pct_share_of_positive_excess": residual_concentration(y, mu, 0.10),
    }


def main() -> None:
    # Repository-week context. We will join only W-1 to each asset admitted in W.
    panel: dict[tuple[str, str], dict[str, float | None]] = {}
    with PANEL.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            week = (row.get("week") or "").strip()
            if not repo or not week:
                continue
            panel[(repo, week)] = {name: f(row.get(name)) for name in CORE_FIELDS + SONAR_FIELDS}

    # Mature substantive feature cohort from RQ1a.
    cohort: dict[tuple[str, str], dict] = {}
    for_path_missing_panel = 0
    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            tracked = i(row.get("tracked_line_count"))
            if tracked < 20:
                continue
            dt = parse_dt(row.get("origin_commit_date"))
            if dt is None or dt > MATURE_CUTOFF:
                continue
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            if not repo or not sha:
                continue
            prior = panel.get((repo, prior_week_key(dt)))
            if prior is None:
                for_path_missing_panel += 1
                continue
            item = {
                "repo": repo,
                "sha": sha,
                "asset_tracked_lines": float(tracked),
                "tracked": tracked,
                "is_ai_origin": 1.0 if (row.get("origin_role") or "").strip() == "AI" else 0.0,
                "corrective_lines": 0,
            }
            item.update(prior)
            cohort[(repo, sha)] = item

    matched_outcomes = 0
    with RQ1B.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            key = (repo, sha)
            if key in cohort:
                cohort[key]["corrective_lines"] = i(row.get("terminal_corrective_line_count"))
                matched_outcomes += 1

    rows = list(cohort.values())
    train = [r for r in rows if not is_test_repo(r["repo"])]
    test = [r for r in rows if is_test_repo(r["repo"])]
    train_repos = sorted({r["repo"] for r in train})
    test_repos = sorted({r["repo"] for r in test})

    model_specs = {
        "size_only": ["asset_tracked_lines"],
        "activity_churn": ["asset_tracked_lines", "is_ai_origin"] + CORE_FIELDS,
        "activity_churn_sonar": ["asset_tracked_lines", "is_ai_origin"] + CORE_FIELDS + SONAR_FIELDS,
    }

    results = {}
    for name, features in model_specs.items():
        x_train, x_test, medians = prep_matrix(train, test, features)
        y_train = [float(r["corrective_lines"]) for r in train]
        y_test = [float(r["corrective_lines"]) for r in test]
        beta = poisson_fit(x_train, y_train, ridge=1.0)
        pred_train = predict(x_train, beta, [r["tracked"] for r in train])
        pred_test = predict(x_test, beta, [r["tracked"] for r in test])

        # Calibrate aggregate count on training only; apply unchanged to held-out repos.
        train_obs = sum(y_train)
        train_pred = sum(pred_train)
        scale = train_obs / train_pred if train_pred > 0 else 1.0
        pred_test = [min(float(r["tracked"]), p * scale) for r, p in zip(test, pred_test)]

        results[name] = {
            "features": features,
            "train_only_calibration_scale": scale,
            "test_metrics": evaluate(y_test, pred_test),
        }

    test_y = [float(r["corrective_lines"]) for r in test]
    actual_top10 = capture_share(test_y, test_y, 0.10)
    summary = {
        "purpose": "Test whether ordinary repository activity/churn observable before admission explains the heavy-tailed corrective burden.",
        "outcome": "terminal_corrective_line_count, a lower-bound first-intervention corrective burden measure",
        "leakage_control": "An admission in week W receives repository context only from W-1; admission-week and all post-admission fields are excluded.",
        "validation_split": "Entire repositories are deterministically split ~70/30 by SHA-256 hash; no repository appears in both train and test.",
        "cohort_and_join": {
            "mature_substantive_feature_assets_before_panel_join": 18669,
            "assets_with_prior_week_panel_context": len(rows),
            "assets_missing_prior_week_panel_context": for_path_missing_panel,
            "matched_corrective_outcomes": matched_outcomes,
            "train_assets": len(train),
            "test_assets": len(test),
            "train_repositories": len(train_repos),
            "test_repositories": len(test_repos),
            "repo_overlap": len(set(train_repos) & set(test_repos)),
        },
        "held_out_actual_top10pct_burden_share": actual_top10,
        "models": results,
        "interpretation_guardrail": "This controls repository-level recent activity and codebase health, not file/subsystem-local pre-admission churn. Persistence of residual concentration would justify a stronger local-churn baseline before TechLedger-specific predictors.",
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
