from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.webscrape import github_commit_scraper as gcs

RQ1A = Path("dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv")
RQ1B = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
META = Path("filtered_full_sample_repos.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
WORK = Path("/tmp/techledger_exact180")
MATURE_CUTOFF = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)
HORIZON_DAYS = 180.0
PILOT_REPOS = 3


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


def as_int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def is_test_repo(repo: str) -> bool:
    h = int(hashlib.sha256(repo.encode("utf-8")).hexdigest()[:8], 16)
    return h % 10 < 3


def pct(n: float, d: float) -> float | None:
    return n / d if d else None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def read_assets_and_labels():
    assets_by_repo: dict[str, dict[str, dict]] = defaultdict(dict)
    commit_label: dict[tuple[str, str], str] = {}
    label_conflicts = 0

    with RQ1A.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            if not repo or not sha:
                continue
            maintenance = (row.get("origin_maintenance_class") or "").strip()
            if maintenance:
                key = (repo, sha)
                prior = commit_label.get(key)
                if prior and prior != maintenance:
                    label_conflicts += 1
                else:
                    commit_label[key] = maintenance

            if (row.get("origin_primary_operational_intent") or "").strip() != "feature":
                continue
            tracked = as_int(row.get("tracked_line_count"))
            if tracked < 20:
                continue
            dt = parse_dt(row.get("origin_commit_date"))
            if dt is None or dt > MATURE_CUTOFF:
                continue
            assets_by_repo[repo][sha] = {
                "sha": sha,
                "origin_date": dt,
                "tracked_packaged": tracked,
                "origin_role": (row.get("origin_role") or "").strip(),
            }

    with RQ1B.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("first_terminal_commit_sha") or "").strip()
            maintenance = (row.get("first_terminal_maintenance_class") or "").strip()
            if not repo or not sha or sha == "mixed" or not maintenance or maintenance == "mixed":
                continue
            key = (repo, sha)
            prior = commit_label.get(key)
            if prior and prior != maintenance:
                label_conflicts += 1
            else:
                commit_label[key] = maintenance

    return assets_by_repo, commit_label, label_conflicts


def select_pilot(assets_by_repo: dict[str, dict[str, dict]]) -> list[dict]:
    metadata = {}
    with META.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo_slug") or "").strip()
            if repo:
                metadata[repo] = row

    candidates = []
    for repo, assets in assets_by_repo.items():
        n = len(assets)
        if not 40 <= n <= 140:
            continue
        m = metadata.get(repo, {})
        branch = (m.get("branch_checked") or "").strip() or "auto"
        candidates.append({
            "repo": repo,
            "branch": branch,
            "assets": n,
            "language": (m.get("primary_language") or "").strip(),
            "prs": as_int(m.get("num_PRs")),
            "min_monthly_commits": as_int(m.get("min_monthly_commits_observed")),
            "test_split": is_test_repo(repo),
        })

    candidates.sort(key=lambda x: (x["prs"] if x["prs"] > 0 else 10**9, abs(x["assets"] - 75), x["repo"]))
    selected = []
    languages = set()
    for c in candidates:
        if c["language"] and c["language"] in languages:
            continue
        selected.append(c)
        if c["language"]:
            languages.add(c["language"])
        if len(selected) == PILOT_REPOS:
            break
    if len(selected) < PILOT_REPOS:
        for c in candidates:
            if c in selected:
                continue
            selected.append(c)
            if len(selected) == PILOT_REPOS:
                break
    return selected


