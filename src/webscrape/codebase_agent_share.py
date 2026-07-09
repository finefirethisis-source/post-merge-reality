from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - optional dependency
    _tqdm = None


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.webscrape.github_commit_scraper import (  # noqa: E402
    GitHubClient,
    apply_github_commit_metadata,
    ensure_local_repo,
    extension_for,
    includes_extension,
    is_test_file,
    normalize_extension_values,
    normalize_local_commit,
    normalize_local_commits,
    remove_cloned_repo_dir,
    resolve_branch_ref,
)


DEFAULT_CLONE_DIR = Path("src/webscrape/repos")
DEFAULT_OUTPUT_DIR = Path("src/webscrape/output")
SNAPSHOTS_OUTPUT_NAME = "codebase_agent_share_snapshots.jsonl"
FILES_OUTPUT_NAME = "codebase_agent_share_files.jsonl"
COMMIT_CACHE_NAME = ".codebase_agent_share_commit_cache.json"

BLAME_HEADER_RE = re.compile(r"^\^?(?P<sha>[0-9a-f]{40})\s+\d+\s+\d+(?:\s+\d+)?$")
VALID_CATEGORIES = {"ai_coauthored", "not_ai_coauthored", "other_bot", "unknown"}
PROGRESS_MODE = "auto"


@dataclass(frozen=True)
class FileBlameResult:
    path: str
    extension: str
    blamed_lines_by_sha: Counter[str]
    error: str = ""


@dataclass(frozen=True)
class SnapshotMetrics:
    files_considered: int
    files_analyzed: int
    files_skipped: int
    lines_by_bucket: Counter[str]
    file_rows: list[dict[str, Any]]


def configure_progress(mode: str) -> None:
    global PROGRESS_MODE
    PROGRESS_MODE = mode


def progress_enabled() -> bool:
    if PROGRESS_MODE == "never" or _tqdm is None:
        return False
    if PROGRESS_MODE == "always":
        return True
    return sys.stderr.isatty()


def progress_iter(
    iterable: Iterable[Any],
    *,
    total: int | None = None,
    desc: str = "",
    unit: str = "it",
) -> Iterable[Any]:
    if not progress_enabled():
        return iterable
    return _tqdm(
        iterable,
        total=total,
        desc=desc,
        unit=unit,
        dynamic_ncols=True,
        leave=False,
        file=sys.stderr,
    )


def log(message: str) -> None:
    formatted = f"[codebase_agent_share] {message}"
    if progress_enabled():
        _tqdm.write(formatted, file=sys.stderr)
    else:
        print(formatted, file=sys.stderr, flush=True)


def repo_key_for(repo: str) -> str:
    return repo.replace("/", "__")


def git_datetime_arg(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    stripped = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
        date_value = datetime.fromisoformat(stripped).date()
        chosen_time = datetime_time(23, 59, 59) if end_of_day else datetime_time.min
        return datetime.combine(date_value, chosen_time, tzinfo=timezone.utc)

    normalized = stripped.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_month(value: datetime) -> datetime:
    year = value.year + (value.month // 12)
    month = 1 if value.month == 12 else value.month + 1
    if value.month == 12:
        year = value.year + 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    day = min(value.day, days_in_month[month - 1])
    return value.replace(year=year, month=month, day=day)


def generate_snapshot_dates(
    since: datetime,
    until: datetime,
    interval: str,
    include_final_snapshot: bool,
) -> list[datetime]:
    if since > until:
        raise ValueError("--since must be earlier than or equal to --until")

    dates: list[datetime] = []
    current = since
    while current <= until:
        dates.append(current)
        if interval == "daily":
            current += timedelta(days=1)
        elif interval == "weekly":
            current += timedelta(days=7)
        elif interval == "monthly":
            current = add_month(current)
        else:  # pragma: no cover - argparse enforces this.
            raise ValueError(f"unsupported interval: {interval}")

    if include_final_snapshot and dates[-1] != until:
        dates.append(until)
    return dates


def run_git(repo_dir: Path, args: list[str], timeout: int) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): "
            f"git -C {repo_dir} {' '.join(args)}\n{completed.stderr}"
        )
    return completed.stdout


def branch_commit_date(repo_dir: Path, ref: str, timeout: int) -> datetime:
    output = run_git(repo_dir, ["show", "-s", "--format=%cI", ref], timeout).strip()
    return parse_datetime(output)


def branch_end_date(repo_dir: Path, branch_ref: str, timeout: int) -> datetime:
    return branch_commit_date(repo_dir, branch_ref, timeout)


