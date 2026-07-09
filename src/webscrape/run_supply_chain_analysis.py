from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - optional dependency
    _tqdm = None


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_CLONE_DIR = Path("src/webscrape/repos")
DEFAULT_FINDINGS_OUTPUT_NAME = "commit_supply_chain_findings.jsonl"
DEFAULT_SUMMARY_OUTPUT_NAME = "commit_supply_chain_summary.jsonl"

COMMIT_CLASSIFICATION_FILE = "commit_contribution_classifications.jsonl"
COMMIT_FILE_CLASSIFICATION_FILE = "commit_file_contribution_classifications.jsonl"


@dataclass(frozen=True)
class MaterializedFile:
    actual_path: str
    logical_path: str
    file_change: dict[str, Any]


@dataclass(frozen=True)
class OSVRun:
    results: list[dict[str, Any]]
    errors: list[Any]
    returncode: int
    stderr: str
    command: str


@dataclass(frozen=True)
class OSVFinding:
    side: str
    actual_path: str
    logical_path: str
    source_type: str
    package_name: str
    package_version: str
    ecosystem: str
    vulnerability_id: str
    vulnerability_ids: tuple[str, ...]
    aliases: tuple[str, ...]
    summary: str
    details: str
    severity: str
    severity_score: str
    fixed_versions: tuple[str, ...]
    match_key: str
    file_change: dict[str, Any]


@dataclass(frozen=True)
class WorkerContext:
    repo_key: str
    repo: str
    repo_path: Path
    commit_metadata: dict[str, dict[str, Any]]
    commit_classifications: dict[str, dict[str, Any]]
    file_classifications: dict[tuple[str, str], dict[str, Any]]
    osv_bin: str
    osv_timeout: int
    git_timeout: int
    max_files_per_commit: int | None
    keep_temp: bool


_WORKER_CONTEXT: WorkerContext | None = None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, sort_keys=True) + "\n")


def normalize_repo_filter(value: str) -> str:
    return value.replace("/", "__")


def repo_name_from_key(repo_key: str) -> str:
    if "__" in repo_key:
        owner, name = repo_key.split("__", 1)
        return f"{owner}/{name}"
    return repo_key


def discover_repo_dirs(input_dir: Path, repo_filters: set[str] | None) -> list[Path]:
    if not input_dir.exists():
        return []
    repo_dirs = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_dir() and (path / "commit_file_changes.jsonl").exists()
    ]
    if repo_filters is None:
        return repo_dirs
    return [path for path in repo_dirs if path.name in repo_filters]


def validate_managed_clone_path(repo_path: Path, clone_dir: Path) -> None:
    resolved_clone_dir = clone_dir.resolve()
    resolved_repo_path = repo_path.resolve()
    if resolved_repo_path == resolved_clone_dir or resolved_clone_dir not in resolved_repo_path.parents:
        raise RuntimeError(
            f"refusing to manage clone outside clone dir: {repo_path} "
            f"(clone dir: {clone_dir})"
        )


def ensure_local_repo(repo: str, clone_dir: Path, repo_key: str) -> Path:
    clone_dir.mkdir(parents=True, exist_ok=True)
    repo_path = clone_dir / repo_key
    validate_managed_clone_path(repo_path, clone_dir)
    if repo_path.exists():
        if not (repo_path / ".git").exists():
            raise RuntimeError(f"expected git clone at {repo_path}, but .git was not found")
        print(f"using existing local repo: {repo_path}", file=sys.stderr, flush=True)
        return repo_path

    repo_url = f"https://github.com/{repo}.git"
    print(f"cloning {repo_url} into {repo_path}", file=sys.stderr, flush=True)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    command = ["git", "clone", "--no-tags", repo_url, str(repo_path)]
    try:
        subprocess.run(command, env=env, check=True)
    except Exception:
        if repo_path.exists():
            print(f"removing incomplete cloned repo: {repo_path}", file=sys.stderr, flush=True)
            shutil.rmtree(repo_path)
        raise
    return repo_path


def remove_cloned_repo_dir(repo_path: Path, clone_dir: Path) -> None:
    if not repo_path.exists():
        return
    validate_managed_clone_path(repo_path, clone_dir)
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"refusing to remove non-git clone path: {repo_path}")
    print(f"removing cloned repo dir: {repo_path}", file=sys.stderr, flush=True)
    shutil.rmtree(repo_path)


