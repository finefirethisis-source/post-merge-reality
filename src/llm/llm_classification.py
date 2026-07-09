from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.llm.model import (
    DEFAULT_OPENROUTER_API_URL,
    DEFAULT_OPENROUTER_MODEL,
    ChatMessage,
    LLMClient,
    ModelNotConfiguredError,
    ModelRequest,
    OpenRouterClient,
)
from src.webscrape.commit_contribution_classifier import (
    OPERATIONAL_INTENT_ORDER,
    read_jsonl,
)


DEFAULT_OUTPUT_NAME = "llm_commit_contribution_classifications.jsonl"
DEFAULT_REQUEST_OUTPUT_NAME = "llm_classification_requests.jsonl"
LLM_SELECTED_PRIMARY_INTENTS = frozenset({"mixed", "unknown"})
LOGGER = logging.getLogger(__name__)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
REAL_OPERATIONAL_INTENTS = tuple(
    intent for intent in OPERATIONAL_INTENT_ORDER if intent not in {"mixed", "unknown"}
)

INTENT_DEFINITIONS = {
    "security_fix": "Fixes or hardens a security issue, vulnerability, auth flaw, secret exposure, or permission problem.",
    "revert": "Reverts an earlier change or backs out a commit.",
    "bug_fix": "Corrects broken, incorrect, crashing, failing, or regressed behavior.",
    "dependency_update": "Updates dependency manifests, lockfiles, package versions, vendored dependency files, or dependency bot output.",
    "merge_release_versioning": "Merges branches or pull requests, prepares releases, bumps project versions, or updates changelog/release files.",
    "performance": "Improves speed, memory, caching, latency, throughput, or scalability.",
    "feature": "Adds or exposes new user-visible or developer-facing capability.",
    "refactor_cleanup": "Restructures, simplifies, renames, removes dead code, or cleans implementation without intended behavior change.",
    "test": "Adds or changes tests, test fixtures, test infrastructure, or coverage.",
    "documentation": "Adds or changes documentation, comments intended as docs, examples, guides, README, changelog text, or prose explanations.",
    "resource": "Adds or changes assets, generated static resources, localization, fixtures, datasets, images, fonts, or other non-code resource files.",
    "build_config_ci": "Changes build systems, CI workflows, linters, formatting tools, project config, packaging config, or developer automation.",
    "style_formatting": "Formatting-only or style-only changes with no intended behavioral change.",
}

JUDGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "primary_operational_intent",
        "operational_intents",
        "confidence",
    ],
    "properties": {
        "primary_operational_intent": {
            "type": "string",
            "enum": list(OPERATIONAL_INTENT_ORDER),
        },
        "operational_intents": {
            "type": "array",
            "items": {"type": "string", "enum": list(OPERATIONAL_INTENT_ORDER)},
            "minItems": 1,
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}


@dataclass(frozen=True)
class LLMClassificationSettings:
    max_files: int = 5
    max_patch_chars_per_file: int = 500
    include_patch: bool = False


@dataclass(frozen=True)
class ClassificationRunSummary:
    input_path: Path
    output_path: Path
    requests_output_path: Path | None
    total_rows: int
    selected_rows: int
    classified_rows: int
    error_rows: int
    pending_rows: int = 0
    reused_rows: int = 0
    model_request_rows: int = 0

    @property
    def ambiguous_rows(self) -> int:
        return self.selected_rows


def should_send_to_llm(classification: dict[str, Any]) -> bool:
    return str(classification.get("primary_operational_intent") or "") in LLM_SELECTED_PRIMARY_INTENTS


def truncate_text(value: Any, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if max_chars <= 0:
        return None
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars].rstrip()}\n[truncated {omitted} chars]"


def summarize_file_change(
    file_change: dict[str, Any],
    settings: LLMClassificationSettings,
) -> dict[str, Any]:
    summary = {
        "path": file_change.get("filename"),
        "previous_path": file_change.get("previous_filename"),
        "status": file_change.get("status"),
        "additions": file_change.get("additions"),
        "deletions": file_change.get("deletions"),
        "changes": file_change.get("changes"),
    }
    if settings.include_patch:
        patch_excerpt = truncate_text(file_change.get("patch"), settings.max_patch_chars_per_file)
        if patch_excerpt:
            summary["patch_excerpt"] = patch_excerpt
    return {key: value for key, value in summary.items() if value is not None}


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def summarize_files(
    files: list[dict[str, Any]],
    settings: LLMClassificationSettings,
) -> dict[str, Any]:
    visible_files = files[: settings.max_files]
    additions = sum(safe_int(file_change.get("additions")) for file_change in files)
    deletions = sum(safe_int(file_change.get("deletions")) for file_change in files)
    return {
        "total_files_changed": len(files),
        "total_additions": additions,
        "total_deletions": deletions,
        "files_omitted": max(0, len(files) - len(visible_files)),
        "files": [
            summarize_file_change(file_change, settings)
            for file_change in visible_files
        ],
    }


