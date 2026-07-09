from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - optional dependency
    _tqdm = None


DEFAULT_CLONE_DIR = Path("src/webscrape/repos")
DEFAULT_OUTPUT_DIR = Path("src/webscrape/output")
DEFAULT_SONAR_HOST_URL = "http://localhost:9000"
DEFAULT_SUMMARY_OUTPUT_NAME = "sonarqube_snapshot_scans.jsonl"
SUCCESSFUL_SCAN_STATUSES = {"ok", "copied_repeated_snapshot"}
ISSUE_SUMMARY_FACETS = ("severities", "types", "statuses", "resolutions")
DEFAULT_METRICS = (
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
GENERATED_CODE_EXCLUSIONS = (
    "**/src/main/java/**/proto/**",
    "**/src/main/java/**/protobuf/**",
    "**/target/generated-sources/**",
    "**/build/generated/**",
    "**/generated/**",
    "**/gen/**",
    "**/*Proto.java",
    "**/*Grpc.java",
    "**/*.pb.java",
    "**/*.g.java",
)
SONAR_UNINDEXABLE_PATH_EXAMPLE_LIMIT = 5
PROGRESS_MODE = "auto"


@dataclass(frozen=True)
class Snapshot:
    index: int
    requested_date: datetime
    sha: str
    commit_date: str
    version: str


@dataclass(frozen=True)
class ScannerResult:
    returncode: int
    duration_seconds: float
    stdout_tail: str
    stderr_tail: str
    report_task: dict[str, str]


@dataclass(frozen=True)
class IssueSummary:
    total: int
    by_severity: dict[str, int]
    by_type: dict[str, int]
    by_status: dict[str, int]
    by_resolution: dict[str, int]
    strategy: str = "summary_facets"


def configure_progress(mode: str) -> None:
    global PROGRESS_MODE
    PROGRESS_MODE = mode


def progress_enabled() -> bool:
    if PROGRESS_MODE == "never" or _tqdm is None:
        return False
    if PROGRESS_MODE == "always":
        return True
    return sys.stderr.isatty()


def progress_iter(iterable, *, total: int | None = None, desc: str = "", unit: str = "it"):
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
    formatted = f"[sonarqube_snapshot] {message}"
    if progress_enabled():
        _tqdm.write(formatted, file=sys.stderr)
    else:
        print(formatted, file=sys.stderr, flush=True)


def repo_key_for(repo: str) -> str:
    return repo.replace("/", "__")


def validate_managed_clone_path(repo_dir: Path, clone_dir: Path) -> None:
    resolved_clone_dir = clone_dir.resolve()
    resolved_repo_dir = repo_dir.resolve()
    if resolved_repo_dir == resolved_clone_dir or resolved_clone_dir not in resolved_repo_dir.parents:
        raise RuntimeError(
            f"refusing to manage clone outside clone dir: {repo_dir} "
            f"(clone dir: {clone_dir})"
        )


def project_key_for(repo: str, prefix: str) -> str:
    raw = repo_key_for(repo)
    return f"{prefix}{raw}" if prefix else raw


def git_datetime_arg(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    stripped = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
        date_value = datetime.fromisoformat(stripped).date()
        chosen_time = datetime_time(23, 59, 59) if end_of_day else datetime_time.min
        return datetime.combine(date_value, chosen_time, tzinfo=timezone.utc)

    parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
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
    return value.replace(year=year, month=month, day=min(value.day, days_in_month[month - 1]))


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
        else:  # pragma: no cover - argparse validates this.
            raise ValueError(f"unsupported interval: {interval}")
    if include_final_snapshot and dates[-1] != until:
        dates.append(until)
    return dates


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
    )


def append_tail(value: str, chunk: str, max_chars: int = 6000) -> str:
    return f"{value}{chunk}"[-max_chars:]


def write_scanner_stream_line(stream_name: str, line: str, label: str = "") -> None:
    line = line.rstrip("\n")
    prefix = f"[sonar-scanner:{stream_name}]"
    if label:
        prefix = f"[sonar-scanner:{label}:{stream_name}]"
    if progress_enabled():
        _tqdm.write(f"{prefix} {line}", file=sys.stderr)
    else:
        print(f"{prefix} {line}", file=sys.stderr, flush=True)


def read_streamed_scanner_output(
    stream,
    *,
    stream_name: str,
    token: str,
    label: str,
    tails: dict[str, str],
    lock: threading.Lock,
) -> None:
    try:
        for line in iter(stream.readline, ""):
            redacted = redact_token(line, token)
            with lock:
                tails[stream_name] = append_tail(tails[stream_name], redacted)
            write_scanner_stream_line(stream_name, redacted, label=label)
    finally:
        stream.close()


def run_streamed_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    token: str,
    label: str,
) -> tuple[int, str, str]:
    process = subprocess.Popen(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    tails = {"stdout": "", "stderr": ""}
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=read_streamed_scanner_output,
            kwargs={
                "stream": process.stdout,
                "stream_name": "stdout",
                "token": token,
                "label": label,
                "tails": tails,
                "lock": lock,
            },
            daemon=True,
        ),
        threading.Thread(
            target=read_streamed_scanner_output,
            kwargs={
                "stream": process.stderr,
                "stream_name": "stderr",
                "token": token,
                "label": label,
                "tails": tails,
                "lock": lock,
            },
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=5)
        with lock:
            raise subprocess.TimeoutExpired(
                args,
                timeout,
                output=tails["stdout"],
                stderr=tails["stderr"],
            )

    for thread in threads:
        thread.join(timeout=5)
    with lock:
        return returncode, tails["stdout"], tails["stderr"]


