from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from commit_contribution_classifier import SOURCE_EXTENSIONS


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("src/webscrape/csv")

COMMIT_CLASSIFICATION_FILE = "commit_contribution_classifications.jsonl"
COMMIT_FILE_CLASSIFICATION_FILE = "commit_file_contribution_classifications.jsonl"
STATIC_FINDINGS_FILE = "commit_static_findings.jsonl"
STATIC_SUMMARY_FILE = "commit_static_analysis_summary.jsonl"
SUPPLY_CHAIN_FINDINGS_FILE = "commit_supply_chain_findings.jsonl"
SUPPLY_CHAIN_SUMMARY_FILE = "commit_supply_chain_summary.jsonl"
COMMIT_PRS_FILE = "commit_prs.jsonl"
COMMIT_PR_SUMMARY_FILE = "commit_pr_summary.jsonl"
CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE = "codebase_agent_share_snapshots.jsonl"
CODEBASE_AGENT_SHARE_FILES_FILE = "codebase_agent_share_files.jsonl"
SONARQUBE_SNAPSHOT_SCANS_FILE = "sonarqube_snapshot_scans.jsonl"
DEFAULT_SOURCE_EXTENSIONS = frozenset(SOURCE_EXTENSIONS)
REPO_FILE_COLUMNS = (
    "repo_slug",
    "repo",
    "repo_key",
    "repository",
    "repository_full_name",
    "full_name",
    "slug",
    "repo_url",
    "repository_url",
    "url",
)

SONARQUBE_DEFAULT_METRICS = (
    "ncloc",
    "bugs",
    "vulnerabilities",
    "security_hotspots",
    "code_smells",
    "duplicated_lines_density",
    "comment_lines_density",
    "cognitive_complexity",
    "complexity",
    "software_quality_maintainability_remediation_effort",
    "sqale_index",
    "coverage",
)

COMMIT_COLUMNS = (
    "repo_key",
    "repo",
    "sha",
    "date",
    "category",
    "labels",
    "author_login",
    "author_type",
    "committer_login",
    "committer_type",
    "git_author_name",
    "git_author_email",
    "git_committer_name",
    "git_committer_email",
    "coauthor_names",
    "coauthor_emails",
    "parents",
    "message_subject",
    "message_body",
    "html_url",
    "api_url",
    "github_metadata_enriched",
    "files_changed",
    "additions",
    "deletions",
    "changed_lines",
)

COMMIT_FILE_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "commit_index",
    "commit_date",
    "commit_category",
    "commit_labels",
    "filename",
    "previous_filename",
    "status",
    "additions",
    "deletions",
    "changed_lines",
    "hunks_count",
    "added_lines",
    "deleted_lines",
    "html_url",
    "blob_url",
    "raw_url",
)

LINE_LIFECYCLE_COLUMNS = (
    "repo_key",
    "repo",
    "origin_commit_sha",
    "origin_commit_date",
    "origin_commit_category",
    "origin_commit_labels",
    "origin_file",
    "origin_line",
    "current_file",
    "current_line",
    "status",
    "line_content",
    "terminal_commit_sha",
    "terminal_commit_date",
    "terminal_commit_category",
    "terminal_commit_labels",
    "terminal_reason",
    "terminal_line",
    "terminal_commit_message_subject",
    "terminal_commit_message_body",
    "terminal_commit_author_login",
    "terminal_commit_author_type",
    "terminal_commit_committer_login",
    "terminal_commit_committer_type",
    "terminal_git_author_name",
    "terminal_git_author_email",
    "terminal_git_committer_name",
    "terminal_git_committer_email",
    "terminal_github_metadata_enriched",
    "origin_html_url",
)

COMMIT_CLASSIFICATION_COLUMNS = (
    "repo_key",
    "repo",
    "sha",
    "date",
    "input_commit_file",
    "is_origin_commit",
    "is_observation_commit",
    "labeled_by_llm",
    "message_subject",
    "primary_operational_intent",
    "operational_intents",
    "maintenance_class",
    "maintenance_classes",
    "secondary_object_tags",
    "confidence",
    "additions",
    "deletions",
    "changed_lines",
    "files_changed",
    "evidence_summary",
    "evidence_json",
)

COMMIT_FILE_CLASSIFICATION_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "date",
    "input_commit_file",
    "is_origin_commit",
    "is_observation_commit",
    "filename",
    "previous_filename",
    "status",
    "additions",
    "deletions",
    "changed_lines",
    "message_subject",
    "file_primary_role",
    "file_object_tags",
    "file_context_intents",
    "file_confidence",
    "commit_primary_operational_intent",
    "commit_operational_intents",
    "commit_maintenance_class",
    "commit_maintenance_classes",
    "commit_confidence",
    "evidence_summary",
    "evidence_json",
)

COMMIT_LIFECYCLE_SUMMARY_COLUMNS = (
    "repo_key",
    "repo",
    "origin_commit_sha",
    "origin_commit_date",
    "origin_commit_category",
    "origin_commit_labels",
    "total_lines",
    "survived_lines",
    "modified_lines",
    "deleted_lines",
    "terminated_lines",
    "survival_rate",
    "origin_files",
    "terminal_commit_count",
    "terminal_commits",
    "terminal_category_counts",
    "terminal_reason_counts",
    "terminal_intent_counts",
    "first_terminal_commit_date",
)

STATIC_FINDINGS_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "parent_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "commit_primary_operational_intent",
    "commit_operational_intents",
    "commit_maintenance_class",
    "commit_confidence",
    "filename",
    "previous_filename",
    "file_status",
    "file_primary_role",
    "file_object_tags",
    "file_context_intents",
    "finding_status",
    "rule_id",
    "severity",
    "message",
    "metadata_cwe",
    "metadata_owasp",
    "before_path",
    "before_start_line",
    "before_end_line",
    "before_start_col",
    "before_end_col",
    "after_path",
    "after_start_line",
    "after_end_line",
    "after_start_col",
    "after_end_col",
    "match_key",
    "semgrep_configs",
)

STATIC_SUMMARY_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "parent_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "files_changed",
    "before_files_materialized",
    "after_files_materialized",
    "before_raw_findings",
    "after_raw_findings",
    "introduced_findings",
    "removed_findings",
    "persistent_findings",
    "before_semgrep_returncode",
    "after_semgrep_returncode",
    "before_semgrep_errors",
    "after_semgrep_errors",
    "materialization_errors",
    "skipped_reason",
)

SUPPLY_CHAIN_FINDINGS_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "parent_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "commit_primary_operational_intent",
    "commit_operational_intents",
    "commit_maintenance_class",
    "commit_confidence",
    "filename",
    "previous_filename",
    "file_status",
    "file_primary_role",
    "file_object_tags",
    "file_context_intents",
    "finding_status",
    "ecosystem",
    "package_name",
    "before_package_version",
    "after_package_version",
    "source_type",
    "before_source_path",
    "after_source_path",
    "vulnerability_id",
    "vulnerability_ids",
    "aliases",
    "severity",
    "severity_score",
    "fixed_versions",
    "summary",
    "match_key",
)

SUPPLY_CHAIN_SUMMARY_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "parent_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "files_changed",
    "before_files_materialized",
    "after_files_materialized",
    "before_raw_results",
    "after_raw_results",
    "before_raw_findings",
    "after_raw_findings",
    "introduced_findings",
    "removed_findings",
    "persistent_findings",
    "before_osv_returncode",
    "after_osv_returncode",
    "before_osv_errors",
    "after_osv_errors",
    "before_osv_command",
    "after_osv_command",
    "materialization_errors",
    "skipped_reason",
)