def build_llm_case(
    commit: dict[str, Any],
    source_classification: dict[str, Any],
    files: list[dict[str, Any]],
    settings: LLMClassificationSettings | None = None,
) -> dict[str, Any]:
    settings = settings or LLMClassificationSettings()
    return {
        "task": "Classify the operational intent of this commit.",
        "commit": {
            "repo": commit.get("repo") or source_classification.get("repo"),
            "repo_key": source_classification.get("repo_key"),
            "sha": commit.get("sha") or source_classification.get("sha"),
            "date": commit.get("date") or source_classification.get("date"),
            "message_subject": commit.get("message_subject")
            or source_classification.get("message_subject"),
            "message_body": commit.get("message_body"),
            "author_login": commit.get("author_login"),
            "committer_login": commit.get("committer_login"),
            "git_author_name": commit.get("git_author_name"),
            "git_author_email": commit.get("git_author_email"),
            "labels": commit.get("labels") or [],
        },
        "changed_files": summarize_files(files, settings),
    }


def build_system_prompt() -> str:
    intent_lines = "\n".join(
        f"- {intent}: {INTENT_DEFINITIONS[intent]}"
        for intent in REAL_OPERATIONAL_INTENTS
    )
    return (
        "You are a careful software repository analysis judge. "
        "Your job is to classify software commits by operational intent.\n\n"
        "Classify the commit's operational intent from the repository name, commit subject, "
        "commit body, and changed file paths. "
        "Choose the dominant concrete intent when one purpose plausibly explains the commit. "
        "Avoid mixed and unknown labels. "
        "Use mixed only when you are very sure that two or more independent purposes are genuinely co-primary. "
        "Use unknown only when you are very confused because the supplied commit and changed-file context is too incomplete or contradictory to support any concrete intent.\n\n"
        "Allowed operational intents:\n"
        f"{intent_lines}\n"
        "- mixed: Multiple independent operational intents are clearly co-primary.\n"
        "- unknown: The supplied commit and changed-file context is too incomplete or contradictory to support any concrete intent.\n\n"
        "Return strict JSON only. Do not include markdown."
    )


def format_prompt_value(value: Any, missing: str = "(not supplied)") -> str:
    if value is None:
        return missing
    if isinstance(value, str):
        return value.strip() or missing
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "(none)"
    return str(value)


def format_patch_excerpt(value: Any) -> str:
    text = format_prompt_value(value)
    return text.replace("```", "`` `")


def build_prompt_context(case: dict[str, Any]) -> str:
    commit = case.get("commit") or {}
    changed_files = case.get("changed_files") or {}
    lines = [
        "# Commit",
        "",
        f"Repository: {format_prompt_value(commit.get('repo'))}",
        "",
        "## Commit Subject",
        format_prompt_value(commit.get("message_subject")),
        "",
        "## Commit Body",
        format_prompt_value(commit.get("message_body"), missing="(no commit body supplied)"),
        "",
        "# Changed Files",
    ]

    files = changed_files.get("files") or []
    if not files:
        lines.extend(["", "No changed file details were supplied."])
        return "\n".join(lines)

    for file_change in files:
        path = format_prompt_value(file_change.get("path"), missing="(unknown path)")
        lines.append(f"- {path}")
        patch_excerpt = file_change.get("patch_excerpt")
        if patch_excerpt:
            lines.extend(
                [
                    "",
                    "### Patch Excerpt",
                    "```diff",
                    format_patch_excerpt(patch_excerpt),
                    "```",
                ]
            )
    return "\n".join(lines)


def build_user_prompt(case: dict[str, Any]) -> str:
    return (
        "Classify this commit. "
        "Return JSON matching the response schema.\n\n"
        f"{build_prompt_context(case)}"
    )