def run_git(repo_dir: Path, args: list[str], timeout: int) -> str:
    completed = run_command(["git", "-C", str(repo_dir), *args], timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): git -C {repo_dir} {' '.join(args)}\n"
            f"{completed.stderr}"
        )
    return completed.stdout


def ensure_local_repo(repo: str, clone_dir: Path, skip_fetch: bool, git_timeout: int) -> Path:
    clone_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = clone_dir / repo_key_for(repo)
    validate_managed_clone_path(repo_dir, clone_dir)
    if repo_dir.exists():
        if not (repo_dir / ".git").exists():
            raise RuntimeError(f"expected git clone at {repo_dir}, but .git was not found")
        if skip_fetch:
            log(f"Using existing local repo without fetch: {repo_dir}")
            return repo_dir
        log(f"Fetching latest refs for {repo}")
        completed = run_command(
            ["git", "-C", str(repo_dir), "fetch", "--prune", "--no-tags", "origin"],
            timeout=git_timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git fetch failed for {repo}:\n{completed.stderr}")
        return repo_dir

    repo_url = f"https://github.com/{repo}.git"
    log(f"Cloning {repo_url} into {repo_dir}")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = run_command(
            ["git", "clone", "--no-tags", "--filter=blob:none", repo_url, str(repo_dir)],
            timeout=git_timeout,
            env=env,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"git clone failed for {repo}:\n{completed.stderr}")
    except Exception:
        if repo_dir.exists():
            log(f"Removing incomplete cloned repo after clone failure: {repo_dir}")
            shutil.rmtree(repo_dir)
        raise
    return repo_dir


def remove_cloned_repo_dir(repo_dir: Path, clone_dir: Path) -> None:
    if not repo_dir.exists():
        return
    validate_managed_clone_path(repo_dir, clone_dir)
    if not (repo_dir / ".git").exists():
        raise RuntimeError(f"refusing to remove non-git clone path: {repo_dir}")
    log(f"Removing cloned repo dir: {repo_dir}")
    shutil.rmtree(repo_dir)


def resolve_auto_branch(repo_dir: Path, git_timeout: int) -> str:
    try:
        ref = run_git(repo_dir, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], git_timeout).strip()
        if ref.startswith("origin/"):
            return ref.removeprefix("origin/")
        if ref:
            return ref
    except RuntimeError:
        pass
    try:
        current = run_git(repo_dir, ["branch", "--show-current"], git_timeout).strip()
        if current:
            return current
    except RuntimeError:
        pass
    return "main"