def load_commit_metadata(repo_dir: Path) -> dict[str, dict[str, Any]]:
    return {row["sha"]: row for row in read_jsonl(repo_dir / "all_commits.jsonl") if row.get("sha")}


def load_commit_classifications(repo_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        row["sha"]: row
        for row in read_jsonl(repo_dir / COMMIT_CLASSIFICATION_FILE)
        if row.get("sha")
    }


def load_file_classifications(repo_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = read_jsonl(repo_dir / COMMIT_FILE_CLASSIFICATION_FILE)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        commit_sha = row.get("commit_sha")
        filename = row.get("filename")
        if commit_sha and filename:
            result[(commit_sha, filename)] = row
        previous_filename = row.get("previous_filename")
        if commit_sha and previous_filename:
            result[(commit_sha, previous_filename)] = row
    return result


def load_observation_shas(repo_dir: Path) -> set[str]:
    return {row["sha"] for row in read_jsonl(repo_dir / "observation_commits.jsonl") if row.get("sha")}


def group_file_changes(
    repo_dir: Path,
    commit_source: str,
    max_commits: int | None,
) -> dict[str, list[dict[str, Any]]]:
    allowed_shas: set[str] | None = None
    if commit_source == "observation":
        allowed_shas = load_observation_shas(repo_dir)

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in read_jsonl(repo_dir / "commit_file_changes.jsonl"):
        commit_sha = row.get("commit_sha")
        if not commit_sha:
            continue
        if allowed_shas is not None and commit_sha not in allowed_shas:
            continue
        if commit_sha not in groups:
            if max_commits is not None and len(groups) >= max_commits:
                continue
            groups[commit_sha] = []
        groups[commit_sha].append(row)
    return groups


def repo_from_rows(repo_key: str, commit_metadata: dict[str, dict[str, Any]]) -> str:
    for row in commit_metadata.values():
        repo = row.get("repo")
        if repo:
            return str(repo)
    return repo_name_from_key(repo_key)


def first_parent(commit: dict[str, Any]) -> str | None:
    parents = commit.get("parents") or []
    if not parents:
        return None
    return str(parents[0])


def safe_repo_path(path: str | None) -> str | None:
    if not path:
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in pure.parts):
        return None
    return pure.as_posix()


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def git_show_file(
    repo_path: Path,
    commit_sha: str,
    file_path: str,
    timeout: int,
) -> tuple[bytes | None, str | None]:
    safe_path = safe_repo_path(file_path)
    if safe_path is None:
        return None, f"unsafe path: {file_path!r}"
    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "show", f"{commit_sha}:{safe_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"git show timed out after {timeout}s for {commit_sha}:{safe_path}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return None, stderr or f"git show failed for {commit_sha}:{safe_path}"
    return proc.stdout, None


def materialize_commit_files(
    repo_path: Path,
    parent_sha: str | None,
    commit_sha: str,
    file_changes: list[dict[str, Any]],
    before_root: Path,
    after_root: Path,
    git_timeout: int,
    max_files_per_commit: int | None,
) -> tuple[
    dict[str, MaterializedFile],
    dict[str, MaterializedFile],
    list[str],
]:
    before_lookup: dict[str, MaterializedFile] = {}
    after_lookup: dict[str, MaterializedFile] = {}
    errors: list[str] = []

    selected_changes = file_changes[:max_files_per_commit] if max_files_per_commit else file_changes
    for file_change in selected_changes:
        logical_path = safe_repo_path(file_change.get("filename"))
        if logical_path is None:
            errors.append(f"skipped unsafe filename: {file_change.get('filename')!r}")
            continue
        before_path = safe_repo_path(file_change.get("previous_filename")) or logical_path
        after_path = logical_path
        status = str(file_change.get("status") or "")

        if parent_sha and status != "added":
            content, error = git_show_file(repo_path, parent_sha, before_path, git_timeout)
            if content is not None:
                output_path = before_root / before_path
                write_bytes(output_path, content)
                before_lookup[before_path] = MaterializedFile(
                    actual_path=before_path,
                    logical_path=logical_path,
                    file_change=file_change,
                )
            elif error:
                errors.append(f"before {parent_sha}:{before_path}: {error}")

        if status not in {"removed", "deleted"}:
            content, error = git_show_file(repo_path, commit_sha, after_path, git_timeout)
            if content is not None:
                output_path = after_root / after_path
                write_bytes(output_path, content)
                after_lookup[after_path] = MaterializedFile(
                    actual_path=after_path,
                    logical_path=logical_path,
                    file_change=file_change,
                )
            elif error:
                errors.append(f"after {commit_sha}:{after_path}: {error}")

    return before_lookup, after_lookup, errors