def build_classification_request(
    commit: dict[str, Any],
    source_classification: dict[str, Any],
    files: list[dict[str, Any]],
    settings: LLMClassificationSettings | None = None,
) -> ModelRequest:
    case = build_llm_case(commit, source_classification, files, settings)
    return ModelRequest(
        messages=[
            ChatMessage(role="system", content=build_system_prompt()),
            ChatMessage(role="user", content=build_user_prompt(case)),
        ],
        temperature=0.0,
        response_schema=JUDGE_RESPONSE_SCHEMA,
        metadata={
            "repo": case["commit"].get("repo"),
            "sha": case["commit"].get("sha"),
        },
    )


def parse_judge_json(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM judge response must be a JSON object")
    primary_intent = parsed.get("primary_operational_intent")
    if primary_intent not in OPERATIONAL_INTENT_ORDER:
        raise ValueError(f"Invalid primary operational intent: {primary_intent!r}")
    operational_intents = parsed.get("operational_intents")
    if (
        not isinstance(operational_intents, list)
        or not operational_intents
        or any(intent not in OPERATIONAL_INTENT_ORDER for intent in operational_intents)
    ):
        raise ValueError(f"Invalid operational intents: {operational_intents!r}")
    confidence = parsed.get("confidence")
    if confidence not in {"high", "medium", "low"}:
        raise ValueError(f"Invalid confidence: {confidence!r}")
    return parsed


def model_request_to_dict(request: ModelRequest) -> dict[str, Any]:
    return {
        "messages": [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ],
        "temperature": request.temperature,
        "response_schema": request.response_schema,
        "metadata": request.metadata,
    }


def write_jsonl_row(handle: TextIO, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, sort_keys=True) + "\n")
    handle.flush()


def result_row_key(row: dict[str, Any]) -> str | None:
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


def is_valid_classified_result_row(row: dict[str, Any]) -> bool:
    return (
        row.get("llm_primary_operational_intent") in OPERATIONAL_INTENT_ORDER
        and valid_operational_intents(row.get("llm_operational_intents"))
        and row.get("llm_confidence") in {"high", "medium", "low"}
    )


def is_reusable_result_row(
    row: dict[str, Any],
    *,
    client_available: bool,
    retry_errors: bool,
) -> bool:
    status = row.get("classification_status")
    if status == "classified":
        return is_valid_classified_result_row(row)
    if status == "pending_model":
        return not client_available
    if status == "model_error":
        return not retry_errors
    return False


def load_reusable_result_rows(
    output_path: Path,
    *,
    client_available: bool,
    retry_errors: bool,
) -> dict[str, dict[str, Any]]:
    reusable_rows: dict[str, dict[str, Any]] = {}
    if not output_path.exists():
        return reusable_rows

    ignored_rows = 0
    invalid_rows = 0
    with output_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid_rows += 1
                LOGGER.warning(
                    "ignoring invalid JSONL row in existing LLM output %s:%d",
                    output_path,
                    line_number,
                )
                continue
            if not isinstance(row, dict):
                invalid_rows += 1
                LOGGER.warning(
                    "ignoring non-object JSONL row in existing LLM output %s:%d",
                    output_path,
                    line_number,
                )
                continue
            key = result_row_key(row)
            if key is None or not is_reusable_result_row(
                row,
                client_available=client_available,
                retry_errors=retry_errors,
            ):
                ignored_rows += 1
                continue
            reusable_rows[key] = row

    LOGGER.info(
        "loaded %d reusable existing LLM result row(s) from %s; ignored=%d invalid=%d",
        len(reusable_rows),
        output_path,
        ignored_rows,
        invalid_rows,
    )
    return reusable_rows


def count_result_status(row: dict[str, Any]) -> tuple[int, int, int]:
    status = row.get("classification_status")
    if status == "classified":
        return 1, 0, 0
    if status == "model_error":
        return 0, 1, 0
    if status == "pending_model":
        return 0, 0, 1
    return 0, 0, 0


def default_output_path(classification_path: Path) -> Path:
    return classification_path.with_name(DEFAULT_OUTPUT_NAME)


def default_requests_output_path(classification_path: Path) -> Path:
    return classification_path.with_name(DEFAULT_REQUEST_OUTPUT_NAME)


def infer_commit_paths(
    classification_path: Path,
    classifications: list[dict[str, Any]],
) -> list[Path]:
    repo_dir = classification_path.parent
    names: list[str] = []
    for row in classifications:
        input_name = row.get("input_commit_file")
        if isinstance(input_name, str) and input_name and input_name not in names:
            names.append(input_name)
    for fallback_name in ("observation_commits.jsonl", "all_commits.jsonl"):
        if fallback_name not in names:
            names.append(fallback_name)
    return [repo_dir / name for name in names]