def resolve_branch_ref(repo_dir: Path, branch: str, git_timeout: int) -> str:
    candidates = [
        branch,
        f"refs/heads/{branch}",
        f"origin/{branch}",
        f"refs/remotes/origin/{branch}",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            run_git(repo_dir, ["rev-parse", "--verify", f"{candidate}^{{commit}}"], git_timeout)
            return candidate
        except RuntimeError:
            continue
    raise RuntimeError(f"Could not resolve branch {branch!r} in {repo_dir}")


def commit_date(repo_dir: Path, ref: str, git_timeout: int) -> str:
    return run_git(repo_dir, ["show", "-s", "--format=%cI", ref], git_timeout).strip()


def branch_end_date(repo_dir: Path, branch_ref: str, git_timeout: int) -> datetime:
    return parse_datetime(commit_date(repo_dir, branch_ref, git_timeout))


def snapshot_sha_for_date(
    repo_dir: Path,
    branch_ref: str,
    snapshot_date: datetime,
    first_parent: bool,
    git_timeout: int,
) -> str | None:
    args = ["rev-list", "-n", "1"]
    if first_parent:
        args.append("--first-parent")
    args.append(f"--before={git_datetime_arg(snapshot_date)}")
    args.append(branch_ref)
    sha = run_git(repo_dir, args, git_timeout).strip()
    return sha or None


def snapshot_version(prefix: str, snapshot_date: datetime, sha: str) -> str:
    date_part = snapshot_date.strftime("%Y-%m-%d")
    return f"{prefix}{date_part}-{sha[:12]}" if prefix else f"{date_part}-{sha[:12]}"


def build_snapshot_dates(args: argparse.Namespace, repo_dir: Path, branch_ref: str) -> list[datetime]:
    if args.snapshot_date:
        return sorted(
            set(parse_datetime(value, end_of_day=True) for value in args.snapshot_date)
        )
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


def build_snapshots(args: argparse.Namespace, repo_dir: Path, branch_ref: str) -> list[Snapshot]:
    dates = build_snapshot_dates(args, repo_dir, branch_ref)
    snapshots: list[Snapshot] = []
    seen_versions: set[str] = set()
    for requested_date in dates:
        sha = snapshot_sha_for_date(
            repo_dir,
            branch_ref,
            requested_date,
            first_parent=not args.no_first_parent_snapshots,
            git_timeout=args.git_timeout,
        )
        if not sha:
            log(f"No commit found before {git_datetime_arg(requested_date)}; skipping")
            continue
        version = snapshot_version(args.version_prefix, requested_date, sha)
        if version in seen_versions:
            continue
        seen_versions.add(version)
        snapshots.append(
            Snapshot(
                index=len(snapshots) + 1,
                requested_date=requested_date,
                sha=sha,
                commit_date=commit_date(repo_dir, sha, args.git_timeout),
                version=version,
            )
        )
    return snapshots


def parse_csv_values(values: list[str]) -> list[str]:
    parsed: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                parsed.append(part)
    return parsed


def merge_csv_values(existing: list[str], additions: tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *additions]:
        for part in value.split(","):
            part = part.strip()
            if part and part not in seen:
                merged.append(part)
                seen.add(part)
    return merged


def split_sonar_property(prop: str) -> tuple[str, str] | None:
    key, separator, value = prop.partition("=")
    if not separator:
        return None
    key = key.strip()
    if not key:
        return None
    return key, value.strip()


def sonar_property_keys(properties: list[str]) -> set[str]:
    keys: set[str] = set()
    for prop in properties:
        parsed = split_sonar_property(prop)
        if parsed is not None:
            keys.add(parsed[0])
    return keys


def load_sonar_project_properties(worktree_path: Path) -> dict[str, str]:
    path = worktree_path / "sonar-project.properties"
    if not path.exists():
        return {}
    properties: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            key, separator, value = line.partition("=")
            if not separator:
                key, separator, value = line.partition(":")
            if not separator:
                continue
            key = key.strip()
            if key:
                properties[key] = value.strip()
    return properties


def merge_project_csv_property(
    properties: list[str],
    project_properties: dict[str, str],
    key: str,
) -> list[str]:
    project_value = project_properties.get(key)
    if not project_value:
        return properties

    passthrough: list[str] = []
    values = [project_value]
    found = False
    for prop in properties:
        parsed = split_sonar_property(prop)
        if parsed is None or parsed[0] != key:
            passthrough.append(prop)
            continue
        values.append(parsed[1])
        found = True

    if not found:
        return properties

    merged = merge_csv_values(values, ())
    return [*passthrough, f"{key}={','.join(merged)}"]


def apply_generated_code_exclusions(properties: list[str]) -> list[str]:
    passthrough: list[str] = []
    exclusions: list[str] = []
    for prop in properties:
        parsed = split_sonar_property(prop)
        if parsed is not None and parsed[0] == "sonar.exclusions":
            exclusions.append(parsed[1])
        else:
            passthrough.append(prop)
    merged_exclusions = merge_csv_values(exclusions, GENERATED_CODE_EXCLUSIONS)
    return [*passthrough, f"sonar.exclusions={','.join(merged_exclusions)}"]


def read_repo_file(path: Path) -> list[str]:
    repos: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            if value:
                repos.append(value)
    return repos


def redact_token(text: str, token: str | None) -> str:
    if not token:
        return text
    return text.replace(token, "<redacted-token>")


def tail_text(value: str, max_chars: int = 6000) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_report_task(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            key, separator, value = line.strip().partition("=")
            if separator:
                values[key] = value
    return values


def make_worktree(repo_dir: Path, sha: str, worktree_root: Path, git_timeout: int) -> Path:
    worktree_root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"sonar-{sha[:12]}-", dir=worktree_root))
    run_git(repo_dir, ["worktree", "add", "--detach", "--force", str(path), sha], git_timeout)
    return path