def snapshot_sha_for_date(
    repo_dir: Path,
    branch_ref: str,
    snapshot_date: datetime,
    first_parent: bool,
    timeout: int,
) -> str | None:
    args = ["rev-list", "-n", "1"]
    if first_parent:
        args.append("--first-parent")
    args.append(f"--before={git_datetime_arg(snapshot_date)}")
    args.append(branch_ref)
    sha = run_git(repo_dir, args, timeout).strip()
    return sha or None


def build_snapshot_dates(args: argparse.Namespace, repo_dir: Path, branch_ref: str) -> list[datetime]:
    if args.snapshot_date:
        dates = [parse_datetime(value, end_of_day=True) for value in args.snapshot_date]
        return sorted(set(dates))

    since = parse_datetime(args.since, end_of_day=True)
    until = (
        parse_datetime(args.until, end_of_day=True)
        if args.until
        else branch_end_date(repo_dir, branch_ref, args.git_timeout)
    )
    dates = generate_snapshot_dates(
        since,
        until,
        args.interval,
        include_final_snapshot=not args.no_final_snapshot,
    )
    if args.max_snapshots is not None:
        dates = dates[: args.max_snapshots]
    return dates


def list_snapshot_files(
    repo_dir: Path,
    snapshot_sha: str,
    include_extensions: frozenset[str],
    exclude_test_files: bool,
    max_files: int | None,
    timeout: int,
) -> list[str]:
    output = run_git(repo_dir, ["ls-tree", "-r", "--name-only", "-z", snapshot_sha], timeout)
    files = [path for path in output.split("\0") if path]
    filtered: list[str] = []
    for path in files:
        if not includes_extension(path, include_extensions):
            continue
        if exclude_test_files and is_test_file(path):
            continue
        filtered.append(path)
        if max_files is not None and len(filtered) >= max_files:
            break
    return filtered


def parse_blame_counts(output: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    current_sha: str | None = None
    for line in output.splitlines():
        header = BLAME_HEADER_RE.match(line)
        if header:
            current_sha = header.group("sha")
            continue
        if line.startswith("\t") and current_sha:
            counts[current_sha] += 1
    return counts


def blame_file(
    repo_dir: Path,
    snapshot_sha: str,
    path: str,
    git_timeout: int,
    ignore_revs_file: Path | None,
) -> FileBlameResult:
    args = ["blame", "--line-porcelain"]
    if ignore_revs_file is not None:
        args.append(f"--ignore-revs-file={ignore_revs_file}")
    args.extend([snapshot_sha, "--", path])
    try:
        output = run_git(repo_dir, args, git_timeout)
        return FileBlameResult(
            path=path,
            extension=extension_for(path),
            blamed_lines_by_sha=parse_blame_counts(output),
        )
    except Exception as exc:  # noqa: BLE001 - file-level failures should not stop the run.
        return FileBlameResult(
            path=path,
            extension=extension_for(path),
            blamed_lines_by_sha=Counter(),
            error=str(exc).splitlines()[0],
        )


def blame_snapshot_files(
    repo_dir: Path,
    snapshot_sha: str,
    files: list[str],
    workers: int,
    git_timeout: int,
    ignore_revs_file: Path | None,
) -> list[FileBlameResult]:
    if not files:
        return []
    if workers <= 1:
        results = []
        for path in progress_iter(
            files,
            total=len(files),
            desc=f"Blaming {snapshot_sha[:12]}",
            unit="file",
        ):
            results.append(blame_file(repo_dir, snapshot_sha, path, git_timeout, ignore_revs_file))
        return results

    results: list[FileBlameResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                blame_file,
                repo_dir,
                snapshot_sha,
                path,
                git_timeout,
                ignore_revs_file,
            ): path
            for path in files
        }
        future_iter = progress_iter(
            as_completed(futures),
            total=len(futures),
            desc=f"Blaming {snapshot_sha[:12]}",
            unit="file",
        )
        for future in future_iter:
            results.append(future.result())
    return sorted(results, key=lambda item: item.path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def append_jsonl(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, sort_keys=True) + "\n")