COMMIT_PRS_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "message_subject",
    "has_pr",
    "is_primary_pr",
    "associated_pr_count",
    "association_method",
    "missing_reason",
    "error",
    "pr_number",
    "pr_id",
    "pr_node_id",
    "pr_title",
    "pr_state",
    "pr_url",
    "pr_api_url",
    "pr_user_login",
    "pr_user_type",
    "pr_created_at",
    "pr_updated_at",
    "pr_closed_at",
    "pr_merged_at",
    "pr_draft",
    "comments",
    "review_comments",
    "base_ref",
    "base_sha",
    "head_ref",
    "head_sha",
    "head_repo_full_name",
    "merge_commit_sha",
    "pr_detail_hydrated",
    "pr_detail_error",
)

COMMIT_PR_SUMMARY_COLUMNS = (
    "repo_key",
    "repo",
    "commit_sha",
    "commit_date",
    "commit_category",
    "commit_labels",
    "message_subject",
    "has_pr",
    "associated_pr_count",
    "associated_pr_numbers",
    "message_pr_numbers",
    "primary_association_method",
    "primary_pr_number",
    "primary_pr_title",
    "primary_pr_url",
    "primary_pr_merged_at",
    "missing_reason",
    "error",
)

CODEBASE_AGENT_SHARE_SNAPSHOT_COLUMNS = (
    "repo_key",
    "repo",
    "branch",
    "snapshot_index",
    "snapshot_date",
    "snapshot_sha",
    "snapshot_commit_date",
    "files_considered",
    "files_analyzed",
    "files_skipped",
    "total_living_lines",
    "ai_living_lines",
    "ai_living_line_share",
    "human_living_lines",
    "human_living_line_share",
    "other_bot_living_lines",
    "other_bot_living_line_share",
    "unknown_living_lines",
    "unknown_living_line_share",
)

CODEBASE_AGENT_SHARE_SOURCE_SNAPSHOT_COLUMNS = (
    "repo_key",
    "repo",
    "branch",
    "snapshot_index",
    "snapshot_date",
    "snapshot_sha",
    "snapshot_commit_date",
    "source_extensions",
    "source_files",
    "source_total_living_lines",
    "source_ai_living_lines",
    "source_ai_living_line_share",
    "source_human_living_lines",
    "source_human_living_line_share",
    "source_other_bot_living_lines",
    "source_other_bot_living_line_share",
    "source_unknown_living_lines",
    "source_unknown_living_line_share",
)

SONARQUBE_SNAPSHOT_SCAN_COLUMNS = (
    "repo_key",
    "repo",
    "branch",
    "snapshot_index",
    "snapshot_date",
    "snapshot_sha",
    "snapshot_commit_date",
    "project_key",
    "project_version",
    "scan_status",
    "error",
    "scanner_returncode",
    "scanner_duration_seconds",
    "scanner_stdout_tail",
    "scanner_stderr_tail",
    "ce_task_id",
    "ce_task_status",
    "analysis_id",
    "dashboard_url",
    "issue_count",
    "issue_counts_by_severity",
    "issue_counts_by_type",
    "issue_counts_by_status",
    "issue_counts_by_resolution",
    "metric_ncloc",
    "metric_bugs",
    "metric_vulnerabilities",
    "metric_security_hotspots",
    "metric_code_smells",
    "metric_duplicated_lines_density",
    "metric_comment_lines_density",
    "metric_cognitive_complexity",
    "metric_complexity",
    "metric_software_quality_maintainability_remediation_effort",
    "metric_sqale_index",
    "metric_coverage",
    "metrics_json",
)

REPO_SUMMARY_COLUMNS = (
    "repo_key",
    "repo",
    "branch",
    "branch_end_sha",
    "blame_since",
    "total_commits",
    "ai_coauthored_commits",
    "not_ai_coauthored_commits",
    "other_bot_commits",
    "unknown_commits",
    "file_changes",
    "additions",
    "deletions",
    "changed_lines",
    "commit_classifications",
    "llm_labeled_commit_classifications",
    "commit_file_classifications",
    "static_analysis_commits",
    "static_finding_rows",
    "static_introduced_findings",
    "static_removed_findings",
    "static_persistent_findings",
    "supply_chain_analysis_commits",
    "supply_chain_finding_rows",
    "supply_chain_introduced_findings",
    "supply_chain_removed_findings",
    "supply_chain_persistent_findings",
    "codebase_agent_share_snapshots",
    "latest_total_living_lines",
    "latest_ai_living_lines",
    "latest_ai_living_line_share",
    "latest_source_living_lines",
    "latest_source_ai_living_lines",
    "latest_source_ai_living_line_share",
    "sonarqube_snapshot_scans",
    "sonarqube_successful_scans",
    "sonarqube_failed_scans",
    "pr_summary_commits",
    "pr_commits_with_pr",
    "pr_commits_without_pr",
    "pr_association_rows",
    "pr_primary_rows",
    "pr_conversation_comments",
    "pr_review_comments",
    "lifecycle_total_lines",
    "survived_lines",
    "modified_lines",
    "deleted_lines",
    "survival_rate",
    "primary_intent_counts",
    "maintenance_class_counts",
    "file_role_counts",
)

REPO_SUMMARY_PR_COLUMNS = frozenset(
    {
        "pr_summary_commits",
        "pr_commits_with_pr",
        "pr_commits_without_pr",
        "pr_association_rows",
        "pr_primary_rows",
        "pr_conversation_comments",
        "pr_review_comments",
    }
)

COMPACT_DROP_COLUMNS = {
    "commits": frozenset(
        {
            "git_author_email",
            "git_committer_email",
            "coauthor_names",
            "coauthor_emails",
            "message_body",
            "html_url",
            "api_url",
        }
    ),
    "commit_files": frozenset({"html_url", "blob_url", "raw_url"}),
    "line_lifecycle": frozenset(
        {
            "line_content",
            "terminal_commit_message_body",
            "terminal_commit_author_login",
            "terminal_commit_author_type",
            "terminal_commit_committer_login",
            "terminal_commit_committer_type",
            "terminal_git_author_name",
            "terminal_git_author_email",
            "terminal_git_committer_name",
            "terminal_git_committer_email",
            "terminal_github_metadata_enriched",
            "origin_html_url",
        }
    ),
    "commit_classifications": frozenset({"evidence_json"}),
    "commit_file_classifications": frozenset({"evidence_json"}),
    "static_findings": frozenset(
        {
            "message",
            "metadata_owasp",
            "before_path",
            "before_start_col",
            "before_end_col",
            "after_path",
            "after_start_col",
            "after_end_col",
            "match_key",
            "semgrep_configs",
        }
    ),
    "static_summary": frozenset(
        {
            "before_semgrep_errors",
            "after_semgrep_errors",
            "materialization_errors",
        }
    ),
    "supply_chain_findings": frozenset(
        {
            "vulnerability_ids",
            "aliases",
            "severity_score",
            "summary",
            "match_key",
        }
    ),
    "supply_chain_summary": frozenset(
        {
            "before_osv_errors",
            "after_osv_errors",
            "before_osv_command",
            "after_osv_command",
            "materialization_errors",
        }
    ),
    "commit_prs": frozenset(
        {
            "pr_api_url",
            "pr_node_id",
            "base_sha",
            "head_sha",
            "head_repo_full_name",
            "merge_commit_sha",
            "pr_detail_error",
        }
    ),
    "commit_pr_summary": frozenset(
        {
            "associated_pr_numbers",
            "message_pr_numbers",
            "primary_pr_url",
        }
    ),
    "codebase_agent_share_source_snapshots": frozenset({"source_extensions"}),
    "sonarqube_snapshot_scans": frozenset(
        {
            "scanner_stdout_tail",
            "scanner_stderr_tail",
            "dashboard_url",
            "metrics_json",
        }
    ),
}