def load_commits_by_sha(paths: list[Path]) -> dict[str, dict[str, Any]]:
    commits_by_sha: dict[str, dict[str, Any]] = {}
    for path in paths:
        for commit in read_jsonl(path):
            sha = commit.get("sha")
            if isinstance(sha, str) and sha not in commits_by_sha:
                commits_by_sha[sha] = commit
    return commits_by_sha


def load_file_changes_by_sha(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for file_change in read_jsonl(path):
        sha = file_change.get("commit_sha")
        if isinstance(sha, str):
            grouped.setdefault(sha, []).append(file_change)
    return grouped


def commit_files_from_commit(commit: dict[str, Any]) -> list[dict[str, Any]]:
    files = commit.get("files")
    if isinstance(files, list):
        return [file_change for file_change in files if isinstance(file_change, dict)]
    return []


def file_change_lookup_keys(file_change: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("filename", "path", "previous_filename", "previous_path"):
        value = file_change.get(field)
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    return keys


def merge_embedded_patch_context(
    grouped_changes: list[dict[str, Any]],
    embedded_changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not grouped_changes or not embedded_changes:
        return grouped_changes

    embedded_by_path: dict[str, dict[str, Any]] = {}
    for embedded_change in embedded_changes:
        for key in file_change_lookup_keys(embedded_change):
            embedded_by_path.setdefault(key, embedded_change)

    merged_changes: list[dict[str, Any]] = []
    for index, file_change in enumerate(grouped_changes):
        merged = dict(file_change)
        if not merged.get("patch"):
            embedded_match = None
            for key in file_change_lookup_keys(file_change):
                embedded_match = embedded_by_path.get(key)
                if embedded_match is not None:
                    break
            if embedded_match is None and index < len(embedded_changes):
                embedded_match = embedded_changes[index]
            if embedded_match is not None and embedded_match.get("patch"):
                merged["patch"] = embedded_match["patch"]
        merged_changes.append(merged)
    return merged_changes


def commit_context_for_classification(
    classification: dict[str, Any],
    commits_by_sha: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sha = classification.get("sha")
    commit = commits_by_sha.get(sha, {}) if isinstance(sha, str) else {}
    return {
        "repo": commit.get("repo") or classification.get("repo"),
        "sha": commit.get("sha") or classification.get("sha"),
        "date": commit.get("date") or classification.get("date"),
        "message_subject": commit.get("message_subject") or classification.get("message_subject"),
        "message_body": commit.get("message_body"),
        "author_login": commit.get("author_login"),
        "committer_login": commit.get("committer_login"),
        "git_author_name": commit.get("git_author_name"),
        "git_author_email": commit.get("git_author_email"),
        "labels": commit.get("labels") or [],
    }


def file_changes_for_classification(
    classification: dict[str, Any],
    commits_by_sha: dict[str, dict[str, Any]],
    file_changes_by_sha: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    sha = classification.get("sha")
    if isinstance(sha, str):
        commit = commits_by_sha.get(sha, {})
        commit_files = commit_files_from_commit(commit)
        grouped_changes = file_changes_by_sha.get(sha)
        if grouped_changes is not None:
            return merge_embedded_patch_context(grouped_changes, commit_files)
        if commit_files:
            return commit_files
    return []


def request_record(
    classification: dict[str, Any],
    request: ModelRequest,
) -> dict[str, Any]:
    return {
        "repo": classification.get("repo"),
        "repo_key": classification.get("repo_key"),
        "sha": classification.get("sha"),
        "message_subject": classification.get("message_subject"),
        "request": model_request_to_dict(request),
    }


def print_prompt_setup(
    classification: dict[str, Any],
    request: ModelRequest,
    output: TextIO,
) -> None:
    for message in request.messages:
        output.write(f"--- {message.role} ---\n")
        output.write(message.content.rstrip())
        output.write("\n\n")
    output.write("\n")


def pending_result_row(
    classification: dict[str, Any],
    request: ModelRequest,
) -> dict[str, Any]:
    return {
        "repo": classification.get("repo"),
        "repo_key": classification.get("repo_key"),
        "sha": classification.get("sha"),
        "message_subject": classification.get("message_subject"),
        "classification_status": "pending_model",
        "heuristic_primary_operational_intent": classification.get("primary_operational_intent"),
        "heuristic_operational_intents": classification.get("operational_intents") or [],
        "request_metadata": request.metadata,
    }


def classified_result_row(
    classification: dict[str, Any],
    judge_result: dict[str, Any],
    model_name: str | None,
    model_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "repo": classification.get("repo"),
        "repo_key": classification.get("repo_key"),
        "sha": classification.get("sha"),
        "message_subject": classification.get("message_subject"),
        "classification_status": "classified",
        "heuristic_primary_operational_intent": classification.get("primary_operational_intent"),
        "heuristic_operational_intents": classification.get("operational_intents") or [],
        "heuristic_maintenance_class": classification.get("maintenance_class"),
        "heuristic_maintenance_classes": classification.get("maintenance_classes") or [],
        "llm_primary_operational_intent": judge_result.get("primary_operational_intent"),
        "llm_operational_intents": judge_result.get("operational_intents") or [],
        "llm_confidence": judge_result.get("confidence"),
        "model": model_name,
        "model_usage": model_usage,
    }


def error_result_row(
    classification: dict[str, Any],
    exc: Exception,
    attempts: int | None = None,
) -> dict[str, Any]:
    row = {
        "repo": classification.get("repo"),
        "repo_key": classification.get("repo_key"),
        "sha": classification.get("sha"),
        "message_subject": classification.get("message_subject"),
        "classification_status": "model_error",
        "heuristic_primary_operational_intent": classification.get("primary_operational_intent"),
        "heuristic_operational_intents": classification.get("operational_intents") or [],
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if attempts is not None:
        row["model_attempts"] = attempts
    return row


def complete_and_parse_with_retries(
    client: LLMClient,
    request: ModelRequest,
    *,
    repo: Any,
    sha: Any,
    max_retries: int,
    retry_delay: float,
) -> tuple[Any, dict[str, Any], int]:
    attempts = max(1, max_retries + 1)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.complete(request)
            judge_result = parse_judge_json(response.content)
            if attempt > 1:
                LOGGER.info(
                    "model classification succeeded after retry repo=%s sha=%s attempt=%d/%d",
                    repo,
                    sha,
                    attempt,
                    attempts,
                )
            return response, judge_result, attempt
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            LOGGER.warning(
                "model classification attempt failed repo=%s sha=%s attempt=%d/%d; retrying in %.2fs: %s",
                repo,
                sha,
                attempt,
                attempts,
                retry_delay,
                exc,
            )
            if retry_delay > 0:
                time.sleep(retry_delay)

    assert last_exc is not None
    raise last_exc


def classify_selected_file(
    classification_path: Path,
    output_path: Path | None = None,
    requests_output_path: Path | None = None,
    client: LLMClient | None = None,
    settings: LLMClassificationSettings | None = None,
    commit_paths: list[Path] | None = None,
    file_changes_path: Path | None = None,
    limit: int | None = None,
    print_prompt_setups: bool = False,
    prompt_output: TextIO | None = None,
    progress_interval: int = 25,
    resume: bool = True,
    retry_errors: bool = True,
    model_retries: int = 2,
    model_retry_delay: float = 5.0,
) -> ClassificationRunSummary:
    settings = settings or LLMClassificationSettings()
    output_path = output_path or default_output_path(classification_path)
    LOGGER.info("reading previous classifications from %s", classification_path)
    classifications = read_jsonl(classification_path)
    selected_rows = [row for row in classifications if should_send_to_llm(row)]
    if limit is not None:
        selected_rows = selected_rows[: max(0, limit)]
    LOGGER.info(
        "selected %d/%d classification row(s) for LLM review%s",
        len(selected_rows),
        len(classifications),
        f" with limit={limit}" if limit is not None else "",
    )

    commit_paths = commit_paths or infer_commit_paths(classification_path, classifications)
    for commit_path in commit_paths:
        if not commit_path.exists():
            LOGGER.debug("commit metadata path does not exist: %s", commit_path)
    commits_by_sha = load_commits_by_sha(commit_paths)
    LOGGER.info(
        "loaded %d commit metadata row(s) from %d candidate path(s)",
        len(commits_by_sha),
        len(commit_paths),
    )
    if file_changes_path is None:
        inferred_file_changes_path = classification_path.with_name("commit_file_changes.jsonl")
        file_changes_path = inferred_file_changes_path if inferred_file_changes_path.exists() else None
    file_changes_by_sha = load_file_changes_by_sha(file_changes_path)
    if file_changes_path is None:
        LOGGER.info("no commit_file_changes.jsonl path found; changed-file context may be sparse")
    else:
        LOGGER.info(
            "loaded changed-file context for %d commit(s) from %s",
            len(file_changes_by_sha),
            file_changes_path,
        )

    classified_count = 0
    error_count = 0
    pending_count = 0
    reused_count = 0
    result_count = 0
    request_count = 0
    total_selected = len(selected_rows)
    progress_interval = max(0, progress_interval)
    model_retries = max(0, model_retries)
    model_retry_delay = max(0.0, model_retry_delay)
    reusable_rows: dict[str, dict[str, Any]] = {}
    if resume:
        reusable_rows = load_reusable_result_rows(
            output_path,
            client_available=client is not None,
            retry_errors=retry_errors,
        )
    elif output_path.exists():
        LOGGER.info("resume disabled; overwriting existing LLM output at %s", output_path)

    LOGGER.info("streaming LLM classification rows to %s", output_path)
    request_handle: TextIO | None = None
    with output_path.open("w", encoding="utf-8") as result_handle:
        try:
            if requests_output_path is not None:
                LOGGER.info("streaming model request rows to %s", requests_output_path)
                request_handle = requests_output_path.open("w", encoding="utf-8")
            for index, classification in enumerate(selected_rows, start=1):
                commit = commit_context_for_classification(classification, commits_by_sha)
                files = file_changes_for_classification(classification, commits_by_sha, file_changes_by_sha)
                request = build_classification_request(commit, classification, files, settings)
                repo = classification.get("repo")
                sha = classification.get("sha")
                if progress_interval and (
                    index == 1 or index % progress_interval == 0 or index == total_selected
                ):
                    LOGGER.info(
                        "processing selected commit %d/%d repo=%s sha=%s files=%d",
                        index,
                        total_selected,
                        repo,
                        sha,
                        len(files),
                    )
                else:
                    LOGGER.debug(
                        "processing selected commit %d/%d repo=%s sha=%s files=%d",
                        index,
                        total_selected,
                        repo,
                        sha,
                        len(files),
                    )
                if request_handle is not None:
                    write_jsonl_row(request_handle, request_record(classification, request))
                    request_count += 1
                if client is None and print_prompt_setups:
                    print_prompt_setup(classification, request, prompt_output or sys.stdout)
                key = result_row_key(classification)
                existing_row = reusable_rows.get(key) if key is not None else None
                if existing_row is not None:
                    write_jsonl_row(result_handle, existing_row)
                    reused_count += 1
                    result_count += 1
                    classified_delta, error_delta, pending_delta = count_result_status(existing_row)
                    classified_count += classified_delta
                    error_count += error_delta
                    pending_count += pending_delta
                    LOGGER.debug(
                        "reused existing LLM result for selected commit %d/%d repo=%s sha=%s status=%s",
                        index,
                        total_selected,
                        repo,
                        sha,
                        existing_row.get("classification_status"),
                    )
                    continue
                if client is None:
                    write_jsonl_row(result_handle, pending_result_row(classification, request))
                    pending_count += 1
                    result_count += 1
                    continue
                try:
                    response, judge_result, _attempt_count = complete_and_parse_with_retries(
                        client,
                        request,
                        repo=repo,
                        sha=sha,
                        max_retries=model_retries,
                        retry_delay=model_retry_delay,
                    )
                    write_jsonl_row(
                        result_handle,
                        classified_result_row(
                            classification,
                            judge_result,
                            response.model,
                            response.usage,
                        ),
                    )
                    classified_count += 1
                    result_count += 1
                except Exception as exc:
                    LOGGER.exception("model classification failed repo=%s sha=%s", repo, sha)
                    write_jsonl_row(
                        result_handle,
                        error_result_row(classification, exc, attempts=model_retries + 1),
                    )
                    error_count += 1
                    result_count += 1
        finally:
            if request_handle is not None:
                request_handle.close()

    LOGGER.info(
        "finished streaming %d LLM classification row(s) to %s",
        result_count,
        output_path,
    )
    if requests_output_path is not None:
        LOGGER.info(
            "finished streaming %d model request row(s) to %s",
            request_count,
            requests_output_path,
        )
    LOGGER.info(
        "finished LLM classification: selected=%d classified=%d errors=%d pending=%d",
        len(selected_rows),
        classified_count,
        error_count,
        pending_count,
    )
    if reused_count:
        LOGGER.info("reused %d existing LLM classification row(s)", reused_count)

    return ClassificationRunSummary(
        input_path=classification_path,
        output_path=output_path,
        requests_output_path=requests_output_path,
        total_rows=len(classifications),
        selected_rows=len(selected_rows),
        classified_rows=classified_count,
        error_rows=error_count,
        pending_rows=pending_count,
        reused_rows=reused_count,
        model_request_rows=request_count,
    )


def classify_ambiguous_file(
    classification_path: Path,
    output_path: Path | None = None,
    requests_output_path: Path | None = None,
    client: LLMClient | None = None,
    settings: LLMClassificationSettings | None = None,
    commit_paths: list[Path] | None = None,
    file_changes_path: Path | None = None,
    limit: int | None = None,
    print_prompt_setups: bool = False,
    prompt_output: TextIO | None = None,
    progress_interval: int = 25,
    resume: bool = True,
    retry_errors: bool = True,
    model_retries: int = 2,
    model_retry_delay: float = 5.0,
) -> ClassificationRunSummary:
    return classify_selected_file(
        classification_path=classification_path,
        output_path=output_path,
        requests_output_path=requests_output_path,
        client=client,
        settings=settings,
        commit_paths=commit_paths,
        file_changes_path=file_changes_path,
        limit=limit,
        print_prompt_setups=print_prompt_setups,
        prompt_output=prompt_output,
        progress_interval=progress_interval,
        resume=resume,
        retry_errors=retry_errors,
        model_retries=model_retries,
        model_retry_delay=model_retry_delay,
    )


def load_client(spec: str) -> LLMClient:
    module_name, separator, attribute_name = spec.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("--client must be in module:attribute form")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute_name)
    client = target() if callable(target) else target
    if not hasattr(client, "complete"):
        raise TypeError(f"{spec} did not produce an object with a complete(request) method")
    return client


def configure_logging(level: str, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def build_cli_client(args: argparse.Namespace) -> LLMClient | None:
    if args.client and args.provider != "none":
        raise SystemExit("--client and --provider cannot be used together")
    if args.client:
        return load_client(args.client)
    if args.provider == "openrouter":
        client = OpenRouterClient(
            model=args.model,
            api_key=args.openrouter_api_key,
            api_url=args.openrouter_api_url,
            timeout=args.model_timeout,
            max_tokens=args.max_output_tokens,
            site_url=args.openrouter_site_url,
            app_name=args.openrouter_app_name,
            use_response_schema=not args.no_response_schema,
        )
        try:
            client.resolved_api_key()
        except ModelNotConfiguredError as exc:
            raise SystemExit(str(exc)) from exc
        return client
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build and optionally run LLM judge classifications for selected "
            "commit contribution classification rows."
        )
    )
    parser.add_argument(
        "classification_file",
        type=Path,
        help="Previous commit_contribution_classifications.jsonl file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Output JSONL for LLM classifications. Default: sibling {DEFAULT_OUTPUT_NAME}.",
    )
    parser.add_argument(
        "--requests-output",
        type=Path,
        help=(
            "Optional JSONL containing provider-neutral model requests. "
            f"Use --write-requests without a path to write sibling {DEFAULT_REQUEST_OUTPUT_NAME}."
        ),
    )
    parser.add_argument(
        "--write-requests",
        action="store_true",
        help="Write provider-neutral model requests for every selected commit.",
    )
    parser.add_argument(
        "--print-prompt-setup",
        action="store_true",
        help=(
            "When no --client or built-in --provider is provided, print each selected "
            "commit's model messages to stdout."
        ),
    )
    parser.add_argument(
        "--client",
        help=(
            "Optional LLM client factory in module:attribute form. "
            "The object must implement complete(ModelRequest) -> ModelResponse. "
            "Cannot be combined with --provider."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("none", "openrouter"),
        default="none",
        help=(
            "Built-in model provider to call. Default: none, which writes pending_model "
            "rows unless --client is provided."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENROUTER_MODEL,
        help=f"Model id for built-in providers. Default: {DEFAULT_OPENROUTER_MODEL}.",
    )
    parser.add_argument(
        "--openrouter-api-key",
        help="OpenRouter API key. Defaults to OPENROUTER_API_KEY.",
    )
    parser.add_argument(
        "--openrouter-api-url",
        default=DEFAULT_OPENROUTER_API_URL,
        help=f"OpenRouter chat completions endpoint. Default: {DEFAULT_OPENROUTER_API_URL}.",
    )
    parser.add_argument(
        "--openrouter-site-url",
        help="Optional site URL sent as OpenRouter HTTP-Referer.",
    )
    parser.add_argument(
        "--openrouter-app-name",
        help="Optional app name sent as OpenRouter X-Title.",
    )
    parser.add_argument(
        "--model-timeout",
        type=float,
        default=60.0,
        help="Model request timeout in seconds for built-in providers. Default: 60.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="Optional max output tokens for built-in providers.",
    )
    parser.add_argument(
        "--no-response-schema",
        action="store_true",
        help=(
            "Do not send the JSON schema as provider response_format. The prompt still "
            "asks for strict JSON and responses are still validated locally."
        ),
    )
    parser.add_argument(
        "--commit-file",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional commit metadata JSONL. Can be repeated. "
            "Defaults to sibling input_commit_file values, observation_commits.jsonl, and all_commits.jsonl."
        ),
    )
    parser.add_argument(
        "--file-changes",
        type=Path,
        help="Optional commit_file_changes.jsonl. Defaults to sibling commit_file_changes.jsonl when present.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum selected commits to process.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=LLMClassificationSettings.max_files,
        help="Maximum changed files included in each request.",
    )
    parser.add_argument(
        "--max-patch-chars-per-file",
        type=int,
        default=LLMClassificationSettings.max_patch_chars_per_file,
        help="Maximum patch characters included per file when --include-patch is used.",
    )
    parser.add_argument(
        "--include-patch",
        action="store_true",
        help="Include bounded patch excerpts in model input. Default: exclude patches.",
    )
    parser.add_argument(
        "--no-patch",
        action="store_true",
        help="Compatibility no-op; patch excerpts are excluded by default.",
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="INFO",
        help="Logging verbosity. Default: INFO.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Optional file to also write logs to.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=25,
        help=(
            "Log INFO progress every N selected commits. Use 0 to disable periodic "
            "progress logs. Default: 25."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Do not reuse existing output rows. By default, valid existing rows are "
            "preserved and only missing or retryable rows are processed."
        ),
    )
    parser.add_argument(
        "--no-retry-errors",
        action="store_true",
        help=(
            "When resuming, keep existing model_error rows instead of retrying them. "
            "By default, model_error rows are retried."
        ),
    )
    parser.add_argument(
        "--model-retries",
        type=int,
        default=2,
        help="Retries after the first model attempt for each commit. Default: 2.",
    )
    parser.add_argument(
        "--model-retry-delay",
        type=float,
        default=5.0,
        help="Seconds to wait between model retry attempts. Default: 5.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level, args.log_file)
    client = build_cli_client(args)
    requests_output_path = args.requests_output
    if requests_output_path is None and (args.write_requests or client is None):
        requests_output_path = default_requests_output_path(args.classification_file)
    settings = LLMClassificationSettings(
        max_files=max(0, args.max_files),
        max_patch_chars_per_file=max(0, args.max_patch_chars_per_file),
        include_patch=args.include_patch and not args.no_patch,
    )
    summary = classify_selected_file(
        classification_path=args.classification_file,
        output_path=args.output,
        requests_output_path=requests_output_path,
        client=client,
        settings=settings,
        commit_paths=args.commit_file or None,
        file_changes_path=args.file_changes,
        limit=args.limit,
        print_prompt_setups=args.print_prompt_setup,
        progress_interval=args.progress_interval,
        resume=not args.no_resume,
        retry_errors=not args.no_retry_errors,
        model_retries=args.model_retries,
        model_retry_delay=args.model_retry_delay,
    )
    print(f"read {summary.total_rows} previous classifications from {summary.input_path}")
    print(f"processed {summary.selected_rows} selected classification row(s)")
    if client is None:
        print(
            "no --client provided; wrote pending_model rows and provider-neutral requests",
            file=sys.stderr,
        )
    else:
        print(
            (
                f"classified={summary.classified_rows} model_errors={summary.error_rows} "
                f"reused={summary.reused_rows}"
            ),
            file=sys.stderr,
        )
    print(f"wrote {summary.output_path}")
    if summary.requests_output_path is not None:
        print(f"wrote {summary.requests_output_path}")


if __name__ == "__main__":
    main()
