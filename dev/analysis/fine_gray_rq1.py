from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_rq1")
DEFAULT_OBSERVATION_END = "2026-05-31"
DEFAULT_ORIGIN_START = "2025-05"
DEFAULT_ORIGIN_END = "2026-05"
DEFAULT_MAX_TRACKED_LINES = 500_000
DEFAULT_DURATION_PRECISION = 6

LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
COMMIT_CLASSIFICATION_FILE = "commit_contribution_classifications.jsonl"
COMMIT_FILE_CLASSIFICATION_FILE = "commit_file_contribution_classifications.jsonl"

EVENT_STATUSES = {"modified", "deleted"}
SUPPORTED_ORIGIN_CATEGORIES = {
    "ai_coauthored": "AI",
    "not_ai_coauthored": "Human",
}
VALID_MAINTENANCE_CLASSES = {
    "adaptive",
    "corrective",
    "management",
    "perfective",
    "preventive",
    "unknown",
}
VALID_OPERATIONAL_INTENTS = {
    "security_fix",
    "revert",
    "bug_fix",
    "dependency_update",
    "merge_release_versioning",
    "performance",
    "feature",
    "refactor_cleanup",
    "test",
    "documentation",
    "resource",
    "build_config_ci",
    "style_formatting",
    "mixed",
    "unknown",
}
AMBIGUOUS_TERMINAL_OPERATIONAL_INTENTS = {"mixed", "unknown"}
AMBIGUOUS_TERMINAL_MAINTENANCE_CLASSES = {"unknown"}
EVENT_TAXONOMIES = ("maintenance_class", "operational_intent")

LOG = logging.getLogger(__name__)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
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
                yield row


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


def repo_from_dir(repo_dir: Path) -> str:
    return repo_dir.name.replace("__", "/")