@dataclass
class CommitLifecycleAggregate:
    repo_key: str
    repo: str
    origin_commit_sha: str
    origin_commit_date: str = ""
    origin_commit_category: str = ""
    origin_commit_labels: list[str] = field(default_factory=list)
    statuses: Counter[str] = field(default_factory=Counter)
    origin_files: set[str] = field(default_factory=set)
    terminal_commits: Counter[str] = field(default_factory=Counter)
    terminal_categories: Counter[str] = field(default_factory=Counter)
    terminal_reasons: Counter[str] = field(default_factory=Counter)
    terminal_intents: Counter[str] = field(default_factory=Counter)
    first_terminal_commit_date: str = ""
    first_terminal_datetime: datetime | None = None


@dataclass
class RepoAggregate:
    repo_key: str
    repo: str
    branch: str = ""
    branch_end_sha: str = ""
    blame_since: str = ""
    total_commits: int = 0
    commit_categories: Counter[str] = field(default_factory=Counter)
    file_changes: int = 0
    additions: int = 0
    deletions: int = 0
    changed_lines: int = 0
    commit_classifications: int = 0
    llm_labeled_commit_classifications: int = 0
    commit_file_classifications: int = 0
    static_analysis_commits: int = 0
    static_finding_rows: int = 0
    static_introduced_findings: int = 0
    static_removed_findings: int = 0
    static_persistent_findings: int = 0
    supply_chain_analysis_commits: int = 0
    supply_chain_finding_rows: int = 0
    supply_chain_introduced_findings: int = 0
    supply_chain_removed_findings: int = 0
    supply_chain_persistent_findings: int = 0
    codebase_agent_share_snapshots: int = 0
    latest_total_living_lines: int = 0
    latest_ai_living_lines: int = 0
    latest_ai_living_line_share: str = ""
    latest_source_living_lines: int = 0
    latest_source_ai_living_lines: int = 0
    latest_source_ai_living_line_share: str = ""
    sonarqube_snapshot_scans: int = 0
    sonarqube_successful_scans: int = 0
    sonarqube_failed_scans: int = 0
    pr_summary_commits: int = 0
    pr_commits_with_pr: int = 0
    pr_commits_without_pr: int = 0
    pr_association_rows: int = 0
    pr_primary_rows: int = 0
    pr_conversation_comments: int = 0
    pr_review_comments: int = 0
    lifecycle_statuses: Counter[str] = field(default_factory=Counter)
    primary_intents: Counter[str] = field(default_factory=Counter)
    maintenance_classes: Counter[str] = field(default_factory=Counter)
    file_roles: Counter[str] = field(default_factory=Counter)


@dataclass
class CodebaseSourceSnapshotAggregate:
    repo_key: str
    repo: str
    branch: str = ""
    snapshot_index: str = ""
    snapshot_date: str = ""
    snapshot_sha: str = ""
    snapshot_commit_date: str = ""
    source_files: int = 0
    total_living_lines: int = 0
    ai_living_lines: int = 0
    human_living_lines: int = 0
    other_bot_living_lines: int = 0
    unknown_living_lines: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten webscrape JSONL outputs into analysis-friendly CSV tables."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing per-repo webscrape outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory where CSV files are written. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit export to a repo, either as owner/repo or owner__repo. Can be repeated.",
    )
    parser.add_argument(
        "--repo-file",
        "--repo-list",
        dest="repo_file",
        action="append",
        default=[],
        type=Path,
        help=(
            "Path to a text or CSV file containing repos to export. Text files use one "
            "owner/repo or owner__repo per line. CSV files may use a repo_slug, repo, "
            "repo_key, repository, repository_full_name, full_name, slug, repo_url, "
            "repository_url, or url column. Blank lines and lines starting with # are "
            "ignored. Can be repeated."
        ),
    )
    parser.add_argument(
        "--skip-line-lifecycle",
        action="store_true",
        help="Do not write line_lifecycle.csv or commit_lifecycle_summary.csv.",
    )
    parser.add_argument(
        "--csv-profile",
        choices=("full", "compact"),
        default="full",
        help=(
            "CSV schema profile. full keeps every export column; compact drops "
            "verbose/debug columns while preserving join keys and analysis dimensions. "
            "Default: full."
        ),
    )
    parser.add_argument(
        "--include-pr-analysis",
        action="store_true",
        help=(
            "Include PR association tables and PR aggregate columns. By default, "
            "PR analysis is ignored."
        ),
    )
    parser.add_argument(
        "--source-extension",
        action="append",
        default=[],
        help=(
            "Extension to treat as source code when exporting source-only codebase "
            "agent share tables. Can be repeated or comma-separated. Defaults to a "
            "built-in set of common source-code extensions."
        ),
    )
    return parser.parse_args()


def normalize_repo_filter(repo: str) -> str:
    value = str(repo or "").strip()
    if not value:
        return ""
    value = value.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if value.startswith("git@github.com:"):
        value = value.split(":", 1)[1]
    elif "api.github.com/repos/" in value:
        value = value.split("api.github.com/repos/", 1)[1]
    elif "github.com/" in value:
        value = value.split("github.com/", 1)[1]

    basename = Path(value).name
    if "__" in basename:
        return basename

    parts = [part for part in value.strip("/").split("/") if part]
    if len(parts) >= 2:
        return f"{parts[-2]}__{parts[-1]}"
    return value.replace("/", "__")


def strip_inline_comment(value: str) -> str:
    return value.split("#", 1)[0].strip()


def repo_entries_from_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"repo list file not found: {path}")

    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []

    first_fields = [field.strip().lower() for field in next(csv.reader([lines[0]]))]
    looks_like_csv = "," in lines[0] or any(field in REPO_FILE_COLUMNS for field in first_fields)
    if not looks_like_csv:
        return [
            entry
            for entry in (strip_inline_comment(line) for line in lines)
            if entry
        ]

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    fieldnames = reader.fieldnames or []
    fields_by_lower = {field.strip().lower(): field for field in fieldnames if field}
    repo_column = next(
        (fields_by_lower[column] for column in REPO_FILE_COLUMNS if column in fields_by_lower),
        None,
    )
    if repo_column is None and len(fieldnames) == 1:
        repo_column = fieldnames[0]
    if repo_column is None:
        columns = ", ".join(REPO_FILE_COLUMNS)
        raise ValueError(
            f"{path} looks like a CSV repo list but none of these columns were found: {columns}"
        )

    entries: list[str] = []
    for line_number, row in enumerate(reader, start=2):
        entry = str(row.get(repo_column) or "").strip()
        if not entry:
            print(
                f"Skipping empty repo entry in {path}:{line_number}",
                file=sys.stderr,
            )
            continue
        entries.append(entry)
    return entries


