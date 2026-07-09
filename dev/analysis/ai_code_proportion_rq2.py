from __future__ import annotations

import argparse
import bisect
import calendar
import csv
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.ticker import FormatStrFormatter, PercentFormatter
from scipy import stats

try:
    import statsmodels.api as sm
except ModuleNotFoundError:
    sm = None

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:  # Allows `python -m dev.analysis.ai_code_proportion_rq2`.
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.webscrape.commit_contribution_classifier import (
    MAINTENANCE_CLASS_ORDER,
    SOURCE_EXTENSIONS,
    is_test_path,
)


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_rq2")
DEFAULT_ORIGIN_START = "2025-05"
DEFAULT_ORIGIN_END = "2026-05"
FONT_DIR = ROOT / "font"

COMMIT_FILE = "all_commits.jsonl"
COMMIT_CLASSIFICATIONS_FILE = "commit_contribution_classifications.jsonl"
COMMIT_FILE_CHANGES_FILE = "commit_file_changes.jsonl"
COMMIT_FILE_CLASSIFICATIONS_FILE = "commit_file_contribution_classifications.jsonl"
LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE = "codebase_agent_share_snapshots.jsonl"
SONARQUBE_SCANS_FILE = "sonarqube_snapshot_scans.jsonl"
CODEBASE_AGENT_SHARE_FILES_FILE = "codebase_agent_share_files.jsonl"
COMMIT_ROLE_CACHE_FILE = ".codebase_agent_share_commit_cache.json"
AI_COAUTHORED_COMMITS_FILE = "ai_coauthored_commits.jsonl"
NOT_AI_COAUTHORED_COMMITS_FILE = "not_ai_coauthored_commits.jsonl"

RQ2_INPUT_FILES = (
    COMMIT_CLASSIFICATIONS_FILE,
    LINE_LIFECYCLE_FILE,
    CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE,
)

LOG = logging.getLogger(__name__)
CORRECTIVE_INTENTS = {"bug_fix", "security_fix", "revert"}
AI_COLOR = "#8789C0"
HUMAN_COLOR = "#F58F29"
DEFAULT_PANEL_CSV_NAME = "rq2_repo_week_panel.csv"
DEFAULT_BETABINOMIAL_PREDICTION_CSV_NAME = "rq2_panel_glmer_predicted_rates.csv"
DEFAULT_MAX_COMMIT_ADDITIONS = 500_000
MAINTENANCE_CLASSES = tuple(
    maintenance_class
    for maintenance_class in MAINTENANCE_CLASS_ORDER
    if maintenance_class != "unknown"
)
MAINTENANCE_CLASS_PANEL_COLUMNS = tuple(
    column
    for maintenance_class in MAINTENANCE_CLASSES
    for column in (
        f"maintenance_{maintenance_class}_commits",
        f"maintenance_{maintenance_class}_rate",
        f"maintenance_{maintenance_class}_changed_lines",
        f"maintenance_{maintenance_class}_line_rate",
    )
)


def setup_nimbus_font() -> None:
    regular_font = FONT_DIR / "NimbusRomNo9L-Reg.otf"
    nimbus_fonts = sorted(FONT_DIR.glob("NimbusRomNo9L-*.otf"))
    if not regular_font.exists() or not nimbus_fonts:
        return
    for font_path in nimbus_fonts:
        font_manager.fontManager.addfont(str(font_path))
    font_name = font_manager.FontProperties(fname=str(regular_font)).get_name()
    plt.rcParams["font.family"] = font_name
SONAR_PANEL_COLUMNS = (
    "sonar_scan_count",
    "sonar_ncloc",
    "sonar_issue_count",
    "sonar_bug_count",
    "sonar_vulnerability_count",
    "sonar_code_smell_count",
    "sonar_security_hotspot_count",
    "sonar_defect_security_issue_count",
    "sonar_severe_issue_count",
    "sonar_complexity",
    "sonar_cognitive_complexity",
    "sonar_duplicated_lines_density",
    "sonar_comment_lines_density",
    "sonar_sqale_index",
    "sonar_maintainability_effort",
    "sonar_issue_density_per_kloc",
    "sonar_bug_density_per_kloc",
    "sonar_vulnerability_density_per_kloc",
    "sonar_code_smell_density_per_kloc",
    "sonar_security_hotspot_density_per_kloc",
    "sonar_defect_security_issue_density_per_kloc",
    "sonar_severe_issue_density_per_kloc",
    "sonar_complexity_per_kloc",
    "sonar_cognitive_complexity_per_kloc",
    "sonar_sqale_index_per_kloc",
    "sonar_maintainability_effort_per_kloc",
)
SONAR_FINDING_SPECS = (
    ("sonar_issue_density_per_kloc", "sonar_issue_count", "Total issues"),
    ("sonar_bug_density_per_kloc", "sonar_bug_count", "Bugs"),
    (
        "sonar_vulnerability_density_per_kloc",
        "sonar_vulnerability_count",
        "Vulnerabilities",
    ),
    ("sonar_code_smell_density_per_kloc", "sonar_code_smell_count", "Code smells"),
    (
        "sonar_security_hotspot_density_per_kloc",
        "sonar_security_hotspot_count",
        "Security hotspots",
    ),
    (
        "sonar_severe_issue_density_per_kloc",
        "sonar_severe_issue_count",
        "Blocker + critical issues",
    ),
)
SONAR_COMPLEXITY_SPECS = (
    ("sonar_complexity_per_kloc", "Complexity / 1K NCLOC"),
    ("sonar_cognitive_complexity_per_kloc", "Cognitive complexity / 1K NCLOC"),
    ("sonar_duplicated_lines_density", "Duplicated lines density"),
    ("sonar_comment_lines_density", "Comment lines density"),
    ("sonar_sqale_index_per_kloc", "Technical debt / 1K NCLOC"),
    (
        "sonar_maintainability_effort_per_kloc",
        "Maintainability remediation / 1K NCLOC",
    ),
)
SONAR_COMPLEXITY_DEMEANED_SPECS = (
    ("sonar_complexity", "Complexity"),
    ("sonar_cognitive_complexity", "Cognitive complexity"),
    ("sonar_duplicated_lines_density", "Duplicated lines density"),
    ("sonar_comment_lines_density", "Comment lines density"),
    ("sonar_sqale_index", "Technical debt"),
    ("sonar_maintainability_effort", "Maintainability remediation"),
)
REPO_ADOPTION_LABELS = {
    "ai_dominated": "AI-dominated",
    "balanced": "Balanced",
    "human_dominated": "Human-dominated",
}
REPO_ADOPTION_ORDER = (
    "ai_dominated",
    "balanced",
    "human_dominated",
)
REPO_ADOPTION_POINT_COLORS = {
    "ai_dominated": AI_COLOR,
    "balanced": "#59a14f",
    "human_dominated": HUMAN_COLOR,
}
COMMIT_LINE_FIELD_PATTERNS = {
    "sha": re.compile(r'"sha"\s*:\s*"([^"]+)"'),
    "date": re.compile(r'"date"\s*:\s*"([^"]+)"'),
    "category": re.compile(r'"category"\s*:\s*"([^"]+)"'),
}
COMMIT_FILE_CHANGE_PATTERNS = {
    "commit_sha": re.compile(r'"commit_sha"\s*:\s*"([^"]+)"'),
    "sha": re.compile(r'"sha"\s*:\s*"([^"]+)"'),
    "additions": re.compile(r'"additions"\s*:\s*(\d+)'),
    "deletions": re.compile(r'"deletions"\s*:\s*(\d+)'),
    "changes": re.compile(r'"changes"\s*:\s*(\d+)'),
}
LINE_LIFECYCLE_DATE_PATTERNS = {
    "origin_commit_date": re.compile(r'"origin_commit_date"\s*:\s*"([^"]+)"'),
    "terminal_commit_date": re.compile(r'"terminal_commit_date"\s*:\s*"([^"]+)"'),
}
LINE_LIFECYCLE_FIELD_PATTERNS = {
    "origin_commit_category": re.compile(
        r'"origin_commit_category"\s*:\s*"([^"]+)"'
    ),
    "terminal_commit_sha": re.compile(r'"terminal_commit_sha"\s*:\s*"([^"]+)"'),
}
REPO_WEEK_PANEL_COLUMNS = (
    "repo_key",
    "repo",
    "week",
    "adoption_class",
    "ai_share",
    "lagged_ai_share",
    "total_commits",
    "total_changed_lines",
    "corrective_commits",
    "corrective_rate",
    "corrective_changed_lines",
    "corrective_line_rate",
    "ai_total_commits",
    "human_total_commits",
    "human_total_changed_lines",
    "human_corrective_commits",
    "human_corrective_rate",
    "human_corrective_changed_lines",
    "human_corrective_line_rate",
    "human_origin_living_tracked_lines_at_week_start",
    "human_origin_corrective_terminated_lines",
    "human_origin_corrective_line_rate",
    *MAINTENANCE_CLASS_PANEL_COLUMNS,
    *SONAR_PANEL_COLUMNS,
    "living_tracked_lines_at_week_start",
    "terminated_existing_lines",
    "tracked_line_churn_rate",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def read_sonarqube_scan_summaries(repo_dir: Path) -> list[dict[str, Any]]:
    """Stream SonarQube scan JSONL and keep only fields needed for RQ2 plots."""
    path = repo_dir / SONARQUBE_SCANS_FILE
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "snapshot_date": row.get("snapshot_date"),
                    "scan_status": row.get("scan_status"),
                    "scanner_returncode": row.get("scanner_returncode"),
                    "ce_task_status": row.get("ce_task_status"),
                    "issue_count": row.get("issue_count"),
                    "issue_counts_by_severity": row.get("issue_counts_by_severity"),
                    "issue_counts_by_type": row.get("issue_counts_by_type"),
                    "metrics": row.get("metrics"),
                }
            )
    return rows


def slim_commit_role_row(row: dict[str, Any]) -> dict[str, Any] | None:
    sha = row.get("sha")
    date = row.get("date")
    category = row.get("category")
    if not isinstance(sha, str) or not sha:
        return None
    if category not in {"ai_coauthored", "not_ai_coauthored"}:
        return None
    return {"sha": sha, "date": date, "category": category}


def extract_commit_role_fields(line: str, default_category: str | None = None) -> dict[str, Any] | None:
    values: dict[str, str] = {}
    for field, pattern in COMMIT_LINE_FIELD_PATTERNS.items():
        match = pattern.search(line)
        if match is not None:
            values[field] = match.group(1)
    if default_category is not None:
        values["category"] = default_category
    return slim_commit_role_row(values)


def read_commit_roles(repo_dir: Path) -> list[dict[str, Any]]:
    cache_path = repo_dir / COMMIT_ROLE_CACHE_FILE
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as handle:
            cache = json.load(handle)
        if isinstance(cache, dict):
            return [
                slim
                for value in cache.values()
                if isinstance(value, dict)
                for slim in [slim_commit_role_row(value)]
                if slim is not None
            ]

    rows: list[dict[str, Any]] = []
    for filename, category in (
        (AI_COAUTHORED_COMMITS_FILE, "ai_coauthored"),
        (NOT_AI_COAUTHORED_COMMITS_FILE, "not_ai_coauthored"),
    ):
        path = repo_dir / filename
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = extract_commit_role_fields(line, default_category=category)
                if row is not None:
                    rows.append(row)
    if rows:
        return rows

    all_commits_path = repo_dir / COMMIT_FILE
    if all_commits_path.exists():
        with all_commits_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = extract_commit_role_fields(line)
                if row is not None:
                    rows.append(row)
    return rows


