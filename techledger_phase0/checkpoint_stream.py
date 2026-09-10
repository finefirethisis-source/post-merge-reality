from __future__ import annotations

import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path

FINEGRAY = Path("dev/analysis/output_rq1/rq1_fine_gray_maintenance_class_line_data.csv.gz")
MODEL = Path("dev/analysis/output_rq1/rq1_fine_gray_model_summary.csv")
RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
HORIZON = 180.0


def aj_cif_at_horizon(rows_by_time: dict[float, dict[str, float]], total_weight: float, horizon: float) -> dict:
    y = float(total_weight)
    survival = 1.0
    cif_corrective = 0.0
    events_corrective = 0.0
    events_competing = 0.0
    censored_before_horizon = 0.0

    for t in sorted(rows_by_time):
        if t > horizon or y <= 0:
            break
        counts = rows_by_time[t]
        d_corr = counts.get("corrective", 0.0)
        d_other = counts.get("other_event", 0.0)
        cens = counts.get("censored", 0.0)
        d_all = d_corr + d_other
        if d_all > y + 1e-9:
            raise RuntimeError(f"events exceed risk set at t={t}: events={d_all}, risk={y}")
        if y > 0:
            cif_corrective += survival * (d_corr / y)
            survival *= 1.0 - (d_all / y)
        y -= d_all + cens
        events_corrective += d_corr
        events_competing += d_other
        censored_before_horizon += cens

    return {
        "initial_weighted_lines": total_weight,
        "day180_corrective_cumulative_incidence": cif_corrective,
        "day180_event_free_survival": survival,
        "corrective_events_observed_by_day180": events_corrective,
        "competing_events_observed_by_day180": events_competing,
        "censored_before_day180": censored_before_horizon,
        "risk_set_remaining_after_day180_processing": y,
    }


def main() -> None:
    # Audit the committed compact asset summaries.
    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
        rq1a_header = next(csv.reader(handle))
    with RQ1B.open("r", encoding="utf-8", newline="") as handle:
        rq1b_header = next(csv.reader(handle))

    # Recompute a day-180 provenance anchor from the packaged weighted line-survival table.
    by_group_time: dict[str, dict[float, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    total_by_group: dict[str, float] = defaultdict(float)
    finegray_header: list[str] = []
    row_count = 0

    with gzip.open(FINEGRAY, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        finegray_header = list(reader.fieldnames or [])
        for row in reader:
            row_count += 1
            group = (row.get("origin_group") or "").strip()
            if group not in {"AI", "Human"}:
                continue
            weight = float(row.get("weight") or 0)
            if weight <= 0:
                continue
            t = float(row.get("survival_days_positive") or row.get("survival_days") or 0)
            observed = int(float(row.get("event_observed") or 0))
            event_type = (row.get("event_type") or "").strip()
            total_by_group[group] += weight
            if observed:
                key = "corrective" if event_type == "corrective" else "other_event"
            else:
                key = "censored"
            by_group_time[group][t][key] += weight

    cif = {
        group: aj_cif_at_horizon(by_group_time[group], total_by_group[group], HORIZON)
        for group in ("AI", "Human")
    }
    ai_cif = cif["AI"]["day180_corrective_cumulative_incidence"]
    human_cif = cif["Human"]["day180_corrective_cumulative_incidence"]
    cif["comparison"] = {
        "AI_to_Human_day180_CIF_ratio": ai_cif / human_cif if human_cif else None,
        "AI_minus_Human_day180_CIF_percentage_points": (ai_cif - human_cif) * 100.0,
    }

    # Read the authors' fitted corrective provenance effect as the sanity anchor.
    paper_corrective = None
    with MODEL.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("event_type") or "").strip() == "corrective" and (row.get("status") or "").strip() == "fit":
                paper_corrective = {
                    "subhazard_ratio": float(row["subhazard_ratio"]),
                    "ci_lower": float(row["ci_lower"]),
                    "ci_upper": float(row["ci_upper"]),
                    "wald_p_value": float(row["wald_p_value"]),
                    "weighted_lines": int(float(row["weighted_lines"])),
                    "human_lines": int(float(row["human_lines"])),
                    "ai_lines": int(float(row["ai_lines"])),
                }
                break

    asset_exact180_possible = (
        "origin_commit_sha" in finegray_header
        and "survival_days" in finegray_header
        and "event_type" in finegray_header
    )

    summary = {
        "purpose": "Correct the TechLedger experiment outcome to an exact 180-day horizon and audit whether the packaged repository can support that outcome per admitted asset.",
        "required_asset_level_fields": [
            "origin_commit_sha",
            "origin_commit_date",
            "origin provenance",
            "one row or otherwise recoverable timing for each original line's first terminal intervention",
            "terminal maintenance class",
        ],
        "packaged_data_audit": {
            "rq1a_asset_summary_has_origin_commit_sha": "origin_commit_sha" in rq1a_header,
            "rq1a_asset_summary_has_only_first_termination_days_not_each_line_event_time": "first_termination_days" in rq1a_header,
            "rq1b_asset_summary_has_origin_commit_sha": "origin_commit_sha" in rq1b_header,
            "rq1b_asset_summary_has_total_corrective_line_count": "terminal_corrective_line_count" in rq1b_header,
            "rq1b_asset_summary_has_first_termination_days": "first_termination_days" in rq1b_header,
            "rq1b_asset_summary_has_per_corrective_line_event_times": False,
            "finegray_weighted_line_table_rows": row_count,
            "finegray_has_survival_days": "survival_days" in finegray_header,
            "finegray_has_event_type": "event_type" in finegray_header,
            "finegray_has_origin_provenance": "origin_group" in finegray_header,
            "finegray_has_origin_commit_sha": "origin_commit_sha" in finegray_header,
            "finegray_has_origin_feature_intent": False,
            "exact_day180_burden_per_asset_recoverable_from_packaged_files": asset_exact180_possible,
        },
        "why_asset_exact180_is_not_recoverable": (
            "The per-commit RQ1b table retains the total number of corrective first-intervention lines across the full follow-up but not the event time of each of those lines. "
            "The Fine-Gray table retains line event time and provenance but deliberately compresses away origin_commit_sha. Therefore corrective lines occurring after day 180 cannot be removed from each specific asset without the raw line_lifecycle data."
        ),
        "day180_population_provenance_anchor": cif,
        "authors_corrective_fine_gray_anchor": paper_corrective,
        "invalidated_prior_techledger_results": [
            "corrective-burden concentration based on variable follow-up through 2026-05-31",
            "size/activity/Sonar burden baselines based on that variable-follow-up asset outcome",
            "individual marker screen based on that variable-follow-up asset outcome",
        ],
        "still_valid_descriptive_finding": (
            "The source paper's provenance effect remains a valid external/statistical anchor; this checkpoint separately estimates the day-180 population cumulative incidence from its packaged weighted survival table."
        ),
        "next_required_data": (
            "Raw line_lifecycle data (or an equivalent table preserving origin_commit_sha, terminal event class, and terminal event time) is required before rerunning TechLedger's per-asset 180-day experiments correctly."
        ),
        "guardrail": "Do not use the old per-asset corrective counts for further TechLedger modeling once exact 180-day follow-up is adopted.",
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