def normalize_path(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip().replace("\\", "/")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_observation_end(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T23:59:59+00:00"
    return parse_datetime(text)


def parse_time_bound(value: Any, *, is_end: bool) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "all", "open"}:
        return None
    if len(text) == 7 and text[4] == "-":
        try:
            year, month = map(int, text.split("-"))
            if is_end:
                last_day = calendar.monthrange(year, month)[1]
                return datetime.combine(
                    datetime(year, month, last_day).date(),
                    time(23, 59, 59, 999999),
                    tzinfo=timezone.utc,
                )
            return datetime(year, month, 1, tzinfo=timezone.utc)
        except ValueError:
            return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        suffix = "T23:59:59.999999+00:00" if is_end else "T00:00:00+00:00"
        return parse_datetime(f"{text}{suffix}")
    return parse_datetime(text)


def duration_days(start: datetime, end: datetime, *, precision: int) -> float | None:
    days = (end - start).total_seconds() / 86_400
    if days < 0:
        return None
    return round(days, precision)


def origin_role_from_category(category: Any) -> str | None:
    return SUPPORTED_ORIGIN_CATEGORIES.get(str(category or ""))


def clean_maintenance_class(value: Any) -> str:
    maintenance_class = str(value or "unknown").strip() or "unknown"
    if maintenance_class not in VALID_MAINTENANCE_CLASSES:
        return "unknown"
    return maintenance_class


def clean_operational_intent(value: Any) -> str:
    operational_intent = str(value or "unknown").strip() or "unknown"
    if operational_intent not in VALID_OPERATIONAL_INTENTS:
        return "unknown"
    return operational_intent


def load_commit_event_labels(repo_dir: Path) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = {}
    for row in iter_jsonl(repo_dir / COMMIT_CLASSIFICATION_FILE):
        sha = row.get("sha")
        if isinstance(sha, str) and sha:
            labels[sha] = {
                "maintenance_class": clean_maintenance_class(row.get("maintenance_class")),
                "operational_intent": clean_operational_intent(
                    row.get("primary_operational_intent")
                ),
            }
    return labels


def load_file_primary_roles(repo_dir: Path) -> dict[tuple[str, str], str]:
    roles: dict[tuple[str, str], str] = {}
    for row in iter_jsonl(repo_dir / COMMIT_FILE_CLASSIFICATION_FILE):
        sha = row.get("commit_sha")
        filename = normalize_path(row.get("filename"))
        if isinstance(sha, str) and sha and filename:
            role = str(row.get("file_primary_role") or "unknown").strip() or "unknown"
            roles[(sha, filename)] = role
    return roles


def base_line_context(
    line: dict[str, Any],
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
) -> tuple[tuple[str, str, datetime, str] | None, str | None]:
    role = origin_role_from_category(line.get("origin_commit_category"))
    if role is None:
        return None, "unsupported_origin_role"

    origin_sha = line.get("origin_commit_sha")
    if not isinstance(origin_sha, str) or not origin_sha:
        return None, "missing_origin_commit_sha"

    origin_date = parse_datetime(line.get("origin_commit_date"))
    if origin_date is None:
        return None, "missing_origin_date"
    if origin_start is not None and origin_date < origin_start:
        return None, "origin_before_range"
    if origin_end is not None and origin_date > origin_end:
        return None, "origin_after_range"

    origin_file = normalize_path(line.get("origin_file"))
    if origin_file is None:
        return None, "missing_origin_file"

    return (role, origin_sha, origin_date, origin_file), None


def compute_competing_risk_event(
    line: dict[str, Any],
    *,
    origin_date: datetime,
    observation_end: datetime,
    terminal_event_labels: dict[str, dict[str, str]],
    duration_precision: int,
) -> tuple[tuple[int, dict[str, str], float] | None, str | None]:
    status = str(line.get("status") or "unknown")
    if status in EVENT_STATUSES:
        terminal_date = parse_datetime(line.get("terminal_commit_date"))
        if terminal_date is None:
            return None, "event_missing_terminal_date"
        if terminal_date > observation_end:
            event_status = 0
            event_types = {
                "maintenance_class": "censored",
                "operational_intent": "censored",
            }
            end_date = observation_end
        else:
            event_status = 1
            terminal_sha = line.get("terminal_commit_sha")
            event_types = (
                terminal_event_labels.get(terminal_sha, {})
                if isinstance(terminal_sha, str)
                else {}
            )
            event_types = {
                "maintenance_class": clean_maintenance_class(
                    event_types.get("maintenance_class")
                ),
                "operational_intent": clean_operational_intent(
                    event_types.get("operational_intent")
                ),
            }
            if event_types["maintenance_class"] in AMBIGUOUS_TERMINAL_MAINTENANCE_CLASSES:
                return None, (
                    "ambiguous_terminal_maintenance_class:"
                    f"{event_types['maintenance_class']}"
                )
            if event_types["operational_intent"] in AMBIGUOUS_TERMINAL_OPERATIONAL_INTENTS:
                return None, (
                    "ambiguous_terminal_operational_intent:"
                    f"{event_types['operational_intent']}"
                )
            end_date = terminal_date
    elif status == "survived":
        event_status = 0
        event_types = {
            "maintenance_class": "censored",
            "operational_intent": "censored",
        }
        end_date = observation_end
    else:
        return None, f"unsupported_status:{status}"

    duration = duration_days(origin_date, end_date, precision=duration_precision)
    if duration is None:
        return None, "negative_duration"
    return (event_status, event_types, duration), None


def load_repo_fine_gray_data(
    repo_dir: Path,
    observation_end: datetime,
    origin_start: datetime | None,
    origin_end: datetime | None,
    max_tracked_lines: int | None,
    duration_precision: int,
) -> dict[str, Any]:
    repo = repo_from_dir(repo_dir)
    repo_key = repo_dir.name
    terminal_event_labels = load_commit_event_labels(repo_dir)
    file_roles = load_file_primary_roles(repo_dir)
    origin_line_counts: Counter[str] = Counter()
    outlier_origin_shas: set[str] = set()
    rows_by_taxonomy: dict[str, Counter[tuple[str, str, str, float, int, str]]] = {
        taxonomy: Counter() for taxonomy in EVENT_TAXONOMIES
    }
    skipped: Counter[str] = Counter()
    event_counts_by_taxonomy: dict[str, Counter[str]] = {
        taxonomy: Counter() for taxonomy in EVENT_TAXONOMIES
    }
    role_counts: Counter[str] = Counter()
    lifecycle_rows = 0
    included_lines = 0

    for line in iter_jsonl(repo_dir / LINE_LIFECYCLE_FILE):
        context, _reason = base_line_context(
            line,
            origin_start=origin_start,
            origin_end=origin_end,
        )
        if context is None:
            continue
        _role, origin_sha, _origin_date, _origin_file = context
        origin_line_counts[origin_sha] += 1

    if max_tracked_lines is not None:
        outlier_origin_shas = {
            sha
            for sha, line_count in origin_line_counts.items()
            if line_count > max_tracked_lines
        }

    for line in iter_jsonl(repo_dir / LINE_LIFECYCLE_FILE):
        lifecycle_rows += 1
        context, skip_reason = base_line_context(
            line,
            origin_start=origin_start,
            origin_end=origin_end,
        )
        if context is None:
            assert skip_reason is not None
            skipped[skip_reason] += 1
            continue
        role, origin_sha, origin_date, origin_file = context
        if origin_sha in outlier_origin_shas:
            skipped["outlier_origin_commit_lines"] += 1
            continue

        event_info, event_skip_reason = compute_competing_risk_event(
            line,
            origin_date=origin_date,
            observation_end=observation_end,
            terminal_event_labels=terminal_event_labels,
            duration_precision=duration_precision,
        )
        if event_info is None:
            assert event_skip_reason is not None
            skipped[event_skip_reason] += 1
            continue
        event_status, event_types, survival_days = event_info
        file_primary_role = file_roles.get((origin_sha, origin_file), "unknown")
        for taxonomy, event_type in event_types.items():
            if taxonomy not in rows_by_taxonomy:
                continue
            rows_by_taxonomy[taxonomy][
                (
                    role,
                    "ai_coauthored" if role == "AI" else "not_ai_coauthored",
                    file_primary_role,
                    survival_days,
                    event_status,
                    event_type,
                )
            ] += 1
            event_counts_by_taxonomy[taxonomy][event_type] += 1
        included_lines += 1
        role_counts[role] += 1

    return {
        "repo": repo,
        "repo_key": repo_key,
        "rows_by_taxonomy": rows_by_taxonomy,
        "rows": rows_by_taxonomy["maintenance_class"],
        "lifecycle_rows": lifecycle_rows,
        "included_lines": included_lines,
        "event_counts_by_taxonomy": event_counts_by_taxonomy,
        "event_counts": event_counts_by_taxonomy["maintenance_class"],
        "role_counts": role_counts,
        "skipped": skipped,
        "outlier_origin_commits": len(outlier_origin_shas),
    }


def repo_summary(repo_dir: Path, data: dict[str, Any]) -> str:
    role_counts = data.get("role_counts") or {}
    event_counts = data.get("event_counts") or {}
    return (
        f"included={int(data.get('included_lines') or 0):,} "
        f"AI={int(role_counts.get('AI') or 0):,} "
        f"Human={int(role_counts.get('Human') or 0):,} "
        f"events={sum(count for key, count in event_counts.items() if key != 'censored'):,}"
    )


def fine_gray_csv_path(output_dir: Path, taxonomy: str) -> Path:
    return output_dir / f"rq1_fine_gray_{taxonomy}_line_data.csv"


def summary_csv_path(output_dir: Path, taxonomy: str) -> Path:
    return output_dir / f"rq1_fine_gray_{taxonomy}_summary.csv"


def write_fine_gray_csv(
    repo_data: list[dict[str, Any]],
    output_path: Path,
    *,
    taxonomy: str,
    observation_end: datetime,
    origin_start: datetime | None,
    origin_end: datetime | None,
    max_tracked_lines: int | None,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "repo",
        "repo_key",
        "origin_group",
        "origin_commit_category",
        "origin_ai",
        "file_primary_role",
        "survival_days",
        "survival_days_positive",
        "cr_status",
        "event_type",
        "event_type_factor",
        "event_observed",
        "weight",
        "observation_end",
        "origin_start",
        "origin_end",
        "max_tracked_lines",
    ]
    event_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    file_role_counts: Counter[str] = Counter()
    output_rows = 0
    weighted_lines = 0
    repos = set()
    origin_start_text = origin_start.isoformat() if origin_start else ""
    origin_end_text = origin_end.isoformat() if origin_end else ""
    max_tracked_lines_text = "" if max_tracked_lines is None else str(max_tracked_lines)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for repo in repo_data:
            repo_name = str(repo.get("repo") or repo.get("repo_key") or "unknown")
            repo_key = str(repo.get("repo_key") or repo_name.replace("/", "__"))
            repos.add(repo_key)
            rows = (repo.get("rows_by_taxonomy") or {}).get(taxonomy) or repo.get("rows") or {}
            for (
                origin_group,
                origin_commit_category,
                file_primary_role,
                survival_days,
                event_status,
                event_type,
            ), weight in sorted(rows.items()):
                if not weight:
                    continue
                survival_days_float = float(survival_days)
                survival_days_positive = survival_days_float if survival_days_float > 0 else 1e-9
                event_type = str(event_type)
                event_observed = int(event_status)
                writer.writerow(
                    {
                        "repo": repo_name,
                        "repo_key": repo_key,
                        "origin_group": origin_group,
                        "origin_commit_category": origin_commit_category,
                        "origin_ai": 1 if origin_group == "AI" else 0,
                        "file_primary_role": file_primary_role,
                        "survival_days": f"{survival_days_float:.12g}",
                        "survival_days_positive": f"{survival_days_positive:.12g}",
                        "cr_status": event_type if event_observed else "censored",
                        "event_type": event_type,
                        "event_type_factor": (
                            "censored" if not event_observed else event_type
                        ),
                        "event_observed": event_observed,
                        "weight": int(weight),
                        "observation_end": observation_end.isoformat(),
                        "origin_start": origin_start_text,
                        "origin_end": origin_end_text,
                        "max_tracked_lines": max_tracked_lines_text,
                    }
                )
                output_rows += 1
                weighted_lines += int(weight)
                event_counts["censored" if not event_observed else event_type] += int(weight)
                role_counts[origin_group] += int(weight)
                file_role_counts[str(file_primary_role)] += int(weight)

    return {
        "taxonomy": taxonomy,
        "path": output_path,
        "rows": output_rows,
        "weighted_lines": weighted_lines,
        "repos": len(repos),
        "event_counts": event_counts,
        "role_counts": role_counts,
        "file_role_counts": file_role_counts,
    }


def write_summary_csv(repo_data: list[dict[str, Any]], export_summary: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    skipped = Counter()
    outlier_origin_commits = 0
    lifecycle_rows = 0
    included_lines = 0
    for repo in repo_data:
        skipped.update(repo.get("skipped") or {})
        outlier_origin_commits += int(repo.get("outlier_origin_commits") or 0)
        lifecycle_rows += int(repo.get("lifecycle_rows") or 0)
        included_lines += int(repo.get("included_lines") or 0)

    fieldnames = ["section", "key", "value"]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for key, value in [
            ("taxonomy", export_summary["taxonomy"]),
            ("repos", export_summary["repos"]),
            ("csv_rows", export_summary["rows"]),
            ("weighted_lines", export_summary["weighted_lines"]),
            ("lifecycle_rows", lifecycle_rows),
            ("included_lines", included_lines),
            ("outlier_origin_commits", outlier_origin_commits),
        ]:
            writer.writerow({"section": "overview", "key": key, "value": value})
        for key, count in sorted((export_summary.get("event_counts") or {}).items()):
            writer.writerow({"section": "event_counts", "key": key, "value": count})
        for key, count in sorted((export_summary.get("role_counts") or {}).items()):
            writer.writerow({"section": "origin_group_counts", "key": key, "value": count})
        for key, count in sorted((export_summary.get("file_role_counts") or {}).items()):
            writer.writerow({"section": "file_primary_role_counts", "key": key, "value": count})
        for key, count in sorted(skipped.items()):
            if count:
                writer.writerow({"section": "skipped", "key": key, "value": count})


def print_summary(export_summary: dict[str, Any], summary_path: Path) -> None:
    taxonomy = str(export_summary.get("taxonomy") or "unknown")
    print(f"fine_gray_{taxonomy}_export:")
    print(f"  repos={int(export_summary['repos']):,}")
    print(f"  csv_rows={int(export_summary['rows']):,}")
    print(f"  weighted_lines={int(export_summary['weighted_lines']):,}")
    print(f"  wrote_csv={export_summary['path']}")
    print(f"  wrote_summary={summary_path}")
    print("  origin_group_counts:")
    for key, count in sorted((export_summary.get("role_counts") or {}).items()):
        print(f"    {key}: {count:,}")
    print("  event_counts:")
    for key, count in sorted((export_summary.get("event_counts") or {}).items()):
        print(f"    {key}: {count:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export compact weighted line-level competing-risk data for Fine-Gray "
            "models over terminal commit maintenance classes and operational intents."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing per-repo scraper outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for CSV outputs. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        help=(
            "Optional explicit path for the maintenance-class compact Fine-Gray CSV. "
            "Operational-intent output still uses the standard output-dir filename."
        ),
    )
    parser.add_argument(
        "--repo",
        action="append",
        help="Limit to one or more repos, as owner/repo or owner__repo. Supports comma-separated values.",
    )
    parser.add_argument(
        "--repo-file",
        type=Path,
        help="Optional text file containing repos to analyze.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Number of worker processes for per-repo loading.",
    )
    parser.add_argument(
        "--observation-end",
        default=DEFAULT_OBSERVATION_END,
        help=(
            "Censoring date for survived lines. Date-only values are treated as "
            "end-of-day UTC."
        ),
    )
    parser.add_argument(
        "--origin-start",
        default=DEFAULT_ORIGIN_START,
        help=(
            "Inclusive origin commit lower bound. Accepts YYYY-MM, YYYY-MM-DD, "
            f"or ISO datetime. Use 'none' for open. Default: {DEFAULT_ORIGIN_START}."
        ),
    )
    parser.add_argument(
        "--origin-end",
        default=DEFAULT_ORIGIN_END,
        help=(
            "Inclusive origin commit upper bound. Accepts YYYY-MM, YYYY-MM-DD, "
            f"or ISO datetime. Use 'none' for open. Default: {DEFAULT_ORIGIN_END}."
        ),
    )
    parser.add_argument(
        "--max-tracked-lines",
        type=int,
        default=DEFAULT_MAX_TRACKED_LINES,
        help=(
            "Exclude origin commits with more tracked lifecycle lines than this "
            "cap after role/time filters. Set to 0 to disable. "
            f"Default: {DEFAULT_MAX_TRACKED_LINES}."
        ),
    )
    parser.add_argument(
        "--duration-precision",
        type=int,
        default=DEFAULT_DURATION_PRECISION,
        help=f"Decimal places for duration-day aggregation. Default: {DEFAULT_DURATION_PRECISION}.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-repo loading progress.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.duration_precision < 0:
        raise ValueError("--duration-precision must be non-negative")
    if args.max_tracked_lines is not None and args.max_tracked_lines < 0:
        raise ValueError("--max-tracked-lines must be non-negative")

    observation_end = parse_observation_end(args.observation_end)
    if observation_end is None:
        raise ValueError(f"invalid --observation-end: {args.observation_end}")
    origin_start = parse_time_bound(args.origin_start, is_end=False)
    if args.origin_start and origin_start is None and str(args.origin_start).lower() not in {
        "none",
        "all",
        "open",
    }:
        raise ValueError(f"invalid --origin-start: {args.origin_start}")
    origin_end = parse_time_bound(args.origin_end, is_end=True)
    if args.origin_end and origin_end is None and str(args.origin_end).lower() not in {
        "none",
        "all",
        "open",
    }:
        raise ValueError(f"invalid --origin-end: {args.origin_end}")
    if origin_start is not None and origin_end is not None and origin_start > origin_end:
        raise ValueError("--origin-start must be <= --origin-end")
    max_tracked_lines = None if args.max_tracked_lines == 0 else args.max_tracked_lines

    repos = split_csv(args.repo)
    repo_dirs = resolve_repo_dirs(
        args.input_dir,
        repo_file=args.repo_file,
        repos=repos,
        required_files=[
            LINE_LIFECYCLE_FILE,
            COMMIT_CLASSIFICATION_FILE,
            COMMIT_FILE_CLASSIFICATION_FILE,
        ],
    )
    repo_data = load_repo_data_parallel(
        repo_dirs,
        load_repo_fine_gray_data,
        observation_end=observation_end,
        origin_start=origin_start,
        origin_end=origin_end,
        max_tracked_lines=max_tracked_lines,
        duration_precision=args.duration_precision,
        workers=args.workers,
        default_worker_limit=4,
        task_label="RQ1 Fine-Gray maintenance-class data",
        logger=LOG,
        result_summary=repo_summary,
        preserve_order=True,
    )

    for taxonomy in EVENT_TAXONOMIES:
        output_path = (
            args.output_file
            if taxonomy == "maintenance_class" and args.output_file is not None
            else fine_gray_csv_path(args.output_dir, taxonomy)
        )
        export_summary = write_fine_gray_csv(
            repo_data,
            output_path,
            taxonomy=taxonomy,
            observation_end=observation_end,
            origin_start=origin_start,
            origin_end=origin_end,
            max_tracked_lines=max_tracked_lines,
        )
        summary_path = summary_csv_path(args.output_dir, taxonomy)
        write_summary_csv(repo_data, export_summary, summary_path)
        print_summary(export_summary, summary_path)


if __name__ == "__main__":
    main()
