from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output")
DEFAULT_ARTIFACT_EXTENSIONS = (
    ".csv",
    ".json",
    ".jsonl",
    ".lock",
    ".map",
    ".svg",
    ".xml",
    ".yaml",
    ".yml",
)


@dataclass
class RepoAnalysis:
    repo_key: str
    total_commits: int = 0
    commit_categories: Counter[str] = field(default_factory=Counter)
    commit_labels: Counter[str] = field(default_factory=Counter)
    authors: set[str] = field(default_factory=set)
    first_commit_date: str | None = None
    last_commit_date: str | None = None
    file_changes: int = 0
    additions: int = 0
    deletions: int = 0
    changed_files: Counter[str] = field(default_factory=Counter)
    changed_extensions: Counter[str] = field(default_factory=Counter)
    line_statuses: Counter[str] = field(default_factory=Counter)
    line_statuses_by_category: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    line_statuses_by_label: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    line_statuses_by_commit: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    commit_category_by_sha: dict[str, str] = field(default_factory=dict)
    commit_labels_by_sha: dict[str, list[str]] = field(default_factory=dict)
    terminal_files: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class FileFilter:
    included_extensions: frozenset[str] = frozenset()
    excluded_extensions: frozenset[str] = frozenset()
    exclude_test_files: bool = False

    def includes(self, path: str | None) -> bool:
        if not path:
            return not self.included_extensions
        if self.exclude_test_files and is_test_file(path):
            return False
        extension = extension_for(path)
        if self.included_extensions and extension not in self.included_extensions:
            return False
        return extension not in self.excluded_extensions


def read_jsonl(path: Path):
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


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_repo_filter(repo: str) -> str:
    return repo.replace("/", "__")


def discover_repo_dirs(input_dir: Path, repo_filters: set[str] | None) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"webscrape output directory not found: {input_dir}")

    repo_dirs = [
        path
        for path in input_dir.iterdir()
        if path.is_dir() and (path / "all_commits.jsonl").exists()
    ]
    if repo_filters:
        repo_dirs = [path for path in repo_dirs if path.name in repo_filters]
    return sorted(repo_dirs, key=lambda path: path.name)


def add_date(repo: RepoAnalysis, date: str | None) -> None:
    if not date:
        return
    if repo.first_commit_date is None or date < repo.first_commit_date:
        repo.first_commit_date = date
    if repo.last_commit_date is None or date > repo.last_commit_date:
        repo.last_commit_date = date


def extension_for(path: str | None) -> str:
    if not path:
        return "(unknown)"
    suffix = Path(path).suffix.lower()
    return suffix or "(none)"


def is_test_file(path: str) -> bool:
    return Path(path).name.startswith("test_")


def normalize_extension(extension: str) -> str:
    extension = extension.strip().lower()
    if not extension:
        raise ValueError("extensions must not be empty")
    if extension in {"(none)", "(unknown)"}:
        return extension
    return extension if extension.startswith(".") else f".{extension}"


def build_file_filter(args: argparse.Namespace) -> FileFilter:
    included_extensions = set(args.include_extension)
    excluded_extensions = set()
    if args.exclude_artifact_files:
        excluded_extensions.update(DEFAULT_ARTIFACT_EXTENSIONS)
    for extension in args.exclude_extension:
        excluded_extensions.add(extension)
    return FileFilter(
        frozenset(included_extensions),
        frozenset(excluded_extensions),
        args.exclude_test_files,
    )


def analyze_repo(repo_dir: Path, file_filter: FileFilter) -> RepoAnalysis:
    repo = RepoAnalysis(repo_key=repo_dir.name)

    for commit in read_jsonl(repo_dir / "all_commits.jsonl"):
        repo.total_commits += 1
        category = commit.get("category") or "unknown"
        repo.commit_categories[category] += 1
        add_date(repo, commit.get("date"))

        author = commit.get("author_login") or commit.get("git_author_email") or commit.get("git_author_name")
        if author:
            repo.authors.add(str(author))

        labels = commit.get("labels") or ["none"]
        for label in labels:
            repo.commit_labels[str(label)] += 1

    for file_change in read_jsonl(repo_dir / "commit_file_changes.jsonl"):
        filename = file_change.get("filename") or "(unknown)"
        if not file_filter.includes(filename):
            continue
        repo.file_changes += 1
        repo.additions += safe_int(file_change.get("additions"))
        repo.deletions += safe_int(file_change.get("deletions"))
        repo.changed_files[filename] += 1
        repo.changed_extensions[extension_for(filename)] += 1

    for line in read_jsonl(repo_dir / "line_lifecycle.jsonl"):
        origin_file = line.get("origin_file") or "(unknown)"
        current_file = line.get("current_file")
        if not file_filter.includes(origin_file):
            continue
        if current_file is not None and not file_filter.includes(current_file):
            continue
        status = line.get("status") or "unknown"
        sha = line.get("origin_commit_sha") or "(unknown)"
        category = line.get("origin_commit_category") or "unknown"
        labels = line.get("origin_commit_labels") or ["none"]

        repo.line_statuses[status] += 1
        repo.line_statuses_by_category[category][status] += 1
        repo.line_statuses_by_commit[sha][status] += 1
        repo.commit_category_by_sha[sha] = category
        repo.commit_labels_by_sha[sha] = [str(label) for label in labels]
        for label in labels:
            repo.line_statuses_by_label[str(label)][status] += 1

        if status in {"modified", "deleted"}:
            terminal_file = line.get("current_file") or line.get("origin_file") or "(unknown)"
            repo.terminal_files[terminal_file] += 1

    return repo


def pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.0%"
    return f"{100 * numerator / denominator:.1f}%"


def line_total(counter: Counter[str]) -> int:
    return sum(counter.values())


def is_changed_later(statuses: Counter[str]) -> bool:
    return statuses["modified"] + statuses["deleted"] > 0


def changed_commit_statuses(repo: RepoAnalysis) -> tuple[Counter[str], int]:
    statuses = Counter()
    commit_count = 0
    for commit_statuses in repo.line_statuses_by_commit.values():
        if not is_changed_later(commit_statuses):
            continue
        statuses.update(commit_statuses)
        commit_count += 1
    return statuses, commit_count


def untouched_commit_count(repo: RepoAnalysis) -> int:
    return sum(
        1
        for commit_statuses in repo.line_statuses_by_commit.values()
        if not is_changed_later(commit_statuses)
    )


def changed_commit_statuses_by_group(
    repo: RepoAnalysis,
    group_field: str,
) -> dict[str, tuple[Counter[str], int]]:
    grouped: dict[str, tuple[Counter[str], int]] = defaultdict(lambda: (Counter(), 0))
    for sha, commit_statuses in repo.line_statuses_by_commit.items():
        if not is_changed_later(commit_statuses):
            continue
        if group_field == "category":
            group_names = [repo.commit_category_by_sha.get(sha, "unknown")]
        elif group_field == "label":
            group_names = repo.commit_labels_by_sha.get(sha) or ["none"]
        else:
            raise ValueError(f"unsupported changed commit group field: {group_field}")

        for group_name in group_names:
            statuses, count = grouped[str(group_name)]
            statuses.update(commit_statuses)
            grouped[str(group_name)] = (statuses, count + 1)
    return grouped