def reconcile_and_measure(repo: str, assets: dict[str, dict], line_path: Path, commit_label: dict[tuple[str, str], str]) -> dict:
    raw_counts = Counter()
    corrective_180 = Counter()
    terminal_within_180 = Counter()
    unlabeled_terminal_lines = Counter()
    labeled_terminal_lines = Counter()
    unlabeled_terminal_shas = set()
    labeled_terminal_shas = set()

    with line_path.open("r", encoding="utf-8") as handle:
        for text in handle:
            row = json.loads(text)
            sha = row.get("origin_commit_sha")
            if sha not in assets:
                continue
            raw_counts[sha] += 1
            terminal_date = parse_dt(row.get("terminal_commit_date"))
            if terminal_date is None:
                continue
            age = (terminal_date - assets[sha]["origin_date"]).total_seconds() / 86400.0
            if age < 0 or age > HORIZON_DAYS:
                continue
            terminal_within_180[sha] += 1
            terminal_sha = (row.get("terminal_commit_sha") or "").strip()
            klass = commit_label.get((repo, terminal_sha)) if terminal_sha else None
            if klass:
                labeled_terminal_lines[sha] += 1
                labeled_terminal_shas.add(terminal_sha)
                if klass == "corrective":
                    corrective_180[sha] += 1
            else:
                unlabeled_terminal_lines[sha] += 1
                if terminal_sha:
                    unlabeled_terminal_shas.add(terminal_sha)

    per_asset = []
    exact_tracked_matches = 0
    total_packaged = 0
    total_raw = 0
    for sha, asset in assets.items():
        raw = raw_counts[sha]
        packaged = asset["tracked_packaged"]
        if raw == packaged:
            exact_tracked_matches += 1
        total_packaged += packaged
        total_raw += raw
        per_asset.append({
            "sha": sha,
            "role": asset["origin_role"],
            "tracked": raw,
            "tracked_packaged": packaged,
            "corrective_180": corrective_180[sha],
            "terminal_180": terminal_within_180[sha],
            "unlabeled_terminal_180": unlabeled_terminal_lines[sha],
        })

    usable = [a for a in per_asset if a["tracked"] > 0]
    total_corrective = sum(a["corrective_180"] for a in usable)
    total_tracked = sum(a["tracked"] for a in usable)
    zero_share = pct(sum(1 for a in usable if a["corrective_180"] == 0), len(usable))
    ranked = sorted(usable, key=lambda a: a["corrective_180"], reverse=True)
    topn = max(1, round(len(ranked) * 0.10)) if ranked else 0
    top10_capture = pct(sum(a["corrective_180"] for a in ranked[:topn]), total_corrective)

    role_stats = {}
    for role in ("AI", "Human"):
        rs = [a for a in usable if a["role"] == role]
        tr = sum(a["tracked"] for a in rs)
        corr = sum(a["corrective_180"] for a in rs)
        role_stats[role] = {
            "assets": len(rs),
            "tracked_lines": tr,
            "corrective_180_lines": corr,
            "line_weighted_corrective_fraction": pct(corr, tr),
            "median_asset_corrective_fraction": median([a["corrective_180"] / a["tracked"] for a in rs if a["tracked"]]),
        }

    labeled = sum(labeled_terminal_lines.values())
    unlabeled = sum(unlabeled_terminal_lines.values())
    return {
        "target_assets": len(assets),
        "raw_assets_seen": len(usable),
        "exact_tracked_line_count_matches": exact_tracked_matches,
        "exact_tracked_match_share": pct(exact_tracked_matches, len(assets)),
        "packaged_tracked_lines": total_packaged,
        "reconstructed_tracked_lines": total_raw,
        "reconstructed_to_packaged_line_ratio": pct(total_raw, total_packaged),
        "terminal_lines_within_180": sum(terminal_within_180.values()),
        "labeled_terminal_lines_within_180": labeled,
        "unlabeled_terminal_lines_within_180": unlabeled,
        "terminal_line_label_coverage": pct(labeled, labeled + unlabeled),
        "unique_labeled_terminal_shas": len(labeled_terminal_shas),
        "unique_unlabeled_terminal_shas": len(unlabeled_terminal_shas),
        "total_corrective_180_lines": total_corrective,
        "aggregate_corrective_180_fraction": pct(total_corrective, total_tracked),
        "zero_corrective_asset_share": zero_share,
        "top10_asset_share_of_corrective_180_burden": top10_capture,
        "by_origin_role": role_stats,
    }