def commit_cache_entry(commit: dict[str, Any]) -> dict[str, Any]:
    category = str(commit.get("category") or "unknown")
    if category not in VALID_CATEGORIES:
        category = "unknown"
    return {
        "sha": commit.get("sha"),
        "category": category,
        "labels": commit.get("labels") or [],
        "date": commit.get("date"),
        "author_login": commit.get("author_login"),
        "author_type": commit.get("author_type"),
        "committer_login": commit.get("committer_login"),
        "committer_type": commit.get("committer_type"),
        "git_author_name": commit.get("git_author_name"),
        "git_committer_name": commit.get("git_committer_name"),
        "message_subject": commit.get("message_subject"),
        "github_metadata_enriched": bool(commit.get("github_metadata_enriched")),
    }


class CommitCategoryCache:
    def __init__(
        self,
        *,
        repo: str,
        repo_dir: Path,
        cache_path: Path,
        use_cache: bool,
        use_github_api: bool,
        token: str | None,
        github_api_sleep: float,
    ) -> None:
        self.repo = repo
        self.repo_dir = repo_dir
        self.cache_path = cache_path
        self.use_cache = use_cache
        self.client = (
            GitHubClient(token=token, sleep_seconds=github_api_sleep)
            if use_github_api
            else None
        )
        self.entries: dict[str, dict[str, Any]] = {}
        if use_cache:
            raw = read_json(cache_path)
            self.entries = {
                str(sha): entry
                for sha, entry in raw.items()
                if isinstance(sha, str) and isinstance(entry, dict)
            }

    def classify_many(self, shas: set[str]) -> None:
        missing = sorted(
            sha
            for sha in shas
            if sha and sha not in self.entries and not set(sha) <= {"0"}
        )
        if not missing:
            return

        log(f"Classifying {len(missing)} blamed commit(s)")
        try:
            commits = normalize_local_commits(self.repo, self.repo_dir, missing)
        except Exception as exc:  # noqa: BLE001 - fall back to per-commit classification.
            log(f"Batch commit metadata load failed; falling back per commit: {exc}")
            commits = []
            for sha in progress_iter(
                missing,
                total=len(missing),
                desc="Classifying commits",
                unit="commit",
            ):
                try:
                    commits.append(normalize_local_commit(self.repo, self.repo_dir, sha))
                except Exception as item_exc:  # noqa: BLE001
                    self.entries[sha] = {
                        "sha": sha,
                        "category": "unknown",
                        "labels": [],
                        "error": str(item_exc).splitlines()[0],
                    }

        for commit in progress_iter(
            commits,
            total=len(commits),
            desc="Enriching commit categories",
            unit="commit",
        ):
            if self.client is not None:
                try:
                    item, _headers = self.client.get(f"/repos/{self.repo}/commits/{commit['sha']}")
                    commit = apply_github_commit_metadata(commit, item)
                except Exception as exc:  # noqa: BLE001
                    commit = dict(commit)
                    commit["github_metadata_error"] = str(exc).splitlines()[0]
            self.entries[str(commit["sha"])] = commit_cache_entry(commit)

    def category_for(self, sha: str) -> str:
        entry = self.entries.get(sha)
        if not entry:
            return "unknown"
        category = str(entry.get("category") or "unknown")
        return category if category in VALID_CATEGORIES else "unknown"

    def save(self) -> None:
        if self.use_cache:
            write_json(self.cache_path, self.entries)


def bucket_for_category(category: str) -> str:
    if category == "ai_coauthored":
        return "ai"
    if category == "not_ai_coauthored":
        return "human"
    if category == "other_bot":
        return "other_bot"
    return "unknown"


def share(value: int, total: int) -> float | None:
    if total <= 0:
        return None
    return value / total


def line_buckets_for_counts(
    blamed_lines_by_sha: Counter[str],
    classifier: CommitCategoryCache,
) -> Counter[str]:
    classifier.classify_many(set(blamed_lines_by_sha))
    buckets: Counter[str] = Counter()
    for sha, line_count in blamed_lines_by_sha.items():
        buckets[bucket_for_category(classifier.category_for(sha))] += line_count
    return buckets


def file_row_from_buckets(
    *,
    repo_key: str,
    repo: str,
    branch: str,
    snapshot_index: int,
    snapshot_date: datetime,
    snapshot_sha: str,
    snapshot_commit_date: str,
    file_result: FileBlameResult,
    buckets: Counter[str],
) -> dict[str, Any]:
    total = sum(buckets.values())
    return {
        "repo_key": repo_key,
        "repo": repo,
        "branch": branch,
        "snapshot_index": snapshot_index,
        "snapshot_date": git_datetime_arg(snapshot_date),
        "snapshot_sha": snapshot_sha,
        "snapshot_commit_date": snapshot_commit_date,
        "path": file_result.path,
        "extension": file_result.extension,
        "total_living_lines": total,
        "ai_living_lines": buckets["ai"],
        "human_living_lines": buckets["human"],
        "other_bot_living_lines": buckets["other_bot"],
        "unknown_living_lines": buckets["unknown"],
        "ai_living_line_share": share(buckets["ai"], total),
        "human_living_line_share": share(buckets["human"], total),
        "other_bot_living_line_share": share(buckets["other_bot"], total),
        "unknown_living_line_share": share(buckets["unknown"], total),
    }