def is_sonar_unindexable_path(path: str) -> bool:
    for part in path.split("/"):
        if ";" in part:
            return True
        if any(ord(char) < 32 for char in part):
            return True
    return False


def prune_sonar_unindexable_paths(worktree_path: Path, git_timeout: int) -> None:
    tracked = run_git(worktree_path, ["ls-files", "-z"], git_timeout)
    unsafe_paths = [
        path
        for path in tracked.split("\0")
        if path and is_sonar_unindexable_path(path)
    ]
    if not unsafe_paths:
        return

    removed = 0
    failed: list[str] = []
    for relative_path in unsafe_paths:
        path = worktree_path / relative_path
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            failed.append(relative_path)

    examples = ", ".join(unsafe_paths[:SONAR_UNINDEXABLE_PATH_EXAMPLE_LIMIT])
    log(
        f"Pruned {removed} SonarQube-incompatible path(s) from temporary worktree"
        f" before scanning: {examples}"
    )
    if failed:
        failed_examples = ", ".join(failed[:SONAR_UNINDEXABLE_PATH_EXAMPLE_LIMIT])
        log(f"Failed to prune {len(failed)} SonarQube-incompatible path(s): {failed_examples}")


def remove_worktree(repo_dir: Path, worktree_path: Path, git_timeout: int, keep_worktree: bool) -> None:
    if keep_worktree:
        return
    try:
        run_git(repo_dir, ["worktree", "remove", "--force", str(worktree_path)], git_timeout)
    except Exception as exc:  # noqa: BLE001
        log(f"git worktree remove failed for {worktree_path}: {exc}")
        shutil.rmtree(worktree_path, ignore_errors=True)


def sonar_auth_header(token: str) -> str:
    # SonarQube accepts bearer tokens, and local server versions also accept basic auth.
    return f"Bearer {token}"