def repo_filters_from_args(args: argparse.Namespace) -> set[str] | None:
    repos = list(args.repo or [])
    for path in args.repo_file or []:
        repos.extend(repo_entries_from_file(path))
    normalized = {repo_key for repo in repos if (repo_key := normalize_repo_filter(repo))}
    return normalized or None


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized:
        raise ValueError("source extensions must not be empty")
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def normalize_source_extensions(values: list[str]) -> frozenset[str]:
    if not values:
        return DEFAULT_SOURCE_EXTENSIONS
    extensions: set[str] = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                extensions.add(normalize_extension(part))
    if not extensions:
        raise ValueError("--source-extension was provided but no extensions were parsed")
    return frozenset(extensions)


def discover_repo_dirs(input_dir: Path, repo_filters: set[str] | None) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"webscrape output directory not found: {input_dir}")
    discovery_files = {
        "all_commits.jsonl",
        CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE,
        CODEBASE_AGENT_SHARE_FILES_FILE,
        SONARQUBE_SNAPSHOT_SCANS_FILE,
    }
    repo_dirs = [
        path
        for path in input_dir.iterdir()
        if path.is_dir() and any((path / filename).exists() for filename in discovery_files)
    ]
    if repo_filters:
        repo_dirs = [path for path in repo_dirs if path.name in repo_filters]
    return sorted(repo_dirs, key=lambda path: path.name)


def columns_for_profile(
    table_key: str,
    columns: tuple[str, ...],
    csv_profile: str,
    include_pr_analysis: bool = False,
) -> tuple[str, ...]:
    if table_key == "repo_summary" and not include_pr_analysis:
        columns = tuple(
            column for column in columns if column not in REPO_SUMMARY_PR_COLUMNS
        )
    if csv_profile == "full":
        return columns
    drop_columns = COMPACT_DROP_COLUMNS.get(table_key, frozenset())
    return tuple(column for column in columns if column not in drop_columns)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON in {path}:{line_number}: {exc}", file=sys.stderr)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def safe_float(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}f}"