def compact_json(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def normalize_result_path(root: Path, result_path: str) -> str:
    path = Path(result_path)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.name
    result = PurePosixPath(result_path).as_posix()
    if result.startswith("./"):
        result = result[2:]
    return result


def osv_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def has_regular_files(root: Path) -> bool:
    if not root.exists():
        return False
    return any(path.is_file() for path in root.rglob("*"))


def run_osv_scanner(
    osv_bin: str,
    root: Path,
    timeout: int,
) -> OSVRun:
    if not has_regular_files(root):
        return OSVRun(results=[], errors=[], returncode=0, stderr="", command="")

    command = [
        osv_bin,
        "scan",
        "source",
        "--format",
        "json",
        "--no-resolve",
        "-r",
        ".",
    ]
    command_string = shlex.join(command)
    print(f"running osv-scanner cwd={root}: {command_string}", file=sys.stderr, flush=True)

    proc = subprocess.Popen(
        command,
        cwd=root,
        env=osv_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        return OSVRun(
            results=[],
            errors=[f"osv-scanner timed out after {timeout + 30}s"],
            returncode=124,
            stderr=(stderr or "")[-4000:],
            command=command_string,
        )

    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return OSVRun(
            results=[],
            errors=[
                {
                    "message": "osv-scanner did not return JSON",
                    "stdout": (stdout or "")[:1000],
                }
            ],
            returncode=proc.returncode,
            stderr=(stderr or "")[-4000:],
            command=command_string,
        )

    return OSVRun(
        results=payload.get("results") or [],
        errors=payload.get("errors") or [],
        returncode=proc.returncode,
        stderr=(stderr or "")[-4000:],
        command=command_string,
    )


def sorted_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value}))


def vulnerability_id(vulnerability: dict[str, Any]) -> str:
    return str(vulnerability.get("id") or "")


def vulnerability_aliases(vulnerability: dict[str, Any]) -> list[str]:
    aliases = vulnerability.get("aliases") or []
    if not isinstance(aliases, list):
        return []
    return [str(alias) for alias in aliases if alias]


def group_vulnerability_ids(
    group: dict[str, Any] | None,
    vulnerabilities: list[dict[str, Any]],
) -> tuple[str, ...]:
    if group:
        ids = group.get("ids") or []
        if isinstance(ids, list) and ids:
            return sorted_strings(ids)
    ids: list[str] = []
    for vulnerability in vulnerabilities:
        ids.append(vulnerability_id(vulnerability))
        ids.extend(vulnerability_aliases(vulnerability))
    return sorted_strings(ids)