def sonar_request(
    host_url: str,
    token: str,
    api_path: str,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = host_url.rstrip("/") + api_path
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": sonar_auth_header(token),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"SonarQube API failed: {exc.code} {url}\n{detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"SonarQube API failed: {url}\n{exc}") from exc


def wait_for_ce_task(
    host_url: str,
    token: str,
    task_id: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_task: dict[str, Any] = {}
    while time.time() < deadline:
        response = sonar_request(
            host_url,
            token,
            "/api/ce/task",
            {"id": task_id},
        )
        task = response.get("task") or {}
        last_task = task
        status = str(task.get("status") or "")
        if status in {"SUCCESS", "FAILED", "CANCELED"}:
            return task
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for SonarQube CE task {task_id}: {last_task}")


def fetch_measures(
    host_url: str,
    token: str,
    project_key: str,
    metrics: list[str],
) -> dict[str, Any]:
    if not metrics:
        return {}
    response = sonar_request(
        host_url,
        token,
        "/api/measures/component",
        {
            "component": project_key,
            "metricKeys": ",".join(metrics),
        },
    )
    measures: dict[str, Any] = {}
    component = response.get("component") or {}
    for measure in component.get("measures") or []:
        metric = measure.get("metric")
        value = measure.get("value")
        if metric:
            measures[str(metric)] = value
    return measures


def issue_search_params(
    project_key: str,
    page_size: int,
    page: int,
    facets: tuple[str, ...] = (),
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "componentKeys": project_key,
        "ps": page_size,
        "p": page,
    }
    if facets:
        params["facets"] = ",".join(facets)
    return params


def response_total(response: dict[str, Any], fallback: int = 0) -> int:
    paging = response.get("paging") or {}
    return safe_int(paging.get("total") or fallback)


def response_facet_counts(response: dict[str, Any], facet_name: str) -> dict[str, int]:
    facets = response.get("facets") or []
    if not isinstance(facets, list):
        return {}
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        if facet.get("property") != facet_name:
            continue
        values = facet.get("values") or []
        counts: dict[str, int] = {}
        for value in values:
            if not isinstance(value, dict):
                continue
            raw_value = value.get("val")
            if raw_value in (None, ""):
                continue
            counts[str(raw_value)] = safe_int(value.get("count"))
        return counts
    return {}


def empty_issue_summary(strategy: str = "skipped") -> IssueSummary:
    return IssueSummary(
        total=0,
        by_severity={},
        by_type={},
        by_status={},
        by_resolution={},
        strategy=strategy,
    )


def fetch_issue_summary(
    host_url: str,
    token: str,
    project_key: str,
    page_size: int,
) -> IssueSummary:
    response = sonar_request(
        host_url,
        token,
        "/api/issues/search",
        issue_search_params(project_key, page_size, 1, ISSUE_SUMMARY_FACETS),
    )
    return IssueSummary(
        total=response_total(response),
        by_severity=response_facet_counts(response, "severities"),
        by_type=response_facet_counts(response, "types"),
        by_status=response_facet_counts(response, "statuses"),
        by_resolution=response_facet_counts(response, "resolutions"),
    )


def issue_summary_fields(issue_summary: IssueSummary | None) -> dict[str, Any]:
    if issue_summary is None:
        issue_summary = empty_issue_summary()
    return {
        "issue_count": issue_summary.total,
        "issue_search_total": issue_summary.total,
        "issue_results_truncated": False,
        "issue_fetch_limit": 0,
        "issue_counts_by_severity": issue_summary.by_severity,
        "issue_counts_by_type": issue_summary.by_type,
        "issue_counts_by_status": issue_summary.by_status,
        "issue_counts_by_resolution": issue_summary.by_resolution,
        "issue_fetch_strategy": issue_summary.strategy,
        "issue_rows_saved": 0,
    }


def run_sonar_scanner(
    *,
    worktree_path: Path,
    scanner_bin: str,
    host_url: str,
    token: str,
    project_key: str,
    project_name: str,
    version: str,
    scanner_timeout: int,
    extra_properties: list[str],
    java_binaries: str,
    stream_output: bool,
    stream_label: str,
) -> ScannerResult:
    report_path = worktree_path / ".scannerwork" / "report-task.txt"
    if report_path.exists():
        report_path.unlink()
    project_properties = load_sonar_project_properties(worktree_path)
    extra_properties = merge_project_csv_property(
        extra_properties,
        project_properties,
        "sonar.exclusions",
    )
    extra_property_keys = sonar_property_keys(extra_properties)
    cmd = [
        scanner_bin,
        f"-Dsonar.projectKey={project_key}",
        f"-Dsonar.projectName={project_name}",
        f"-Dsonar.projectVersion={version}",
        f"-Dsonar.host.url={host_url}",
        f"-Dsonar.token={token}",
        "-Dsonar.scm.disabled=true",
        f"-Dsonar.java.binaries={java_binaries}",
    ]
    if (
        "sonar.sources" not in extra_property_keys
        and "sonar.sources" not in project_properties
    ):
        cmd.append("-Dsonar.sources=.")
    for prop in extra_properties:
        cmd.append(f"-D{prop}")

    started = time.monotonic()
    if stream_output:
        returncode, stdout_tail, stderr_tail = run_streamed_command(
            cmd,
            cwd=worktree_path,
            timeout=scanner_timeout,
            token=token,
            label=stream_label,
        )
    else:
        completed = run_command(cmd, cwd=worktree_path, timeout=scanner_timeout)
        returncode = completed.returncode
        stdout_tail = redact_token(tail_text(completed.stdout), token)
        stderr_tail = redact_token(tail_text(completed.stderr), token)
    duration = time.monotonic() - started
    return ScannerResult(
        returncode=returncode,
        duration_seconds=duration,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        report_task=parse_report_task(report_path),
    )


def base_summary_row(
    *,
    repo: str,
    branch: str,
    project_key: str,
    snapshot: Snapshot,
) -> dict[str, Any]:
    return {
        "repo_key": repo_key_for(repo),
        "repo": repo,
        "branch": branch,
        "project_key": project_key,
        "snapshot_index": snapshot.index,
        "snapshot_date": git_datetime_arg(snapshot.requested_date),
        "snapshot_sha": snapshot.sha,
        "snapshot_commit_date": snapshot.commit_date,
        "project_version": snapshot.version,
    }


def summarize_scan(
    *,
    repo: str,
    branch: str,
    project_key: str,
    snapshot: Snapshot,
    scanner_result: ScannerResult | None,
    ce_task: dict[str, Any] | None,
    measures: dict[str, Any],
    issue_summary: IssueSummary | None,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    row = base_summary_row(
        repo=repo,
        branch=branch,
        project_key=project_key,
        snapshot=snapshot,
    )
    row.update(
        {
            "scan_status": status,
            "error": error,
            "scanner_returncode": scanner_result.returncode if scanner_result else None,
            "scanner_duration_seconds": (
                round(scanner_result.duration_seconds, 3) if scanner_result else None
            ),
            "scanner_stdout_tail": scanner_result.stdout_tail if scanner_result else "",
            "scanner_stderr_tail": scanner_result.stderr_tail if scanner_result else "",
            "ce_task_id": (
                (scanner_result.report_task or {}).get("ceTaskId")
                if scanner_result
                else None
            ),
            "ce_task_status": (ce_task or {}).get("status"),
            "analysis_id": (ce_task or {}).get("analysisId"),
            "dashboard_url": (
                (scanner_result.report_task or {}).get("dashboardUrl")
                if scanner_result
                else None
            ),
            "metrics": measures,
        }
    )
    row.update(issue_summary_fields(issue_summary))
    return row


def copy_snapshot_row(
    row: dict[str, Any],
    *,
    branch: str,
    snapshot: Snapshot,
    status: str,
) -> dict[str, Any]:
    copied = dict(row)
    copied.update(
        {
            "branch": branch,
            "snapshot_index": snapshot.index,
            "snapshot_date": git_datetime_arg(snapshot.requested_date),
            "snapshot_sha": snapshot.sha,
            "snapshot_commit_date": snapshot.commit_date,
            "project_version": snapshot.version,
            "scan_status": status,
            "copied_from_snapshot_sha": row.get("snapshot_sha"),
            "copied_from_project_version": row.get("project_version"),
        }
    )
    return copied


def write_jsonl(handle: Any, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, sort_keys=True) + "\n")


def summary_row_key(row: dict[str, Any]) -> tuple[str, str] | None:
    repo = str(row.get("repo") or "")
    version = str(row.get("project_version") or "")
    if repo and version:
        return repo, version
    return None


def compact_summary_file(path: Path) -> None:
    if not path.exists():
        return

    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    key_order: list[tuple[str, str]] = []
    input_rows = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = summary_row_key(row)
            if key is None:
                continue
            input_rows += 1
            if key not in latest_by_key:
                key_order.append(key)
            latest_by_key[key] = row

    if input_rows == len(latest_by_key):
        return

    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for key in key_order:
            write_jsonl(handle, latest_by_key[key])
    temp_path.replace(path)
    log(f"Compacted {path}: kept {len(latest_by_key)} latest summary row(s)")


def load_existing_successes(path: Path) -> set[tuple[str, str]]:
    successes: set[tuple[str, str]] = set()
    if not path.exists():
        return successes
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(row.get("scan_status") or "") in SUCCESSFUL_SCAN_STATUSES:
                key = summary_row_key(row)
                if key is not None:
                    successes.add(key)
    return successes


def process_snapshot(
    *,
    args: argparse.Namespace,
    repo: str,
    branch: str,
    repo_dir: Path,
    project_key: str,
    snapshot: Snapshot,
    previous_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if previous_summary and previous_summary.get("snapshot_sha") == snapshot.sha:
        return copy_snapshot_row(
            previous_summary,
            branch=branch,
            snapshot=snapshot,
            status="copied_repeated_snapshot",
        )

    if args.dry_run:
        return summarize_scan(
            repo=repo,
            branch=branch,
            project_key=project_key,
            snapshot=snapshot,
            scanner_result=None,
            ce_task=None,
            measures={},
            issue_summary=empty_issue_summary("dry_run"),
            status="dry_run",
        )

    worktree_path: Path | None = None
    try:
        worktree_path = make_worktree(
            repo_dir,
            snapshot.sha,
            args.worktree_dir,
            args.git_timeout,
        )
        prune_sonar_unindexable_paths(worktree_path, args.git_timeout)
        scanner_result = run_sonar_scanner(
            worktree_path=worktree_path,
            scanner_bin=args.sonar_scanner_bin,
            host_url=args.sonar_host_url,
            token=args.sonar_token,
            project_key=project_key,
            project_name=project_key,
            version=snapshot.version,
            scanner_timeout=args.scanner_timeout,
            extra_properties=args.sonar_property,
            java_binaries=args.sonar_java_binaries,
            stream_output=args.stream_scanner_output,
            stream_label=f"{repo_key_for(repo)}:{snapshot.version}",
        )
        if scanner_result.returncode != 0:
            return summarize_scan(
                repo=repo,
                branch=branch,
                project_key=project_key,
                snapshot=snapshot,
                scanner_result=scanner_result,
                ce_task=None,
                measures={},
                issue_summary=None,
                status="scanner_failed",
                error=f"sonar-scanner exited {scanner_result.returncode}",
            )

        task_id = scanner_result.report_task.get("ceTaskId")
        if not task_id:
            return summarize_scan(
                repo=repo,
                branch=branch,
                project_key=project_key,
                snapshot=snapshot,
                scanner_result=scanner_result,
                ce_task=None,
                measures={},
                issue_summary=None,
                status="missing_ce_task",
                error="sonar-scanner did not produce ceTaskId",
            )

        ce_task = wait_for_ce_task(
            args.sonar_host_url,
            args.sonar_token,
            task_id,
            args.ce_timeout,
            args.ce_poll_seconds,
        )
        if ce_task.get("status") != "SUCCESS":
            return summarize_scan(
                repo=repo,
                branch=branch,
                project_key=project_key,
                snapshot=snapshot,
                scanner_result=scanner_result,
                ce_task=ce_task,
                measures={},
                issue_summary=None,
                status="ce_failed",
                error=str(ce_task.get("errorMessage") or ce_task.get("status") or ""),
            )

        measures = fetch_measures(
            args.sonar_host_url,
            args.sonar_token,
            project_key,
            args.metric,
        )
        issue_summary = (
            empty_issue_summary()
            if args.skip_issues
            else fetch_issue_summary(
                args.sonar_host_url,
                args.sonar_token,
                project_key,
                args.issue_page_size,
            )
        )
        return summarize_scan(
            repo=repo,
            branch=branch,
            project_key=project_key,
            snapshot=snapshot,
            scanner_result=scanner_result,
            ce_task=ce_task,
            measures=measures,
            issue_summary=issue_summary,
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001
        return summarize_scan(
            repo=repo,
            branch=branch,
            project_key=project_key,
            snapshot=snapshot,
            scanner_result=None,
            ce_task=None,
            measures={},
            issue_summary=None,
            status="error",
            error=str(exc),
        )
    finally:
        if worktree_path is not None:
            remove_worktree(
                repo_dir,
                worktree_path,
                args.git_timeout,
                keep_worktree=args.keep_worktrees,
            )


def process_repo(args: argparse.Namespace, repo: str) -> dict[str, Any]:
    repo_key = repo_key_for(repo)
    output_repo_dir = args.output_dir / repo_key
    output_repo_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_repo_dir / args.summary_output_name
    if args.resume:
        compact_summary_file(summary_path)
        existing_successes = load_existing_successes(summary_path)
    else:
        existing_successes = set()

    managed_clone = args.local_repo is None
    repo_dir = (
        args.local_repo
        if args.local_repo is not None
        else ensure_local_repo(repo, args.clone_dir, args.skip_fetch, args.git_timeout)
    )
    try:
        if not (repo_dir / ".git").exists():
            raise RuntimeError(f"not a git repository: {repo_dir}")

        branch = resolve_auto_branch(repo_dir, args.git_timeout) if args.branch == "auto" else args.branch
        branch_ref = resolve_branch_ref(repo_dir, branch, args.git_timeout)
        snapshots = build_snapshots(args, repo_dir, branch_ref)
        if args.max_snapshots is not None:
            snapshots = snapshots[: args.max_snapshots]

        project_key = project_key_for(repo, args.project_key_prefix)
        log(f"{repo}: branch={branch} snapshots={len(snapshots)} project_key={project_key}")

        written = 0
        skipped = 0
        failed = 0
        previous_summary: dict[str, Any] | None = None

        with summary_path.open("a", encoding="utf-8") as summary_file:
            for snapshot in progress_iter(
                snapshots,
                total=len(snapshots),
                desc=f"SonarQube {repo_key}",
                unit="snapshot",
            ):
                if (repo, snapshot.version) in existing_successes:
                    skipped += 1
                    continue
                log(f"{repo}: scanning {snapshot.version} {snapshot.sha[:12]}")
                summary = process_snapshot(
                    args=args,
                    repo=repo,
                    branch=branch,
                    repo_dir=repo_dir,
                    project_key=project_key,
                    snapshot=snapshot,
                    previous_summary=previous_summary,
                )
                write_jsonl(summary_file, summary)
                summary_file.flush()
                written += 1
                if summary.get("scan_status") not in {"ok", "copied_repeated_snapshot", "dry_run"}:
                    failed += 1
                previous_summary = summary

        if args.resume:
            compact_summary_file(summary_path)

        return {
            "repo": repo,
            "snapshots": len(snapshots),
            "written": written,
            "skipped": skipped,
            "failed": failed,
            "summary_path": str(summary_path),
        }
    finally:
        if managed_clone and not args.keep_clone:
            remove_cloned_repo_dir(repo_dir, args.clone_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SonarQube scanner across repository snapshots."
    )
    parser.add_argument("--repo", action="append", default=[], help="Repository owner/name. Can be repeated.")
    parser.add_argument(
        "--repo-file",
        action="append",
        default=[],
        type=Path,
        help="Text file with one owner/name repo per line. Can be repeated.",
    )
    parser.add_argument("--local-repo", type=Path, help="Use a local checkout for a single --repo run.")
    parser.add_argument("--clone-dir", type=Path, default=DEFAULT_CLONE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--branch", default="main", help="Branch to scan, or auto. Default: main.")
    parser.add_argument("--since", "--start-date", dest="since")
    parser.add_argument("--until", "--end-date", dest="until")
    parser.add_argument("--snapshot-date", action="append", default=[])
    parser.add_argument(
        "--interval",
        choices=("daily", "weekly", "monthly"),
        default="weekly",
    )
    parser.add_argument("--no-final-snapshot", action="store_true")
    parser.add_argument("--max-snapshots", type=int)
    parser.add_argument("--no-first-parent-snapshots", action="store_true")
    parser.add_argument("--sonar-host-url", default=os.environ.get("SONAR_HOST", DEFAULT_SONAR_HOST_URL))
    parser.add_argument("--sonar-token", default=os.environ.get("SONAR_TOKEN"))
    parser.add_argument("--sonar-scanner-bin", default=os.environ.get("SONAR_SCANNER_PATH", "sonar-scanner"))
    parser.add_argument(
        "--sonar-property",
        action="append",
        default=[],
        help="Extra sonar-scanner property as key=value. Can be repeated.",
    )
    parser.add_argument(
        "--exclude-generated-code",
        action="store_true",
        help=(
            "Append broad generated-code patterns to sonar.exclusions, including "
            "generated Java/protobuf/grpc source paths."
        ),
    )
    parser.add_argument("--sonar-java-binaries", default=".")
    parser.add_argument("--project-key-prefix", default=os.environ.get("SONAR_PROJECT_KEY_PREFIX", ""))
    parser.add_argument("--version-prefix", default="")
    parser.add_argument("--metric", action="append", default=list(DEFAULT_METRICS))
    parser.add_argument("--skip-issues", action="store_true")
    parser.add_argument("--issue-page-size", type=int, default=500)
    parser.add_argument("--max-issues-per-snapshot", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--summary-output-name", default=DEFAULT_SUMMARY_OUTPUT_NAME)
    parser.add_argument("--issues-output-name", help=argparse.SUPPRESS)
    parser.add_argument("--scanner-timeout", type=int, default=1800)
    parser.add_argument(
        "--stream-scanner-output",
        action="store_true",
        help=(
            "Print sonar-scanner stdout/stderr live to stderr while retaining "
            "scanner_stdout_tail and scanner_stderr_tail in the summary output."
        ),
    )
    parser.add_argument("--ce-timeout", type=int, default=3600)
    parser.add_argument("--ce-poll-seconds", type=float, default=2.0)
    parser.add_argument("--git-timeout", type=int, default=300)
    parser.add_argument("--repo-workers", type=int, default=1)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the managed clone under --clone-dir after analysis. Local repos are never removed.",
    )
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument(
        "--worktree-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "sonarqube-snapshot-worktrees",
    )
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
    )
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()

    if args.no_progress:
        args.progress = "never"
    configure_progress(args.progress)
    if args.exclude_generated_code:
        args.sonar_property = apply_generated_code_exclusions(args.sonar_property)
        log(
            "Enabled generated-code exclusions: "
            + ",".join(GENERATED_CODE_EXCLUSIONS)
        )
    if args.progress != "never" and _tqdm is None:
        log("tqdm is not installed; progress bars disabled")
    repos = list(args.repo)
    for path in args.repo_file:
        repos.extend(read_repo_file(path))
    args.repos = sorted(dict.fromkeys(repos))
    if not args.repos:
        parser.error("provide at least one --repo or --repo-file")
    if args.local_repo is not None and len(args.repos) != 1:
        parser.error("--local-repo can only be used with exactly one --repo")
    if not args.snapshot_date and not args.since:
        parser.error("--since is required unless --snapshot-date is provided")
    if not args.dry_run and not args.sonar_token:
        parser.error("--sonar-token or SONAR_TOKEN is required unless --dry-run is set")
    if args.repo_workers < 1:
        parser.error("--repo-workers must be at least 1")
    if args.max_snapshots is not None and args.max_snapshots < 1:
        parser.error("--max-snapshots must be at least 1")
    if args.issue_page_size < 1 or args.issue_page_size > 500:
        parser.error("--issue-page-size must be between 1 and 500")
    if args.max_issues_per_snapshot is not None:
        log("--max-issues-per-snapshot is ignored; issue rows are no longer saved")
    if args.issues_output_name:
        log("--issues-output-name is ignored; issue rows are no longer saved")
    args.metric = parse_csv_values(args.metric)
    return args


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.worktree_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    if args.repo_workers == 1:
        for repo in args.repos:
            results.append(process_repo(args, repo))
    else:
        log(
            "Running multiple repos concurrently against one SonarQube server; "
            "reduce --repo-workers if Elasticsearch or CE becomes unstable."
        )
        with ThreadPoolExecutor(max_workers=args.repo_workers) as executor:
            future_to_repo = {
                executor.submit(process_repo, args, repo): repo
                for repo in args.repos
            }
            for future in as_completed(future_to_repo):
                results.append(future.result())

    failures = sum(result["failed"] for result in results)
    for result in results:
        print(json.dumps(result, sort_keys=True))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