def compact_json(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def join_values(values: Any) -> str:
    if values in (None, ""):
        return ""
    if not isinstance(values, list):
        return str(values)
    parts: list[str] = []
    for value in values:
        if isinstance(value, (dict, list)):
            parts.append(compact_json(value))
        else:
            parts.append(str(value))
    return ";".join(parts)


def counter_string(counter: Counter[str]) -> str:
    return ";".join(
        f"{key}:{value}"
        for key, value in sorted(counter.items())
        if key and value
    )


def evidence_summary(evidence: Any) -> str:
    if not isinstance(evidence, list):
        return ""
    parts = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source = item.get("source") or ""
        rule = item.get("rule") or ""
        detail = item.get("detail") or ""
        label = ":".join(part for part in (source, rule) if part)
        parts.append(f"{label}={detail}" if label else str(detail))
    return ";".join(parts)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def update_first_date(
    current_value: str,
    current_datetime: datetime | None,
    candidate_value: str | None,
) -> tuple[str, datetime | None]:
    candidate_datetime = parse_datetime(candidate_value)
    if candidate_datetime is None:
        return current_value, current_datetime
    if current_datetime is None or candidate_datetime < current_datetime:
        return str(candidate_value), candidate_datetime
    return current_value, current_datetime


def line_total(statuses: Counter[str]) -> int:
    return sum(statuses.values())


def survival_rate(statuses: Counter[str]) -> str:
    total = line_total(statuses)
    if total == 0:
        return ""
    return safe_float(statuses["survived"] / total)


def repo_name_for(repo_dir: Path) -> str:
    for filename in (
        "all_commits.jsonl",
        CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE,
        CODEBASE_AGENT_SHARE_FILES_FILE,
        SONARQUBE_SNAPSHOT_SCANS_FILE,
    ):
        for row in read_jsonl(repo_dir / filename):
            repo = row.get("repo")
            if isinstance(repo, str) and repo:
                return repo
            break
    return repo_dir.name.replace("__", "/")


def commit_stats(commit: dict[str, Any]) -> tuple[int, int, int, int]:
    files = commit.get("files") or []
    if not isinstance(files, list):
        return 0, 0, 0, 0
    additions = sum(safe_int(file.get("additions")) for file in files if isinstance(file, dict))
    deletions = sum(safe_int(file.get("deletions")) for file in files if isinstance(file, dict))
    changes = sum(safe_int(file.get("changes")) for file in files if isinstance(file, dict))
    return len(files), additions, deletions, changes


def hunk_line_counts(file_change: dict[str, Any]) -> tuple[int, int]:
    added = 0
    deleted = 0
    for hunk in file_change.get("hunks") or []:
        if not isinstance(hunk, dict):
            continue
        added += len(hunk.get("added_lines") or [])
        deleted += len(hunk.get("deleted_lines") or [])
    return added, deleted


def commit_row(repo_key: str, repo: str, commit: dict[str, Any]) -> dict[str, Any]:
    files_changed, additions, deletions, changes = commit_stats(commit)
    coauthors = commit.get("coauthors") or []
    return {
        "repo_key": repo_key,
        "repo": commit.get("repo") or repo,
        "sha": commit.get("sha"),
        "date": commit.get("date"),
        "category": commit.get("category"),
        "labels": join_values(commit.get("labels") or []),
        "author_login": commit.get("author_login"),
        "author_type": commit.get("author_type"),
        "committer_login": commit.get("committer_login"),
        "committer_type": commit.get("committer_type"),
        "git_author_name": commit.get("git_author_name"),
        "git_author_email": commit.get("git_author_email"),
        "git_committer_name": commit.get("git_committer_name"),
        "git_committer_email": commit.get("git_committer_email"),
        "coauthor_names": join_values(
            [item.get("name") for item in coauthors if isinstance(item, dict)]
        ),
        "coauthor_emails": join_values(
            [item.get("email") for item in coauthors if isinstance(item, dict)]
        ),
        "parents": join_values(commit.get("parents") or []),
        "message_subject": commit.get("message_subject"),
        "message_body": commit.get("message_body"),
        "html_url": commit.get("html_url"),
        "api_url": commit.get("api_url"),
        "github_metadata_enriched": commit.get("github_metadata_enriched"),
        "files_changed": files_changed,
        "additions": additions,
        "deletions": deletions,
        "changed_lines": changes,
    }


def commit_file_row(repo_key: str, repo: str, file_change: dict[str, Any]) -> dict[str, Any]:
    added_lines, deleted_lines = hunk_line_counts(file_change)
    return {
        "repo_key": repo_key,
        "repo": repo,
        "commit_sha": file_change.get("commit_sha"),
        "commit_index": file_change.get("commit_index"),
        "commit_date": file_change.get("commit_date"),
        "commit_category": file_change.get("commit_category"),
        "commit_labels": join_values(file_change.get("commit_labels") or []),
        "filename": file_change.get("filename"),
        "previous_filename": file_change.get("previous_filename"),
        "status": file_change.get("status"),
        "additions": file_change.get("additions"),
        "deletions": file_change.get("deletions"),
        "changed_lines": file_change.get("changes"),
        "hunks_count": len(file_change.get("hunks") or []),
        "added_lines": added_lines,
        "deleted_lines": deleted_lines,
        "html_url": file_change.get("html_url"),
        "blob_url": file_change.get("blob_url"),
        "raw_url": file_change.get("raw_url"),
    }


def line_lifecycle_row(repo_key: str, repo: str, line: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "repo": repo,
        "origin_commit_sha": line.get("origin_commit_sha"),
        "origin_commit_date": line.get("origin_commit_date"),
        "origin_commit_category": line.get("origin_commit_category"),
        "origin_commit_labels": join_values(line.get("origin_commit_labels") or []),
        "origin_file": line.get("origin_file"),
        "origin_line": line.get("origin_line"),
        "current_file": line.get("current_file"),
        "current_line": line.get("current_line"),
        "status": line.get("status"),
        "line_content": line.get("line_content"),
        "terminal_commit_sha": line.get("terminal_commit_sha"),
        "terminal_commit_date": line.get("terminal_commit_date"),
        "terminal_commit_category": line.get("terminal_commit_category"),
        "terminal_commit_labels": join_values(line.get("terminal_commit_labels") or []),
        "terminal_reason": line.get("terminal_reason"),
        "terminal_line": line.get("terminal_line"),
        "terminal_commit_message_subject": line.get("terminal_commit_message_subject"),
        "terminal_commit_message_body": line.get("terminal_commit_message_body"),
        "terminal_commit_author_login": line.get("terminal_commit_author_login"),
        "terminal_commit_author_type": line.get("terminal_commit_author_type"),
        "terminal_commit_committer_login": line.get("terminal_commit_committer_login"),
        "terminal_commit_committer_type": line.get("terminal_commit_committer_type"),
        "terminal_git_author_name": line.get("terminal_git_author_name"),
        "terminal_git_author_email": line.get("terminal_git_author_email"),
        "terminal_git_committer_name": line.get("terminal_git_committer_name"),
        "terminal_git_committer_email": line.get("terminal_git_committer_email"),
        "terminal_github_metadata_enriched": line.get("terminal_github_metadata_enriched"),
        "origin_html_url": line.get("origin_html_url"),
    }


def commit_classification_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    stats = row.get("stats") or {}
    evidence = row.get("evidence") or []
    return {
        "repo_key": repo_key,
        "repo": row.get("repo") or repo,
        "sha": row.get("sha"),
        "date": row.get("date"),
        "input_commit_file": row.get("input_commit_file"),
        "is_origin_commit": row.get("is_origin_commit"),
        "is_observation_commit": row.get("is_observation_commit"),
        "labeled_by_llm": bool(row.get("labeled_by_llm")),
        "message_subject": row.get("message_subject"),
        "primary_operational_intent": row.get("primary_operational_intent"),
        "operational_intents": join_values(row.get("operational_intents") or []),
        "maintenance_class": row.get("maintenance_class"),
        "maintenance_classes": join_values(row.get("maintenance_classes") or []),
        "secondary_object_tags": join_values(row.get("secondary_object_tags") or []),
        "confidence": row.get("confidence"),
        "additions": stats.get("additions"),
        "deletions": stats.get("deletions"),
        "changed_lines": stats.get("changed_lines"),
        "files_changed": stats.get("files_changed"),
        "evidence_summary": evidence_summary(evidence),
        "evidence_json": compact_json(evidence),
    }


def commit_file_classification_row(
    repo_key: str,
    repo: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    evidence = row.get("evidence") or []
    return {
        "repo_key": repo_key,
        "repo": row.get("repo") or repo,
        "commit_sha": row.get("commit_sha"),
        "date": row.get("date"),
        "input_commit_file": row.get("input_commit_file"),
        "is_origin_commit": row.get("is_origin_commit"),
        "is_observation_commit": row.get("is_observation_commit"),
        "filename": row.get("filename"),
        "previous_filename": row.get("previous_filename"),
        "status": row.get("status"),
        "additions": row.get("additions"),
        "deletions": row.get("deletions"),
        "changed_lines": row.get("changed_lines"),
        "message_subject": row.get("message_subject"),
        "file_primary_role": row.get("file_primary_role"),
        "file_object_tags": join_values(row.get("file_object_tags") or []),
        "file_context_intents": join_values(row.get("file_context_intents") or []),
        "file_confidence": row.get("file_confidence"),
        "commit_primary_operational_intent": row.get("commit_primary_operational_intent"),
        "commit_operational_intents": join_values(row.get("commit_operational_intents") or []),
        "commit_maintenance_class": row.get("commit_maintenance_class"),
        "commit_maintenance_classes": join_values(row.get("commit_maintenance_classes") or []),
        "commit_confidence": row.get("commit_confidence"),
        "evidence_summary": evidence_summary(evidence),
        "evidence_json": compact_json(evidence),
    }


def csv_cell(value: Any) -> Any:
    if isinstance(value, list):
        return join_values(value)
    if isinstance(value, dict):
        return compact_json(value)
    return value


def projected_row(
    repo_key: str,
    repo: str,
    row: dict[str, Any],
    columns: tuple[str, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in columns:
        if column == "repo_key":
            result[column] = row.get("repo_key") or repo_key
        elif column == "repo":
            result[column] = row.get("repo") or repo
        else:
            result[column] = csv_cell(row.get(column))
    return result


def static_finding_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, STATIC_FINDINGS_COLUMNS)


def static_summary_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, STATIC_SUMMARY_COLUMNS)


def supply_chain_finding_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, SUPPLY_CHAIN_FINDINGS_COLUMNS)


def supply_chain_summary_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, SUPPLY_CHAIN_SUMMARY_COLUMNS)


def commit_pr_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, COMMIT_PRS_COLUMNS)


def commit_pr_summary_row(repo_key: str, repo: str, row: dict[str, Any]) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, COMMIT_PR_SUMMARY_COLUMNS)


def metric_value(metrics: Any, metric_name: str) -> Any:
    if not isinstance(metrics, dict):
        return ""
    return csv_cell(metrics.get(metric_name))


def unexpected_metrics_json(metrics: Any) -> str:
    if not isinstance(metrics, dict):
        return ""
    extra = {
        key: value
        for key, value in metrics.items()
        if str(key) not in SONARQUBE_DEFAULT_METRICS
    }
    return compact_json(extra)