def scrape_repo(repo_info: dict, assets: dict[str, dict], commit_label: dict[tuple[str, str], str]) -> dict:
    repo = repo_info["repo"]
    requested_branch = repo_info["branch"]
    repo_dir = WORK / "repos" / repo.replace("/", "__")
    output_root = WORK / "output"
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir = gcs.ensure_local_repo(repo, WORK / "repos", skip_fetch=False, full_clone=False)
    try:
        checked_out_branch = gcs.run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        branch = checked_out_branch if requested_branch in {"", "auto"} else requested_branch
        earliest = min(a["origin_date"] for a in assets.values())
        latest = max(a["origin_date"] for a in assets.values())
        since = earliest - timedelta(days=2)
        horizon_end = latest + timedelta(days=HORIZON_DAYS, seconds=1)

        branch_commits, _true_head = gcs.fetch_branch_commits(repo, repo_dir, branch, since=since)
        observation_commits = [c for c in branch_commits if gcs.commit_datetime(c) <= horizon_end]
        available = {c["sha"] for c in observation_commits}
        target_shas = set(assets) & available
        missing_origins = sorted(set(assets) - target_shas)
        origin_commits = [c for c in observation_commits if c["sha"] in target_shas]
        if not origin_commits:
            raise RuntimeError(f"No target origin commits found for {repo}")
        branch_end_sha = observation_commits[-1]["sha"]

        detailed = gcs.add_git_file_changes(repo, repo_dir, observation_commits)
        detailed_by_sha = {c["sha"]: c for c in detailed}
        origin_detailed = [detailed_by_sha[c["sha"]] for c in origin_commits]

        gcs.write_outputs(
            origin_detailed,
            output_root / repo.replace("/", "__"),
            repo_dir,
            blame_workers=2,
            branch=branch,
            branch_end_sha=branch_end_sha,
            blame_since=since,
            lifecycle_commits=detailed,
            lifecycle_origin_shas=target_shas,
        )

        line_path = output_root / repo.replace("/", "__") / "line_lifecycle.jsonl"
        measured = reconcile_and_measure(repo, {sha: assets[sha] for sha in target_shas}, line_path, commit_label)
        measured["missing_target_origin_commits"] = len(missing_origins)
        measured["requested_branch"] = requested_branch
        measured["resolved_branch"] = branch
        measured["observation_endpoint_sha"] = branch_end_sha
        measured["earliest_target_origin"] = earliest.isoformat()
        measured["latest_target_origin"] = latest.isoformat()
        measured["observation_horizon_end"] = horizon_end.isoformat()
        return measured
    finally:
        gcs.remove_cloned_repo_dir(repo_dir)


def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)

    assets_by_repo, commit_label, label_conflicts = read_assets_and_labels()
    pilot = select_pilot(assets_by_repo)
    if len(pilot) < PILOT_REPOS:
        raise RuntimeError(f"Could only select {len(pilot)} pilot repositories")

    results = {}
    failures = []
    for info in pilot:
        repo = info["repo"]
        try:
            results[repo] = scrape_repo(repo, assets_by_repo[repo], commit_label)
        except Exception as exc:
            failures.append({
                "repo": repo,
                "error": str(exc)[:4000],
                "traceback": traceback.format_exc()[-12000:],
            })
            break

    summary = {
        "purpose": "Validate reconstruction of exact 180-day corrective burden per feature admission using the authors' own line-lifecycle scraper on a small pilot.",
        "horizon_days": HORIZON_DAYS,
        "asset_definition": "feature-classified origin commit with >=20 packaged tracked lines and origin date <= 2025-12-02",
        "pilot_selection": pilot,
        "terminal_label_source": "Packaged RQ1A origin_maintenance_class plus RQ1B first_terminal_commit_sha/class mapping; no new LLM classification.",
        "commit_label_map_size": len(commit_label),
        "commit_label_conflicts": label_conflicts,
        "results": results,
        "failures": failures,
        "success_criteria": {
            "all_selected_repos_complete": len(failures) == 0 and len(results) == len(pilot),
            "tracked_line_reconciliation_target": ">=95% exact asset matches and reconstructed/package aggregate ratio near 1.0",
            "terminal_classification_coverage_target": ">=95% of within-180 terminal lines labeled",
        },
        "guardrail": "This pilot validates the corrected outcome pipeline only. Do not infer TechLedger marker effects from three selected repositories.",
        "next_if_success": "Scale the same exact-180 reconstruction across the study repositories in batches, then rerun burden concentration and marker screening on equal follow-up.",
    }
    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
