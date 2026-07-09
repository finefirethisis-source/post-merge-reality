from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.webscrape.commit_contribution_classifier import (  # noqa: E402
    DEFAULT_FILE_OUTPUT_NAME,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_NAME,
    OPERATIONAL_INTENT_ORDER,
    choose_maintenance_classes,
    classify_file_change,
    discover_repo_dirs,
    read_jsonl,
)


DEFAULT_LLM_OUTPUT_NAME = "llm_commit_contribution_classifications.jsonl"
DEFAULT_REPORT_NAME = "llm_classification_apply_report.json"
CONFIDENCE_ORDER = ("low", "medium", "high")
DEFAULT_TARGET_PRIMARY_INTENTS = ("mixed", "unknown")
OLD_APPLY_DETAIL_FIELDS = (
    "heuristic_maintenance_class",
    "heuristic_maintenance_classes",
    "heuristic_primary_operational_intent",
    "heuristic_operational_intents",
    "heuristic_confidence",
    "llm_classification_applied",
    "llm_classification_status",
    "llm_primary_operational_intent",
    "llm_operational_intents",
    "llm_confidence",
    "llm_model",
    "llm_application",
)
OLD_FILE_APPLY_DETAIL_FIELDS = (
    "commit_llm_classification_applied",
)


@dataclass
class RepoApplyReport:
    repo_dir: str
    llm_path: str
    commit_input_path: str
    commit_output_path: str
    file_input_path: str
    file_output_path: str
    dry_run: bool
    commit_rows_read: int = 0
    file_rows_read: int = 0
    llm_rows_read: int = 0
    valid_llm_rows: int = 0
    invalid_llm_rows: int = 0
    ignored_llm_rows: int = 0
    duplicate_llm_keys: int = 0
    target_commit_rows: int = 0
    applied_commit_rows: int = 0
    unchanged_commit_rows: int = 0
    missing_llm_rows: int = 0
    skipped_non_target_rows: int = 0
    applied_file_rows: int = 0
    unchanged_file_rows: int = 0
    missing_file_rows: int = 0
    llm_status_counts: Counter[str] = field(default_factory=Counter)
    llm_confidence_counts: Counter[str] = field(default_factory=Counter)
    applied_intent_counts: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_dir": self.repo_dir,
            "llm_path": self.llm_path,
            "commit_input_path": self.commit_input_path,
            "commit_output_path": self.commit_output_path,
            "file_input_path": self.file_input_path,
            "file_output_path": self.file_output_path,
            "dry_run": self.dry_run,
            "commit_rows_read": self.commit_rows_read,
            "file_rows_read": self.file_rows_read,
            "llm_rows_read": self.llm_rows_read,
            "valid_llm_rows": self.valid_llm_rows,
            "invalid_llm_rows": self.invalid_llm_rows,
            "ignored_llm_rows": self.ignored_llm_rows,
            "duplicate_llm_keys": self.duplicate_llm_keys,
            "target_commit_rows": self.target_commit_rows,
            "applied_commit_rows": self.applied_commit_rows,
            "unchanged_commit_rows": self.unchanged_commit_rows,
            "missing_llm_rows": self.missing_llm_rows,
            "skipped_non_target_rows": self.skipped_non_target_rows,
            "applied_file_rows": self.applied_file_rows,
            "unchanged_file_rows": self.unchanged_file_rows,
            "missing_file_rows": self.missing_file_rows,
            "llm_status_counts": dict(sorted(self.llm_status_counts.items())),
            "llm_confidence_counts": dict(sorted(self.llm_confidence_counts.items())),
            "applied_intent_counts": dict(sorted(self.applied_intent_counts.items())),
        }


def normalize_repo_filter(repo: str) -> str:
    return repo.replace("/", "__")


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    tmp_path.replace(path)


def backup_file(path: Path) -> None:
    if not path.exists():
        return
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)


def row_key(row: dict[str, Any]) -> str | None:
    sha = row.get("sha")
    if isinstance(sha, str) and sha.strip():
        return f"sha:{sha.strip()}"
    repo = row.get("repo") or row.get("repo_key")
    subject = row.get("message_subject")
    repo_text = str(repo).strip() if repo is not None else ""
    subject_text = str(subject).strip() if subject is not None else ""
    if repo_text or subject_text:
        return f"fallback:{repo_text}\0{subject_text}"
    return None


def valid_operational_intents(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(intent in OPERATIONAL_INTENT_ORDER for intent in value)
    )


def confidence_allowed(confidence: Any, min_confidence: str) -> bool:
    if confidence not in CONFIDENCE_ORDER:
        return False
    return CONFIDENCE_ORDER.index(confidence) >= CONFIDENCE_ORDER.index(min_confidence)