def regex_int_from_line(line: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(line)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def read_commit_change_counts(repo_dir: Path) -> dict[str, dict[str, int]]:
    """Stream commit_file_changes.jsonl into one churn aggregate per commit SHA."""
    path = repo_dir / COMMIT_FILE_CHANGES_FILE
    counts_by_sha: dict[str, dict[str, int]] = {}
    if not path.exists():
        return counts_by_sha

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            sha_match = COMMIT_FILE_CHANGE_PATTERNS["commit_sha"].search(line)
            if sha_match is None:
                sha_match = COMMIT_FILE_CHANGE_PATTERNS["sha"].search(line)
            if sha_match is None:
                continue

            sha = sha_match.group(1)
            additions = (
                regex_int_from_line(line, COMMIT_FILE_CHANGE_PATTERNS["additions"])
                or 0
            )
            deletions = (
                regex_int_from_line(line, COMMIT_FILE_CHANGE_PATTERNS["deletions"])
                or 0
            )
            changes = regex_int_from_line(
                line, COMMIT_FILE_CHANGE_PATTERNS["changes"]
            )
            if changes is None:
                changes = additions + deletions

            bucket = counts_by_sha.setdefault(
                sha,
                {
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                    "changed_lines": 0,
                },
            )
            bucket["additions"] += additions
            bucket["deletions"] += deletions
            bucket["changes"] += changes
            bucket["changed_lines"] += changes

    return counts_by_sha


def read_line_lifecycle_week_counts(
    repo_dir: Path,
    *,
    corrective_terminal_shas: set[str] | None = None,
) -> dict[str, Any]:
    path = repo_dir / LINE_LIFECYCLE_FILE
    origin_lines_by_week: Counter[datetime] = Counter()
    terminal_lines_by_week: Counter[datetime] = Counter()
    terminated_existing_lines_by_week: Counter[datetime] = Counter()
    human_origin_lines_by_week: Counter[datetime] = Counter()
    human_origin_terminal_lines_by_week: Counter[datetime] = Counter()
    human_origin_corrective_terminal_lines_by_week: Counter[datetime] = Counter()
    total_lines = 0
    missing_origin_dates = 0
    corrective_terminal_shas = corrective_terminal_shas or set()

    if not path.exists():
        return {
            "origin_lines_by_week": origin_lines_by_week,
            "terminal_lines_by_week": terminal_lines_by_week,
            "terminated_existing_lines_by_week": terminated_existing_lines_by_week,
            "human_origin_lines_by_week": human_origin_lines_by_week,
            "human_origin_terminal_lines_by_week": human_origin_terminal_lines_by_week,
            "human_origin_corrective_terminal_lines_by_week": (
                human_origin_corrective_terminal_lines_by_week
            ),
            "total_lines": total_lines,
            "missing_origin_dates": missing_origin_dates,
        }

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            total_lines += 1
            origin_match = LINE_LIFECYCLE_DATE_PATTERNS["origin_commit_date"].search(
                line
            )
            if origin_match is None:
                missing_origin_dates += 1
                continue
            origin_date_value = origin_match.group(1)
            origin_date = parse_datetime(origin_date_value)
            origin_week = week_bucket(origin_date_value)
            if origin_date is None or origin_week is None:
                missing_origin_dates += 1
                continue
            origin_lines_by_week[origin_week] += 1
            origin_category_match = LINE_LIFECYCLE_FIELD_PATTERNS[
                "origin_commit_category"
            ].search(line)
            is_human_origin = (
                origin_category_match is not None
                and origin_category_match.group(1) == "not_ai_coauthored"
            )
            if is_human_origin:
                human_origin_lines_by_week[origin_week] += 1

            terminal_match = LINE_LIFECYCLE_DATE_PATTERNS[
                "terminal_commit_date"
            ].search(line)
            if terminal_match is None:
                continue
            terminal_date_value = terminal_match.group(1)
            terminal_week = week_bucket(terminal_date_value)
            if terminal_week is None:
                continue
            terminal_lines_by_week[terminal_week] += 1
            if origin_date < terminal_week:
                terminated_existing_lines_by_week[terminal_week] += 1
                if is_human_origin:
                    human_origin_terminal_lines_by_week[terminal_week] += 1
                    terminal_sha_match = LINE_LIFECYCLE_FIELD_PATTERNS[
                        "terminal_commit_sha"
                    ].search(line)
                    if (
                        terminal_sha_match is not None
                        and terminal_sha_match.group(1) in corrective_terminal_shas
                    ):
                        human_origin_corrective_terminal_lines_by_week[
                            terminal_week
                        ] += 1

    return {
        "origin_lines_by_week": origin_lines_by_week,
        "terminal_lines_by_week": terminal_lines_by_week,
        "terminated_existing_lines_by_week": terminated_existing_lines_by_week,
        "human_origin_lines_by_week": human_origin_lines_by_week,
        "human_origin_terminal_lines_by_week": human_origin_terminal_lines_by_week,
        "human_origin_corrective_terminal_lines_by_week": (
            human_origin_corrective_terminal_lines_by_week
        ),
        "total_lines": total_lines,
        "missing_origin_dates": missing_origin_dates,
    }


def split_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def normalized_repo_aliases(repo: str) -> set[str]:
    normalized = repo.strip()
    if not normalized:
        return set()
    return {normalized, normalized.replace("/", "__"), normalized.replace("__", "/")}


def repo_filter_aliases(
    repos: list[str] | None,
    repo_file: Path | None,
) -> set[str]:
    aliases: set[str] = set()
    for repo in split_csv(repos):
        aliases.update(normalized_repo_aliases(repo))
    if repo_file is not None and repo_file.exists():
        with repo_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                repo = line.strip()
                if not repo or repo.startswith("#"):
                    continue
                aliases.update(normalized_repo_aliases(repo))
    return aliases


def filter_panel_rows_by_repo(
    rows: list[dict[str, Any]],
    repo_aliases: set[str],
) -> list[dict[str, Any]]:
    if not repo_aliases:
        return rows
    return [
        row
        for row in rows
        if str(row.get("repo") or "") in repo_aliases
        or str(row.get("repo_key") or "") in repo_aliases
    ]


def filter_panel_rows_by_week(
    rows: list[dict[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    if start is None and end is None:
        return rows
    return [
        row
        for row in rows
        if row.get("week") is not None
        and (start is None or row["week"] >= week_bucket(start))
        and (end is None or row["week"] <= week_bucket(end))
    ]


def repo_from_dir(repo_dir: Path) -> str:
    return repo_dir.name.replace("__", "/")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_time_bound(value: Any, *, is_end: bool) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "all", "open"}:
        return None

    if len(text) == 7 and text[4] == "-":
        try:
            year, month = map(int, text.split("-"))
        except ValueError:
            return None
        if is_end:
            last_day = calendar.monthrange(year, month)[1]
            return datetime.combine(
                datetime(year, month, last_day).date(),
                time(23, 59, 59, 999999),
                tzinfo=timezone.utc,
            )
        return datetime(year, month, 1, tzinfo=timezone.utc)

    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        suffix = "T23:59:59.999999+00:00" if is_end else "T00:00:00+00:00"
        return parse_datetime(f"{text}{suffix}")

    return parse_datetime(text)


def value_is_open_time_bound(value: Any) -> bool:
    return str(value or "").strip().lower() in {"", "none", "all", "open"}


def date_in_window(
    value: Any,
    *,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    parsed = parse_datetime(value)
    if parsed is None:
        return False
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
    return parsed


def commit_added_line_count(row: dict[str, Any]) -> int | None:
    for key in ("additions", "total_additions"):
        value = safe_int(row.get(key))
        if value is not None:
            return value

    # Older exports may only have churn-style fields. Use them as a fallback so
    # the outlier guard still catches extremely large commits on older data.
    for key in ("changed_lines", "changes"):
        value = safe_int(row.get(key))
        if value is not None:
            return value
    return None


def commit_changed_line_count(row: dict[str, Any]) -> int:
    for key in ("changed_lines", "changes", "total_changed_lines"):
        value = safe_int(row.get(key))
        if value is not None:
            return max(value, 0)
    additions = commit_added_line_count(row) or 0
    deletions = safe_int(row.get("deletions")) or safe_int(row.get("total_deletions")) or 0
    return max(additions + deletions, 0)


def commit_row_with_change_counts(
    row: dict[str, Any],
    change_counts_by_sha: dict[str, dict[str, int]],
) -> dict[str, Any]:
    sha = row.get("sha") or row.get("commit_sha")
    if not isinstance(sha, str) or not sha:
        return row
    change_counts = change_counts_by_sha.get(sha)
    if not change_counts:
        return row
    combined = dict(row)
    combined.update(change_counts)
    return combined


def is_commit_addition_outlier(
    row: dict[str, Any],
    max_commit_additions: int | None,
) -> bool:
    if max_commit_additions is None:
        return False
    added_lines = commit_added_line_count(row)
    return added_lines is not None and added_lines >= max_commit_additions


def maintenance_class_for_commit(row: dict[str, Any]) -> str | None:
    maintenance_class = row.get("maintenance_class")
    if isinstance(maintenance_class, str) and maintenance_class in MAINTENANCE_CLASSES:
        return maintenance_class
    return None


def week_bucket(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    week_start = parsed - timedelta(days=parsed.weekday())
    return datetime(
        week_start.year,
        week_start.month,
        week_start.day,
        tzinfo=timezone.utc,
    )


def normalized_file_path(path: str | None) -> str:
    return (path or "").replace("\\", "/")


def file_classification_paths(row: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("filename", "previous_filename"):
        value = row.get(key)
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)
    return paths


def is_source_file_classification(row: dict[str, Any]) -> bool:
    tags = {str(tag) for tag in row.get("file_object_tags") or []}
    return row.get("file_primary_role") == "source_code" or "source_code" in tags


def is_source_codebase_agent_share_file(row: dict[str, Any]) -> bool:
    path = str(row.get("path") or "")
    extension = str(row.get("extension") or "").lower()
    if extension in {"", "(none)"}:
        extension = Path(path).suffix.lower()
    return extension in SOURCE_EXTENSIONS and not is_test_path(path)


def is_corrective_commit(row: dict[str, Any]) -> bool:
    return row.get("primary_operational_intent") in CORRECTIVE_INTENTS


def commit_role(row: dict[str, Any]) -> str | None:
    category = row.get("category")
    if category == "ai_coauthored":
        return "AI"
    if category == "not_ai_coauthored":
        return "Human"
    return None


def commit_roles_by_sha(rows: list[dict[str, Any]]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for row in rows:
        sha = row.get("sha")
        role = commit_role(row)
        if isinstance(sha, str) and sha and role in {"AI", "Human"}:
            roles[sha] = role
    return roles


def corrective_commit_shas(classification_rows: list[dict[str, Any]]) -> set[str]:
    shas: set[str] = set()
    for row in classification_rows:
        sha = row.get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        if is_corrective_commit(row):
            shas.add(sha)
    return shas


def weekly_ai_human_counts(
    repo: dict[str, Any],
    excluded_shas: set[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[datetime, Counter[str]]:
    counts_by_week: dict[datetime, Counter[str]] = defaultdict(Counter)
    for row in repo.get("commit_roles") or repo.get("commits") or []:
        sha = row.get("sha")
        if excluded_shas and isinstance(sha, str) and sha in excluded_shas:
            continue
        if not date_in_window(row.get("date"), start=start, end=end):
            continue
        role = commit_role(row)
        if role is None:
            continue
        week = week_bucket(row.get("date"))
        if week is None:
            continue
        counts_by_week[week][role] += 1
    return counts_by_week


def classify_repo_ai_adoption(weekly_counts: dict[datetime, Counter[str]]) -> str:
    ai_dominated_weeks = 0
    has_ai_majority_week = False
    for counts in weekly_counts.values():
        ai_commits = counts["AI"]
        human_commits = counts["Human"]
        if ai_commits > human_commits:
            has_ai_majority_week = True
        if ai_commits > human_commits * 2:
            ai_dominated_weeks += 1

    if ai_dominated_weeks >= 4:
        return "ai_dominated"
    if not has_ai_majority_week:
        return "human_dominated"
    return "balanced"


def source_touching_commit_shas(repo: dict[str, Any]) -> set[str]:
    shas: set[str] = set()
    for row in repo.get("commit_file_classifications") or []:
        if not is_source_file_classification(row):
            continue
        commit_sha = row.get("commit_sha")
        if isinstance(commit_sha, str) and commit_sha:
            shas.add(commit_sha)
    return shas


def aggregate_snapshot_ai_share_by_week(
    rows: list[dict[str, Any]],
) -> dict[datetime, tuple[datetime, float]]:
    by_week: dict[datetime, tuple[datetime, float]] = {}
    for row in rows:
        snapshot_date = parse_datetime(row.get("snapshot_date"))
        week = week_bucket(row.get("snapshot_date"))
        if snapshot_date is None or week is None:
            continue
        total_lines = float(row.get("total_living_lines") or 0)
        ai_lines = float(row.get("ai_living_lines") or 0)
        share = ai_lines / total_lines if total_lines else float(row.get("ai_living_line_share") or 0)
        current = by_week.get(week)
        if current is None or snapshot_date >= current[0]:
            by_week[week] = (snapshot_date, share)
    return by_week


def first_numeric_value(*values: Any) -> float | None:
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def nested_numeric_value(mapping: Any, key: str) -> float | None:
    if not isinstance(mapping, dict):
        return None
    return safe_float(mapping.get(key))


def sonarqube_scan_is_usable(row: dict[str, Any]) -> bool:
    scan_status = row.get("scan_status")
    if scan_status not in {"ok", "copied_repeated_snapshot"}:
        return False
    returncode = safe_int(row.get("scanner_returncode"))
    return returncode in (None, 0)


def sonar_density(count: float | None, ncloc: float | None) -> float | None:
    if count is None or ncloc is None or ncloc <= 0:
        return None
    return count / ncloc * 1000.0


def sonar_panel_fields(row: dict[str, Any], *, scan_count: int) -> dict[str, Any]:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    type_counts = (
        row.get("issue_counts_by_type")
        if isinstance(row.get("issue_counts_by_type"), dict)
        else {}
    )
    severity_counts = (
        row.get("issue_counts_by_severity")
        if isinstance(row.get("issue_counts_by_severity"), dict)
        else {}
    )

    ncloc = nested_numeric_value(metrics, "ncloc")
    issue_count = first_numeric_value(row.get("issue_count"))
    bug_count = first_numeric_value(
        nested_numeric_value(metrics, "bugs"),
        nested_numeric_value(type_counts, "BUG"),
    )
    vulnerability_count = first_numeric_value(
        nested_numeric_value(metrics, "vulnerabilities"),
        nested_numeric_value(type_counts, "VULNERABILITY"),
    )
    code_smell_count = first_numeric_value(
        nested_numeric_value(metrics, "code_smells"),
        nested_numeric_value(type_counts, "CODE_SMELL"),
    )
    security_hotspot_count = nested_numeric_value(metrics, "security_hotspots")
    defect_security_issue_count = sum(
        value or 0.0
        for value in (
            bug_count,
            vulnerability_count,
            security_hotspot_count,
        )
    )
    complexity = nested_numeric_value(metrics, "complexity")
    cognitive_complexity = nested_numeric_value(metrics, "cognitive_complexity")
    duplicated_lines_density = nested_numeric_value(metrics, "duplicated_lines_density")
    comment_lines_density = nested_numeric_value(metrics, "comment_lines_density")
    sqale_index = nested_numeric_value(metrics, "sqale_index")
    maintainability_effort = nested_numeric_value(
        metrics,
        "software_quality_maintainability_remediation_effort",
    )
    severe_issue_count = sum(
        value or 0.0
        for value in (
            nested_numeric_value(severity_counts, "BLOCKER"),
            nested_numeric_value(severity_counts, "CRITICAL"),
        )
    )

    return {
        "sonar_scan_count": scan_count,
        "sonar_ncloc": ncloc,
        "sonar_issue_count": issue_count,
        "sonar_bug_count": bug_count,
        "sonar_vulnerability_count": vulnerability_count,
        "sonar_code_smell_count": code_smell_count,
        "sonar_security_hotspot_count": security_hotspot_count,
        "sonar_defect_security_issue_count": defect_security_issue_count,
        "sonar_severe_issue_count": severe_issue_count,
        "sonar_complexity": complexity,
        "sonar_cognitive_complexity": cognitive_complexity,
        "sonar_duplicated_lines_density": duplicated_lines_density,
        "sonar_comment_lines_density": comment_lines_density,
        "sonar_sqale_index": sqale_index,
        "sonar_maintainability_effort": maintainability_effort,
        "sonar_issue_density_per_kloc": sonar_density(issue_count, ncloc),
        "sonar_bug_density_per_kloc": sonar_density(bug_count, ncloc),
        "sonar_vulnerability_density_per_kloc": sonar_density(
            vulnerability_count,
            ncloc,
        ),
        "sonar_code_smell_density_per_kloc": sonar_density(
            code_smell_count,
            ncloc,
        ),
        "sonar_security_hotspot_density_per_kloc": sonar_density(
            security_hotspot_count,
            ncloc,
        ),
        "sonar_defect_security_issue_density_per_kloc": sonar_density(
            defect_security_issue_count,
            ncloc,
        ),
        "sonar_severe_issue_density_per_kloc": sonar_density(
            severe_issue_count,
            ncloc,
        ),
        "sonar_complexity_per_kloc": sonar_density(complexity, ncloc),
        "sonar_cognitive_complexity_per_kloc": sonar_density(
            cognitive_complexity,
            ncloc,
        ),
        "sonar_sqale_index_per_kloc": sonar_density(sqale_index, ncloc),
        "sonar_maintainability_effort_per_kloc": sonar_density(
            maintainability_effort,
            ncloc,
        ),
    }


def aggregate_sonarqube_scans_by_week(
    rows: list[dict[str, Any]],
) -> dict[datetime, dict[str, Any]]:
    latest_scan_by_week: dict[datetime, tuple[datetime, dict[str, Any]]] = {}
    scan_counts_by_week: Counter[datetime] = Counter()
    for row in rows:
        if not sonarqube_scan_is_usable(row):
            continue
        snapshot_date = parse_datetime(row.get("snapshot_date"))
        week = week_bucket(row.get("snapshot_date"))
        if snapshot_date is None or week is None:
            continue
        scan_counts_by_week[week] += 1
        current = latest_scan_by_week.get(week)
        if current is None or snapshot_date >= current[0]:
            latest_scan_by_week[week] = (snapshot_date, row)

    return {
        week: sonar_panel_fields(row, scan_count=scan_counts_by_week[week])
        for week, (_snapshot_date, row) in latest_scan_by_week.items()
    }


def aggregate_source_snapshot_ai_share_by_week(
    rows: list[dict[str, Any]],
) -> dict[datetime, tuple[datetime, float]]:
    counts_by_week: dict[datetime, dict[str, Any]] = defaultdict(
        lambda: {"snapshot_date": None, "ai_lines": 0.0, "total_lines": 0.0}
    )
    for row in rows:
        if not is_source_codebase_agent_share_file(row):
            continue
        snapshot_date = parse_datetime(row.get("snapshot_date"))
        week = week_bucket(row.get("snapshot_date"))
        if snapshot_date is None or week is None:
            continue
        bucket = counts_by_week[week]
        if bucket["snapshot_date"] is None or snapshot_date > bucket["snapshot_date"]:
            bucket["snapshot_date"] = snapshot_date
            bucket["ai_lines"] = 0.0
            bucket["total_lines"] = 0.0
        if snapshot_date == bucket["snapshot_date"]:
            bucket["ai_lines"] += float(row.get("ai_living_lines") or 0)
            bucket["total_lines"] += float(row.get("total_living_lines") or 0)

    shares: dict[datetime, tuple[datetime, float]] = {}
    for week, bucket in counts_by_week.items():
        snapshot_date = bucket["snapshot_date"]
        total_lines = bucket["total_lines"]
        if snapshot_date is not None and total_lines:
            shares[week] = (snapshot_date, bucket["ai_lines"] / total_lines)
    return shares


def lagged_ai_share_for_weeks(
    weeks: list[datetime],
    share_by_week: dict[datetime, tuple[datetime, float]],
) -> list[float | None]:
    shares: list[float | None] = []
    sorted_snapshot_weeks = sorted(share_by_week)
    snapshot_index = 0
    latest_share: float | None = None
    for week in weeks:
        while (
            snapshot_index < len(sorted_snapshot_weeks)
            and sorted_snapshot_weeks[snapshot_index] < week
        ):
            latest_share = share_by_week[sorted_snapshot_weeks[snapshot_index]][1]
            snapshot_index += 1
        shares.append(latest_share)
    return shares


def load_repo_rq2_data(repo_dir: Path) -> dict[str, Any]:
    """Load raw per-repo inputs needed to build the RQ2 repo-week panel later."""
    commit_roles = read_commit_roles(repo_dir)
    commit_classifications = read_jsonl(repo_dir / COMMIT_CLASSIFICATIONS_FILE)
    commit_change_counts = read_commit_change_counts(repo_dir)
    line_lifecycle_week_counts = read_line_lifecycle_week_counts(
        repo_dir,
        corrective_terminal_shas=corrective_commit_shas(commit_classifications),
    )
    codebase_agent_share_snapshots = read_jsonl(
        repo_dir / CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE
    )
    sonarqube_snapshot_scans = read_sonarqube_scan_summaries(repo_dir)

    return {
        "repo_key": repo_dir.name,
        "repo": repo_from_dir(repo_dir),
        "repo_dir": str(repo_dir),
        "commit_roles": commit_roles,
        "commit_classifications": commit_classifications,
        "commit_change_counts": commit_change_counts,
        "line_lifecycle_week_counts": line_lifecycle_week_counts,
        "codebase_agent_share_snapshots": codebase_agent_share_snapshots,
        "sonarqube_snapshot_scans": sonarqube_snapshot_scans,
    }


def rq2_repo_data_summary(repo_dir: Path, repo_data: dict[str, Any]) -> str:
    return (
        f"roles={len(repo_data.get('commit_roles') or []):,} "
        f"commits={len(repo_data.get('commit_classifications') or []):,} "
        f"file_change_commits={len(repo_data.get('commit_change_counts') or {}):,} "
        f"lines={(repo_data.get('line_lifecycle_week_counts') or {}).get('total_lines', 0):,} "
        f"snapshots={len(repo_data.get('codebase_agent_share_snapshots') or []):,} "
        f"sonar_scans={len(repo_data.get('sonarqube_snapshot_scans') or []):,}"
    )


def load_rq2_data(
    input_dir: Path = DEFAULT_INPUT_DIR,
    *,
    repos: list[str] | None = None,
    repo_file: Path | None = None,
    workers: int | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Resolve repo output dirs and load RQ2 inputs in parallel by repo."""
    repo_dirs = resolve_repo_dirs(
        input_dir,
        repo_file=repo_file,
        repos=repos or [],
        required_files=RQ2_INPUT_FILES,
    )
    if not repo_dirs:
        raise FileNotFoundError(f"No repo output directories found under {input_dir}")

    return load_repo_data_parallel(
        repo_dirs,
        load_repo_rq2_data,
        workers=workers,
        default_worker_limit=4,
        task_label="RQ2 input data",
        logger=logger or LOG,
        result_summary=rq2_repo_data_summary,
        preserve_order=True,
    )


def format_panel_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.12g}"
    return str(value)


def parse_panel_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_panel_int(value: Any) -> int | None:
    parsed = parse_panel_float(value)
    if parsed is None:
        return None
    return int(parsed)


def build_repo_week_panel_rows(
    repo_data: list[dict[str, Any]],
    *,
    max_commit_additions: int | None = DEFAULT_MAX_COMMIT_ADDITIONS,
    origin_start: datetime | None = None,
    origin_end: datetime | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo in repo_data:
        repo_key = str(repo.get("repo_key") or "")
        repo_name = str(repo.get("repo") or repo_key or "unknown")

        total_commits_by_week: dict[datetime, int] = defaultdict(int)
        total_changed_lines_by_week: dict[datetime, int] = defaultdict(int)
        corrective_commits_by_week: dict[datetime, int] = defaultdict(int)
        corrective_changed_lines_by_week: dict[datetime, int] = defaultdict(int)
        ai_total_commits_by_week: dict[datetime, int] = defaultdict(int)
        classifications: dict[str, dict[str, Any]] = {}
        change_counts_by_sha = repo.get("commit_change_counts") or {}
        excluded_commit_shas: set[str] = set()
        maintenance_commits_by_week: dict[str, Counter[datetime]] = {
            maintenance_class: Counter() for maintenance_class in MAINTENANCE_CLASSES
        }
        maintenance_changed_lines_by_week: dict[str, Counter[datetime]] = {
            maintenance_class: Counter() for maintenance_class in MAINTENANCE_CLASSES
        }
        role_by_sha = commit_roles_by_sha(repo.get("commit_roles") or [])

        for row in repo.get("commit_classifications") or []:
            sha = row.get("sha")
            if not isinstance(sha, str):
                continue
            if not date_in_window(row.get("date"), start=origin_start, end=origin_end):
                continue
            effective_row = commit_row_with_change_counts(row, change_counts_by_sha)
            if is_commit_addition_outlier(effective_row, max_commit_additions):
                excluded_commit_shas.add(sha)
                continue
            classifications[sha] = effective_row
            week = week_bucket(row.get("date"))
            if week is None:
                continue
            changed_lines = commit_changed_line_count(effective_row)
            total_commits_by_week[week] += 1
            total_changed_lines_by_week[week] += changed_lines
            if role_by_sha.get(sha) == "AI":
                ai_total_commits_by_week[week] += 1
            if is_corrective_commit(effective_row):
                corrective_commits_by_week[week] += 1
                corrective_changed_lines_by_week[week] += changed_lines
            maintenance_class = maintenance_class_for_commit(effective_row)
            if maintenance_class is not None:
                maintenance_commits_by_week[maintenance_class][week] += 1
                maintenance_changed_lines_by_week[maintenance_class][week] += (
                    changed_lines
                )

        adoption_class = classify_repo_ai_adoption(
            weekly_ai_human_counts(
                repo,
                excluded_shas=excluded_commit_shas,
                start=origin_start,
                end=origin_end,
            )
        )

        human_total_commits_by_week: dict[datetime, int] = defaultdict(int)
        human_total_changed_lines_by_week: dict[datetime, int] = defaultdict(int)
        human_corrective_commits_by_week: dict[datetime, int] = defaultdict(int)
        human_corrective_changed_lines_by_week: dict[datetime, int] = defaultdict(int)
        for commit in repo.get("commit_roles") or repo.get("commits") or []:
            if commit_role(commit) != "Human":
                continue
            sha = commit.get("sha")
            if isinstance(sha, str) and sha in excluded_commit_shas:
                continue
            if not date_in_window(commit.get("date"), start=origin_start, end=origin_end):
                continue
            week = week_bucket(commit.get("date"))
            if week is None:
                continue
            human_total_commits_by_week[week] += 1
            classification = classifications.get(sha) if isinstance(sha, str) else None
            classification_changed_lines = (
                commit_changed_line_count(classification)
                if classification is not None
                else 0
            )
            human_total_changed_lines_by_week[week] += classification_changed_lines
            if classification is not None and is_corrective_commit(classification):
                human_corrective_commits_by_week[week] += 1
                human_corrective_changed_lines_by_week[week] += (
                    classification_changed_lines
                )

        lifecycle_counts = repo.get("line_lifecycle_week_counts") or {}
        origin_lines_by_week = lifecycle_counts.get("origin_lines_by_week") or Counter()
        terminal_lines_by_week = lifecycle_counts.get("terminal_lines_by_week") or Counter()
        terminated_existing_lines_by_week = (
            lifecycle_counts.get("terminated_existing_lines_by_week") or Counter()
        )
        human_origin_lines_by_week = (
            lifecycle_counts.get("human_origin_lines_by_week") or Counter()
        )
        human_origin_terminal_lines_by_week = (
            lifecycle_counts.get("human_origin_terminal_lines_by_week") or Counter()
        )
        human_origin_corrective_terminal_lines_by_week = (
            lifecycle_counts.get("human_origin_corrective_terminal_lines_by_week")
            or Counter()
        )

        share_by_week = aggregate_snapshot_ai_share_by_week(
            repo.get("codebase_agent_share_snapshots") or []
        )
        sonarqube_by_week = aggregate_sonarqube_scans_by_week(
            repo.get("sonarqube_snapshot_scans") or []
        )
        event_weeks = sorted(
            set(total_commits_by_week)
            | set(human_total_commits_by_week)
            | set(terminated_existing_lines_by_week)
            | set(share_by_week)
            | set(sonarqube_by_week)
        )
        if not event_weeks:
            continue

        start_week = week_bucket(origin_start) if origin_start is not None else event_weeks[0]
        end_week = week_bucket(origin_end) if origin_end is not None else event_weeks[-1]
        if start_week is None or end_week is None or start_week > end_week:
            continue

        all_weeks: list[datetime] = []
        week = start_week
        last_week = end_week
        while week <= last_week:
            all_weeks.append(week)
            week += timedelta(weeks=1)

        lagged_ai_shares = dict(
            zip(all_weeks, lagged_ai_share_for_weeks(all_weeks, share_by_week))
        )
        same_week_ai_shares = {
            week: share_by_week[week][1] for week in all_weeks if week in share_by_week
        }
        for week in all_weeks:
            total_commits = total_commits_by_week[week]
            total_changed_lines = total_changed_lines_by_week[week]
            corrective_commits = corrective_commits_by_week[week]
            corrective_changed_lines = corrective_changed_lines_by_week[week]
            ai_total_commits = ai_total_commits_by_week[week]
            human_total_commits = human_total_commits_by_week[week]
            human_total_changed_lines = human_total_changed_lines_by_week[week]
            human_corrective_commits = human_corrective_commits_by_week[week]
            human_corrective_changed_lines = human_corrective_changed_lines_by_week[
                week
            ]
            maintenance_fields: dict[str, float | int | None] = {}
            for maintenance_class in MAINTENANCE_CLASSES:
                class_commits = maintenance_commits_by_week[maintenance_class][week]
                class_changed_lines = maintenance_changed_lines_by_week[
                    maintenance_class
                ][week]
                maintenance_fields[f"maintenance_{maintenance_class}_commits"] = (
                    class_commits
                )
                maintenance_fields[f"maintenance_{maintenance_class}_rate"] = (
                    class_commits / total_commits if total_commits > 0 else None
                )
                maintenance_fields[
                    f"maintenance_{maintenance_class}_changed_lines"
                ] = class_changed_lines
                maintenance_fields[f"maintenance_{maintenance_class}_line_rate"] = (
                    class_changed_lines / total_changed_lines
                    if total_changed_lines > 0
                    else None
                )

            born_before_week = sum(
                count
                for origin_week, count in origin_lines_by_week.items()
                if origin_week < week
            )
            terminated_before_week = sum(
                count
                for terminal_week, count in terminal_lines_by_week.items()
                if terminal_week < week
            )
            living_tracked_lines = born_before_week - terminated_before_week
            terminated_existing_lines = terminated_existing_lines_by_week[week]
            tracked_line_churn_rate = (
                terminated_existing_lines / living_tracked_lines
                if living_tracked_lines > 0
                else None
            )
            human_origin_born_before_week = sum(
                count
                for origin_week, count in human_origin_lines_by_week.items()
                if origin_week < week
            )
            human_origin_terminated_before_week = sum(
                count
                for terminal_week, count in human_origin_terminal_lines_by_week.items()
                if terminal_week < week
            )
            human_origin_living_lines = (
                human_origin_born_before_week - human_origin_terminated_before_week
            )
            human_origin_corrective_terminated_lines = (
                human_origin_corrective_terminal_lines_by_week[week]
            )
            human_origin_corrective_line_rate = (
                human_origin_corrective_terminated_lines / human_origin_living_lines
                if human_origin_living_lines > 0
                else None
            )
            sonar_fields: dict[str, Any] = {column: None for column in SONAR_PANEL_COLUMNS}
            sonar_fields.update(sonarqube_by_week.get(week) or {})

            rows.append(
                {
                    "repo_key": repo_key,
                    "repo": repo_name,
                    "week": week,
                    "adoption_class": adoption_class,
                    "ai_share": same_week_ai_shares.get(week),
                    "lagged_ai_share": lagged_ai_shares.get(week),
                    "total_commits": total_commits,
                    "total_changed_lines": total_changed_lines,
                    "corrective_commits": corrective_commits,
                    "corrective_rate": (
                        corrective_commits / total_commits
                        if total_commits > 0
                        else None
                    ),
                    "corrective_changed_lines": corrective_changed_lines,
                    "corrective_line_rate": (
                        corrective_changed_lines / total_changed_lines
                        if total_changed_lines > 0
                        else None
                    ),
                    "ai_total_commits": ai_total_commits,
                    "human_total_commits": human_total_commits,
                    "human_total_changed_lines": human_total_changed_lines,
                    "human_corrective_commits": human_corrective_commits,
                    "human_corrective_rate": (
                        human_corrective_commits / human_total_commits
                        if human_total_commits > 0
                        else None
                    ),
                    "human_corrective_changed_lines": human_corrective_changed_lines,
                    "human_corrective_line_rate": (
                        human_corrective_changed_lines / human_total_changed_lines
                        if human_total_changed_lines > 0
                        else None
                    ),
                    "human_origin_living_tracked_lines_at_week_start": (
                        human_origin_living_lines
                        if human_origin_living_lines > 0
                        else None
                    ),
                    "human_origin_corrective_terminated_lines": (
                        human_origin_corrective_terminated_lines
                    ),
                    "human_origin_corrective_line_rate": (
                        human_origin_corrective_line_rate
                    ),
                    **maintenance_fields,
                    **sonar_fields,
                    "living_tracked_lines_at_week_start": (
                        living_tracked_lines if living_tracked_lines > 0 else None
                    ),
                    "terminated_existing_lines": terminated_existing_lines,
                    "tracked_line_churn_rate": tracked_line_churn_rate,
                }
            )
    return rows


def write_repo_week_panel_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPO_WEEK_PANEL_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: format_panel_value(row.get(column))
                    for column in REPO_WEEK_PANEL_COLUMNS
                }
            )


def read_repo_week_panel_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    int_columns = {
        "total_commits",
        "total_changed_lines",
        "corrective_commits",
        "corrective_changed_lines",
        "ai_total_commits",
        "human_total_commits",
        "human_total_changed_lines",
        "human_corrective_commits",
        "human_corrective_changed_lines",
        "human_origin_living_tracked_lines_at_week_start",
        "human_origin_corrective_terminated_lines",
        "living_tracked_lines_at_week_start",
        "terminated_existing_lines",
        "sonar_scan_count",
        "sonar_issue_count",
        "sonar_bug_count",
        "sonar_vulnerability_count",
        "sonar_code_smell_count",
        "sonar_security_hotspot_count",
        "sonar_defect_security_issue_count",
        "sonar_severe_issue_count",
        *(
            f"maintenance_{maintenance_class}_commits"
            for maintenance_class in MAINTENANCE_CLASSES
        ),
        *(
            f"maintenance_{maintenance_class}_changed_lines"
            for maintenance_class in MAINTENANCE_CLASSES
        ),
    }
    float_columns = {
        "ai_share",
        "lagged_ai_share",
        "corrective_rate",
        "corrective_line_rate",
        "human_corrective_rate",
        "human_corrective_line_rate",
        "human_origin_corrective_line_rate",
        "tracked_line_churn_rate",
        "sonar_ncloc",
        "sonar_complexity",
        "sonar_cognitive_complexity",
        "sonar_duplicated_lines_density",
        "sonar_comment_lines_density",
        "sonar_sqale_index",
        "sonar_maintainability_effort",
        "sonar_issue_density_per_kloc",
        "sonar_bug_density_per_kloc",
        "sonar_vulnerability_density_per_kloc",
        "sonar_code_smell_density_per_kloc",
        "sonar_security_hotspot_density_per_kloc",
        "sonar_defect_security_issue_density_per_kloc",
        "sonar_severe_issue_density_per_kloc",
        "sonar_complexity_per_kloc",
        "sonar_cognitive_complexity_per_kloc",
        "sonar_sqale_index_per_kloc",
        "sonar_maintainability_effort_per_kloc",
        *(
            f"maintenance_{maintenance_class}_rate"
            for maintenance_class in MAINTENANCE_CLASSES
        ),
        *(
            f"maintenance_{maintenance_class}_line_rate"
            for maintenance_class in MAINTENANCE_CLASSES
        ),
    }
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            row: dict[str, Any] = dict(raw_row)
            row["week"] = parse_datetime(raw_row.get("week"))
            for column in int_columns:
                row[column] = parse_panel_int(raw_row.get(column))
            for column in float_columns:
                row[column] = parse_panel_float(raw_row.get(column))
            if row["week"] is not None:
                rows.append(row)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a reusable RQ2 repo-week panel and PDF model plot."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Folder containing per-repo webscrape output directories.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit to owner/repo or owner__repo. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--repo-file",
        type=Path,
        help="Optional text file containing repos to include, one per line.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Number of worker processes for parallel per-repo loading.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where RQ2 outputs are written.",
    )
    parser.add_argument(
        "--panel-csv",
        type=Path,
        help=(
            "Where to write the reusable repo-week panel CSV. Defaults to "
            f"--output-dir/{DEFAULT_PANEL_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--use-panel-csv",
        type=Path,
        help="Read this previously exported repo-week panel CSV and skip raw JSONL loading.",
    )
    parser.add_argument(
        "--skip-panel-export",
        action="store_true",
        help="Build the PDF plot from raw data without writing the repo-week panel CSV.",
    )
    parser.add_argument(
        "--run-python-panel-fe",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Python OLS panel models are disabled; "
            "run R_code/Data_Analysis_rq2.Rmd for the repo-month GLMER panel models."
        ),
    )
    parser.add_argument(
        "--glmer-predictions-csv",
        "--betabinomial-predictions-csv",
        dest="betabinomial_predictions_csv",
        type=Path,
        help=(
            "Prediction CSV exported by the RQ2 notebook/modeling step. "
            "Defaults to --output-dir/"
            f"{DEFAULT_BETABINOMIAL_PREDICTION_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--origin-start",
        default=DEFAULT_ORIGIN_START,
        help=(
            "Inclusive lower bound for commit dates used in repo-week panels. "
            "Accepts YYYY-MM, YYYY-MM-DD, ISO datetime, or none/all/open. "
            f"Default: {DEFAULT_ORIGIN_START}."
        ),
    )
    parser.add_argument(
        "--origin-end",
        default=DEFAULT_ORIGIN_END,
        help=(
            "Inclusive upper bound for commit dates used in repo-week panels. "
            "Accepts YYYY-MM, YYYY-MM-DD, ISO datetime, or none/all/open. "
            f"Default: {DEFAULT_ORIGIN_END}."
        ),
    )
    parser.add_argument(
        "--max-commit-additions",
        type=int,
        default=DEFAULT_MAX_COMMIT_ADDITIONS,
        help=(
            "Exclude commits with at least this many added lines when building "
            "repo-week commit-rate panels. Use 0 to disable. "
            f"Default: {DEFAULT_MAX_COMMIT_ADDITIONS:,}."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-repo loading progress.",
    )
    return parser.parse_args()

def graph_survival_rate(repo_data):

    

    for repo in repo_data:

        survived = 0

        for line in repo['line_lifecycle']:

            # check if line is source code 
            if line['status'] != 'survived':
                continue
            survived += 1

        print(f"{repo['repo']}: {survived} survived lines")
        print(f"Survival rate: {survived / len(repo['line_lifecycle']):.2%}")



def graph_mean_survival_rate_based_on_human_vs_ai_commit_classification(repo_data):


    for repo in repo_data:

        lifecycle_counts_by_commit: dict[str, list[int]] = {}
        for line in repo['line_lifecycle']:
            commit_sha = line['origin_commit_sha']
            counts = lifecycle_counts_by_commit.setdefault(commit_sha, [0, 0])
            counts[0] += 1
            if line['status'] == 'survived':
                counts[1] += 1

        human_survival_rate_sum = 0.0
        human_survival_rate_count = 0
        ai_survival_rate_sum = 0.0
        ai_survival_rate_count = 0

        for commit in repo['commits']:

            lifecycle_counts = lifecycle_counts_by_commit.get(commit['sha'])

            if lifecycle_counts is None:
                continue

            total_lines, survived_lines = lifecycle_counts
            survival_rate = survived_lines / total_lines
            
            if commit['category'] == 'not_ai_coauthored':
                human_survival_rate_sum += survival_rate
                human_survival_rate_count += 1
            elif commit['category'] == 'ai_coauthored':
                ai_survival_rate_sum += survival_rate
                ai_survival_rate_count += 1

        mean_human_survival_rate = human_survival_rate_sum / human_survival_rate_count if human_survival_rate_count else 0
        mean_ai_survival_rate = ai_survival_rate_sum / ai_survival_rate_count if ai_survival_rate_count else 0

        print(f"{repo['repo']}: Mean Human Survival Rate: {mean_human_survival_rate:.2%}, Mean AI Survival Rate: {mean_ai_survival_rate:.2%}")


def build_repo_week_timeline_series(
    repo: dict[str, Any],
    *,
    source_only: bool = False,
) -> dict[str, Any] | None:
    source_shas = source_touching_commit_shas(repo) if source_only else set()
    total_commits_by_week: dict[datetime, int] = defaultdict(int)
    corrective_commits_by_week: dict[datetime, int] = defaultdict(int)

    for row in repo.get("commit_classifications") or []:
        sha = row.get("sha")
        if source_only and sha not in source_shas:
            continue
        week = week_bucket(row.get("date"))
        if week is None:
            continue
        total_commits_by_week[week] += 1
        if is_corrective_commit(row):
            corrective_commits_by_week[week] += 1

    weeks = sorted(total_commits_by_week)
    if not weeks:
        return None

    corrective_rates = [
        corrective_commits_by_week[week] / total_commits_by_week[week]
        for week in weeks
    ]
    if source_only:
        share_by_week = aggregate_source_snapshot_ai_share_by_week(
            repo.get("codebase_agent_share_files") or []
        )
    else:
        share_by_week = aggregate_snapshot_ai_share_by_week(
            repo.get("codebase_agent_share_snapshots") or []
        )

    return {
        "repo": repo.get("repo") or repo.get("repo_key") or "unknown",
        "weeks": weeks,
        "lagged_ai_shares": lagged_ai_share_for_weeks(weeks, share_by_week),
        "corrective_rates": corrective_rates,
        "total_commits": [total_commits_by_week[week] for week in weeks],
        "corrective_commits": [corrective_commits_by_week[week] for week in weeks],
    }


def draw_empty_plot(output_path: Path, title: str, message: str) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.axis("off")
    ax.set_title(title)
    ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def plot_repo_week_timeline_grid(
    repo_data: list[dict[str, Any]],
    output_path: Path,
    *,
    source_only: bool = False,
) -> None:
    series_by_repo = [
        series
        for repo in repo_data
        if (series := build_repo_week_timeline_series(repo, source_only=source_only))
    ]
    title = (
        "Repo-Week Timeline: Source-Touching Corrective Rate"
        if source_only
        else "Repo-Week Timeline: Corrective Rate"
    )
    if not series_by_repo:
        draw_empty_plot(
            output_path,
            title,
            "No repo-week corrective-rate data available.",
        )
        return

    columns = 3
    rows = math.ceil(len(series_by_repo) / columns)
    fig, axes = plt.subplots(
        rows,
        columns,
        figsize=(columns * 5.2, rows * 3.4),
        squeeze=False,
    )
    ai_color = AI_COLOR
    corrective_color = HUMAN_COLOR
    ai_line = None
    corrective_line = None

    for index, series in enumerate(series_by_repo):
        row_index = index // columns
        column_index = index % columns
        ax = axes[row_index][column_index]
        ax2 = ax.twinx()

        weeks = series["weeks"]
        ai_points = [
            (week, share * 100)
            for week, share in zip(weeks, series["lagged_ai_shares"])
            if share is not None
        ]
        if ai_points:
            ai_line = ax.plot(
                [point[0] for point in ai_points],
                [point[1] for point in ai_points],
                color=ai_color,
                marker="o",
                markersize=3,
                linewidth=1.5,
                label="Lagged AI living line share",
            )[0]
        corrective_line = ax2.plot(
            weeks,
            [rate * 100 for rate in series["corrective_rates"]],
            color=corrective_color,
            marker="s",
            markersize=3,
            linewidth=1.5,
            label="Corrective maintenance rate",
        )[0]

        ax.set_title(str(series["repo"]), fontsize=10)
        ax.set_ylim(0, 100)
        ax2.set_ylim(0, 100)
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=100))
        ax2.yaxis.set_major_formatter(PercentFormatter(xmax=100))
        ax.grid(True, axis="y", linestyle=":", linewidth=0.7, alpha=0.6)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
        if column_index == 0:
            ax.set_ylabel("Lagged AI share", color=ai_color)
        else:
            ax.tick_params(axis="y", labelleft=False)
        if column_index == columns - 1:
            ax2.set_ylabel("Corrective rate", color=corrective_color)
        else:
            ax2.tick_params(axis="y", labelright=False)
        ax.tick_params(axis="y", colors=ai_color)
        ax2.tick_params(axis="y", colors=corrective_color)

    for index in range(len(series_by_repo), rows * columns):
        axes[index // columns][index % columns].axis("off")

    legend_handles = [handle for handle in (ai_line, corrective_line) if handle is not None]
    if legend_handles:
        fig.legend(
            legend_handles,
            [handle.get_label() for handle in legend_handles],
            loc="upper center",
            ncols=len(legend_handles),
            frameon=False,
            bbox_to_anchor=(0.5, 0.995),
        )
    fig.suptitle(title, y=1.02, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def build_repo_week_scatter_points(
    repo_data: list[dict[str, Any]],
    *,
    source_only: bool = False,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for repo in repo_data:
        series = build_repo_week_timeline_series(repo, source_only=source_only)
        if series is None:
            continue
        adoption_class = classify_repo_ai_adoption(weekly_ai_human_counts(repo))
        for week, ai_share, corrective_rate, total_commits, corrective_commits in zip(
            series["weeks"],
            series["lagged_ai_shares"],
            series["corrective_rates"],
            series["total_commits"],
            series["corrective_commits"],
        ):
            if ai_share is None or total_commits <= 0:
                continue
            points.append(
                {
                    "repo": series["repo"],
                    "week": week,
                    "adoption_class": adoption_class,
                    "ai_share": ai_share,
                    "corrective_rate": corrective_rate,
                    "total_commits": total_commits,
                    "corrective_commits": corrective_commits,
                }
            )
    return points


def commit_classifications_by_sha(repo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_sha: dict[str, dict[str, Any]] = {}
    for row in repo.get("commit_classifications") or []:
        sha = row.get("sha")
        if isinstance(sha, str) and sha:
            by_sha[sha] = row
    return by_sha


def build_repo_week_human_corrective_burden_points(
    repo_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for repo in repo_data:
        classifications = commit_classifications_by_sha(repo)
        total_human_commits_by_week: dict[datetime, int] = defaultdict(int)
        corrective_human_commits_by_week: dict[datetime, int] = defaultdict(int)

        for commit in repo.get("commit_roles") or repo.get("commits") or []:
            if commit_role(commit) != "Human":
                continue
            week = week_bucket(commit.get("date"))
            if week is None:
                continue
            total_human_commits_by_week[week] += 1
            sha = commit.get("sha")
            classification = classifications.get(sha) if isinstance(sha, str) else None
            if classification is not None and is_corrective_commit(classification):
                corrective_human_commits_by_week[week] += 1

        weeks = sorted(total_human_commits_by_week)
        if not weeks:
            continue

        share_by_week = aggregate_snapshot_ai_share_by_week(
            repo.get("codebase_agent_share_snapshots") or []
        )
        lagged_ai_shares = lagged_ai_share_for_weeks(weeks, share_by_week)
        adoption_class = classify_repo_ai_adoption(weekly_ai_human_counts(repo))
        repo_name = repo.get("repo") or repo.get("repo_key") or "unknown"

        for week, ai_share in zip(weeks, lagged_ai_shares):
            total_human_commits = total_human_commits_by_week[week]
            if ai_share is None or total_human_commits <= 0:
                continue
            corrective_human_commits = corrective_human_commits_by_week[week]
            points.append(
                {
                    "repo": repo_name,
                    "week": week,
                    "adoption_class": adoption_class,
                    "ai_share": ai_share,
                    "corrective_rate": corrective_human_commits / total_human_commits,
                    "total_commits": total_human_commits,
                    "corrective_commits": corrective_human_commits,
                }
            )

    return points


def build_repo_week_line_churn_points(
    repo_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for repo in repo_data:
        total_commits_by_week: dict[datetime, int] = defaultdict(int)
        for row in repo.get("commit_classifications") or []:
            week = week_bucket(row.get("date"))
            if week is not None:
                total_commits_by_week[week] += 1
        weeks = sorted(total_commits_by_week)
        if not weeks:
            continue

        origin_dates: list[datetime] = []
        terminal_dates: list[datetime] = []
        terminated_existing_lines_by_week: dict[datetime, int] = defaultdict(int)
        skipped_lines = 0
        for line in repo.get("line_lifecycle") or []:
            origin_date = parse_datetime(line.get("origin_commit_date"))
            if origin_date is None:
                skipped_lines += 1
                continue
            origin_dates.append(origin_date)
            terminal_date = parse_datetime(line.get("terminal_commit_date"))
            if terminal_date is None:
                continue
            terminal_dates.append(terminal_date)
            terminal_week = week_bucket(terminal_date)
            if terminal_week is not None and origin_date < terminal_week:
                terminated_existing_lines_by_week[terminal_week] += 1

        if not origin_dates:
            continue
        origin_dates.sort()
        terminal_dates.sort()

        share_by_week = aggregate_snapshot_ai_share_by_week(
            repo.get("codebase_agent_share_snapshots") or []
        )
        lagged_ai_shares = lagged_ai_share_for_weeks(weeks, share_by_week)
        adoption_class = classify_repo_ai_adoption(weekly_ai_human_counts(repo))
        repo_name = repo.get("repo") or repo.get("repo_key") or "unknown"

        for week, ai_share in zip(weeks, lagged_ai_shares):
            if ai_share is None:
                continue
            born_before_week = bisect.bisect_left(origin_dates, week)
            terminated_before_week = bisect.bisect_left(terminal_dates, week)
            living_lines_at_week_start = born_before_week - terminated_before_week
            if living_lines_at_week_start <= 0:
                continue
            terminated_existing_lines = terminated_existing_lines_by_week[week]
            points.append(
                {
                    "repo": repo_name,
                    "week": week,
                    "adoption_class": adoption_class,
                    "ai_share": ai_share,
                    "corrective_rate": (
                        terminated_existing_lines / living_lines_at_week_start
                    ),
                    "total_commits": total_commits_by_week[week],
                    "corrective_commits": terminated_existing_lines,
                    "living_tracked_lines_at_week_start": (
                        living_lines_at_week_start
                    ),
                    "skipped_lifecycle_lines": skipped_lines,
                }
            )

    return points


def panel_rows_by_repo(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        repo = str(row.get("repo") or row.get("repo_key") or "unknown")
        by_repo[repo].append(row)
    for repo_rows in by_repo.values():
        repo_rows.sort(key=lambda row: row["week"])
    return by_repo


def plot_repo_week_timeline_grid_from_panel(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    by_repo = panel_rows_by_repo(
        [
            row
            for row in rows
            if row.get("week") is not None
            and (row.get("total_commits") or 0) > 0
            and row.get("corrective_rate") is not None
        ]
    )
    title = "Repo-Week Timeline: Corrective Rate"
    if not by_repo:
        draw_empty_plot(
            output_path,
            title,
            "No repo-week corrective-rate data available.",
        )
        return

    series_by_repo = list(by_repo.items())
    columns = 3
    plot_rows = math.ceil(len(series_by_repo) / columns)
    fig, axes = plt.subplots(
        plot_rows,
        columns,
        figsize=(columns * 5.2, plot_rows * 3.4),
        squeeze=False,
    )
    ai_line = None
    corrective_line = None

    for index, (repo_name, repo_rows) in enumerate(series_by_repo):
        row_index = index // columns
        column_index = index % columns
        ax = axes[row_index][column_index]
        ax2 = ax.twinx()

        weeks = [row["week"] for row in repo_rows]
        ai_points = [
            (row["week"], float(row["lagged_ai_share"]) * 100)
            for row in repo_rows
            if row.get("lagged_ai_share") is not None
        ]
        if ai_points:
            ai_line = ax.plot(
                [point[0] for point in ai_points],
                [point[1] for point in ai_points],
                color=AI_COLOR,
                marker="o",
                markersize=3,
                linewidth=1.5,
                label="Lagged AI living line share",
            )[0]
        corrective_line = ax2.plot(
            weeks,
            [float(row["corrective_rate"]) * 100 for row in repo_rows],
            color=HUMAN_COLOR,
            marker="s",
            markersize=3,
            linewidth=1.5,
            label="Corrective maintenance rate",
        )[0]

        ax.set_title(repo_name, fontsize=10)
        ax.set_ylim(0, 100)
        ax2.set_ylim(0, 100)
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=100))
        ax2.yaxis.set_major_formatter(PercentFormatter(xmax=100))
        ax.grid(True, axis="y", linestyle=":", linewidth=0.7, alpha=0.6)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
        ax.xaxis.set_major_formatter(
            mdates.ConciseDateFormatter(ax.xaxis.get_major_locator())
        )
        if column_index == 0:
            ax.set_ylabel("Lagged AI share", color=AI_COLOR)
        else:
            ax.tick_params(axis="y", labelleft=False)
        if column_index == columns - 1:
            ax2.set_ylabel("Corrective rate", color=HUMAN_COLOR)
        else:
            ax2.tick_params(axis="y", labelright=False)
        ax.tick_params(axis="y", colors=AI_COLOR)
        ax2.tick_params(axis="y", colors=HUMAN_COLOR)

    for index in range(len(series_by_repo), plot_rows * columns):
        axes[index // columns][index % columns].axis("off")

    legend_handles = [handle for handle in (ai_line, corrective_line) if handle is not None]
    if legend_handles:
        fig.legend(
            legend_handles,
            [handle.get_label() for handle in legend_handles],
            loc="upper center",
            ncols=len(legend_handles),
            frameon=False,
            bbox_to_anchor=(0.5, 0.995),
        )
    fig.suptitle(title, y=1.02, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def panel_points_for_metric(
    rows: list[dict[str, Any]],
    *,
    y_field: str,
    total_field: str,
    count_field: str,
    ai_share_field: str = "lagged_ai_share",
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in rows:
        ai_share = row.get(ai_share_field)
        y_value = row.get(y_field)
        total_value = row.get(total_field)
        if ai_share is None or y_value is None or total_value is None:
            continue
        if total_value <= 0:
            continue
        points.append(
            {
                "repo": row.get("repo") or row.get("repo_key") or "unknown",
                "week": row["week"],
                "adoption_class": row.get("adoption_class") or "balanced",
                "ai_share": ai_share,
                "corrective_rate": y_value,
                "total_commits": total_value,
                "corrective_commits": row.get(count_field) or 0,
            }
        )
    return points


def scatter_point_size(total_commits: int) -> float:
    return 16 + min(170, math.sqrt(max(total_commits, 1)) * 7)


def lowess_smooth(
    x_values: list[float],
    y_values: list[float],
    *,
    frac: float = 0.35,
    grid_size: int = 140,
) -> tuple[np.ndarray, np.ndarray] | None:
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    if len(x) < 4 or float(np.ptp(x)) == 0.0:
        return None

    order = np.argsort(x)
    x = x[order]
    y = y[order]
    n = len(x)
    window = max(4, min(n, int(math.ceil(frac * n))))
    grid = np.linspace(float(x[0]), float(x[-1]), min(grid_size, max(25, n)))
    smoothed: list[float] = []

    for x0 in grid:
        distances = np.abs(x - x0)
        bandwidth = float(np.partition(distances, window - 1)[window - 1])
        if bandwidth <= 0:
            bandwidth = float(np.max(distances))
        if bandwidth <= 0:
            smoothed.append(float(np.mean(y)))
            continue

        scaled_distances = distances / bandwidth
        weights = np.where(
            scaled_distances <= 1,
            (1 - scaled_distances**3) ** 3,
            0.0,
        )
        weight_sum = float(np.sum(weights))
        if weight_sum <= 0:
            smoothed.append(float(np.mean(y)))
            continue

        centered_x = x - x0
        sx = float(np.sum(weights * centered_x))
        sy = float(np.sum(weights * y))
        sxx = float(np.sum(weights * centered_x * centered_x))
        sxy = float(np.sum(weights * centered_x * y))
        denom = weight_sum * sxx - sx * sx
        if abs(denom) < 1e-12:
            smoothed.append(sy / weight_sum)
        else:
            intercept = (sy * sxx - sx * sxy) / denom
            smoothed.append(intercept)

    return grid, np.asarray(smoothed)


def format_p_value(p_value: float) -> str:
    if not math.isfinite(p_value):
        return "NA"
    if p_value < 0.001:
        return f"{p_value:.2e}"
    return f"{p_value:.3f}"


def significance_stars(p_value: float) -> str:
    if not math.isfinite(p_value):
        return ""
    if p_value < 0.001:
        return "***"
    if p_value < 0.01:
        return "**"
    if p_value < 0.05:
        return "*"
    if p_value < 0.1:
        return "."
    return ""


def regression_summary(
    x_values: list[float],
    y_values: list[float],
    clusters: list[str] | None = None,
) -> dict[str, Any] | None:
    x = np.asarray(x_values, dtype=float)
    y = np.asarray(y_values, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    cluster_values = None
    if clusters is not None and len(clusters) == len(x_values):
        cluster_values = np.asarray(clusters, dtype=object)[mask]
    x = x[mask]
    y = y[mask]
    if len(x) < 2 or float(np.ptp(x)) == 0.0:
        return None

    if sm is not None and cluster_values is not None and len(set(cluster_values)) >= 2:
        design = sm.add_constant(x, has_constant="add")
        model = sm.OLS(y, design).fit()
        robust = model.get_robustcov_results(
            cov_type="cluster",
            groups=cluster_values,
            use_t=True,
        )
        grid = np.asarray([float(np.min(x)), float(np.max(x))])
        intercept = float(robust.params[0])
        slope = float(robust.params[1])
        conf_int = robust.conf_int(alpha=0.05)
        return {
            "x": grid,
            "y": intercept + slope * grid,
            "slope": slope,
            "intercept": intercept,
            "p_value": float(robust.pvalues[1]),
            "std_error": float(robust.bse[1]),
            "ci_low": float(conf_int[1][0]),
            "ci_high": float(conf_int[1][1]),
            "r_squared": float(model.rsquared),
            "n": int(len(x)),
            "clusters": int(len(set(cluster_values))),
            "covariance": "cluster_robust_by_repo",
        }

    result = stats.linregress(x, y)
    grid = np.asarray([float(np.min(x)), float(np.max(x))])
    slope = float(result.slope)
    intercept = float(result.intercept)
    return {
        "x": grid,
        "y": intercept + slope * grid,
        "slope": slope,
        "intercept": intercept,
        "p_value": float(result.pvalue),
        "std_error": float(result.stderr),
        "ci_low": float("nan"),
        "ci_high": float("nan"),
        "r_squared": float(result.rvalue * result.rvalue),
        "n": int(len(x)),
        "clusters": None,
        "covariance": "ordinary_ols",
    }


def plot_repo_week_scatter_points_with_smoother(
    points: list[dict[str, Any]],
    output_path: Path,
    *,
    title: str,
    y_axis_label: str,
    x_axis_label: str = "Lagged AI living line share",
    y_multiplier: float = 100.0,
    y_axis_percent: bool = True,
    full_percent_y_axis: bool = True,
) -> None:
    if not points:
        draw_empty_plot(
            output_path,
            title,
            "No repo-week points available.",
        )
        return

    fig, ax = plt.subplots(figsize=(9.5, 6.2))
    all_x: list[float] = []
    all_y: list[float] = []

    for adoption_class in REPO_ADOPTION_ORDER:
        class_points = [p for p in points if p["adoption_class"] == adoption_class]
        if not class_points:
            continue
        x_values = [float(p["ai_share"]) * 100 for p in class_points]
        y_values = [float(p["corrective_rate"]) * y_multiplier for p in class_points]
        sizes = [scatter_point_size(int(p["total_commits"])) for p in class_points]
        all_x.extend(x_values)
        all_y.extend(y_values)
        color = REPO_ADOPTION_POINT_COLORS[adoption_class]
        label = REPO_ADOPTION_LABELS[adoption_class]
        ax.scatter(
            x_values,
            y_values,
            s=sizes,
            color=color,
            alpha=0.32,
            edgecolors="none",
            label=f"{label} repo-weeks",
        )
        smooth = lowess_smooth(x_values, y_values)
        if smooth is not None:
            ax.plot(
                smooth[0],
                smooth[1],
                color=color,
                linewidth=2.2,
                alpha=0.95,
                label=f"{label} LOESS",
            )

    overall_smooth = lowess_smooth(all_x, all_y)
    if overall_smooth is not None:
        ax.plot(
            overall_smooth[0],
            overall_smooth[1],
            color="#222222",
            linewidth=2.6,
            linestyle="--",
            label="Overall LOESS",
        )

    ax.set_title(title)
    ax.set_xlabel(x_axis_label)
    ax.set_ylabel(y_axis_label)
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=100))
    if y_axis_percent:
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=100))
    ax.set_xlim(left=0, right=max(100, max(all_x) * 1.03 if all_x else 100))
    y_top = max(all_y) * 1.08 if all_y else 1
    if full_percent_y_axis:
        y_top = max(100, y_top)
    else:
        y_top = max(1, y_top)
    ax.set_ylim(bottom=0, top=y_top)
    ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", fontsize=8, ncols=2, frameon=False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_sonarqube_same_week_scatter(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    points_by_metric: dict[str, list[dict[str, Any]]] = {}
    for density_field, count_field, label in SONAR_FINDING_SPECS:
        points: list[dict[str, Any]] = []
        for row in rows:
            ai_share = row.get("ai_share")
            density = row.get(density_field)
            count = row.get(count_field)
            ncloc = row.get("sonar_ncloc")
            if ai_share is None or density is None or ncloc is None or ncloc <= 0:
                continue
            points.append(
                {
                    "repo": row.get("repo") or row.get("repo_key") or "unknown",
                    "week": row.get("week"),
                    "adoption_class": row.get("adoption_class") or "balanced",
                    "ai_share": float(ai_share),
                    "density": float(density),
                    "count": float(count or 0),
                    "ncloc": float(ncloc),
                    "label": label,
                }
            )
        points_by_metric[density_field] = points

    if not any(points_by_metric.values()):
        draw_empty_plot(
            output_path,
            "Repo-Week Scatter: SonarQube Findings",
            "No repo-week SonarQube scans with same-week AI share available.",
        )
        return

    fig, axes = plt.subplots(2, 3, figsize=(15, 8.4), sharex=True)
    legend_handles: dict[str, Any] = {}

    for ax, (density_field, _count_field, label) in zip(
        axes.flat,
        SONAR_FINDING_SPECS,
    ):
        points = points_by_metric[density_field]
        all_x: list[float] = []
        all_y: list[float] = []
        if not points:
            ax.text(
                0.5,
                0.5,
                "No data",
                ha="center",
                va="center",
                transform=ax.transAxes,
                color="#555555",
            )
            ax.set_title(label)
            ax.set_xlim(0, 100)
            continue

        for adoption_class in REPO_ADOPTION_ORDER:
            class_points = [
                point for point in points if point["adoption_class"] == adoption_class
            ]
            if not class_points:
                continue
            x_values = [float(point["ai_share"]) * 100.0 for point in class_points]
            y_values = [float(point["density"]) for point in class_points]
            sizes = [
                scatter_point_size(max(1, int(float(point["ncloc"]) / 1000.0)))
                for point in class_points
            ]
            all_x.extend(x_values)
            all_y.extend(y_values)
            color = REPO_ADOPTION_POINT_COLORS[adoption_class]
            label_text = REPO_ADOPTION_LABELS[adoption_class]
            scatter = ax.scatter(
                x_values,
                y_values,
                s=sizes,
                color=color,
                alpha=0.32,
                edgecolors="none",
                label=label_text,
            )
            legend_handles.setdefault(label_text, scatter)
            smooth = lowess_smooth(x_values, y_values)
            if smooth is not None:
                ax.plot(
                    smooth[0],
                    smooth[1],
                    color=color,
                    linewidth=2.0,
                    alpha=0.95,
                )

        overall_smooth = lowess_smooth(all_x, all_y)
        if overall_smooth is not None:
            overall_line = ax.plot(
                overall_smooth[0],
                overall_smooth[1],
                color="#222222",
                linewidth=2.4,
                linestyle="--",
                label="Overall LOESS",
            )[0]
            legend_handles.setdefault("Overall LOESS", overall_line)

        ax.set_title(label)
        ax.set_xlim(0, 100)
        y_top = max(all_y) * 1.10 if all_y else 1.0
        ax.set_ylim(bottom=0, top=max(y_top, 0.05))
        ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_formatter(PercentFormatter(xmax=100))

    for ax in axes[:, 0]:
        ax.set_ylabel("Findings per 1K NCLOC")
    for ax in axes[-1, :]:
        ax.set_xlabel("Same-week AI living line share")

    if legend_handles:
        fig.legend(
            list(legend_handles.values()),
            list(legend_handles.keys()),
            loc="upper center",
            ncols=min(4, len(legend_handles)),
            frameon=False,
            bbox_to_anchor=(0.5, 1.01),
        )
    fig.suptitle("Repo-Week Scatter: SonarQube Findings", y=1.035, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_sonarqube_complexity_same_week_scatter(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    points_by_metric: dict[str, list[dict[str, Any]]] = {}
    for metric_field, label in SONAR_COMPLEXITY_SPECS:
        points: list[dict[str, Any]] = []
        for row in rows:
            ai_share = row.get("ai_share")
            metric_value = row.get(metric_field)
            ncloc = row.get("sonar_ncloc")
            if ai_share is None or metric_value is None or ncloc is None or ncloc <= 0:
                continue
            points.append(
                {
                    "repo": row.get("repo") or row.get("repo_key") or "unknown",
                    "week": row.get("week"),
                    "adoption_class": row.get("adoption_class") or "balanced",
                    "ai_share": float(ai_share),
                    "metric_value": float(metric_value),
                    "ncloc": float(ncloc),
                    "label": label,
                }
            )
        points_by_metric[metric_field] = points

    if not any(points_by_metric.values()):
        draw_empty_plot(
            output_path,
            "Repo-Week Scatter: SonarQube Complexity Metrics",
            "No repo-week SonarQube complexity metrics with same-week AI share available.",
        )
        return

    fig, axes = plt.subplots(2, 3, figsize=(15, 8.4), sharex=True)
    legend_handles: dict[str, Any] = {}

    for ax, (metric_field, label) in zip(axes.flat, SONAR_COMPLEXITY_SPECS):
        points = points_by_metric[metric_field]
        all_x: list[float] = []
        all_y: list[float] = []
        if not points:
            ax.text(
                0.5,
                0.5,
                "No data",
                ha="center",
                va="center",
                transform=ax.transAxes,
                color="#555555",
            )
            ax.set_title(label)
            ax.set_xlim(0, 100)
            continue

        for adoption_class in REPO_ADOPTION_ORDER:
            class_points = [
                point for point in points if point["adoption_class"] == adoption_class
            ]
            if not class_points:
                continue
            x_values = [float(point["ai_share"]) * 100.0 for point in class_points]
            y_values = [float(point["metric_value"]) for point in class_points]
            sizes = [
                scatter_point_size(max(1, int(float(point["ncloc"]) / 1000.0)))
                for point in class_points
            ]
            all_x.extend(x_values)
            all_y.extend(y_values)
            color = REPO_ADOPTION_POINT_COLORS[adoption_class]
            label_text = REPO_ADOPTION_LABELS[adoption_class]
            scatter = ax.scatter(
                x_values,
                y_values,
                s=sizes,
                color=color,
                alpha=0.32,
                edgecolors="none",
                label=label_text,
            )
            legend_handles.setdefault(label_text, scatter)
            smooth = lowess_smooth(x_values, y_values)
            if smooth is not None:
                ax.plot(
                    smooth[0],
                    smooth[1],
                    color=color,
                    linewidth=2.0,
                    alpha=0.95,
                )

        overall_smooth = lowess_smooth(all_x, all_y)
        if overall_smooth is not None:
            overall_line = ax.plot(
                overall_smooth[0],
                overall_smooth[1],
                color="#222222",
                linewidth=2.4,
                linestyle="--",
                label="Overall LOESS",
            )[0]
            legend_handles.setdefault("Overall LOESS", overall_line)

        ax.set_title(label)
        ax.set_xlim(0, 100)
        y_top = max(all_y) * 1.10 if all_y else 1.0
        ax.set_ylim(bottom=0, top=max(y_top, 0.05))
        ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_formatter(PercentFormatter(xmax=100))

    for ax in axes[:, 0]:
        ax.set_ylabel("Metric value")
    for ax in axes[-1, :]:
        ax.set_xlabel("Same-week AI living line share")

    if legend_handles:
        fig.legend(
            list(legend_handles.values()),
            list(legend_handles.keys()),
            loc="upper center",
            ncols=min(4, len(legend_handles)),
            frameon=False,
            bbox_to_anchor=(0.5, 1.01),
        )
    fig.suptitle("Repo-Week Scatter: SonarQube Complexity Metrics", y=1.035, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_sonarqube_complexity_same_week_demeaned_scatter(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    metric_points: dict[str, list[dict[str, Any]]] = {}
    for metric_field, label in SONAR_COMPLEXITY_DEMEANED_SPECS:
        points: list[dict[str, Any]] = []
        for row in rows:
            repo = row.get("repo") or row.get("repo_key")
            ai_share = row.get("ai_share")
            metric_value = row.get(metric_field)
            ncloc = row.get("sonar_ncloc")
            if (
                not repo
                or ai_share is None
                or metric_value is None
                or ncloc is None
                or ncloc <= 0
            ):
                continue
            points.append(
                {
                    "repo": str(repo),
                    "week": row.get("week"),
                    "adoption_class": row.get("adoption_class") or "balanced",
                    "ai_share": float(ai_share),
                    "metric_value": float(metric_value),
                    "ncloc": float(ncloc),
                    "label": label,
                }
            )
        metric_points[metric_field] = points

    if not any(metric_points.values()):
        draw_empty_plot(
            output_path,
            "Within-Repo Demeaned Scatter: SonarQube Complexity Metrics",
            "No repo-week SonarQube complexity metrics with same-week AI share available.",
        )
        return

    fig, axes = plt.subplots(2, 3, figsize=(15, 8.4))
    legend_handles: dict[str, Any] = {}

    for ax, (metric_field, label) in zip(axes.flat, SONAR_COMPLEXITY_DEMEANED_SPECS):
        points = metric_points[metric_field]
        points_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in points:
            points_by_repo[point["repo"]].append(point)

        demeaned_points: list[dict[str, Any]] = []
        for repo_points in points_by_repo.values():
            if len(repo_points) < 2:
                continue
            mean_ai_share = sum(point["ai_share"] for point in repo_points) / len(
                repo_points
            )
            mean_metric = sum(point["metric_value"] for point in repo_points) / len(
                repo_points
            )
            for point in repo_points:
                demeaned_points.append(
                    {
                        **point,
                        "demeaned_ai_share": point["ai_share"] - mean_ai_share,
                        "demeaned_metric": point["metric_value"] - mean_metric,
                    }
                )

        if not demeaned_points:
            ax.text(
                0.5,
                0.5,
                "Need >=2 scans per repo",
                ha="center",
                va="center",
                transform=ax.transAxes,
                color="#555555",
            )
            ax.set_title(label)
            continue

        all_x: list[float] = []
        all_y: list[float] = []
        all_clusters: list[str] = []
        for adoption_class in REPO_ADOPTION_ORDER:
            class_points = [
                point
                for point in demeaned_points
                if point["adoption_class"] == adoption_class
            ]
            if not class_points:
                continue
            x_values = [
                float(point["demeaned_ai_share"]) * 100.0
                for point in class_points
            ]
            y_values = [float(point["demeaned_metric"]) for point in class_points]
            sizes = [
                scatter_point_size(max(1, int(float(point["ncloc"]) / 1000.0)))
                for point in class_points
            ]
            all_x.extend(x_values)
            all_y.extend(y_values)
            all_clusters.extend([point["repo"] for point in class_points])
            color = REPO_ADOPTION_POINT_COLORS[adoption_class]
            label_text = REPO_ADOPTION_LABELS[adoption_class]
            scatter = ax.scatter(
                x_values,
                y_values,
                s=sizes,
                color=color,
                alpha=0.32,
                edgecolors="none",
                label=label_text,
            )
            legend_handles.setdefault(label_text, scatter)

        fit = regression_summary(all_x, all_y, clusters=all_clusters)
        if fit is not None:
            line = ax.plot(
                fit["x"],
                fit["y"],
                color="#222222",
                linewidth=2.3,
                label="Overall regression",
            )[0]
            legend_handles.setdefault("Overall regression", line)
            annotation = (
                f"beta={fit['slope']:.3f}, p={format_p_value(fit['p_value'])}\n"
                f"n={fit['n']:,}"
            )
            if fit.get("clusters") is not None:
                annotation += f", repos={fit['clusters']:,}"
            ax.text(
                0.98,
                0.98,
                annotation,
                transform=ax.transAxes,
                ha="right",
                va="top",
                fontsize=8,
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": "white",
                    "edgecolor": "#cccccc",
                    "alpha": 0.88,
                },
            )

        ax.axhline(0, color="#777777", linewidth=1, linestyle=":")
        ax.axvline(0, color="#777777", linewidth=1, linestyle=":")
        max_abs_x = max(1.0, max((abs(value) for value in all_x), default=1.0))
        max_abs_y = max(1.0, max((abs(value) for value in all_y), default=1.0))
        ax.set_xlim(-max_abs_x * 1.08, max_abs_x * 1.08)
        ax.set_ylim(-max_abs_y * 1.08, max_abs_y * 1.08)
        ax.set_title(label)
        ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
        ax.set_axisbelow(True)

    for ax in axes[:, 0]:
        ax.set_ylabel("Metric deviation")
    for ax in axes[-1, :]:
        ax.set_xlabel("Same-week AI share deviation from repo mean (pp)")

    if legend_handles:
        fig.legend(
            list(legend_handles.values()),
            list(legend_handles.keys()),
            loc="upper center",
            ncols=min(4, len(legend_handles)),
            frameon=False,
            bbox_to_anchor=(0.5, 1.01),
        )
    fig.suptitle(
        "Within-Repo Demeaned Scatter: SonarQube Complexity Metrics",
        y=1.035,
        fontsize=14,
    )
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_sonarqube_same_week_demeaned_scatter(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    metric_points: dict[str, list[dict[str, Any]]] = {}
    for density_field, count_field, label in SONAR_FINDING_SPECS:
        points: list[dict[str, Any]] = []
        for row in rows:
            repo = row.get("repo") or row.get("repo_key")
            ai_share = row.get("ai_share")
            count = row.get(count_field)
            ncloc = row.get("sonar_ncloc")
            if (
                not repo
                or ai_share is None
                or ncloc is None
                or ncloc <= 0
                or count is None
            ):
                continue
            points.append(
                {
                    "repo": str(repo),
                    "week": row.get("week"),
                    "adoption_class": row.get("adoption_class") or "balanced",
                    "ai_share": float(ai_share),
                    "count": float(count or 0),
                    "ncloc": float(ncloc),
                    "label": label,
                }
            )
        metric_points[density_field] = points

    if not any(metric_points.values()):
        draw_empty_plot(
            output_path,
            "Within-Repo Demeaned Scatter: SonarQube Findings",
            "No repo-week SonarQube scans with same-week AI share available.",
        )
        return

    fig, axes = plt.subplots(2, 3, figsize=(15, 8.4))
    legend_handles: dict[str, Any] = {}

    for ax, (density_field, _count_field, label) in zip(
        axes.flat,
        SONAR_FINDING_SPECS,
    ):
        points = metric_points[density_field]
        points_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in points:
            points_by_repo[point["repo"]].append(point)

        demeaned_points: list[dict[str, Any]] = []
        for repo_points in points_by_repo.values():
            if len(repo_points) < 2:
                continue
            mean_ai_share = sum(point["ai_share"] for point in repo_points) / len(
                repo_points
            )
            mean_count = sum(point["count"] for point in repo_points) / len(
                repo_points
            )
            for point in repo_points:
                demeaned_points.append(
                    {
                        **point,
                        "demeaned_ai_share": point["ai_share"] - mean_ai_share,
                        "demeaned_count": point["count"] - mean_count,
                    }
                )

        if not demeaned_points:
            ax.text(
                0.5,
                0.5,
                "Need >=2 scans per repo",
                ha="center",
                va="center",
                transform=ax.transAxes,
                color="#555555",
            )
            ax.set_title(label)
            continue

        all_x: list[float] = []
        all_y: list[float] = []
        all_clusters: list[str] = []
        for adoption_class in REPO_ADOPTION_ORDER:
            class_points = [
                point
                for point in demeaned_points
                if point["adoption_class"] == adoption_class
            ]
            if not class_points:
                continue
            x_values = [
                float(point["demeaned_ai_share"]) * 100.0
                for point in class_points
            ]
            y_values = [float(point["demeaned_count"]) for point in class_points]
            sizes = [
                scatter_point_size(max(1, int(float(point["ncloc"]) / 1000.0)))
                for point in class_points
            ]
            all_x.extend(x_values)
            all_y.extend(y_values)
            all_clusters.extend([point["repo"] for point in class_points])
            color = REPO_ADOPTION_POINT_COLORS[adoption_class]
            label_text = REPO_ADOPTION_LABELS[adoption_class]
            scatter = ax.scatter(
                x_values,
                y_values,
                s=sizes,
                color=color,
                alpha=0.32,
                edgecolors="none",
                label=label_text,
            )
            legend_handles.setdefault(label_text, scatter)

        fit = regression_summary(all_x, all_y, clusters=all_clusters)
        if fit is not None:
            line = ax.plot(
                fit["x"],
                fit["y"],
                color="#222222",
                linewidth=2.3,
                label="Overall regression",
            )[0]
            legend_handles.setdefault("Overall regression", line)
            annotation = (
                f"beta={fit['slope']:.3f}, p={format_p_value(fit['p_value'])}\n"
                f"n={fit['n']:,}"
            )
            if fit.get("clusters") is not None:
                annotation += f", repos={fit['clusters']:,}"
            ax.text(
                0.98,
                0.98,
                annotation,
                transform=ax.transAxes,
                ha="right",
                va="top",
                fontsize=8,
                bbox={
                    "boxstyle": "round,pad=0.25",
                    "facecolor": "white",
                    "edgecolor": "#cccccc",
                    "alpha": 0.88,
                },
            )

        ax.axhline(0, color="#777777", linewidth=1, linestyle=":")
        ax.axvline(0, color="#777777", linewidth=1, linestyle=":")
        max_abs_x = max(1.0, max((abs(value) for value in all_x), default=1.0))
        max_abs_y = max(1.0, max((abs(value) for value in all_y), default=1.0))
        ax.set_xlim(-max_abs_x * 1.08, max_abs_x * 1.08)
        ax.set_ylim(-max_abs_y * 1.08, max_abs_y * 1.08)
        ax.set_title(label)
        ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
        ax.set_axisbelow(True)

    for ax in axes[:, 0]:
        ax.set_ylabel("Finding count deviation")
    for ax in axes[-1, :]:
        ax.set_xlabel("Same-week AI share deviation from repo mean (pp)")

    if legend_handles:
        fig.legend(
            list(legend_handles.values()),
            list(legend_handles.keys()),
            loc="upper center",
            ncols=min(4, len(legend_handles)),
            frameon=False,
            bbox_to_anchor=(0.5, 1.01),
        )
    fig.suptitle("Within-Repo Demeaned Scatter: SonarQube Findings", y=1.035, fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_repo_week_scatter_with_smoother(
    repo_data: list[dict[str, Any]],
    output_path: Path,
    *,
    source_only: bool = False,
) -> None:
    title = (
        "Repo-Week Scatter: Source-Touching Corrective Rate"
        if source_only
        else "Repo-Week Scatter: Corrective Rate"
    )
    plot_repo_week_scatter_points_with_smoother(
        build_repo_week_scatter_points(repo_data, source_only=source_only),
        output_path,
        title=title,
        y_axis_label="Corrective commits / total commits",
    )


def plot_repo_week_human_corrective_burden_scatter(
    repo_data: list[dict[str, Any]],
    output_path: Path,
) -> None:
    plot_repo_week_scatter_points_with_smoother(
        build_repo_week_human_corrective_burden_points(repo_data),
        output_path,
        title="Repo-Week Scatter: Human Corrective Burden",
        y_axis_label="Human-written corrective commits / total human-written commits",
    )


def plot_repo_week_line_churn_scatter(
    repo_data: list[dict[str, Any]],
    output_path: Path,
) -> None:
    plot_repo_week_scatter_points_with_smoother(
        build_repo_week_line_churn_points(repo_data),
        output_path,
        title="Repo-Week Scatter: Tracked Line Churn Rate",
        y_axis_label="Terminated tracked lines / living tracked lines at week start",
        full_percent_y_axis=False,
    )


def plot_repo_week_demeaned_scatter_points(
    points: list[dict[str, Any]],
    output_path: Path,
    *,
    title: str,
    y_axis_label: str,
    x_axis_label: str = "Lagged AI living line share deviation from repo mean (percentage points)",
    y_multiplier: float = 100.0,
) -> None:
    if not points:
        draw_empty_plot(
            output_path,
            title,
            "No repo-week points with lagged AI share available.",
        )
        return

    points_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        points_by_repo[str(point["repo"])].append(point)

    demeaned_points: list[dict[str, Any]] = []
    for repo_points in points_by_repo.values():
        if len(repo_points) < 2:
            continue
        mean_ai_share = sum(float(p["ai_share"]) for p in repo_points) / len(repo_points)
        mean_corrective_rate = (
            sum(float(p["corrective_rate"]) for p in repo_points) / len(repo_points)
        )
        for point in repo_points:
            demeaned_points.append(
                {
                    **point,
                    "demeaned_ai_share": float(point["ai_share"]) - mean_ai_share,
                    "demeaned_corrective_rate": (
                        float(point["corrective_rate"]) - mean_corrective_rate
                    ),
                }
            )

    if not demeaned_points:
        draw_empty_plot(
            output_path,
            title,
            "No repos have at least two usable repo-week points.",
        )
        return

    fig, ax = plt.subplots(figsize=(9.5, 6.2))
    all_x: list[float] = []
    all_y: list[float] = []
    all_clusters: list[str] = []

    for adoption_class in REPO_ADOPTION_ORDER:
        class_points = [
            p for p in demeaned_points if p["adoption_class"] == adoption_class
        ]
        if not class_points:
            continue
        x_values = [float(p["demeaned_ai_share"]) * 100 for p in class_points]
        y_values = [
            float(p["demeaned_corrective_rate"]) * y_multiplier
            for p in class_points
        ]
        sizes = [scatter_point_size(int(p["total_commits"])) for p in class_points]
        all_x.extend(x_values)
        all_y.extend(y_values)
        all_clusters.extend([str(p["repo"]) for p in class_points])
        ax.scatter(
            x_values,
            y_values,
            s=sizes,
            color=REPO_ADOPTION_POINT_COLORS[adoption_class],
            alpha=0.32,
            edgecolors="none",
            label=REPO_ADOPTION_LABELS[adoption_class],
        )

    fit = regression_summary(all_x, all_y, clusters=all_clusters)
    if fit is not None:
        ax.plot(
            fit["x"],       
            fit["y"],
            color="#222222",
            linewidth=2.6,
            label="Overall regression",
        )
        if fit["covariance"] == "cluster_robust_by_repo":
            annotation = (
                f"y = {fit['intercept']:.3f} + {fit['slope']:.3f}x\n"
                f"cluster SE = {fit['std_error']:.3f}, "
                f"p = {format_p_value(fit['p_value'])}\n"
                f"95% CI [{fit['ci_low']:.3f}, {fit['ci_high']:.3f}]\n"
                f"R^2 = {fit['r_squared']:.3f}, n = {fit['n']:,}, "
                f"repos = {fit['clusters']:,}"
            )
        else:
            annotation = (
                f"y = {fit['intercept']:.3f} + {fit['slope']:.3f}x\n"
                f"slope p = {format_p_value(fit['p_value'])}\n"
                f"R^2 = {fit['r_squared']:.3f}, n = {fit['n']:,}"
            )
        ax.text(
            0.98,
            0.98,
            annotation,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=9,
            bbox={
                "boxstyle": "round,pad=0.35",
                "facecolor": "white",
                "edgecolor": "#cccccc",
                "alpha": 0.88,
            },
        )
    ax.axhline(0, color="#777777", linewidth=1, linestyle=":")
    ax.axvline(0, color="#777777", linewidth=1, linestyle=":")

    max_abs_x = max((abs(value) for value in all_x), default=1)
    max_abs_y = max((abs(value) for value in all_y), default=1)
    ax.set_xlim(-max_abs_x * 1.08, max_abs_x * 1.08)
    ax.set_ylim(-max_abs_y * 1.08, max_abs_y * 1.08)
    ax.set_title(title)
    ax.set_xlabel(x_axis_label)
    ax.set_ylabel(y_axis_label)
    ax.grid(True, linestyle=":", linewidth=0.7, alpha=0.65)
    ax.set_axisbelow(True)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def plot_repo_week_demeaned_scatter(
    repo_data: list[dict[str, Any]],
    output_path: Path,
    *,
    source_only: bool = False,
) -> None:
    title = (
        "Within-Repo Demeaned Scatter: Source-Touching Corrective Rate"
        if source_only
        else "Within-Repo Demeaned Scatter: Corrective Rate"
    )
    plot_repo_week_demeaned_scatter_points(
        build_repo_week_scatter_points(repo_data, source_only=source_only),
        output_path,
        title=title,
        y_axis_label="Corrective rate deviation from repo mean (percentage points)",
    )


def plot_repo_week_human_corrective_burden_demeaned_scatter(
    repo_data: list[dict[str, Any]],
    output_path: Path,
) -> None:
    plot_repo_week_demeaned_scatter_points(
        build_repo_week_human_corrective_burden_points(repo_data),
        output_path,
        title="Within-Repo Demeaned Scatter: Human Corrective Burden",
        y_axis_label=(
            "Human corrective burden deviation from repo mean (percentage points)"
        ),
    )


def plot_repo_week_line_churn_demeaned_scatter(
    repo_data: list[dict[str, Any]],
    output_path: Path,
) -> None:
    plot_repo_week_demeaned_scatter_points(
        build_repo_week_line_churn_points(repo_data),
        output_path,
        title="Within-Repo Demeaned Scatter: Tracked Line Churn Rate",
        y_axis_label="Tracked line churn rate deviation from repo mean (percentage points)",
    )


BETABINOMIAL_PANEL_MODEL_ORDER = (
    "corrective_commit_rate",
    "maintenance_class_corrective_rate",
    "human_corrective_rate",
    "overall_line_termination_rate",
)
BETABINOMIAL_PREDICTED_RATE_MODELS = (
    "corrective_commit_rate",
    "human_corrective_rate",
)
GLMER_EXPOSURE_PANEL_ORDER = (
    "living_line_share_lag1",
    "cumulative_ai_commit_share_lag1",
)
GLMER_EXPOSURE_PANEL_LABELS = {
    "living_line_share_lag1": "Lagged Agentic living-line share",
    "living_line_ai_share_lag1": "Lagged Agentic living-line share",
    "cumulative_ai_commit_share_lag1": "Lagged cumulative Agentic commit share",
}
BETABINOMIAL_PANEL_MODEL_TITLES = {
    "corrective_commit_rate": "Corrective Commit Rate",
    "maintenance_class_corrective_rate": "Corrective Commit Rate",
    "human_corrective_rate": "Human Corrective Commit Rate",
    "overall_line_termination_rate": "Overall Line Termination Rate",
}
BETABINOMIAL_PANEL_MODEL_YLABELS = {
    "corrective_commit_rate": "Predicted corrective commit rate",
    "maintenance_class_corrective_rate": "Predicted corrective commit rate",
    "human_corrective_rate": "Predicted human corrective commit rate",
    "overall_line_termination_rate": "Predicted line termination rate",
}
BETABINOMIAL_PANEL_MODEL_LABELS = {
    "corrective_commit_rate": "Corrective commit rate",
    "maintenance_class_corrective_rate": "Corrective commit rate",
    "human_corrective_rate": "Human corrective commit rate",
}
BETABINOMIAL_PANEL_MODEL_COLORS = {
    "corrective_commit_rate": HUMAN_COLOR,
    "maintenance_class_corrective_rate": HUMAN_COLOR,
    "human_corrective_rate": AI_COLOR,
}


def read_betabinomial_glmm_predictions(
    path: Path,
) -> dict[str, list[dict[str, Any]]]:
    predictions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not path.exists():
        return {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") != "fit":
                continue
            model = row.get("model") or ""
            if not model:
                continue
            try:
                x_value = float(row["lagged_ai_share_pp"])
                predicted = float(row["predicted_rate"])
                ci_low = float(row["ci_low"])
                ci_high = float(row["ci_high"])
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (x_value, predicted, ci_low, ci_high)):
                continue
            outcome_name = row.get("outcome_name") or (
                "human_corrective_rate"
                if model.endswith("human_corrective_rate")
                else "corrective_commit_rate"
            )
            exposure_name = row.get("exposure_name") or "living_line_share_lag1"
            predictions[model].append(
                {
                    "x": x_value,
                    "predicted": predicted,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "outcome_name": outcome_name,
                    "outcome": row.get("outcome") or BETABINOMIAL_PANEL_MODEL_LABELS.get(
                        outcome_name, outcome_name
                    ),
                    "exposure_name": exposure_name,
                    "exposure_label": row.get("exposure_label")
                    or GLMER_EXPOSURE_PANEL_LABELS.get(exposure_name, exposure_name),
                }
            )
    return {
        model: sorted(rows, key=lambda item: item["x"])
        for model, rows in predictions.items()
        if rows
    }


def plot_glmer_predicted_rates_lagged_code_share(
    predictions_csv: Path,
    output_path: Path,
) -> bool:
    setup_nimbus_font()
    predictions = read_betabinomial_glmm_predictions(predictions_csv)
    lagged_code_share_exposures = {
        "living_line_share_lag1",
        "living_line_ai_share_lag1",
    }
    predictions_by_outcome: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for model_rows in predictions.values():
        for row in model_rows:
            exposure_name = str(row.get("exposure_name") or "")
            outcome_name = str(row.get("outcome_name") or "")
            if exposure_name not in lagged_code_share_exposures:
                continue
            if outcome_name not in BETABINOMIAL_PREDICTED_RATE_MODELS:
                continue
            predictions_by_outcome[outcome_name].append(row)

    if not any(predictions_by_outcome.values()):
        print(
            "skipped_glmer_lagged_code_share_predicted_rates: "
            f"missing usable lagged code-share predictions in {predictions_csv}"
        )
        return False

    fig, ax = plt.subplots(figsize=(5.6, 2.85))
    for outcome_name in BETABINOMIAL_PREDICTED_RATE_MODELS:
        rows = sorted(predictions_by_outcome.get(outcome_name, []), key=lambda item: item["x"])
        if not rows:
            continue
        model_x = np.array([row["x"] for row in rows], dtype=float)
        y_values = np.array([row["predicted"] for row in rows], dtype=float)
        band_low = np.array([row["ci_low"] for row in rows], dtype=float)
        band_high = np.array([row["ci_high"] for row in rows], dtype=float)
        color = BETABINOMIAL_PANEL_MODEL_COLORS.get(outcome_name, AI_COLOR)
        label = BETABINOMIAL_PANEL_MODEL_LABELS.get(
            outcome_name,
            BETABINOMIAL_PANEL_MODEL_TITLES.get(outcome_name, outcome_name),
        )
        ax.plot(
            model_x,
            y_values * 100.0,
            color=color,
            linewidth=2.8,
            label=label,
        )
        ax.fill_between(
            model_x,
            band_low * 100.0,
            band_high * 100.0,
            color=color,
            alpha=0.14,
            linewidth=0,
        )

    ax.set_xlabel("Lagged Agentic code share (pp)", fontsize=13)
    ax.set_ylabel("Predicted corrective commit rate", fontsize=13)
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.0f"))
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
    ax.tick_params(axis="both", labelsize=14)
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(0.02, 1.08),
        borderaxespad=0.0,
        frameon=False,
        fontsize=14,
    )
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return True


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.max_commit_additions < 0:
        raise ValueError("--max-commit-additions must be non-negative")
    origin_start = parse_time_bound(args.origin_start, is_end=False)
    if args.origin_start and origin_start is None and not value_is_open_time_bound(
        args.origin_start
    ):
        raise ValueError(f"invalid --origin-start: {args.origin_start}")
    origin_end = parse_time_bound(args.origin_end, is_end=True)
    if args.origin_end and origin_end is None and not value_is_open_time_bound(
        args.origin_end
    ):
        raise ValueError(f"invalid --origin-end: {args.origin_end}")
    if origin_start is not None and origin_end is not None and origin_start > origin_end:
        raise ValueError("--origin-start must be <= --origin-end")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    panel_csv = args.panel_csv or args.output_dir / DEFAULT_PANEL_CSV_NAME
    betabinomial_predictions_csv = (
        args.betabinomial_predictions_csv
        or args.output_dir / DEFAULT_BETABINOMIAL_PREDICTION_CSV_NAME
    )
    if args.use_panel_csv is not None:
        panel_rows = read_repo_week_panel_csv(args.use_panel_csv)
        panel_rows = filter_panel_rows_by_repo(
            panel_rows,
            repo_filter_aliases(args.repo, args.repo_file),
        )
        panel_rows = filter_panel_rows_by_week(
            panel_rows,
            start=origin_start,
            end=origin_end,
        )
        print(f"loaded_panel_rows: {len(panel_rows):,}")
        print(f"loaded_panel_csv: {args.use_panel_csv}")
        print(
            "panel_time_window: "
            f"start={origin_start.isoformat() if origin_start else 'open'} "
            f"end={origin_end.isoformat() if origin_end else 'open'} "
            "inclusive=True"
        )
    else:
        repo_data = load_rq2_data(
            args.input_dir,
            repos=split_csv(args.repo),
            repo_file=args.repo_file,
            workers=args.workers,
            logger=LOG,
        )
        print(f"loaded_repos: {len(repo_data):,}")
        max_commit_additions = (
            args.max_commit_additions if args.max_commit_additions > 0 else None
        )
        if max_commit_additions is None:
            print("commit_addition_filter: disabled")
        else:
            print(
                "commit_addition_filter: "
                f"excluding commits with additions >= {max_commit_additions:,}"
            )
        print(
            "panel_time_window: "
            f"start={origin_start.isoformat() if origin_start else 'open'} "
            f"end={origin_end.isoformat() if origin_end else 'open'} "
            "inclusive=True"
        )
        panel_rows = build_repo_week_panel_rows(
            repo_data,
            max_commit_additions=max_commit_additions,
            origin_start=origin_start,
            origin_end=origin_end,
        )
        print(f"built_panel_rows: {len(panel_rows):,}")
        if not args.skip_panel_export:
            write_repo_week_panel_csv(panel_rows, panel_csv)
            print(f"wrote_panel_csv: {panel_csv}")

    if args.run_python_panel_fe:
        print(
            "skipped_python_panel_models: Python OLS panel models are disabled. "
            "Run repo-month GLMER panel regressions in "
            f"R_code/Data_Analysis_rq2.Rmd using {panel_csv}."
        )
    else:
        print(
            "skipped_python_panel_models: run repo-month GLMER panel regressions in "
            f"R_code/Data_Analysis_rq2.Rmd using {panel_csv}."
        )
    lagged_code_share_predicted_rates_path = (
        args.output_dir
        / "rq2_panel_glmer_predicted_corrective_rates_lagged_code_share.pdf"
    )
    wrote_lagged_code_share_predicted_rates = (
        plot_glmer_predicted_rates_lagged_code_share(
            betabinomial_predictions_csv,
            lagged_code_share_predicted_rates_path,
        )
    )
    if wrote_lagged_code_share_predicted_rates:
        print(f"wrote {lagged_code_share_predicted_rates_path}")


if __name__ == "__main__":
    main()