def snapshot_row_from_metrics(
    *,
    repo_key: str,
    repo: str,
    branch: str,
    snapshot_index: int,
    snapshot_date: datetime,
    snapshot_sha: str,
    snapshot_commit_date: str,
    metrics: SnapshotMetrics,
) -> dict[str, Any]:
    total = sum(metrics.lines_by_bucket.values())
    return {
        "repo_key": repo_key,
        "repo": repo,
        "branch": branch,
        "snapshot_index": snapshot_index,
        "snapshot_date": git_datetime_arg(snapshot_date),
        "snapshot_sha": snapshot_sha,
        "snapshot_commit_date": snapshot_commit_date,
        "files_considered": metrics.files_considered,
        "files_analyzed": metrics.files_analyzed,
        "files_skipped": metrics.files_skipped,
        "total_living_lines": total,
        "ai_living_lines": metrics.lines_by_bucket["ai"],
        "human_living_lines": metrics.lines_by_bucket["human"],
        "other_bot_living_lines": metrics.lines_by_bucket["other_bot"],
        "unknown_living_lines": metrics.lines_by_bucket["unknown"],
        "ai_living_line_share": share(metrics.lines_by_bucket["ai"], total),
        "human_living_line_share": share(metrics.lines_by_bucket["human"], total),
        "other_bot_living_line_share": share(metrics.lines_by_bucket["other_bot"], total),
        "unknown_living_line_share": share(metrics.lines_by_bucket["unknown"], total),
    }


def compute_snapshot_metrics(
    *,
    repo_key: str,
    repo: str,
    branch: str,
    repo_dir: Path,
    snapshot_index: int,
    snapshot_date: datetime,
    snapshot_sha: str,
    snapshot_commit_date: str,
    files: list[str],
    workers: int,
    git_timeout: int,
    ignore_revs_file: Path | None,
    classifier: CommitCategoryCache,
) -> SnapshotMetrics:
    results = blame_snapshot_files(
        repo_dir,
        snapshot_sha,
        files,
        workers,
        git_timeout,
        ignore_revs_file,
    )
    lines_by_bucket: Counter[str] = Counter()
    file_rows: list[dict[str, Any]] = []
    files_skipped = 0
    blamed_shas: set[str] = set()
    for result in results:
        if not result.error:
            blamed_shas.update(result.blamed_lines_by_sha)
    classifier.classify_many(blamed_shas)

    for result in results:
        if result.error:
            files_skipped += 1
            continue
        buckets = line_buckets_for_counts(result.blamed_lines_by_sha, classifier)
        lines_by_bucket.update(buckets)
        file_rows.append(
            file_row_from_buckets(
                repo_key=repo_key,
                repo=repo,
                branch=branch,
                snapshot_index=snapshot_index,
                snapshot_date=snapshot_date,
                snapshot_sha=snapshot_sha,
                snapshot_commit_date=snapshot_commit_date,
                file_result=result,
                buckets=buckets,
            )
        )

    return SnapshotMetrics(
        files_considered=len(files),
        files_analyzed=len(file_rows),
        files_skipped=files_skipped,
        lines_by_bucket=lines_by_bucket,
        file_rows=file_rows,
    )