def vulnerabilities_for_group(
    group: dict[str, Any] | None,
    vulnerabilities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not group:
        return vulnerabilities
    ids = set(group_vulnerability_ids(group, vulnerabilities))
    if not ids:
        return vulnerabilities
    matched = []
    for vulnerability in vulnerabilities:
        vulnerability_ids = {vulnerability_id(vulnerability), *vulnerability_aliases(vulnerability)}
        if vulnerability_ids.intersection(ids):
            matched.append(vulnerability)
    return matched or vulnerabilities


def extract_aliases(vulnerabilities: list[dict[str, Any]]) -> tuple[str, ...]:
    aliases: list[str] = []
    for vulnerability in vulnerabilities:
        aliases.extend(vulnerability_aliases(vulnerability))
    return sorted_strings(aliases)


def extract_fixed_versions(vulnerabilities: list[dict[str, Any]]) -> tuple[str, ...]:
    fixed_versions: list[str] = []
    for vulnerability in vulnerabilities:
        for affected in vulnerability.get("affected") or []:
            for affected_range in affected.get("ranges") or []:
                for event in affected_range.get("events") or []:
                    fixed = event.get("fixed")
                    if fixed:
                        fixed_versions.append(str(fixed))
            database_specific = affected.get("database_specific") or {}
            fixed = database_specific.get("fixed_version") or database_specific.get("fixed_versions")
            if isinstance(fixed, list):
                fixed_versions.extend(str(value) for value in fixed if value)
            elif fixed:
                fixed_versions.append(str(fixed))
    return sorted_strings(fixed_versions)


def cvss_label(score: str) -> str:
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        return ""
    if numeric >= 9.0:
        return "CRITICAL"
    if numeric >= 7.0:
        return "HIGH"
    if numeric >= 4.0:
        return "MEDIUM"
    if numeric > 0:
        return "LOW"
    return ""


def best_severity_score(scores: list[str]) -> str:
    numeric_scores: list[tuple[float, str]] = []
    for score in scores:
        try:
            numeric_scores.append((float(score), score))
        except (TypeError, ValueError):
            continue
    if numeric_scores:
        return max(numeric_scores)[1]
    return sorted_strings(scores)[-1] if scores else ""


def vulnerability_severity(vulnerabilities: list[dict[str, Any]]) -> tuple[str, str]:
    labels: list[str] = []
    scores: list[str] = []
    for vulnerability in vulnerabilities:
        for key in ("database_specific", "ecosystem_specific"):
            specific = vulnerability.get(key) or {}
            severity = specific.get("severity")
            if severity:
                labels.append(str(severity).upper())
        severity_entries = vulnerability.get("severity") or []
        if isinstance(severity_entries, list):
            for entry in severity_entries:
                if not isinstance(entry, dict):
                    continue
                score = entry.get("score")
                if score:
                    score_value = str(score)
                    scores.append(score_value)
                    label = cvss_label(score_value)
                    if label:
                        labels.append(label)
    severity_order = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    for label in severity_order:
        if label in labels:
            return label, best_severity_score(scores)
    return (labels[0] if labels else "", best_severity_score(scores))


def vulnerability_summary(vulnerabilities: list[dict[str, Any]]) -> str:
    for vulnerability in vulnerabilities:
        summary = vulnerability.get("summary")
        if summary:
            return str(summary)
    return ""


def vulnerability_details(vulnerabilities: list[dict[str, Any]]) -> str:
    for vulnerability in vulnerabilities:
        details = vulnerability.get("details")
        if details:
            return str(details)[:4000]
    return ""


def make_match_key(
    logical_path: str,
    source_type: str,
    ecosystem: str,
    package_name: str,
    vulnerability_ids: tuple[str, ...],
) -> str:
    return compact_json(
        {
            "path": logical_path,
            "source_type": source_type,
            "ecosystem": ecosystem,
            "package_name": package_name,
            "vulnerability_ids": list(vulnerability_ids),
        }
    )


def materialized_for_source(
    root: Path,
    source_path: str,
    lookup: dict[str, MaterializedFile],
) -> tuple[str, str, dict[str, Any]]:
    actual_path = normalize_result_path(root, source_path)
    materialized = lookup.get(actual_path)
    if materialized is not None:
        return materialized.actual_path, materialized.logical_path, materialized.file_change
    return actual_path, actual_path, {}


def convert_results(
    side: str,
    root: Path,
    results: list[dict[str, Any]],
    lookup: dict[str, MaterializedFile],
) -> list[OSVFinding]:
    findings: list[OSVFinding] = []
    for result in results:
        source = result.get("source") or {}
        source_path = str(source.get("path") or "")
        if not source_path:
            continue
        actual_path, logical_path, file_change = materialized_for_source(root, source_path, lookup)
        source_type = str(source.get("type") or "")
        for package_result in result.get("packages") or []:
            package = package_result.get("package") or {}
            package_name = str(package.get("name") or "")
            package_version = str(package.get("version") or "")
            ecosystem = str(package.get("ecosystem") or "")
            vulnerabilities = [
                item
                for item in (package_result.get("vulnerabilities") or [])
                if isinstance(item, dict)
            ]
            if not vulnerabilities:
                continue

            groups = package_result.get("groups") or []
            group_items = groups if isinstance(groups, list) and groups else [None]
            for group in group_items:
                group_vulns = vulnerabilities_for_group(group, vulnerabilities)
                vulnerability_ids = group_vulnerability_ids(group, group_vulns)
                if not vulnerability_ids:
                    continue
                severity, severity_score = vulnerability_severity(group_vulns)
                match_key = make_match_key(
                    logical_path=logical_path,
                    source_type=source_type,
                    ecosystem=ecosystem,
                    package_name=package_name,
                    vulnerability_ids=vulnerability_ids,
                )
                findings.append(
                    OSVFinding(
                        side=side,
                        actual_path=actual_path,
                        logical_path=logical_path,
                        source_type=source_type,
                        package_name=package_name,
                        package_version=package_version,
                        ecosystem=ecosystem,
                        vulnerability_id=vulnerability_ids[0],
                        vulnerability_ids=vulnerability_ids,
                        aliases=extract_aliases(group_vulns),
                        summary=vulnerability_summary(group_vulns),
                        details=vulnerability_details(group_vulns),
                        severity=severity,
                        severity_score=severity_score,
                        fixed_versions=extract_fixed_versions(group_vulns),
                        match_key=match_key,
                        file_change=file_change,
                    )
                )
    return findings


def group_findings(findings: list[OSVFinding]) -> dict[str, list[OSVFinding]]:
    grouped: dict[str, list[OSVFinding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.match_key].append(finding)
    for values in grouped.values():
        values.sort(
            key=lambda item: (
                item.logical_path,
                item.ecosystem,
                item.package_name,
                item.package_version,
                item.vulnerability_id,
            )
        )
    return grouped


def pair_findings(
    before_findings: list[OSVFinding],
    after_findings: list[OSVFinding],
) -> list[tuple[str, OSVFinding | None, OSVFinding | None]]:
    before_grouped = group_findings(before_findings)
    after_grouped = group_findings(after_findings)
    keys = sorted(set(before_grouped) | set(after_grouped))
    pairs: list[tuple[str, OSVFinding | None, OSVFinding | None]] = []
    for key in keys:
        before_values = before_grouped.get(key, [])
        after_values = after_grouped.get(key, [])
        common = min(len(before_values), len(after_values))
        for index in range(common):
            pairs.append(("persistent", before_values[index], after_values[index]))
        for finding in after_values[common:]:
            pairs.append(("introduced", None, finding))
        for finding in before_values[common:]:
            pairs.append(("removed", finding, None))
    return pairs


def row_for_pair(
    repo_key: str,
    repo: str,
    commit_sha: str,
    parent_sha: str | None,
    commit: dict[str, Any],
    commit_classification: dict[str, Any] | None,
    file_classifications: dict[tuple[str, str], dict[str, Any]],
    status: str,
    before_finding: OSVFinding | None,
    after_finding: OSVFinding | None,
) -> dict[str, Any]:
    representative = after_finding or before_finding
    assert representative is not None
    logical_path = representative.logical_path
    file_classification = file_classifications.get((commit_sha, logical_path), {})
    file_change_row = representative.file_change or {}

    return {
        "repo_key": repo_key,
        "repo": repo,
        "commit_sha": commit_sha,
        "parent_sha": parent_sha or "",
        "commit_date": commit.get("date") or file_change_row.get("commit_date") or "",
        "commit_category": commit.get("category") or file_change_row.get("commit_category") or "",
        "commit_labels": commit.get("labels") or file_change_row.get("commit_labels") or [],
        "commit_primary_operational_intent": (commit_classification or {}).get(
            "primary_operational_intent", ""
        ),
        "commit_operational_intents": (commit_classification or {}).get(
            "operational_intents", []
        ),
        "commit_maintenance_class": (commit_classification or {}).get("maintenance_class", ""),
        "commit_confidence": (commit_classification or {}).get("confidence", ""),
        "filename": logical_path,
        "previous_filename": file_change_row.get("previous_filename") or "",
        "file_status": file_change_row.get("status") or "",
        "file_primary_role": file_classification.get("file_primary_role", ""),
        "file_object_tags": file_classification.get("file_object_tags", []),
        "file_context_intents": file_classification.get("file_context_intents", []),
        "finding_status": status,
        "source_type": representative.source_type,
        "before_source_path": before_finding.actual_path if before_finding else "",
        "after_source_path": after_finding.actual_path if after_finding else "",
        "ecosystem": representative.ecosystem,
        "package_name": representative.package_name,
        "before_package_version": before_finding.package_version if before_finding else "",
        "after_package_version": after_finding.package_version if after_finding else "",
        "vulnerability_id": representative.vulnerability_id,
        "vulnerability_ids": list(representative.vulnerability_ids),
        "aliases": list(representative.aliases),
        "severity": representative.severity,
        "severity_score": representative.severity_score,
        "fixed_versions": list(representative.fixed_versions),
        "summary": representative.summary,
        "details": representative.details,
        "match_key": representative.match_key,
    }


def analysis_summary_row(
    repo_key: str,
    repo: str,
    commit_sha: str,
    parent_sha: str | None,
    commit: dict[str, Any],
    file_changes: list[dict[str, Any]],
    before_lookup: dict[str, MaterializedFile],
    after_lookup: dict[str, MaterializedFile],
    before_run: OSVRun,
    after_run: OSVRun,
    before_finding_count: int,
    after_finding_count: int,
    counts: Counter[str],
    errors: list[str],
    skipped_reason: str = "",
) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "repo": repo,
        "commit_sha": commit_sha,
        "parent_sha": parent_sha or "",
        "commit_date": commit.get("date") or "",
        "commit_category": commit.get("category") or "",
        "commit_labels": commit.get("labels") or [],
        "files_changed": len(file_changes),
        "before_files_materialized": len(before_lookup),
        "after_files_materialized": len(after_lookup),
        "before_raw_results": len(before_run.results),
        "after_raw_results": len(after_run.results),
        "before_raw_findings": before_finding_count,
        "after_raw_findings": after_finding_count,
        "introduced_findings": counts["introduced"],
        "removed_findings": counts["removed"],
        "persistent_findings": counts["persistent"],
        "before_osv_returncode": before_run.returncode,
        "after_osv_returncode": after_run.returncode,
        "before_osv_errors": before_run.errors,
        "after_osv_errors": after_run.errors,
        "before_osv_stderr": before_run.stderr,
        "after_osv_stderr": after_run.stderr,
        "before_osv_command": before_run.command,
        "after_osv_command": after_run.command,
        "materialization_errors": errors,
        "skipped_reason": skipped_reason,
    }


def process_commit(
    repo_key: str,
    repo: str,
    repo_path: Path,
    commit_sha: str,
    file_changes: list[dict[str, Any]],
    commit_metadata: dict[str, dict[str, Any]],
    commit_classifications: dict[str, dict[str, Any]],
    file_classifications: dict[tuple[str, str], dict[str, Any]],
    osv_bin: str,
    osv_timeout: int,
    git_timeout: int,
    max_files_per_commit: int | None,
    keep_temp: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    commit = commit_metadata.get(commit_sha, {})
    parent_sha = first_parent(commit)
    if not parent_sha and commit.get("parents") is None:
        parent_sha = file_changes[0].get("parent_sha")

    temp_dir = Path(tempfile.mkdtemp(prefix=f"supply-chain-{repo_key}-{commit_sha[:12]}-"))
    before_root = temp_dir / "before"
    after_root = temp_dir / "after"
    before_root.mkdir(parents=True, exist_ok=True)
    after_root.mkdir(parents=True, exist_ok=True)

    try:
        before_lookup, after_lookup, materialization_errors = materialize_commit_files(
            repo_path=repo_path,
            parent_sha=parent_sha,
            commit_sha=commit_sha,
            file_changes=file_changes,
            before_root=before_root,
            after_root=after_root,
            git_timeout=git_timeout,
            max_files_per_commit=max_files_per_commit,
        )

        if not before_lookup and not after_lookup:
            empty_run = OSVRun(results=[], errors=[], returncode=0, stderr="", command="")
            summary = analysis_summary_row(
                repo_key,
                repo,
                commit_sha,
                parent_sha,
                commit,
                file_changes,
                before_lookup,
                after_lookup,
                empty_run,
                empty_run,
                0,
                0,
                Counter(),
                materialization_errors,
                skipped_reason="no_files_materialized",
            )
            return [], summary

        before_run = run_osv_scanner(
            osv_bin=osv_bin,
            root=before_root,
            timeout=osv_timeout,
        )
        after_run = run_osv_scanner(
            osv_bin=osv_bin,
            root=after_root,
            timeout=osv_timeout,
        )
        before_findings = convert_results("before", before_root, before_run.results, before_lookup)
        after_findings = convert_results("after", after_root, after_run.results, after_lookup)

        rows: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        for status, before_finding, after_finding in pair_findings(before_findings, after_findings):
            counts[status] += 1
            if status == "persistent":
                continue
            row = row_for_pair(
                repo_key=repo_key,
                repo=repo,
                commit_sha=commit_sha,
                parent_sha=parent_sha,
                commit=commit,
                commit_classification=commit_classifications.get(commit_sha),
                file_classifications=file_classifications,
                status=status,
                before_finding=before_finding,
                after_finding=after_finding,
            )
            rows.append(row)

        summary = analysis_summary_row(
            repo_key,
            repo,
            commit_sha,
            parent_sha,
            commit,
            file_changes,
            before_lookup,
            after_lookup,
            before_run,
            after_run,
            len(before_findings),
            len(after_findings),
            counts,
            materialization_errors,
        )
        return rows, summary
    finally:
        if keep_temp:
            print(f"kept temp dir for {commit_sha}: {temp_dir}", file=sys.stderr)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def init_worker(context: WorkerContext) -> None:
    global _WORKER_CONTEXT
    _WORKER_CONTEXT = context


def process_commit_job(job: tuple[str, list[dict[str, Any]]]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    context = _WORKER_CONTEXT
    if context is None:
        raise RuntimeError("worker context was not initialized")
    commit_sha, file_changes = job
    finding_rows, summary_row = process_commit(
        repo_key=context.repo_key,
        repo=context.repo,
        repo_path=context.repo_path,
        commit_sha=commit_sha,
        file_changes=file_changes,
        commit_metadata=context.commit_metadata,
        commit_classifications=context.commit_classifications,
        file_classifications=context.file_classifications,
        osv_bin=context.osv_bin,
        osv_timeout=context.osv_timeout,
        git_timeout=context.git_timeout,
        max_files_per_commit=context.max_files_per_commit,
        keep_temp=context.keep_temp,
    )
    return commit_sha, finding_rows, summary_row


def progress_iter(items: Iterable[Any], total: int, desc: str, mode: str) -> Iterable[Any]:
    if mode == "never" or _tqdm is None:
        return items
    if mode == "auto" and not sys.stderr.isatty():
        return items
    return _tqdm(items, total=total, desc=desc, unit="commit", dynamic_ncols=True)


def process_repo(
    repo_dir: Path,
    clone_dir: Path,
    output_root: Path | None,
    osv_bin: str,
    osv_timeout: int,
    git_timeout: int,
    commit_source: str,
    max_commits: int | None,
    max_files_per_commit: int | None,
    keep_temp: bool,
    workers: int,
    findings_output_name: str,
    summary_output_name: str,
    progress: str,
) -> tuple[Path, Path, int, int]:
    repo_key = repo_dir.name
    commit_metadata = load_commit_metadata(repo_dir)
    commit_classifications = load_commit_classifications(repo_dir)
    file_classifications = load_file_classifications(repo_dir)
    repo = repo_from_rows(repo_key, commit_metadata)
    repo_path = ensure_local_repo(repo, clone_dir, repo_key)
    groups = group_file_changes(repo_dir, commit_source=commit_source, max_commits=max_commits)

    output_dir = (output_root / repo_key) if output_root is not None else repo_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    findings_path = output_dir / findings_output_name
    summary_path = output_dir / summary_output_name

    items = list(groups.items())
    context = WorkerContext(
        repo_key=repo_key,
        repo=repo,
        repo_path=repo_path,
        commit_metadata=commit_metadata,
        commit_classifications=commit_classifications,
        file_classifications=file_classifications,
        osv_bin=osv_bin,
        osv_timeout=osv_timeout,
        git_timeout=git_timeout,
        max_files_per_commit=max_files_per_commit,
        keep_temp=keep_temp,
    )

    finding_count = 0
    with findings_path.open("w", encoding="utf-8") as findings_handle, summary_path.open(
        "w", encoding="utf-8"
    ) as summary_handle:
        if workers <= 1 or len(items) <= 1:
            iterator = progress_iter(items, total=len(items), desc=repo_key, mode=progress)
            for commit_sha, file_changes in iterator:
                finding_rows, summary_row = process_commit(
                    repo_key=repo_key,
                    repo=repo,
                    repo_path=repo_path,
                    commit_sha=commit_sha,
                    file_changes=file_changes,
                    commit_metadata=commit_metadata,
                    commit_classifications=commit_classifications,
                    file_classifications=file_classifications,
                    osv_bin=osv_bin,
                    osv_timeout=osv_timeout,
                    git_timeout=git_timeout,
                    max_files_per_commit=max_files_per_commit,
                    keep_temp=keep_temp,
                )
                for row in finding_rows:
                    append_jsonl(findings_handle, row)
                append_jsonl(summary_handle, summary_row)
                finding_count += len(finding_rows)
        else:
            worker_count = min(workers, len(items))
            with ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=init_worker,
                initargs=(context,),
            ) as executor:
                futures = [executor.submit(process_commit_job, item) for item in items]
                iterator = progress_iter(
                    as_completed(futures),
                    total=len(futures),
                    desc=f"{repo_key} workers={worker_count}",
                    mode=progress,
                )
                for future in iterator:
                    _commit_sha, finding_rows, summary_row = future.result()
                    for row in finding_rows:
                        append_jsonl(findings_handle, row)
                    append_jsonl(summary_handle, summary_row)
                    finding_count += len(finding_rows)
    return findings_path, summary_path, finding_count, len(groups)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run OSV-Scanner on before/after snapshots for files changed by commits, "
            "then emit introduced and removed package vulnerability deltas."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing per-repo scraper outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=DEFAULT_CLONE_DIR,
        help=(
            "Directory for managed owner__repo git clones. Missing repos are cloned from "
            f"GitHub. Default: {DEFAULT_CLONE_DIR}"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional root for supply-chain outputs. Default: write inside each repo output dir.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit analysis to a repo, either owner/repo or owner__repo. Can be repeated.",
    )
    parser.add_argument(
        "--osv-bin",
        default="osv-scanner",
        help="OSV-Scanner executable to run. Default: osv-scanner",
    )
    parser.add_argument(
        "--osv-timeout",
        type=int,
        default=120,
        help="OSV-Scanner per-scan timeout in seconds. Default: 120",
    )
    parser.add_argument(
        "--git-timeout",
        type=int,
        default=60,
        help="git show timeout per file in seconds. Default: 60",
    )
    parser.add_argument(
        "--commit-source",
        choices=("all", "observation"),
        default="all",
        help="Which commits from commit_file_changes.jsonl to analyze. Default: all.",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        help="Limit commits per repo for smoke tests or samples.",
    )
    parser.add_argument(
        "--max-files-per-commit",
        type=int,
        help="Limit files materialized per commit for smoke tests or samples.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of commits to process in parallel. Default: 1.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep before/after temp directories for debugging.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the local clone under --clone-dir after supply-chain analysis.",
    )
    parser.add_argument(
        "--findings-output-name",
        default=DEFAULT_FINDINGS_OUTPUT_NAME,
        help=f"Findings JSONL filename. Default: {DEFAULT_FINDINGS_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--summary-output-name",
        default=DEFAULT_SUMMARY_OUTPUT_NAME,
        help=f"Per-commit summary JSONL filename. Default: {DEFAULT_SUMMARY_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="Progress bar mode. Default: auto.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    repo_filters = {normalize_repo_filter(repo) for repo in args.repo} if args.repo else None
    repo_dirs = discover_repo_dirs(args.input_dir, repo_filters)
    if not repo_dirs:
        requested = f" matching {sorted(repo_filters)}" if repo_filters else ""
        raise SystemExit(f"No scraper result directories found in {args.input_dir}{requested}.")

    if shutil.which(args.osv_bin) is None:
        raise SystemExit(f"OSV-Scanner executable not found: {args.osv_bin}")

    for repo_dir in repo_dirs:
        repo_path = args.clone_dir / repo_dir.name
        try:
            try:
                findings_path, summary_path, finding_count, commit_count = process_repo(
                    repo_dir=repo_dir,
                    clone_dir=args.clone_dir,
                    output_root=args.output_dir,
                    osv_bin=args.osv_bin,
                    osv_timeout=args.osv_timeout,
                    git_timeout=args.git_timeout,
                    commit_source=args.commit_source,
                    max_commits=args.max_commits,
                    max_files_per_commit=args.max_files_per_commit,
                    keep_temp=args.keep_temp,
                    workers=args.workers,
                    findings_output_name=args.findings_output_name,
                    summary_output_name=args.summary_output_name,
                    progress=args.progress,
                )
            except FileNotFoundError as exc:
                print(f"skipped {repo_dir.name}: {exc}", file=sys.stderr)
                continue
            print(
                f"{repo_dir.name}: analyzed {commit_count} commits, wrote {finding_count} "
                f"delta finding rows to {findings_path} and summaries to {summary_path}"
            )
        finally:
            if not args.keep_clone:
                remove_cloned_repo_dir(repo_path, args.clone_dir)


if __name__ == "__main__":
    main()