def sonarqube_snapshot_scan_row(
    repo_key: str,
    repo: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    result = projected_row(repo_key, repo, row, SONARQUBE_SNAPSHOT_SCAN_COLUMNS)
    result.update(
        {
            "issue_counts_by_severity": compact_json(
                row.get("issue_counts_by_severity") or {}
            ),
            "issue_counts_by_type": compact_json(row.get("issue_counts_by_type") or {}),
            "issue_counts_by_status": compact_json(
                row.get("issue_counts_by_status") or {}
            ),
            "issue_counts_by_resolution": compact_json(
                row.get("issue_counts_by_resolution") or {}
            ),
            "metrics_json": unexpected_metrics_json(metrics),
        }
    )
    for metric in SONARQUBE_DEFAULT_METRICS:
        result[f"metric_{metric}"] = metric_value(metrics, metric)
    return result


def codebase_agent_share_snapshot_row(
    repo_key: str,
    repo: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    return projected_row(repo_key, repo, row, CODEBASE_AGENT_SHARE_SNAPSHOT_COLUMNS)


def line_share(lines: int, total: int) -> str:
    if total <= 0:
        return ""
    return safe_float(lines / total)


def codebase_agent_share_source_snapshot_row(
    aggregate: CodebaseSourceSnapshotAggregate,
    source_extensions: frozenset[str],
) -> dict[str, Any]:
    total = aggregate.total_living_lines
    return {
        "repo_key": aggregate.repo_key,
        "repo": aggregate.repo,
        "branch": aggregate.branch,
        "snapshot_index": aggregate.snapshot_index,
        "snapshot_date": aggregate.snapshot_date,
        "snapshot_sha": aggregate.snapshot_sha,
        "snapshot_commit_date": aggregate.snapshot_commit_date,
        "source_extensions": join_values(sorted(source_extensions)),
        "source_files": aggregate.source_files,
        "source_total_living_lines": total,
        "source_ai_living_lines": aggregate.ai_living_lines,
        "source_ai_living_line_share": line_share(aggregate.ai_living_lines, total),
        "source_human_living_lines": aggregate.human_living_lines,
        "source_human_living_line_share": line_share(aggregate.human_living_lines, total),
        "source_other_bot_living_lines": aggregate.other_bot_living_lines,
        "source_other_bot_living_line_share": line_share(
            aggregate.other_bot_living_lines,
            total,
        ),
        "source_unknown_living_lines": aggregate.unknown_living_lines,
        "source_unknown_living_line_share": line_share(
            aggregate.unknown_living_lines,
            total,
        ),
    }


def source_snapshot_key(row: dict[str, Any]) -> tuple[str, str]:
    snapshot_index = str(row.get("snapshot_index") or "")
    snapshot_sha = str(row.get("snapshot_sha") or "")
    return snapshot_index, snapshot_sha


def update_codebase_source_snapshot_aggregate(
    aggregate: CodebaseSourceSnapshotAggregate,
    row: dict[str, Any],
) -> None:
    if not aggregate.branch:
        aggregate.branch = str(row.get("branch") or "")
    if not aggregate.snapshot_index:
        aggregate.snapshot_index = str(row.get("snapshot_index") or "")
    if not aggregate.snapshot_date:
        aggregate.snapshot_date = str(row.get("snapshot_date") or "")
    if not aggregate.snapshot_sha:
        aggregate.snapshot_sha = str(row.get("snapshot_sha") or "")
    if not aggregate.snapshot_commit_date:
        aggregate.snapshot_commit_date = str(row.get("snapshot_commit_date") or "")

    aggregate.source_files += 1
    aggregate.total_living_lines += safe_int(row.get("total_living_lines"))
    aggregate.ai_living_lines += safe_int(row.get("ai_living_lines"))
    aggregate.human_living_lines += safe_int(row.get("human_living_lines"))
    aggregate.other_bot_living_lines += safe_int(row.get("other_bot_living_lines"))
    aggregate.unknown_living_lines += safe_int(row.get("unknown_living_lines"))


def update_lifecycle_aggregate(
    aggregate: CommitLifecycleAggregate,
    line: dict[str, Any],
    terminal_intent_by_sha: dict[str, str],
) -> None:
    status = str(line.get("status") or "unknown")
    aggregate.statuses[status] += 1
    if not aggregate.origin_commit_date:
        aggregate.origin_commit_date = str(line.get("origin_commit_date") or "")
    if not aggregate.origin_commit_category:
        aggregate.origin_commit_category = str(line.get("origin_commit_category") or "")
    if not aggregate.origin_commit_labels:
        aggregate.origin_commit_labels = [
            str(label) for label in line.get("origin_commit_labels") or []
        ]
    origin_file = line.get("origin_file")
    if origin_file:
        aggregate.origin_files.add(str(origin_file))

    terminal_sha = line.get("terminal_commit_sha")
    if not terminal_sha:
        return
    terminal_sha = str(terminal_sha)
    aggregate.terminal_commits[terminal_sha] += 1
    terminal_category = line.get("terminal_commit_category")
    if terminal_category:
        aggregate.terminal_categories[str(terminal_category)] += 1
    terminal_reason = line.get("terminal_reason")
    if terminal_reason:
        aggregate.terminal_reasons[str(terminal_reason)] += 1
    terminal_intent = terminal_intent_by_sha.get(terminal_sha)
    if terminal_intent:
        aggregate.terminal_intents[terminal_intent] += 1
    aggregate.first_terminal_commit_date, aggregate.first_terminal_datetime = update_first_date(
        aggregate.first_terminal_commit_date,
        aggregate.first_terminal_datetime,
        line.get("terminal_commit_date"),
    )


def commit_lifecycle_summary_row(
    aggregate: CommitLifecycleAggregate,
) -> dict[str, Any]:
    total = line_total(aggregate.statuses)
    terminal_total = aggregate.statuses["modified"] + aggregate.statuses["deleted"]
    return {
        "repo_key": aggregate.repo_key,
        "repo": aggregate.repo,
        "origin_commit_sha": aggregate.origin_commit_sha,
        "origin_commit_date": aggregate.origin_commit_date,
        "origin_commit_category": aggregate.origin_commit_category,
        "origin_commit_labels": join_values(aggregate.origin_commit_labels),
        "total_lines": total,
        "survived_lines": aggregate.statuses["survived"],
        "modified_lines": aggregate.statuses["modified"],
        "deleted_lines": aggregate.statuses["deleted"],
        "terminated_lines": terminal_total,
        "survival_rate": safe_float(aggregate.statuses["survived"] / total) if total else "",
        "origin_files": join_values(sorted(aggregate.origin_files)),
        "terminal_commit_count": len(aggregate.terminal_commits),
        "terminal_commits": join_values(sorted(aggregate.terminal_commits)),
        "terminal_category_counts": counter_string(aggregate.terminal_categories),
        "terminal_reason_counts": counter_string(aggregate.terminal_reasons),
        "terminal_intent_counts": counter_string(aggregate.terminal_intents),
        "first_terminal_commit_date": aggregate.first_terminal_commit_date,
    }


def repo_summary_row(aggregate: RepoAggregate) -> dict[str, Any]:
    return {
        "repo_key": aggregate.repo_key,
        "repo": aggregate.repo,
        "branch": aggregate.branch,
        "branch_end_sha": aggregate.branch_end_sha,
        "blame_since": aggregate.blame_since,
        "total_commits": aggregate.total_commits,
        "ai_coauthored_commits": aggregate.commit_categories["ai_coauthored"],
        "not_ai_coauthored_commits": aggregate.commit_categories["not_ai_coauthored"],
        "other_bot_commits": aggregate.commit_categories["other_bot"],
        "unknown_commits": aggregate.commit_categories["unknown"],
        "file_changes": aggregate.file_changes,
        "additions": aggregate.additions,
        "deletions": aggregate.deletions,
        "changed_lines": aggregate.changed_lines,
        "commit_classifications": aggregate.commit_classifications,
        "llm_labeled_commit_classifications": (
            aggregate.llm_labeled_commit_classifications
        ),
        "commit_file_classifications": aggregate.commit_file_classifications,
        "static_analysis_commits": aggregate.static_analysis_commits,
        "static_finding_rows": aggregate.static_finding_rows,
        "static_introduced_findings": aggregate.static_introduced_findings,
        "static_removed_findings": aggregate.static_removed_findings,
        "static_persistent_findings": aggregate.static_persistent_findings,
        "supply_chain_analysis_commits": aggregate.supply_chain_analysis_commits,
        "supply_chain_finding_rows": aggregate.supply_chain_finding_rows,
        "supply_chain_introduced_findings": aggregate.supply_chain_introduced_findings,
        "supply_chain_removed_findings": aggregate.supply_chain_removed_findings,
        "supply_chain_persistent_findings": aggregate.supply_chain_persistent_findings,
        "codebase_agent_share_snapshots": aggregate.codebase_agent_share_snapshots,
        "latest_total_living_lines": aggregate.latest_total_living_lines,
        "latest_ai_living_lines": aggregate.latest_ai_living_lines,
        "latest_ai_living_line_share": aggregate.latest_ai_living_line_share,
        "latest_source_living_lines": aggregate.latest_source_living_lines,
        "latest_source_ai_living_lines": aggregate.latest_source_ai_living_lines,
        "latest_source_ai_living_line_share": aggregate.latest_source_ai_living_line_share,
        "sonarqube_snapshot_scans": aggregate.sonarqube_snapshot_scans,
        "sonarqube_successful_scans": aggregate.sonarqube_successful_scans,
        "sonarqube_failed_scans": aggregate.sonarqube_failed_scans,
        "pr_summary_commits": aggregate.pr_summary_commits,
        "pr_commits_with_pr": aggregate.pr_commits_with_pr,
        "pr_commits_without_pr": aggregate.pr_commits_without_pr,
        "pr_association_rows": aggregate.pr_association_rows,
        "pr_primary_rows": aggregate.pr_primary_rows,
        "pr_conversation_comments": aggregate.pr_conversation_comments,
        "pr_review_comments": aggregate.pr_review_comments,
        "lifecycle_total_lines": line_total(aggregate.lifecycle_statuses),
        "survived_lines": aggregate.lifecycle_statuses["survived"],
        "modified_lines": aggregate.lifecycle_statuses["modified"],
        "deleted_lines": aggregate.lifecycle_statuses["deleted"],
        "survival_rate": survival_rate(aggregate.lifecycle_statuses),
        "primary_intent_counts": counter_string(aggregate.primary_intents),
        "maintenance_class_counts": counter_string(aggregate.maintenance_classes),
        "file_role_counts": counter_string(aggregate.file_roles),
    }


def open_writer(output_dir: Path, filename: str, columns: tuple[str, ...]):
    path = output_dir / filename
    handle = path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    return handle, writer


def export_repo(
    repo_dir: Path,
    writers: dict[str, csv.DictWriter],
    skip_line_lifecycle: bool,
    include_pr_analysis: bool,
    source_extensions: frozenset[str],
) -> RepoAggregate:
    repo_key = repo_dir.name
    repo = repo_name_for(repo_dir)
    summary = read_json(repo_dir / "summary.json")
    repo_summary = RepoAggregate(
        repo_key=repo_key,
        repo=repo,
        branch=str(summary.get("branch") or ""),
        branch_end_sha=str(summary.get("branch_end_sha") or ""),
        blame_since=str(summary.get("blame_since") or ""),
    )
    terminal_intent_by_sha: dict[str, str] = {}

    for commit in read_jsonl(repo_dir / "all_commits.jsonl"):
        writers["commits"].writerow(commit_row(repo_key, repo, commit))
        repo_summary.total_commits += 1
        repo_summary.commit_categories[str(commit.get("category") or "unknown")] += 1

    for file_change in read_jsonl(repo_dir / "commit_file_changes.jsonl"):
        writers["commit_files"].writerow(commit_file_row(repo_key, repo, file_change))
        repo_summary.file_changes += 1
        repo_summary.additions += safe_int(file_change.get("additions"))
        repo_summary.deletions += safe_int(file_change.get("deletions"))
        repo_summary.changed_lines += safe_int(file_change.get("changes"))

    for row in read_jsonl(repo_dir / COMMIT_CLASSIFICATION_FILE):
        writers["commit_classifications"].writerow(
            commit_classification_row(repo_key, repo, row)
        )
        sha = row.get("sha")
        primary_intent = str(row.get("primary_operational_intent") or "unknown")
        if isinstance(sha, str) and sha:
            terminal_intent_by_sha[sha] = primary_intent
        repo_summary.commit_classifications += 1
        if row.get("labeled_by_llm"):
            repo_summary.llm_labeled_commit_classifications += 1
        repo_summary.primary_intents[primary_intent] += 1
        repo_summary.maintenance_classes[str(row.get("maintenance_class") or "unknown")] += 1

    for row in read_jsonl(repo_dir / COMMIT_FILE_CLASSIFICATION_FILE):
        writers["commit_file_classifications"].writerow(
            commit_file_classification_row(repo_key, repo, row)
        )
        repo_summary.commit_file_classifications += 1
        repo_summary.file_roles[str(row.get("file_primary_role") or "unknown")] += 1

    for row in read_jsonl(repo_dir / STATIC_FINDINGS_FILE):
        writers["static_findings"].writerow(static_finding_row(repo_key, repo, row))
        repo_summary.static_finding_rows += 1

    for row in read_jsonl(repo_dir / STATIC_SUMMARY_FILE):
        writers["static_summary"].writerow(static_summary_row(repo_key, repo, row))
        repo_summary.static_analysis_commits += 1
        repo_summary.static_introduced_findings += safe_int(row.get("introduced_findings"))
        repo_summary.static_removed_findings += safe_int(row.get("removed_findings"))
        repo_summary.static_persistent_findings += safe_int(row.get("persistent_findings"))

    for row in read_jsonl(repo_dir / SUPPLY_CHAIN_FINDINGS_FILE):
        writers["supply_chain_findings"].writerow(
            supply_chain_finding_row(repo_key, repo, row)
        )
        repo_summary.supply_chain_finding_rows += 1

    for row in read_jsonl(repo_dir / SUPPLY_CHAIN_SUMMARY_FILE):
        writers["supply_chain_summary"].writerow(
            supply_chain_summary_row(repo_key, repo, row)
        )
        repo_summary.supply_chain_analysis_commits += 1
        repo_summary.supply_chain_introduced_findings += safe_int(
            row.get("introduced_findings")
        )
        repo_summary.supply_chain_removed_findings += safe_int(row.get("removed_findings"))
        repo_summary.supply_chain_persistent_findings += safe_int(
            row.get("persistent_findings")
        )

    for row in read_jsonl(repo_dir / CODEBASE_AGENT_SHARE_SNAPSHOTS_FILE):
        writers["codebase_agent_share_snapshots"].writerow(
            codebase_agent_share_snapshot_row(repo_key, repo, row)
        )
        repo_summary.codebase_agent_share_snapshots += 1
        repo_summary.latest_total_living_lines = safe_int(row.get("total_living_lines"))
        repo_summary.latest_ai_living_lines = safe_int(row.get("ai_living_lines"))
        repo_summary.latest_ai_living_line_share = str(
            row.get("ai_living_line_share") or ""
        )

    source_snapshot_aggregates: dict[
        tuple[str, str],
        CodebaseSourceSnapshotAggregate,
    ] = {}
    for row in read_jsonl(repo_dir / CODEBASE_AGENT_SHARE_FILES_FILE):
        extension = str(row.get("extension") or "").lower()
        if extension not in source_extensions:
            continue
        key = source_snapshot_key(row)
        aggregate = source_snapshot_aggregates.get(key)
        if aggregate is None:
            aggregate = CodebaseSourceSnapshotAggregate(repo_key=repo_key, repo=repo)
            source_snapshot_aggregates[key] = aggregate
        update_codebase_source_snapshot_aggregate(aggregate, row)

    for aggregate in sorted(
        source_snapshot_aggregates.values(),
        key=lambda item: (item.snapshot_date, item.snapshot_index, item.snapshot_sha),
    ):
        writers["codebase_agent_share_source_snapshots"].writerow(
            codebase_agent_share_source_snapshot_row(aggregate, source_extensions)
        )
        repo_summary.latest_source_living_lines = aggregate.total_living_lines
        repo_summary.latest_source_ai_living_lines = aggregate.ai_living_lines
        repo_summary.latest_source_ai_living_line_share = line_share(
            aggregate.ai_living_lines,
            aggregate.total_living_lines,
        )

    for row in read_jsonl(repo_dir / SONARQUBE_SNAPSHOT_SCANS_FILE):
        writers["sonarqube_snapshot_scans"].writerow(
            sonarqube_snapshot_scan_row(repo_key, repo, row)
        )
        repo_summary.sonarqube_snapshot_scans += 1
        scan_status = str(row.get("scan_status") or "")
        if scan_status in {"ok", "copied_repeated_snapshot", "dry_run"}:
            repo_summary.sonarqube_successful_scans += 1
        else:
            repo_summary.sonarqube_failed_scans += 1

    if include_pr_analysis:
        for row in read_jsonl(repo_dir / COMMIT_PR_SUMMARY_FILE):
            writers["commit_pr_summary"].writerow(
                commit_pr_summary_row(repo_key, repo, row)
            )
            repo_summary.pr_summary_commits += 1
            if truthy(row.get("has_pr")):
                repo_summary.pr_commits_with_pr += 1
            else:
                repo_summary.pr_commits_without_pr += 1

        for row in read_jsonl(repo_dir / COMMIT_PRS_FILE):
            writers["commit_prs"].writerow(commit_pr_row(repo_key, repo, row))
            repo_summary.pr_association_rows += 1
            if truthy(row.get("has_pr")) and truthy(row.get("is_primary_pr")):
                repo_summary.pr_primary_rows += 1
                repo_summary.pr_conversation_comments += safe_int(row.get("comments"))
                repo_summary.pr_review_comments += safe_int(row.get("review_comments"))

    if skip_line_lifecycle:
        for status, count in (summary.get("line_lifecycle") or {}).items():
            repo_summary.lifecycle_statuses[str(status)] += safe_int(count)
        return repo_summary

    commit_lifecycle: dict[str, CommitLifecycleAggregate] = {}
    for line in read_jsonl(repo_dir / "line_lifecycle.jsonl"):
        writers["line_lifecycle"].writerow(line_lifecycle_row(repo_key, repo, line))
        status = str(line.get("status") or "unknown")
        repo_summary.lifecycle_statuses[status] += 1
        origin_sha = line.get("origin_commit_sha")
        if not origin_sha:
            continue
        origin_sha = str(origin_sha)
        aggregate = commit_lifecycle.get(origin_sha)
        if aggregate is None:
            aggregate = CommitLifecycleAggregate(
                repo_key=repo_key,
                repo=repo,
                origin_commit_sha=origin_sha,
            )
            commit_lifecycle[origin_sha] = aggregate
        update_lifecycle_aggregate(aggregate, line, terminal_intent_by_sha)

    for aggregate in sorted(
        commit_lifecycle.values(),
        key=lambda item: (item.origin_commit_date, item.origin_commit_sha),
    ):
        writers["commit_lifecycle_summary"].writerow(
            commit_lifecycle_summary_row(aggregate)
        )

    return repo_summary


def main() -> None:
    args = parse_args()
    repo_filters = repo_filters_from_args(args)
    source_extensions = normalize_source_extensions(args.source_extension)
    repo_dirs = discover_repo_dirs(args.input_dir, repo_filters)
    if not repo_dirs:
        requested = f" matching {sorted(repo_filters)}" if repo_filters else ""
        raise SystemExit(f"No webscrape result directories found in {args.input_dir}{requested}.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    handles = []
    writers: dict[str, csv.DictWriter] = {}
    full_specs = {
        "commits": ("commits.csv", COMMIT_COLUMNS),
        "commit_files": ("commit_files.csv", COMMIT_FILE_COLUMNS),
        "commit_classifications": (
            "commit_classifications.csv",
            COMMIT_CLASSIFICATION_COLUMNS,
        ),
        "commit_file_classifications": (
            "commit_file_classifications.csv",
            COMMIT_FILE_CLASSIFICATION_COLUMNS,
        ),
        "static_findings": ("static_findings.csv", STATIC_FINDINGS_COLUMNS),
        "static_summary": ("static_analysis_summary.csv", STATIC_SUMMARY_COLUMNS),
        "supply_chain_findings": (
            "supply_chain_findings.csv",
            SUPPLY_CHAIN_FINDINGS_COLUMNS,
        ),
        "supply_chain_summary": (
            "supply_chain_summary.csv",
            SUPPLY_CHAIN_SUMMARY_COLUMNS,
        ),
        "codebase_agent_share_snapshots": (
            "codebase_agent_share_snapshots.csv",
            CODEBASE_AGENT_SHARE_SNAPSHOT_COLUMNS,
        ),
        "codebase_agent_share_source_snapshots": (
            "codebase_agent_share_source_snapshots.csv",
            CODEBASE_AGENT_SHARE_SOURCE_SNAPSHOT_COLUMNS,
        ),
        "sonarqube_snapshot_scans": (
            "sonarqube_snapshot_scans.csv",
            SONARQUBE_SNAPSHOT_SCAN_COLUMNS,
        ),
        "repo_summary": ("repo_summary.csv", REPO_SUMMARY_COLUMNS),
    }
    if args.include_pr_analysis:
        full_specs["commit_prs"] = ("commit_prs.csv", COMMIT_PRS_COLUMNS)
        full_specs["commit_pr_summary"] = (
            "commit_pr_summary.csv",
            COMMIT_PR_SUMMARY_COLUMNS,
        )
    if not args.skip_line_lifecycle:
        full_specs["line_lifecycle"] = ("line_lifecycle.csv", LINE_LIFECYCLE_COLUMNS)
        full_specs["commit_lifecycle_summary"] = (
            "commit_lifecycle_summary.csv",
            COMMIT_LIFECYCLE_SUMMARY_COLUMNS,
        )
    specs = {
        key: (
            filename,
            columns_for_profile(
                key,
                columns,
                args.csv_profile,
                include_pr_analysis=args.include_pr_analysis,
            ),
        )
        for key, (filename, columns) in full_specs.items()
    }

    try:
        for key, (filename, columns) in specs.items():
            handle, writer = open_writer(args.output_dir, filename, columns)
            handles.append(handle)
            writers[key] = writer

        for repo_dir in repo_dirs:
            print(f"exporting {repo_dir.name}", flush=True)
            repo_summary = export_repo(
                repo_dir,
                writers,
                args.skip_line_lifecycle,
                args.include_pr_analysis,
                source_extensions,
            )
            writers["repo_summary"].writerow(repo_summary_row(repo_summary))
    finally:
        for handle in handles:
            handle.close()

    print(f"wrote CSV tables to {args.output_dir}")


if __name__ == "__main__":
    main()