def copy_file_row_for_snapshot(
    row: dict[str, Any],
    *,
    snapshot_index: int,
    snapshot_date: datetime,
    snapshot_sha: str,
    snapshot_commit_date: str,
) -> dict[str, Any]:
    copied = dict(row)
    copied.update(
        {
            "snapshot_index": snapshot_index,
            "snapshot_date": git_datetime_arg(snapshot_date),
            "snapshot_sha": snapshot_sha,
            "snapshot_commit_date": snapshot_commit_date,
        }
    )
    return copied


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the cumulative share of living codebase lines attributed to "
            "AI-agent commits by running git blame at periodic branch snapshots."
        )
    )
    parser.add_argument("--repo", required=True, help="Repository as owner/name.")
    parser.add_argument(
        "--local-repo",
        type=Path,
        help="Use an existing local clone instead of cloning/fetching from GitHub.",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=DEFAULT_CLONE_DIR,
        help=f"Directory for repository clones. Default: {DEFAULT_CLONE_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Root directory for webscrape outputs. The script writes into "
            "<output-dir>/<owner>__<repo>. Default: src/webscrape/output"
        ),
    )
    parser.add_argument("--branch", default="main", help="Branch to analyze. Default: main.")
    parser.add_argument(
        "--since",
        "--start-date",
        dest="since",
        help=(
            "First generated snapshot date, e.g. 2025-01-01. Required unless "
            "--snapshot-date is provided."
        ),
    )
    parser.add_argument(
        "--until",
        "--end-date",
        dest="until",
        help="Last generated snapshot date. Defaults to the selected branch tip date.",
    )
    parser.add_argument(
        "--snapshot-date",
        action="append",
        default=[],
        help=(
            "Analyze an explicit snapshot date instead of generated intervals. "
            "Can be repeated."
        ),
    )
    parser.add_argument(
        "--interval",
        choices=("daily", "weekly", "monthly"),
        default="weekly",
        help="Generated snapshot interval. Default: weekly.",
    )
    parser.add_argument(
        "--no-final-snapshot",
        action="store_true",
        help="Do not append the --until/branch-tip date when it is off the interval grid.",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        help="Limit the number of generated snapshot dates, useful for smoke tests.",
    )
    parser.add_argument(
        "--include-extension",
        action="append",
        default=[],
        help="Only include files with this extension. Repeat or pass comma-separated values.",
    )
    parser.add_argument(
        "--exclude-test-files",
        action="store_true",
        help="Exclude files whose basename starts with test_.",
    )
    parser.add_argument(
        "--max-files-per-snapshot",
        type=int,
        help="Limit files per snapshot, useful for smoke tests.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel git blame workers per snapshot. Default: 4.",
    )
    parser.add_argument(
        "--git-timeout",
        type=int,
        default=300,
        help="Timeout in seconds for each git command. Default: 300.",
    )
    parser.add_argument(
        "--ignore-revs-file",
        type=Path,
        help="Optional git blame ignore-revs file for formatting-only commits.",
    )
    parser.add_argument(
        "--no-first-parent-snapshots",
        action="store_true",
        help="Select snapshot commits from all reachable history instead of the first-parent chain.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Reuse an existing clone under --clone-dir without deleting or fetching it.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the managed clone under --clone-dir after analysis. Local repos are never removed.",
    )
    parser.add_argument(
        "--full-clone",
        action="store_true",
        help="Clone without --filter=blob:none when a clone is needed.",
    )
    parser.add_argument(
        "--use-github-api",
        action="store_true",
        help=(
            "Hydrate blamed commit metadata from the GitHub API before classification. "
            "This can improve bot-type detection but is slower."
        ),
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument(
        "--github-api-sleep",
        type=float,
        default=0.05,
        help="Sleep between GitHub API calls when --use-github-api is enabled. Default: 0.05.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the internal blamed-commit classification cache.",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="Progress bar mode. Default: auto.",
    )
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars.")
    args = parser.parse_args()

    if args.no_progress:
        args.progress = "never"
    configure_progress(args.progress)
    if args.progress != "never" and _tqdm is None:
        log("tqdm is not installed; progress bars disabled")
    if not args.snapshot_date and not args.since:
        parser.error("--since is required unless at least one --snapshot-date is provided")
    if args.max_snapshots is not None and args.max_snapshots < 1:
        parser.error("--max-snapshots must be at least 1")
    if args.max_files_per_snapshot is not None and args.max_files_per_snapshot < 1:
        parser.error("--max-files-per-snapshot must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.git_timeout < 1:
        parser.error("--git-timeout must be at least 1")
    try:
        args.include_extensions = normalize_extension_values(args.include_extension)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    repo_key = repo_key_for(args.repo)
    output_repo_dir = args.output_dir / repo_key
    output_repo_dir.mkdir(parents=True, exist_ok=True)

    managed_clone = args.local_repo is None
    repo_dir = (
        args.local_repo
        if args.local_repo is not None
        else ensure_local_repo(
            args.repo,
            args.clone_dir,
            skip_fetch=args.skip_fetch,
            full_clone=args.full_clone,
        )
    )
    if args.local_repo is not None and not (repo_dir / ".git").exists():
        raise SystemExit(f"--local-repo is not a git repository: {repo_dir}")

    try:
        branch_ref = resolve_branch_ref(repo_dir, args.branch)
        snapshot_dates = build_snapshot_dates(args, repo_dir, branch_ref)
        first_parent = not args.no_first_parent_snapshots
        log(
            f"Analyzing {args.repo} branch={args.branch} snapshots={len(snapshot_dates)} "
            f"files={'all' if not args.include_extensions else ','.join(sorted(args.include_extensions))}"
        )

        classifier = CommitCategoryCache(
            repo=args.repo,
            repo_dir=repo_dir,
            cache_path=output_repo_dir / COMMIT_CACHE_NAME,
            use_cache=not args.no_cache,
            use_github_api=args.use_github_api,
            token=args.token,
            github_api_sleep=args.github_api_sleep,
        )

        snapshots_path = output_repo_dir / SNAPSHOTS_OUTPUT_NAME
        files_path = output_repo_dir / FILES_OUTPUT_NAME
        previous_sha: str | None = None
        previous_metrics: SnapshotMetrics | None = None

        try:
            with snapshots_path.open("w", encoding="utf-8") as snapshots_file, files_path.open(
                "w",
                encoding="utf-8",
            ) as files_file:
                snapshot_iter = progress_iter(
                    enumerate(snapshot_dates, start=1),
                    total=len(snapshot_dates),
                    desc="Snapshots",
                    unit="snapshot",
                )
                for snapshot_index, snapshot_date in snapshot_iter:
                    snapshot_sha = snapshot_sha_for_date(
                        repo_dir,
                        branch_ref,
                        snapshot_date,
                        first_parent,
                        args.git_timeout,
                    )
                    if not snapshot_sha:
                        log(f"No commit found before {git_datetime_arg(snapshot_date)}; skipping")
                        continue

                    snapshot_commit_date = git_datetime_arg(
                        branch_commit_date(repo_dir, snapshot_sha, args.git_timeout)
                    )
                    if previous_sha == snapshot_sha and previous_metrics is not None:
                        metrics = SnapshotMetrics(
                            files_considered=previous_metrics.files_considered,
                            files_analyzed=previous_metrics.files_analyzed,
                            files_skipped=previous_metrics.files_skipped,
                            lines_by_bucket=Counter(previous_metrics.lines_by_bucket),
                            file_rows=[
                                copy_file_row_for_snapshot(
                                    row,
                                    snapshot_index=snapshot_index,
                                    snapshot_date=snapshot_date,
                                    snapshot_sha=snapshot_sha,
                                    snapshot_commit_date=snapshot_commit_date,
                                )
                                for row in previous_metrics.file_rows
                            ],
                        )
                    else:
                        files = list_snapshot_files(
                            repo_dir,
                            snapshot_sha,
                            args.include_extensions,
                            args.exclude_test_files,
                            args.max_files_per_snapshot,
                            args.git_timeout,
                        )
                        log(
                            f"Snapshot {snapshot_index}/{len(snapshot_dates)} "
                            f"{git_datetime_arg(snapshot_date)} {snapshot_sha[:12]}: "
                            f"blaming {len(files)} file(s)"
                        )
                        metrics = compute_snapshot_metrics(
                            repo_key=repo_key,
                            repo=args.repo,
                            branch=args.branch,
                            repo_dir=repo_dir,
                            snapshot_index=snapshot_index,
                            snapshot_date=snapshot_date,
                            snapshot_sha=snapshot_sha,
                            snapshot_commit_date=snapshot_commit_date,
                            files=files,
                            workers=args.workers,
                            git_timeout=args.git_timeout,
                            ignore_revs_file=args.ignore_revs_file,
                            classifier=classifier,
                        )
                        previous_sha = snapshot_sha
                        previous_metrics = metrics

                    append_jsonl(
                        snapshots_file,
                        snapshot_row_from_metrics(
                            repo_key=repo_key,
                            repo=args.repo,
                            branch=args.branch,
                            snapshot_index=snapshot_index,
                            snapshot_date=snapshot_date,
                            snapshot_sha=snapshot_sha,
                            snapshot_commit_date=snapshot_commit_date,
                            metrics=metrics,
                        ),
                    )
                    for row in metrics.file_rows:
                        append_jsonl(files_file, row)
                    snapshots_file.flush()
                    files_file.flush()
        finally:
            classifier.save()

        log(f"Wrote {snapshots_path}")
        log(f"Wrote {files_path}")
    finally:
        if managed_clone and not args.keep_clone:
            remove_cloned_repo_dir(repo_dir)


if __name__ == "__main__":
    main()