def is_valid_llm_row(row: dict[str, Any], min_confidence: str) -> bool:
    return (
        row.get("classification_status") == "classified"
        and row.get("llm_primary_operational_intent") in OPERATIONAL_INTENT_ORDER
        and valid_operational_intents(row.get("llm_operational_intents"))
        and confidence_allowed(row.get("llm_confidence"), min_confidence)
    )


def load_llm_rows(
    path: Path,
    *,
    min_confidence: str,
    report: RepoApplyReport,
) -> dict[str, dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest_by_key

    for row in read_jsonl(path):
        report.llm_rows_read += 1
        status = str(row.get("classification_status") or "missing")
        report.llm_status_counts[status] += 1
        confidence = row.get("llm_confidence")
        if isinstance(confidence, str):
            report.llm_confidence_counts[confidence] += 1
        key = row_key(row)
        if key is None or not is_valid_llm_row(row, min_confidence):
            if status == "classified":
                report.invalid_llm_rows += 1
            else:
                report.ignored_llm_rows += 1
            continue
        if key in latest_by_key:
            report.duplicate_llm_keys += 1
        latest_by_key[key] = row
        report.valid_llm_rows += 1
    return latest_by_key


def normalized_llm_intents(llm_row: dict[str, Any]) -> tuple[str, list[str]]:
    primary = str(llm_row["llm_primary_operational_intent"])
    raw_intents = [
        intent
        for intent in llm_row.get("llm_operational_intents") or []
        if intent in OPERATIONAL_INTENT_ORDER
    ]

    if primary == "unknown":
        return primary, ["unknown"]
    if primary == "mixed":
        intents = [intent for intent in raw_intents if intent != "unknown"]
        if "mixed" not in intents:
            intents.append("mixed")
    else:
        intents = [
            intent
            for intent in raw_intents
            if intent not in {"mixed", "unknown"}
        ]
        if primary not in intents:
            intents.append(primary)

    unique_intents = sorted(set(intents), key=OPERATIONAL_INTENT_ORDER.index)
    return primary, unique_intents or [primary]


def llm_scores(primary: str, intents: list[str]) -> Counter[str]:
    scores: Counter[str] = Counter()
    for intent in intents:
        if intent not in {"mixed", "unknown"}:
            scores[intent] += 1
    if primary not in {"mixed", "unknown"}:
        scores[primary] += 1
    return scores


def source_primary(row: dict[str, Any]) -> str:
    return str(
        row.get("heuristic_primary_operational_intent")
        or row.get("primary_operational_intent")
        or ""
    )


def is_target_row(
    row: dict[str, Any],
    target_primary_intents: set[str],
    *,
    apply_all_llm_results: bool,
) -> bool:
    return apply_all_llm_results or source_primary(row) in target_primary_intents


def apply_llm_to_commit_row(
    row: dict[str, Any],
    llm_row: dict[str, Any],
    report: RepoApplyReport,
) -> bool:
    original_row = dict(row)
    previous = {
        "maintenance_class": row.get("maintenance_class"),
        "maintenance_classes": row.get("maintenance_classes"),
        "primary_operational_intent": row.get("primary_operational_intent"),
        "operational_intents": row.get("operational_intents"),
        "confidence": row.get("confidence"),
    }
    primary, intents = normalized_llm_intents(llm_row)
    maintenance_class, maintenance_classes = choose_maintenance_classes(
        primary,
        intents,
        llm_scores(primary, intents),
    )
    confidence = str(llm_row.get("llm_confidence"))

    for field_name in OLD_APPLY_DETAIL_FIELDS:
        row.pop(field_name, None)
    evidence = row.get("evidence")
    if isinstance(evidence, list):
        row["evidence"] = [
            item
            for item in evidence
            if not (
                isinstance(item, dict)
                and item.get("source") == "llm"
                and item.get("rule") == "llm_judge_classification"
            )
        ]

    row["maintenance_class"] = maintenance_class
    row["maintenance_classes"] = maintenance_classes
    row["primary_operational_intent"] = primary
    row["operational_intents"] = intents
    row["confidence"] = confidence
    row["labeled_by_llm"] = True
    report.applied_intent_counts[primary] += 1

    return row != original_row


def commit_like_from_classification(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": row.get("repo"),
        "sha": row.get("sha"),
        "date": row.get("date"),
        "message_subject": row.get("message_subject"),
    }


def recompute_file_row(
    old_row: dict[str, Any],
    commit_row: dict[str, Any],
) -> dict[str, Any]:
    file_change = {
        "filename": old_row.get("filename"),
        "previous_filename": old_row.get("previous_filename"),
        "status": old_row.get("status"),
        "additions": old_row.get("additions"),
        "deletions": old_row.get("deletions"),
        "changes": old_row.get("changed_lines"),
    }
    recomputed = classify_file_change(
        commit_like_from_classification(commit_row),
        file_change,
        commit_row,
        repo_key=commit_row.get("repo_key"),
        is_origin_commit=commit_row.get("is_origin_commit"),
        is_observation_commit=commit_row.get("is_observation_commit"),
    )
    new_row = dict(old_row)
    new_row.update(recomputed)
    new_row["input_commit_file"] = old_row.get("input_commit_file")
    for field_name in OLD_FILE_APPLY_DETAIL_FIELDS:
        new_row.pop(field_name, None)
    return new_row


def apply_file_rows(
    file_rows: list[dict[str, Any]],
    updated_commits_by_sha: dict[str, dict[str, Any]],
    report: RepoApplyReport,
) -> list[dict[str, Any]]:
    if not file_rows:
        if updated_commits_by_sha:
            report.missing_file_rows = len(updated_commits_by_sha)
        return file_rows

    seen_updated_shas: set[str] = set()
    output_rows: list[dict[str, Any]] = []
    for row in file_rows:
        sha = row.get("commit_sha")
        commit_row = updated_commits_by_sha.get(sha) if isinstance(sha, str) else None
        if commit_row is None:
            output_rows.append(row)
            continue
        seen_updated_shas.add(sha)
        new_row = recompute_file_row(row, commit_row)
        if new_row != row:
            report.applied_file_rows += 1
        else:
            report.unchanged_file_rows += 1
        output_rows.append(new_row)

    report.missing_file_rows = len(set(updated_commits_by_sha) - seen_updated_shas)
    return output_rows


def apply_repo_dir(
    repo_dir: Path,
    *,
    llm_dir: Path | None,
    commit_input_name: str,
    commit_output_name: str,
    file_input_name: str,
    file_output_name: str,
    llm_name: str,
    report_name: str,
    min_llm_confidence: str,
    target_primary_intents: set[str],
    apply_all_llm_results: bool,
    fail_on_missing_llm: bool,
    dry_run: bool,
    backup: bool,
) -> RepoApplyReport:
    llm_repo_dir = (llm_dir / repo_dir.name) if llm_dir is not None else repo_dir
    llm_path = llm_repo_dir / llm_name
    commit_input_path = repo_dir / commit_input_name
    commit_output_path = repo_dir / commit_output_name
    file_input_path = repo_dir / file_input_name
    file_output_path = repo_dir / file_output_name
    report_path = repo_dir / report_name

    report = RepoApplyReport(
        repo_dir=str(repo_dir),
        llm_path=str(llm_path),
        commit_input_path=str(commit_input_path),
        commit_output_path=str(commit_output_path),
        file_input_path=str(file_input_path),
        file_output_path=str(file_output_path),
        dry_run=dry_run,
    )
    commit_rows = read_jsonl(commit_input_path)
    file_rows = read_jsonl(file_input_path)
    report.commit_rows_read = len(commit_rows)
    report.file_rows_read = len(file_rows)
    llm_rows_by_key = load_llm_rows(
        llm_path,
        min_confidence=min_llm_confidence,
        report=report,
    )

    updated_commits_by_sha: dict[str, dict[str, Any]] = {}
    output_commit_rows: list[dict[str, Any]] = []
    missing_llm_rows: list[dict[str, Any]] = []

    for row in commit_rows:
        key = row_key(row)
        llm_row = llm_rows_by_key.get(key) if key is not None else None
        target = is_target_row(
            row,
            target_primary_intents,
            apply_all_llm_results=apply_all_llm_results,
        )
        if target:
            report.target_commit_rows += 1
        if llm_row is None:
            if target:
                report.missing_llm_rows += 1
                missing_llm_rows.append(row)
            else:
                report.skipped_non_target_rows += 1
            output_commit_rows.append(row)
            continue
        if not target:
            report.skipped_non_target_rows += 1
            output_commit_rows.append(row)
            continue

        new_row = dict(row)
        changed = apply_llm_to_commit_row(new_row, llm_row, report)
        if changed:
            report.applied_commit_rows += 1
        else:
            report.unchanged_commit_rows += 1
        sha = new_row.get("sha")
        if isinstance(sha, str) and sha:
            updated_commits_by_sha[sha] = new_row
        output_commit_rows.append(new_row)

    if fail_on_missing_llm and missing_llm_rows:
        examples = ", ".join(
            str(row.get("sha") or row.get("message_subject") or "?")
            for row in missing_llm_rows[:5]
        )
        raise RuntimeError(
            f"{repo_dir} has {len(missing_llm_rows)} target commit row(s) without "
            f"a valid LLM classification; examples: {examples}"
        )

    output_file_rows = apply_file_rows(file_rows, updated_commits_by_sha, report)

    if not dry_run:
        if backup:
            backup_file(commit_output_path)
            if file_input_path.exists():
                backup_file(file_output_path)
            backup_file(report_path)
        write_jsonl_atomic(commit_output_path, output_commit_rows)
        if file_rows or file_input_path.exists():
            write_jsonl_atomic(file_output_path, output_file_rows)
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply llm_commit_contribution_classifications.jsonl results onto "
            "commit contribution classification outputs."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing per-repo webscrape outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--llm-dir",
        type=Path,
        help=(
            "Directory containing per-repo LLM outputs. Default: use each repo's "
            "input output directory."
        ),
    )
    parser.add_argument(
        "--repo-dir",
        action="append",
        type=Path,
        default=[],
        help="Specific repo output directory to process. Can be repeated.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit to a repo, either owner/repo or owner__repo. Can be repeated.",
    )
    parser.add_argument(
        "--input-name",
        default=DEFAULT_OUTPUT_NAME,
        help=f"Commit classification input filename. Default: {DEFAULT_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--file-input-name",
        default=DEFAULT_FILE_OUTPUT_NAME,
        help=f"File classification input filename. Default: {DEFAULT_FILE_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--llm-name",
        default=DEFAULT_LLM_OUTPUT_NAME,
        help=f"LLM classification filename. Default: {DEFAULT_LLM_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--output-name",
        default=DEFAULT_OUTPUT_NAME,
        help=(
            "Commit classification output filename. Default overwrites the input "
            f"filename: {DEFAULT_OUTPUT_NAME}"
        ),
    )
    parser.add_argument(
        "--file-output-name",
        default=DEFAULT_FILE_OUTPUT_NAME,
        help=(
            "File classification output filename. Default overwrites the input "
            f"filename: {DEFAULT_FILE_OUTPUT_NAME}"
        ),
    )
    parser.add_argument(
        "--report-name",
        default=DEFAULT_REPORT_NAME,
        help=f"Per-repo JSON report filename. Default: {DEFAULT_REPORT_NAME}",
    )
    parser.add_argument(
        "--min-llm-confidence",
        choices=CONFIDENCE_ORDER,
        default="low",
        help="Minimum LLM confidence to apply. Default: low.",
    )
    parser.add_argument(
        "--target-primary",
        action="append",
        default=[],
        choices=OPERATIONAL_INTENT_ORDER,
        help=(
            "Heuristic primary intent eligible for replacement. Can be repeated. "
            "Default: mixed and unknown."
        ),
    )
    parser.add_argument(
        "--apply-all-llm-results",
        action="store_true",
        help="Apply every valid LLM row regardless of the heuristic primary intent.",
    )
    parser.add_argument(
        "--fail-on-missing-llm",
        action="store_true",
        help="Fail if any target commit row has no valid LLM classification.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print reports without writing files.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create .bak files before overwriting outputs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.repo_dir:
        repo_dirs = sorted(args.repo_dir, key=lambda path: str(path))
    else:
        repo_filters = {normalize_repo_filter(repo) for repo in args.repo} if args.repo else None
        repo_dirs = discover_repo_dirs(args.input_dir, repo_filters)
    if not repo_dirs:
        requested = f" matching {sorted(args.repo)}" if args.repo else ""
        raise SystemExit(f"No webscrape output directories found in {args.input_dir}{requested}.")

    target_primary_intents = (
        set(args.target_primary)
        if args.target_primary
        else set(DEFAULT_TARGET_PRIMARY_INTENTS)
    )
    reports: list[dict[str, Any]] = []
    for index, repo_dir in enumerate(repo_dirs, start=1):
        print(f"[{index}/{len(repo_dirs)}] apply LLM classifications: {repo_dir}", file=sys.stderr)
        report = apply_repo_dir(
            repo_dir,
            llm_dir=args.llm_dir,
            commit_input_name=args.input_name,
            commit_output_name=args.output_name,
            file_input_name=args.file_input_name,
            file_output_name=args.file_output_name,
            llm_name=args.llm_name,
            report_name=args.report_name,
            min_llm_confidence=args.min_llm_confidence,
            target_primary_intents=target_primary_intents,
            apply_all_llm_results=args.apply_all_llm_results,
            fail_on_missing_llm=args.fail_on_missing_llm,
            dry_run=args.dry_run,
            backup=args.backup,
        )
        print(
            "  "
            f"commit_rows={report.commit_rows_read}, llm_valid={report.valid_llm_rows}, "
            f"applied_commits={report.applied_commit_rows}, "
            f"applied_files={report.applied_file_rows}, missing_llm={report.missing_llm_rows}",
            file=sys.stderr,
        )
        reports.append(report.to_dict())

    print(json.dumps(reports, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