def untouched_commit_counts_by_group(
    repo: RepoAnalysis,
    group_field: str,
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for sha, commit_statuses in repo.line_statuses_by_commit.items():
        if is_changed_later(commit_statuses):
            continue
        if group_field == "category":
            group_names = [repo.commit_category_by_sha.get(sha, "unknown")]
        elif group_field == "label":
            group_names = repo.commit_labels_by_sha.get(sha) or ["none"]
        else:
            raise ValueError(f"unsupported untouched commit group field: {group_field}")

        for group_name in group_names:
            counts[str(group_name)] += 1
    return counts


def changed_commit_survival_rate(statuses: Counter[str]) -> str:
    return pct(statuses["survived"], line_total(statuses))


def row_for_repo_summary(repo: RepoAnalysis) -> dict[str, Any]:
    tracked_lines = line_total(repo.line_statuses)
    terminal_lines = repo.line_statuses["modified"] + repo.line_statuses["deleted"]
    changed_statuses, changed_commits = changed_commit_statuses(repo)
    return {
        "repo": repo.repo_key,
        "commits": repo.total_commits,
        "ai_commits": repo.commit_categories["ai_coauthored"],
        "other_bot_commits": repo.commit_categories["other_bot"],
        "non_ai_commits": repo.commit_categories["not_ai_coauthored"],
        "authors": len(repo.authors),
        "first_commit_date": repo.first_commit_date or "",
        "last_commit_date": repo.last_commit_date or "",
        "file_changes": repo.file_changes,
        "additions": repo.additions,
        "deletions": repo.deletions,
        "tracked_added_lines": tracked_lines,
        "survived_lines": repo.line_statuses["survived"],
        "modified_lines": repo.line_statuses["modified"],
        "deleted_lines": repo.line_statuses["deleted"],
        "survival_rate": pct(repo.line_statuses["survived"], tracked_lines),
        "terminal_rate": pct(terminal_lines, tracked_lines),
        "changed_commits": changed_commits,
        "untouched_commits": untouched_commit_count(repo),
        "changed_commit_tracked_added_lines": line_total(changed_statuses),
        "changed_commit_survival_rate": changed_commit_survival_rate(changed_statuses),
    }


def rows_for_line_statuses(
    repo: RepoAnalysis,
    grouped: dict[str, Counter[str]],
    group_field: str,
) -> list[dict[str, Any]]:
    rows = []
    changed_grouped = changed_commit_statuses_by_group(repo, group_field)
    untouched_grouped = untouched_commit_counts_by_group(repo, group_field)
    for group_name, statuses in sorted(grouped.items()):
        total = line_total(statuses)
        terminal = statuses["modified"] + statuses["deleted"]
        changed_statuses, changed_commits = changed_grouped.get(group_name, (Counter(), 0))
        rows.append(
            {
                "repo": repo.repo_key,
                group_field: group_name,
                "tracked_added_lines": total,
                "survived_lines": statuses["survived"],
                "modified_lines": statuses["modified"],
                "deleted_lines": statuses["deleted"],
                "survival_rate": pct(statuses["survived"], total),
                "terminal_rate": pct(terminal, total),
                "changed_commits": changed_commits,
                "untouched_commits": untouched_grouped[group_name],
                "changed_commit_tracked_added_lines": line_total(changed_statuses),
                "changed_commit_survival_rate": changed_commit_survival_rate(changed_statuses),
            }
        )
    return rows


def rows_for_top_counter(
    repo: RepoAnalysis,
    counter: Counter[str],
    name_field: str,
    count_field: str,
    limit: int,
) -> list[dict[str, Any]]:
    return [
        {"repo": repo.repo_key, name_field: name, count_field: count}
        for name, count in counter.most_common(limit)
    ]


def format_value(value: Any) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def print_table(
    title: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    limit: int | None = None,
) -> None:
    print(f"\n{title}")
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        print("(no rows)")
        return

    widths = {
        column: max(len(column), *(len(format_value(row.get(column, ""))) for row in rows))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(format_value(row.get(column, "")).ljust(widths[column]) for column in columns))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze GitHub webscrape commit and line lifecycle outputs."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing webscrape per-repo outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit analysis to a repo, either as owner/repo or owner__repo. Can be repeated.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of top files/extensions to show per repo. Default: 10",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for CSV analysis outputs. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--no-write-csv",
        action="store_true",
        help="Only print tables; do not write CSV outputs.",
    )
    parser.add_argument(
        "--exclude-artifact-files",
        action="store_true",
        help=(
            "Exclude common generated/data artifact extensions from file and line "
            f"analysis: {', '.join(DEFAULT_ARTIFACT_EXTENSIONS)}."
        ),
    )
    parser.add_argument(
        "--exclude-test-files",
        action="store_true",
        help="Exclude files whose basename starts with test_ from file and line analysis.",
    )
    parser.add_argument(
        "--include-extension",
        action="append",
        default=[],
        type=normalize_extension,
        help=(
            "Only include a file extension in file and line analysis, e.g. .py. "
            "Can be repeated to include multiple file types."
        ),
    )
    parser.add_argument(
        "--exclude-extension",
        action="append",
        default=[],
        type=normalize_extension,
        help=(
            "Exclude a file extension from file and line analysis, e.g. .json. "
            "Can be repeated and can be used with or without --exclude-artifact-files."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_filters = {normalize_repo_filter(repo) for repo in args.repo} if args.repo else None
    file_filter = build_file_filter(args)
    repo_dirs = discover_repo_dirs(args.input_dir, repo_filters)

    if not repo_dirs:
        requested = f" matching {sorted(repo_filters)}" if repo_filters else ""
        raise SystemExit(f"No webscrape result directories found in {args.input_dir}{requested}.")

    print(f"Analyzing {len(repo_dirs)} repo result directory/directories from {args.input_dir}")
    if file_filter.included_extensions:
        included = ", ".join(sorted(file_filter.included_extensions))
        print(f"Including only file extensions in file/line analysis: {included}")
    if file_filter.excluded_extensions:
        excluded = ", ".join(sorted(file_filter.excluded_extensions))
        print(f"Excluding file extensions from file/line analysis: {excluded}")
    if file_filter.exclude_test_files:
        print("Excluding files with basename prefix test_ from file/line analysis")
    analyses = [analyze_repo(repo_dir, file_filter) for repo_dir in repo_dirs]

    repo_summary_rows = [row_for_repo_summary(repo) for repo in analyses]
    by_category_rows = [
        row
        for repo in analyses
        for row in rows_for_line_statuses(repo, repo.line_statuses_by_category, "category")
    ]
    by_label_rows = [
        row
        for repo in analyses
        for row in rows_for_line_statuses(repo, repo.line_statuses_by_label, "label")
    ]
    top_terminal_file_rows = [
        row
        for repo in analyses
        for row in rows_for_top_counter(
            repo,
            repo.terminal_files,
            "file",
            "modified_or_deleted_lines",
            args.top_n,
        )
    ]
    top_changed_file_rows = [
        row
        for repo in analyses
        for row in rows_for_top_counter(
            repo,
            repo.changed_files,
            "file",
            "file_change_count",
            args.top_n,
        )
    ]
    top_extension_rows = [
        row
        for repo in analyses
        for row in rows_for_top_counter(
            repo,
            repo.changed_extensions,
            "extension",
            "file_change_count",
            args.top_n,
        )
    ]

    print_table(
        "Repo Summary",
        repo_summary_rows,
        [
            "repo",
            "commits",
            "ai_commits",
            "other_bot_commits",
            "non_ai_commits",
            "authors",
            "tracked_added_lines",
            "survived_lines",
            "modified_lines",
            "deleted_lines",
            "survival_rate",
            "terminal_rate",
            "changed_commits",
            "untouched_commits",
            "changed_commit_tracked_added_lines",
            "changed_commit_survival_rate",
        ],
    )
    print_table(
        "Line Lifecycle By Commit Category",
        by_category_rows,
        [
            "repo",
            "category",
            "tracked_added_lines",
            "survived_lines",
            "modified_lines",
            "deleted_lines",
            "survival_rate",
            "terminal_rate",
            "changed_commits",
            "untouched_commits",
            "changed_commit_tracked_added_lines",
            "changed_commit_survival_rate",
        ],
    )
    print_table(
        "Line Lifecycle By AI Label",
        by_label_rows,
        [
            "repo",
            "label",
            "tracked_added_lines",
            "survived_lines",
            "modified_lines",
            "deleted_lines",
            "survival_rate",
            "terminal_rate",
            "changed_commits",
            "untouched_commits",
            "changed_commit_tracked_added_lines",
            "changed_commit_survival_rate",
        ],
    )
    print_table(
        "Top Files Where Added Lines Were Later Modified Or Deleted",
        top_terminal_file_rows,
        ["repo", "file", "modified_or_deleted_lines"],
    )
    print_table(
        "Top Changed Files",
        top_changed_file_rows,
        ["repo", "file", "file_change_count"],
    )
    print_table(
        "Top Changed File Extensions",
        top_extension_rows,
        ["repo", "extension", "file_change_count"],
    )

    if not args.no_write_csv:
        write_csv(
            args.output_dir / "webscrape_repo_summary.csv",
            repo_summary_rows,
            list(repo_summary_rows[0].keys()),
        )
        write_csv(
            args.output_dir / "webscrape_line_lifecycle_by_category.csv",
            by_category_rows,
            [
                "repo",
                "category",
                "tracked_added_lines",
                "survived_lines",
                "modified_lines",
                "deleted_lines",
                "survival_rate",
                "terminal_rate",
                "changed_commits",
                "untouched_commits",
                "changed_commit_tracked_added_lines",
                "changed_commit_survival_rate",
            ],
        )
        write_csv(
            args.output_dir / "webscrape_line_lifecycle_by_label.csv",
            by_label_rows,
            [
                "repo",
                "label",
                "tracked_added_lines",
                "survived_lines",
                "modified_lines",
                "deleted_lines",
                "survival_rate",
                "terminal_rate",
                "changed_commits",
                "untouched_commits",
                "changed_commit_tracked_added_lines",
                "changed_commit_survival_rate",
            ],
        )
        write_csv(
            args.output_dir / "webscrape_top_terminal_files.csv",
            top_terminal_file_rows,
            ["repo", "file", "modified_or_deleted_lines"],
        )
        write_csv(
            args.output_dir / "webscrape_top_changed_files.csv",
            top_changed_file_rows,
            ["repo", "file", "file_change_count"],
        )
        write_csv(
            args.output_dir / "webscrape_top_changed_extensions.csv",
            top_extension_rows,
            ["repo", "extension", "file_change_count"],
        )
        print(f"\nWrote CSV outputs to {args.output_dir}")


if __name__ == "__main__":
    main()
