from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover - exercised when optional dependency is absent.
    _tqdm = None


GITHUB_API_URL = "https://api.github.com"
PROGRESS_MODE = "auto"
LOCAL_COMMIT_BATCH_SIZE = 500
LOCAL_COMMIT_FIELD_SEPARATOR = "\x1f"
LOCAL_COMMIT_RECORD_SEPARATOR = "\x1e"
LOCAL_COMMIT_BATCH_FORMAT = (
    "%x1e%H%x1f%cI%x1f%aI%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%P%x1f%B"
)

AI_PATTERNS = {
    "claude_code": re.compile(r"\b(claude|claude[-_ ]?code|anthropic)\b", re.IGNORECASE),
    "codex": re.compile(r"\b(codex|openai[-_ ]?codex)\b", re.IGNORECASE),
    "copilot": re.compile(r"\b(copilot|github[-_ ]?copilot)\b", re.IGNORECASE),
    "cursor": re.compile(
        r"\b(cursor(?:[-_ ]?(?:agent|ai|bot))?|cursor\[bot\])\b|@cursor\.(?:com|sh)\b",
        re.IGNORECASE,
    ),
    "devin": re.compile(
        r"\b(devin[-_ ]?ai(?:[-_ ]?integration)?(?:\[bot\])?|"
        r"devin[-_ ]?bot|cognition[-_ ]?ai)\b|@cognition\.ai\b",
        re.IGNORECASE,
    ),
    "aider": re.compile(
        r"\b(aider(?:[-_ ]?(?:ai|assistant|bot))?|aider\[bot\])\b",
        re.IGNORECASE,
    ),
    "openhands": re.compile(
        r"\b(openhands|open[-_ ]hands|opendevin|openhands[-_ ]?agent|openhands\[bot\])\b",
        re.IGNORECASE,
    ),
    "cline": re.compile(
        r"\b(cline[-_ .]?(?:bot|agent|ai|assistant)|cline\[bot\])\b|@cline\.bot\b",
        re.IGNORECASE,
    ),
    "roo_code": re.compile(
        r"\b(roo[-_ ]?code|roocode|roo[-_ ]?code\[bot\])\b",
        re.IGNORECASE,
    ),
    "windsurf": re.compile(
        r"\b(windsurf|codeium(?:[-_ ]?(?:ai|bot|assistant))?)\b|@codeium\.com\b",
        re.IGNORECASE,
    ),
    "sweep": re.compile(
        r"\b(sweep[-_ ]?ai|sweepai|sweep\[bot\]|sweep[-_ ]?bot)\b",
        re.IGNORECASE,
    ),
    "replit_agent": re.compile(
        r"\b(replit[-_ ]?agent|replit[-_ ]?ai|replit\[bot\])\b",
        re.IGNORECASE,
    ),
    "jules": re.compile(
        r"\b(google[-_ ]?labs[-_ ]?jules|jules[-_ ]?ai|jules\[bot\]|jules[-_ ]?bot)\b",
        re.IGNORECASE,
    ),
    "gemini": re.compile(
        r"\b(gemini[-_ ]?(?:cli|code[-_ ]?assist|coding[-_ ]?agent|assistant|bot)|"
        r"gemini\[bot\])\b",
        re.IGNORECASE,
    ),
    "amazon_q": re.compile(
        r"\b(amazon[-_ ]?q(?:[-_ ]?developer)?|q[-_ ]?developer|amazon[-_ ]?q[-_ ]?bot)\b",
        re.IGNORECASE,
    ),
    "sourcegraph_cody": re.compile(
        r"\b(sourcegraph[-_ ]?cody|cody[-_ ]?ai|cody\[bot\]|cody[-_ ]?bot)\b",
        re.IGNORECASE,
    ),
}
AI_LABELS = frozenset(AI_PATTERNS)
OTHER_BOT_PATTERN = re.compile(
    r"\b(bot|dependabot|renovate|semantic-release|github-actions|automation)\b",
    re.IGNORECASE,
)
AI_MESSAGE_ATTRIBUTION_PATTERN = re.compile(
    r"\b(generated|authored|created|committed|written|built)\b.*\b(with|by|using)\b",
    re.IGNORECASE,
)
COAUTHORED_BY_PATTERN = re.compile(
    r"^co-authored-by:\s*(?P<name>.*?)\s*<(?P<email>[^>]+)>",
    re.IGNORECASE | re.MULTILINE,
)
HUNK_HEADER_PATTERN = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class CommitGraph:
    ordered_commits: list[dict[str, Any]]
    by_sha: dict[str, dict[str, Any]]
    parents_by_sha: dict[str, list[str]]
    children_by_sha: dict[str, list[str]]
    ancestors_by_sha: dict[str, set[str]]
    topo_index_by_sha: dict[str, int]
    heads: list[str]


@dataclass(frozen=True)
class BlameResult:
    lines: list[dict[str, Any]]
    identity_index: dict[tuple[str, int, str], list[int]]
    sha_content_index: dict[tuple[str, str], list[int]]
    content_index: dict[str, list[int]]


FileBlameCache = dict[tuple[str, str, str | None], BlameResult | None]
LineBlameCache = dict[tuple[str, str, int, str | None], BlameResult | None]
TerminalKey = tuple[str | None, str | None, str, int, str]
TerminalResult = tuple[dict[str, Any] | None, str, str | None, int | None]
TerminalCache = dict[TerminalKey, TerminalResult]


@dataclass(frozen=True)
class GitHubClient:
    token: str | None = None
    api_url: str = GITHUB_API_URL
    sleep_seconds: float = 0.05

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
        if path_or_url.startswith("https://"):
            url = path_or_url
        else:
            path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
            url = f"{self.api_url}{path}"
            if params:
                url = f"{url}?{urlencode(params)}"

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vd-github-commit-scraper",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body) if body else None
                return data, response.headers
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub request failed: {exc.code} {url}\n{detail}") from exc
        finally:
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)


def ensure_local_repo(
    repo: str,
    clone_root: Path,
    skip_fetch: bool = False,
    full_clone: bool = False,
) -> Path:
    clone_root.mkdir(parents=True, exist_ok=True)
    repo_dir = clone_root / repo.replace("/", "__")
    if repo_dir.exists():
        if skip_fetch and (repo_dir / ".git").exists():
            log(f"Using existing local repo without fetch: {repo_dir}")
            return repo_dir
        log(f"Removing existing local repo before fresh clone: {repo_dir}")
        shutil.rmtree(repo_dir)

    repo_url = f"https://github.com/{repo}.git"
    log(f"Cloning {repo_url} into {repo_dir}")
    clone_args = ["git", "clone", "--no-tags"]
    if not full_clone:
        clone_args.append("--filter=blob:none")
    clone_args.extend([repo_url, str(repo_dir)])
    try:
        run_command(clone_args)
    except Exception:
        if repo_dir.exists():
            log(f"Removing incomplete cloned repo after clone failure: {repo_dir}")
            shutil.rmtree(repo_dir)
        raise
    return repo_dir


def remove_cloned_repo_dir(repo_dir: Path) -> None:
    if not repo_dir.exists():
        return
    log(f"Removing cloned repo dir: {repo_dir}")
    shutil.rmtree(repo_dir)


def fetch_branch_commits(
    repo: str,
    repo_dir: Path,
    branch: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    branch_ref = resolve_branch_ref(repo_dir, branch)
    log(f"Resolved branch {branch!r} to {branch_ref}")
    branch_end_sha = run_git(
        repo_dir,
        ["rev-parse", "--verify", f"{branch_ref}^{{commit}}"],
    ).strip()
    args = ["rev-list", "--topo-order", "--reverse"]
    if since is not None:
        args.append(f"--since={git_datetime_arg(since)}")
    if until is not None:
        args.append(f"--until={git_datetime_arg(until)}")
    args.append(branch_ref)
    shas = [line.strip() for line in run_git(repo_dir, args).splitlines() if line.strip()]
    log(f"Discovered {len(shas)} branch commit(s); loading local commit metadata")
    commits = normalize_local_commits(repo, repo_dir, shas)
    return commits, branch_end_sha


def select_origin_commits(
    commits: list[dict[str, Any]],
    until: datetime | None = None,
    max_commits: int | None = None,
) -> list[dict[str, Any]]:
    selected = [
        commit
        for commit in commits
        if until is None or commit_datetime(commit) <= until
    ]
    if max_commits is not None:
        selected = [] if max_commits == 0 else selected[-max_commits:]
    return selected


def observation_commits_for_origins(
    commits: list[dict[str, Any]],
    origin_commits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not origin_commits:
        return []
    origin_shas = {commit["sha"] for commit in origin_commits}
    first_origin_index = next(
        index
        for index, commit in enumerate(commits)
        if commit["sha"] in origin_shas
    )
    return commits[first_origin_index:]


def commit_datetime(commit: dict[str, Any]) -> datetime:
    date = _parse_dt(commit.get("date"))
    if date is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return date


def resolve_branch_ref(repo_dir: Path, branch: str) -> str:
    candidates = unique_nonempty_values(
        branch,
        f"refs/heads/{branch}",
        f"origin/{branch}",
        f"refs/remotes/origin/{branch}",
    )
    for candidate in candidates:
        try:
            run_git(repo_dir, ["rev-parse", "--verify", f"{candidate}^{{commit}}"])
        except RuntimeError:
            continue
        return candidate
    raise RuntimeError(
        f"Could not resolve branch {branch!r}. "
        "Use --branch to choose an existing local or origin branch."
    )


def git_datetime_arg(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def normalize_local_commits(
    repo: str,
    repo_dir: Path,
    shas: list[str],
    batch_size: int = LOCAL_COMMIT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    if not shas:
        return []
    commits_by_sha: dict[str, dict[str, Any]] = {}
    fetched = 0
    progress = progress_bar(
        total=len(shas),
        desc="Fetching branch commits",
        unit="commit",
    )
    try:
        for chunk in chunks(shas, max(1, batch_size)):
            output = run_git(
                repo_dir,
                [
                    "show",
                    "-s",
                    f"--format={LOCAL_COMMIT_BATCH_FORMAT}",
                    *chunk,
                ],
            )
            chunk_commits = parse_local_commit_batch(repo, output)
            for commit in chunk_commits:
                commits_by_sha[commit["sha"]] = commit
            missing_shas = [sha for sha in chunk if sha not in commits_by_sha]
            if missing_shas:
                raise RuntimeError(
                    "Missing local commit metadata for "
                    + ", ".join(sha[:12] for sha in missing_shas[:5])
                )
            fetched += len(chunk)
            if progress is None:
                log(
                    "Fetched branch commit metadata "
                    f"{fetched}/{len(shas)}; latest={chunk[-1][:12]}"
                )
            else:
                progress.update(len(chunk))
                progress_postfix(progress, fetched=fetched)
    finally:
        progress_close(progress)
    return [commits_by_sha[sha] for sha in shas]


def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def parse_local_commit_batch(repo: str, output: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    records = output.split(LOCAL_COMMIT_RECORD_SEPARATOR)
    for record in records:
        if not record.strip():
            continue
        fields = record.lstrip("\n").rstrip("\n").split(LOCAL_COMMIT_FIELD_SEPARATOR, 8)
        if len(fields) != 9:
            raise RuntimeError("Unexpected git show output while parsing local commit batch")
        (
            sha,
            committer_date,
            author_date,
            author_name,
            author_email,
            committer_name,
            committer_email,
            parents,
            message,
        ) = fields
        commits.append(
            local_commit_from_fields(
                repo=repo,
                sha=sha,
                committer_date=committer_date,
                author_date=author_date,
                author_name=author_name,
                author_email=author_email,
                committer_name=committer_name,
                committer_email=committer_email,
                parents=parents,
                message=message,
            )
        )
    return commits


def normalize_local_commit(repo: str, repo_dir: Path, sha: str) -> dict[str, Any]:
    fields = run_git(
        repo_dir,
        [
            "show",
            "-s",
            "--format=%cI%x00%aI%x00%an%x00%ae%x00%cn%x00%ce%x00%P%x00%B",
            sha,
        ],
    ).split("\x00", 7)
    if len(fields) != 8:
        raise RuntimeError(f"Unexpected git show output while normalizing commit {sha}")
    (
        committer_date,
        author_date,
        author_name,
        author_email,
        committer_name,
        committer_email,
        parents,
        message,
    ) = fields
    return local_commit_from_fields(
        repo=repo,
        sha=sha,
        committer_date=committer_date,
        author_date=author_date,
        author_name=author_name,
        author_email=author_email,
        committer_name=committer_name,
        committer_email=committer_email,
        parents=parents,
        message=message,
    )


def local_commit_from_fields(
    repo: str,
    sha: str,
    committer_date: str,
    author_date: str,
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
    parents: str,
    message: str,
) -> dict[str, Any]:
    message = message.rstrip("\n")
    message_subject, message_body = split_commit_message(message)
    coauthors = parse_coauthors(message)
    author = {
        "date": author_date,
        "name": author_name,
        "email": author_email,
    }
    committer = {
        "date": committer_date,
        "name": committer_name,
        "email": committer_email,
    }
    labels = classify_commit(
        message=message,
        coauthors=coauthors,
        github_author={},
        github_committer={},
        author=author,
        committer=committer,
    )
    return {
        "repo": repo,
        "sha": sha,
        "html_url": f"https://github.com/{repo}/commit/{sha}",
        "api_url": None,
        "date": committer_date or author_date,
        "parents": parents.split(),
        "author_login": None,
        "author_type": None,
        "committer_login": None,
        "committer_type": None,
        "git_author_name": author_name,
        "git_author_email": author_email,
        "git_committer_name": committer_name,
        "git_committer_email": committer_email,
        "message_subject": message_subject,
        "message_body": message_body,
        "coauthors": coauthors,
        "labels": labels,
        "category": category_from_labels(labels),
        "github_metadata_enriched": False,
    }


def enrich_commits_with_github_api(
    client: GitHubClient,
    repo: str,
    commits: list[dict[str, Any]],
    branch: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    if not commits:
        return commits

    target_shas = {commit["sha"] for commit in commits if commit.get("sha")}
    metadata_by_sha = fetch_github_branch_commit_metadata(
        client,
        repo,
        branch,
        target_shas,
        since=since,
        until=until,
    )

    missing_shas = [commit["sha"] for commit in commits if commit["sha"] not in metadata_by_sha]
    show_progress = progress_enabled()
    fallback_iter = progress_iter(
        missing_shas,
        total=len(missing_shas),
        desc="GitHub fallback metadata",
        unit="commit",
    )
    for index, sha in enumerate(fallback_iter, start=1):
        if not show_progress:
            log(
                "Fetching GitHub commit metadata fallback "
                f"{index}/{len(missing_shas)}: {sha}"
            )
        item, _headers = client.get(f"/repos/{repo}/commits/{sha}")
        if item:
            metadata_by_sha[sha] = item
        progress_postfix(
            fallback_iter,
            enriched=len(metadata_by_sha),
            total_targets=len(target_shas),
        )

    enriched_commits = [
        apply_github_commit_metadata(commit, metadata_by_sha.get(commit["sha"]))
        for commit in commits
    ]
    enriched_count = sum(1 for commit in enriched_commits if commit.get("github_metadata_enriched"))
    log(f"Enriched {enriched_count}/{len(enriched_commits)} commits with GitHub metadata")
    return enriched_commits


def fetch_github_branch_commit_metadata(
    client: GitHubClient,
    repo: str,
    branch: str,
    target_shas: set[str],
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    metadata_by_sha: dict[str, dict[str, Any]] = {}
    if not target_shas:
        return metadata_by_sha

    params: dict[str, Any] = {
        "sha": branch,
        "per_page": 100,
    }
    if since is not None:
        params["since"] = git_datetime_arg(since)
    if until is not None:
        params["until"] = git_datetime_arg(until)

    page = 1
    next_url: str | None = None
    while True:
        log(
            "Fetching GitHub branch commit metadata page "
            f"{page}; enriched {len(metadata_by_sha)}/{len(target_shas)} target commits"
        )
        if next_url is not None:
            data, headers = client.get(next_url)
        else:
            data, headers = client.get(f"/repos/{repo}/commits", params)
        for item in data or []:
            sha = item.get("sha")
            if sha in target_shas:
                metadata_by_sha[sha] = item
        if target_shas.issubset(metadata_by_sha):
            return metadata_by_sha
        next_url = _next_link(headers)
        if not next_url:
            return metadata_by_sha
        page += 1


def apply_github_commit_metadata(
    commit: dict[str, Any],
    item: dict[str, Any] | None,
) -> dict[str, Any]:
    if not item:
        return dict(commit)

    api_commit = item.get("commit") or {}
    api_author = api_commit.get("author") or {}
    api_committer = api_commit.get("committer") or {}
    github_author = item.get("author") or {}
    github_committer = item.get("committer") or {}
    message = api_commit.get("message")
    if message is None:
        message = "\n".join(
            part
            for part in [
                str(commit.get("message_subject") or ""),
                str(commit.get("message_body") or ""),
            ]
            if part
        )
    coauthors = parse_coauthors(message) if message else commit.get("coauthors", [])
    message_subject, message_body = split_commit_message(message)
    author = {
        "date": api_author.get("date") or commit.get("date"),
        "name": api_author.get("name") or commit.get("git_author_name"),
        "email": api_author.get("email") or commit.get("git_author_email"),
    }
    committer = {
        "date": api_committer.get("date") or commit.get("date"),
        "name": api_committer.get("name") or commit.get("git_committer_name"),
        "email": api_committer.get("email") or commit.get("git_committer_email"),
    }
    labels = classify_commit(
        message=message,
        coauthors=coauthors,
        github_author=github_author,
        github_committer=github_committer,
        author=author,
        committer=committer,
    )

    enriched = dict(commit)
    enriched.update(
        {
            "html_url": item.get("html_url") or commit.get("html_url"),
            "api_url": item.get("url") or commit.get("api_url"),
            "author_login": github_author.get("login"),
            "author_type": github_author.get("type"),
            "committer_login": github_committer.get("login"),
            "committer_type": github_committer.get("type"),
            "git_author_name": author.get("name"),
            "git_author_email": author.get("email"),
            "git_committer_name": committer.get("name"),
            "git_committer_email": committer.get("email"),
            "message_subject": message_subject or commit.get("message_subject", ""),
            "message_body": message_body,
            "coauthors": coauthors,
            "labels": labels,
            "category": category_from_labels(labels),
            "github_metadata_enriched": True,
        }
    )
    return enriched


def add_git_file_changes(
    repo: str,
    repo_dir: Path,
    commits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    detailed_commits: list[dict[str, Any]] = []
    file_count = 0
    show_progress = progress_enabled()
    commit_iter = progress_iter(
        commits,
        total=len(commits),
        desc="Extracting git diffs",
        unit="commit",
    )
    for index, commit in enumerate(commit_iter, start=1):
        if not show_progress:
            log(f"Extracting git diff for commit {index}/{len(commits)}: {commit['sha']}")
        detailed_commit = dict(commit)
        detailed_commit["files"] = parse_git_show_files(
            repo=repo,
            repo_dir=repo_dir,
            commit=detailed_commit,
            commit_index=index,
        )
        detailed_commits.append(detailed_commit)
        file_count += len(detailed_commit.get("files") or [])
        progress_postfix(commit_iter, files=file_count)
    return detailed_commits


def parse_git_show_files(
    repo: str,
    repo_dir: Path,
    commit: dict[str, Any],
    commit_index: int,
) -> list[dict[str, Any]]:
    _ = repo
    output = git_show_patch(repo_dir, commit["sha"])
    raw_files = split_git_diff_files(output)
    return [
        normalize_git_file_change(commit_index, commit, raw_file)
        for raw_file in raw_files
    ]


def git_show_patch(repo_dir: Path, commit_sha: str) -> str:
    return run_git(
        repo_dir,
        [
            "show",
            "--format=",
            "--find-renames",
            "--find-copies",
            "--unified=0",
            "--patch",
            "--diff-merges=remerge",
            commit_sha,
        ],
    )


def split_git_diff_files(diff_output: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in diff_output.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            old_path, new_path = parse_diff_git_paths(line)
            current = {
                "old_path": old_path,
                "new_path": new_path,
                "previous_filename": None,
                "filename": new_path,
                "status": "modified",
                "patch_lines": [],
            }
            continue

        if current is None:
            continue
        if line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "removed"
        elif line.startswith("rename from "):
            current["previous_filename"] = line.removeprefix("rename from ").strip()
            current["status"] = "renamed"
        elif line.startswith("rename to "):
            current["filename"] = line.removeprefix("rename to ").strip()
            current["status"] = "renamed"
        elif line.startswith("--- "):
            old_path = parse_diff_marker_path(line.removeprefix("--- "))
            if old_path is not None:
                current["old_path"] = old_path
        elif line.startswith("+++ "):
            new_path = parse_diff_marker_path(line.removeprefix("+++ "))
            if new_path is not None:
                current["new_path"] = new_path
                current["filename"] = new_path

        if line.startswith("@@") or current["patch_lines"]:
            current["patch_lines"].append(line)

    if current is not None:
        files.append(current)
    return files


def parse_diff_git_paths(line: str) -> tuple[str | None, str | None]:
    raw = line.removeprefix("diff --git ").strip()
    if " b/" not in raw:
        return None, None
    old_raw, new_raw = raw.split(" b/", 1)
    return old_raw.removeprefix("a/"), new_raw


def parse_diff_marker_path(path: str) -> str | None:
    if path == "/dev/null":
        return None
    return path.removeprefix("a/").removeprefix("b/")


def normalize_git_file_change(
    commit_index: int,
    commit: dict[str, Any],
    raw_file: dict[str, Any],
) -> dict[str, Any]:
    patch = "\n".join(raw_file["patch_lines"])
    hunks = parse_patch_hunks(patch)
    additions = sum(len(hunk["added_lines"]) for hunk in hunks)
    deletions = sum(len(hunk["deleted_lines"]) for hunk in hunks)
    return {
        "commit_index": commit_index,
        "commit_sha": commit["sha"],
        "commit_date": commit["date"],
        "commit_category": commit["category"],
        "commit_labels": commit["labels"],
        "filename": raw_file.get("filename"),
        "previous_filename": raw_file.get("previous_filename"),
        "status": raw_file.get("status"),
        "additions": additions,
        "deletions": deletions,
        "changes": additions + deletions,
        "blob_url": (
            f"https://github.com/{commit['repo']}/blob/{commit['sha']}/{raw_file.get('filename')}"
            if raw_file.get("filename")
            else None
        ),
        "raw_url": (
            f"https://github.com/{commit['repo']}/raw/{commit['sha']}/{raw_file.get('filename')}"
            if raw_file.get("filename")
            else None
        ),
        "contents_url": None,
        "patch": patch,
        "hunks": hunks,
    }


def parse_coauthors(message: str) -> list[dict[str, str]]:
    return [
        {
            "name": match.group("name").strip(),
            "email": match.group("email").strip(),
        }
        for match in COAUTHORED_BY_PATTERN.finditer(message)
    ]


def split_commit_message(message: str) -> tuple[str, str]:
    lines = message.rstrip("\n").splitlines()
    if not lines:
        return "", ""
    subject = lines[0]
    body = "\n".join(lines[1:]).strip()
    return subject, body


def ai_message_attribution_evidence(message: str) -> str:
    return " ".join(
        line.strip()
        for line in message.splitlines()
        if AI_MESSAGE_ATTRIBUTION_PATTERN.search(line)
    )


def classify_commit(
    message: str,
    coauthors: list[dict[str, str]],
    github_author: dict[str, Any],
    github_committer: dict[str, Any],
    author: dict[str, Any],
    committer: dict[str, Any],
) -> list[str]:
    _ = message
    evidence = " ".join(
        [
            str(github_author.get("login") or ""),
            str(github_committer.get("login") or ""),
            str(author.get("name") or ""),
            str(author.get("email") or ""),
            str(committer.get("name") or ""),
            str(committer.get("email") or ""),
            *[
                f"{coauthor.get('name', '')} {coauthor.get('email', '')}"
                for coauthor in coauthors
            ],
            ai_message_attribution_evidence(message),
        ]
    )

    labels = [
        label
        for label, pattern in AI_PATTERNS.items()
        if pattern.search(evidence)
    ]
    if labels:
        return sorted(labels)

    if github_author.get("type") == "Bot" or github_committer.get("type") == "Bot":
        return ["other_bot"]
    if OTHER_BOT_PATTERN.search(evidence):
        return ["other_bot"]
    return []


def category_from_labels(labels: list[str]) -> str:
    if any(label in AI_LABELS for label in labels):
        return "ai_coauthored"
    if "other_bot" in labels:
        return "other_bot"
    return "not_ai_coauthored"


def split_by_category(commits: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    categories = {
        "ai_coauthored": [],
        "other_bot": [],
        "not_ai_coauthored": [],
    }
    for commit in commits:
        categories[commit["category"]].append(commit)
    return categories


def parse_patch_hunks(patch: str) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None
    old_line: int | None = None
    new_line: int | None = None

    for patch_line in patch.splitlines():
        header_match = HUNK_HEADER_PATTERN.match(patch_line)
        if header_match:
            old_start = int(header_match.group("old_start"))
            old_count = int(header_match.group("old_count") or "1")
            new_start = int(header_match.group("new_start"))
            new_count = int(header_match.group("new_count") or "1")
            current_hunk = {
                "header": patch_line,
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "old_range": [old_start, old_start + max(old_count - 1, 0)],
                "new_range": [new_start, new_start + max(new_count - 1, 0)],
                "added_lines": [],
                "deleted_lines": [],
            }
            hunks.append(current_hunk)
            old_line = old_start
            new_line = new_start
            continue

        if current_hunk is None or old_line is None or new_line is None:
            continue

        if patch_line.startswith("+") and not patch_line.startswith("+++"):
            current_hunk["added_lines"].append(
                {
                    "new_line": new_line,
                    "content": patch_line[1:],
                }
            )
            new_line += 1
        elif patch_line.startswith("-") and not patch_line.startswith("---"):
            current_hunk["deleted_lines"].append(
                {
                    "old_line": old_line,
                    "content": patch_line[1:],
                }
            )
            old_line += 1
        else:
            old_line += 1
            new_line += 1

    return hunks


def iter_file_changes(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for commit in commits:
        for file_change in commit.get("files") or []:
            row = {
                key: value
                for key, value in file_change.items()
                if key != "patch"
            }
            row["html_url"] = commit.get("html_url")
            rows.append(row)
    return rows


def build_commit_graph(commits: list[dict[str, Any]]) -> CommitGraph:
    by_sha: dict[str, dict[str, Any]] = {}
    input_index_by_sha: dict[str, int] = {}
    sha_less_commits: list[dict[str, Any]] = []
    for input_index, commit in enumerate(commits):
        sha = commit.get("sha")
        if not sha:
            sha_less_commits.append(commit)
            continue
        if sha in by_sha:
            continue
        by_sha[sha] = commit
        input_index_by_sha[sha] = input_index

    parents_by_sha = {
        sha: [
            parent_sha
            for parent_sha in commit.get("parents") or []
            if parent_sha in by_sha
        ]
        for sha, commit in by_sha.items()
    }
    children_by_sha: dict[str, list[str]] = {sha: [] for sha in by_sha}
    for sha, parent_shas in parents_by_sha.items():
        for parent_sha in parent_shas:
            children_by_sha[parent_sha].append(sha)

    def commit_sort_key(sha: str) -> tuple[str, int]:
        return (str(by_sha[sha].get("date") or ""), input_index_by_sha[sha])

    remaining_parent_counts = {
        sha: len(parent_shas)
        for sha, parent_shas in parents_by_sha.items()
    }
    ready = sorted(
        [
            sha
            for sha, parent_count in remaining_parent_counts.items()
            if parent_count == 0
        ],
        key=commit_sort_key,
    )
    ordered_shas: list[str] = []
    seen_shas: set[str] = set()

    while ready:
        sha = ready.pop(0)
        if sha in seen_shas:
            continue
        seen_shas.add(sha)
        ordered_shas.append(sha)
        for child_sha in sorted(children_by_sha[sha], key=commit_sort_key):
            remaining_parent_counts[child_sha] -= 1
            if remaining_parent_counts[child_sha] == 0:
                ready.append(child_sha)
        ready.sort(key=commit_sort_key)

    if len(ordered_shas) != len(by_sha):
        for sha in sorted(
            (sha for sha in by_sha if sha not in seen_shas),
            key=commit_sort_key,
        ):
            ordered_shas.append(sha)

    ancestors_by_sha: dict[str, set[str]] = {}
    for sha in ordered_shas:
        ancestors: set[str] = set()
        for parent_sha in parents_by_sha[sha]:
            ancestors.add(parent_sha)
            ancestors.update(ancestors_by_sha.get(parent_sha, set()))
        ancestors_by_sha[sha] = ancestors

    topo_index_by_sha = {
        sha: topo_index
        for topo_index, sha in enumerate(ordered_shas)
    }
    heads = [
        sha
        for sha in ordered_shas
        if not children_by_sha.get(sha)
    ]
    ordered_commits = [by_sha[sha] for sha in ordered_shas] + sorted(
        sha_less_commits,
        key=lambda commit: str(commit.get("date") or ""),
    )

    return CommitGraph(
        ordered_commits=ordered_commits,
        by_sha=by_sha,
        parents_by_sha=parents_by_sha,
        children_by_sha=children_by_sha,
        ancestors_by_sha=ancestors_by_sha,
        topo_index_by_sha=topo_index_by_sha,
        heads=heads,
    )


def is_descendant_commit(
    commit_graph: CommitGraph,
    commit_sha: str | None,
    ancestor_sha: str | None,
) -> bool:
    if not commit_sha or not ancestor_sha:
        return False
    return (
        commit_sha == ancestor_sha
        or ancestor_sha in commit_graph.ancestors_by_sha.get(commit_sha, set())
    )


def is_between_commits(
    commit_graph: CommitGraph,
    commit_sha: str | None,
    origin_sha: str | None,
    end_sha: str | None,
) -> bool:
    if not commit_sha or not origin_sha or not end_sha:
        return False
    if commit_sha == origin_sha:
        return False
    return (
        is_descendant_commit(commit_graph, commit_sha, origin_sha)
        and is_descendant_commit(commit_graph, end_sha, commit_sha)
    )


def end_commit_for_origin(
    commit_graph: CommitGraph,
    origin_sha: str | None,
) -> dict[str, Any] | None:
    if not commit_graph.ordered_commits:
        return None
    if not origin_sha:
        return commit_graph.ordered_commits[-1]

    candidate_heads = [
        head_sha
        for head_sha in commit_graph.heads
        if is_descendant_commit(commit_graph, head_sha, origin_sha)
    ]
    if not candidate_heads and origin_sha in commit_graph.by_sha:
        candidate_heads = [origin_sha]
    if not candidate_heads:
        return commit_graph.ordered_commits[-1]

    end_sha = max(
        candidate_heads,
        key=lambda sha: commit_graph.topo_index_by_sha.get(sha, -1),
    )
    return commit_graph.by_sha[end_sha]


def build_line_lifecycle(
    commits: list[dict[str, Any]],
    repo_dir: Path | None = None,
    blame_workers: int = 4,
    include_extensions: frozenset[str] = frozenset(),
    exclude_test_files: bool = False,
    branch_end_sha: str | None = None,
    blame_since: datetime | None = None,
    origin_commit_shas: set[str] | None = None,
) -> list[dict[str, Any]]:
    tracked_lines: list[dict[str, Any]] = []
    commit_graph = build_commit_graph(commits)
    sorted_commits = commit_graph.ordered_commits
    log(
        "Collecting tracked added lines from "
        f"{len(sorted_commits)} commit(s) for line lifecycle"
    )
    if include_extensions:
        log(
            "Restricting line lifecycle to origin file extension(s): "
            + ", ".join(sorted(include_extensions))
        )
    if exclude_test_files:
        log("Excluding origin files whose basename starts with test_ from line lifecycle")
    if origin_commit_shas is not None:
        log(
            "Restricting line births to "
            f"{len(origin_commit_shas)} selected origin commit(s)"
        )

    show_progress = progress_enabled()
    commit_iter = progress_iter(
        sorted_commits,
        total=len(sorted_commits),
        desc="Walking lifecycle diffs",
        unit="commit",
    )
    for index, commit in enumerate(commit_iter, start=1):
        if not show_progress and (index == 1 or index == len(sorted_commits) or index % 100 == 0):
            log(
                "Line lifecycle diff walk "
                f"{index}/{len(sorted_commits)} commits; "
                f"tracked lines so far: {len(tracked_lines)}"
            )
        for file_change in commit.get("files") or []:
            filename = file_change.get("filename")
            previous_filename = file_change.get("previous_filename")
            if previous_filename and filename:
                for line in tracked_lines:
                    if line["current_file"] == previous_filename and line["status"] == "survived":
                        line["current_file"] = filename

            if filename:
                apply_file_change_to_tracked_lines(tracked_lines, filename, file_change, commit)

            if origin_commit_shas is not None and commit.get("sha") not in origin_commit_shas:
                continue
            if not includes_extension(filename, include_extensions):
                continue
            if exclude_test_files and is_test_file(filename):
                continue
            for hunk in file_change.get("hunks") or []:
                for added_line in hunk.get("added_lines") or []:
                    tracked_lines.append(
                        {
                            "origin_commit_sha": commit["sha"],
                            "origin_commit_date": commit["date"],
                            "origin_commit_category": commit["category"],
                            "origin_commit_labels": commit["labels"],
                            "origin_html_url": commit["html_url"],
                            "origin_file": filename,
                            "origin_line": added_line["new_line"],
                            "current_file": filename,
                            "current_line": added_line["new_line"],
                            "line_content": added_line["content"],
                            "status": "survived",
                            "terminal_commit_sha": None,
                            "terminal_commit_date": None,
                            "terminal_commit_category": None,
                            "terminal_commit_labels": [],
                            "terminal_commit_message_subject": None,
                            "terminal_commit_message_body": None,
                            "terminal_commit_author_login": None,
                            "terminal_commit_author_type": None,
                            "terminal_git_author_name": None,
                            "terminal_git_author_email": None,
                            "terminal_commit_committer_login": None,
                            "terminal_commit_committer_type": None,
                            "terminal_git_committer_name": None,
                            "terminal_git_committer_email": None,
                            "terminal_github_metadata_enriched": False,
                            "terminal_reason": None,
                            "terminal_line": None,
                        }
                    )
        progress_postfix(commit_iter, tracked_lines=len(tracked_lines))

    if repo_dir is not None and sorted_commits:
        verify_line_lifecycle_with_blame(
            tracked_lines,
            repo_dir,
            commit_graph,
            blame_workers=blame_workers,
            branch_end_sha=branch_end_sha,
            blame_since=blame_since,
        )
    elif repo_dir is None:
        log("Skipping exact-line blame verification because no local repo was provided")

    log(f"Built line lifecycle with {len(tracked_lines)} tracked added line(s)")

    return tracked_lines


def apply_file_change_to_tracked_lines(
    tracked_lines: list[dict[str, Any]],
    filename: str,
    file_change: dict[str, Any],
    commit: dict[str, Any],
) -> None:
    hunks = sorted(file_change.get("hunks") or [], key=lambda hunk: hunk["old_start"])
    if not hunks:
        return

    for line in tracked_lines:
        if line["status"] != "survived":
            continue
        if line["current_file"] != filename:
            continue
        current_line = line.get("current_line")
        if current_line is None:
            continue
        new_line = map_surviving_line(current_line, hunks)
        if new_line is not None:
            line["current_line"] = new_line
            continue

        terminal_hunk = hunk_for_old_line(current_line, hunks)
        reason = (
            "modified"
            if terminal_hunk is not None and terminal_hunk.get("added_lines")
            else "deleted"
        )
        line["status"] = reason
        mark_line_terminal(line, commit, f"line_{reason}", current_line)


def mark_line_terminal(
    line: dict[str, Any],
    commit: dict[str, Any],
    terminal_reason: str,
    terminal_line: Any,
) -> None:
    line["terminal_commit_sha"] = commit["sha"]
    line["terminal_commit_date"] = commit["date"]
    line["terminal_commit_category"] = commit.get("category")
    line["terminal_commit_labels"] = commit.get("labels") or []
    line["terminal_commit_message_subject"] = commit.get("message_subject")
    line["terminal_commit_message_body"] = commit.get("message_body")
    line["terminal_commit_author_login"] = commit.get("author_login")
    line["terminal_commit_author_type"] = commit.get("author_type")
    line["terminal_git_author_name"] = commit.get("git_author_name")
    line["terminal_git_author_email"] = commit.get("git_author_email")
    line["terminal_commit_committer_login"] = commit.get("committer_login")
    line["terminal_commit_committer_type"] = commit.get("committer_type")
    line["terminal_git_committer_name"] = commit.get("git_committer_name")
    line["terminal_git_committer_email"] = commit.get("git_committer_email")
    line["terminal_github_metadata_enriched"] = bool(commit.get("github_metadata_enriched"))
    line["terminal_reason"] = terminal_reason
    line["terminal_line"] = terminal_line


def clear_line_terminal(line: dict[str, Any]) -> None:
    line["terminal_commit_sha"] = None
    line["terminal_commit_date"] = None
    line["terminal_commit_category"] = None
    line["terminal_commit_labels"] = []
    line["terminal_commit_message_subject"] = None
    line["terminal_commit_message_body"] = None
    line["terminal_commit_author_login"] = None
    line["terminal_commit_author_type"] = None
    line["terminal_git_author_name"] = None
    line["terminal_git_author_email"] = None
    line["terminal_commit_committer_login"] = None
    line["terminal_commit_committer_type"] = None
    line["terminal_git_committer_name"] = None
    line["terminal_git_committer_email"] = None
    line["terminal_github_metadata_enriched"] = False
    line["terminal_reason"] = None
    line["terminal_line"] = None


def map_surviving_line(old_line: int, hunks: list[dict[str, Any]]) -> int | None:
    delta = 0
    for hunk in hunks:
        old_start = hunk["old_start"]
        old_count = hunk["old_count"]
        new_count = hunk["new_count"]
        old_end = old_start + old_count - 1
        if old_count and old_start <= old_line <= old_end:
            return None
        if old_line > old_end:
            delta += new_count - old_count
    return old_line + delta


def hunk_for_old_line(old_line: int, hunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for hunk in hunks:
        old_count = hunk["old_count"]
        if not old_count:
            continue
        old_start = hunk["old_start"]
        old_end = old_start + old_count - 1
        if old_start <= old_line <= old_end:
            return hunk
    return None


def verify_line_lifecycle_with_blame(
    tracked_lines: list[dict[str, Any]],
    repo_dir: Path,
    commit_graph: CommitGraph,
    blame_workers: int = 4,
    branch_end_sha: str | None = None,
    blame_since: datetime | None = None,
) -> None:
    log(
        "Starting exact-line blame verification for "
        f"{len(tracked_lines)} tracked line(s)"
    )
    blame_since_arg = git_datetime_arg(blame_since) if blame_since is not None else None
    if blame_since_arg is not None:
        log(
            "Restricting git blame history to selected window: "
            f"--since={blame_since_arg}"
        )
    blame_cache: FileBlameCache = {}
    line_blame_cache: LineBlameCache = {}
    final_path_cache: dict[tuple[str | None, str | None, str], str | None] = {}
    terminal_cache: TerminalCache = {}
    origin_occurrences: Counter[tuple[str | None, str | None, str]] = Counter()
    status_counts: Counter[str] = Counter()
    contexts = prepare_blame_contexts(
        tracked_lines,
        commit_graph,
        final_path_cache,
        origin_occurrences,
        branch_end_sha=branch_end_sha,
    )

    final_full_jobs = {
        (context["end_sha"], path)
        for context in contexts
        for path in context["final_paths"]
    }
    prefetch_blame_file_lines(
        repo_dir,
        blame_cache,
        final_full_jobs,
        blame_workers,
        blame_since_arg,
    )

    terminal_contexts: list[dict[str, Any]] = []
    show_progress = progress_enabled()
    final_iter = progress_iter(
        contexts,
        total=len(contexts),
        desc="Checking final blame",
        unit="line",
    )
    for index, context in enumerate(final_iter, start=1):
        if not show_progress and (index == 1 or index == len(contexts) or index % 1000 == 0):
            log(
                "Final full-file blame verification "
                f"{index}/{len(contexts)} tracked lines; "
                f"file cache entries: {len(blame_cache)}"
            )
        found_survivor = mark_survived_from_full_blame(
            context,
            repo_dir,
            blame_cache,
            blame_since_arg,
        )
        if found_survivor:
            status_counts["survived"] += 1
            progress_postfix(
                final_iter,
                survived=status_counts["survived"],
                unresolved=len(terminal_contexts),
                cache=len(blame_cache),
            )
            continue
        terminal_contexts.append(context)
        progress_postfix(
            final_iter,
            survived=status_counts["survived"],
            unresolved=len(terminal_contexts),
            cache=len(blame_cache),
        )

    prefetch_diff_terminal_candidate_blames(
        terminal_contexts,
        commit_graph,
        repo_dir,
        blame_cache,
        line_blame_cache,
        blame_workers,
        blame_since_arg,
    )
    resolve_terminal_contexts(
        terminal_contexts,
        commit_graph,
        repo_dir,
        blame_cache,
        line_blame_cache,
        terminal_cache,
        blame_workers,
        blame_since_arg,
    )

    terminal_iter = progress_iter(
        terminal_contexts,
        total=len(terminal_contexts),
        desc="Applying terminal results",
        unit="line",
    )
    for index, context in enumerate(terminal_iter, start=1):
        if (
            not show_progress
            and (index == 1 or index == len(terminal_contexts) or index % 1000 == 0)
        ):
            log(
                "Terminal blame verification "
                f"{index}/{len(terminal_contexts)} unresolved lines; "
                f"file cache entries: {len(blame_cache)}"
            )
        line = context["line"]
        terminal_key = terminal_key_for_context(context)
        terminal_commit, status, current_file, current_line = terminal_cache[terminal_key]

        line["current_file"] = current_file
        line["current_line"] = current_line
        if terminal_commit is None:
            line["status"] = "survived"
            status_counts["survived"] += 1
            clear_line_terminal(line)
            continue

        line["status"] = status
        status_counts[status] += 1
        mark_line_terminal(line, terminal_commit, "line_missing_from_blame", current_line)
        progress_postfix(terminal_iter, cache=len(blame_cache), **status_counts)
    log(
        "Finished exact-line blame verification: "
        + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
    )


def prepare_blame_contexts(
    tracked_lines: list[dict[str, Any]],
    commit_graph: CommitGraph,
    final_path_cache: dict[tuple[str | None, str | None, str], str | None],
    origin_occurrences: Counter[tuple[str | None, str | None, str]],
    branch_end_sha: str | None = None,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for line in tracked_lines:
        origin_key = (
            line.get("origin_file"),
            line.get("origin_commit_sha"),
            line["line_content"],
        )
        origin_occurrences[origin_key] += 1
        required_occurrence = origin_occurrences[origin_key]

        if branch_end_sha:
            end_commit = commit_graph.by_sha.get(branch_end_sha)
            if end_commit is None:
                continue
            if not is_descendant_commit(
                commit_graph,
                branch_end_sha,
                line.get("origin_commit_sha"),
            ):
                continue
        else:
            end_commit = end_commit_for_origin(commit_graph, line.get("origin_commit_sha"))
        if end_commit is None:
            continue
        end_sha = end_commit.get("sha")
        if not end_sha:
            continue

        final_path_key = (line.get("origin_file"), line.get("origin_commit_sha"), end_sha)
        if final_path_key not in final_path_cache:
            final_path_cache[final_path_key] = path_at_end_of_window(
                line.get("origin_file"),
                line.get("origin_commit_sha"),
                commit_graph,
                end_sha,
            )
        final_path = final_path_cache[final_path_key]
        final_paths = unique_nonempty_values(
            final_path,
            line.get("origin_file"),
            line.get("current_file"),
        )
        contexts.append(
            {
                "line": line,
                "required_occurrence": required_occurrence,
                "end_sha": end_sha,
                "final_paths": final_paths,
            }
        )
    return contexts


def mark_survived_from_full_blame(
    context: dict[str, Any],
    repo_dir: Path,
    blame_cache: dict[tuple[str, str, str | None], BlameResult | None],
    blame_since: str | None,
) -> bool:
    line = context["line"]
    for final_path_candidate in context["final_paths"]:
        final_blame = cached_blame_file_lines(
            repo_dir,
            blame_cache,
            context["end_sha"],
            final_path_candidate,
            blame_since,
        )
        if final_blame is None:
            continue
        final_line_number = survival_line_number(
            final_blame,
            line,
            context["required_occurrence"],
            allow_window_match=blame_since is not None,
            current_line_hint=line.get("current_line"),
        )
        if final_line_number is None:
            continue
        mark_line_survived(line, final_path_candidate, final_line_number)
        return True
    return False


def mark_line_survived(
    line: dict[str, Any],
    current_file: str,
    current_line: int,
) -> None:
    line["current_file"] = current_file
    line["current_line"] = current_line
    line["status"] = "survived"
    clear_line_terminal(line)


def get_or_compute_cached_blame(
    cache: dict[Any, BlameResult | None],
    key: Any,
    in_flight: dict[Any, threading.Event] | None,
    cache_lock: Any,
    compute: Callable[[], BlameResult | None],
) -> BlameResult | None:
    if cache_lock is None or in_flight is None:
        if key not in cache:
            cache[key] = compute()
        return cache[key]

    while True:
        with cache_lock:
            if key in cache:
                return cache[key]
            pending = in_flight.get(key)
            if pending is None:
                pending = threading.Event()
                in_flight[key] = pending
                break
        pending.wait()

    try:
        result = compute()
    except Exception:
        with cache_lock:
            in_flight.pop(key, None)
            pending.set()
        raise

    with cache_lock:
        cache[key] = result
        in_flight.pop(key, None)
        pending.set()
        return result


def cached_blame_file_lines(
    repo_dir: Path,
    blame_cache: FileBlameCache,
    commit_sha: str,
    path: str,
    blame_since: str | None,
    cache_lock: Any = None,
    in_flight: dict[tuple[str, str, str | None], threading.Event] | None = None,
) -> BlameResult | None:
    key = (commit_sha, path, blame_since)
    def compute() -> BlameResult | None:
        log(
            "Running git blame command: "
            f"{format_git_command(repo_dir, blame_args(commit_sha, path, blame_since=blame_since))}"
        )
        return blame_file_lines(
            repo_dir,
            commit_sha,
            path,
            blame_since=blame_since,
        )

    return get_or_compute_cached_blame(blame_cache, key, in_flight, cache_lock, compute)


def cached_blame_line_range(
    repo_dir: Path,
    line_blame_cache: LineBlameCache,
    commit_sha: str,
    path: str,
    line_number: int,
    blame_since: str | None,
    cache_lock: Any = None,
    in_flight: dict[tuple[str, str, int, str | None], threading.Event] | None = None,
) -> BlameResult | None:
    key = (commit_sha, path, line_number, blame_since)
    def compute() -> BlameResult | None:
        args = blame_args(commit_sha, path, line_number=line_number, blame_since=blame_since)
        log(f"Running git blame command: {format_git_command(repo_dir, args)}")
        return blame_file_lines(
            repo_dir,
            commit_sha,
            path,
            line_number=line_number,
            blame_since=blame_since,
        )

    return get_or_compute_cached_blame(line_blame_cache, key, in_flight, cache_lock, compute)


def prefetch_blame_file_lines(
    repo_dir: Path,
    blame_cache: dict[tuple[str, str, str | None], BlameResult | None],
    jobs: set[tuple[str, str]],
    blame_workers: int,
    blame_since: str | None,
) -> None:
    missing_jobs = sorted(job for job in jobs if (job[0], job[1], blame_since) not in blame_cache)
    if not missing_jobs:
        return
    log(
        "Prefetching full-file blame for "
        f"{len(missing_jobs)} commit/path pair(s) with {max(1, blame_workers)} worker(s)"
    )
    if blame_workers <= 1:
        job_iter = progress_iter(
            missing_jobs,
            total=len(missing_jobs),
            desc="Full-file blame",
            unit="file",
        )
        for commit_sha, path in job_iter:
            blame_cache[(commit_sha, path, blame_since)] = blame_file_lines(
                repo_dir,
                commit_sha,
                path,
                blame_since=blame_since,
            )
            progress_postfix(job_iter, cache=len(blame_cache))
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=blame_workers) as executor:
        future_to_job = {
            executor.submit(
                blame_file_lines,
                repo_dir,
                commit_sha,
                path,
                blame_since=blame_since,
            ): (commit_sha, path)
            for commit_sha, path in missing_jobs
        }
        future_iter = progress_iter(
            concurrent.futures.as_completed(future_to_job),
            total=len(future_to_job),
            desc="Full-file blame",
            unit="file",
        )
        for future in future_iter:
            commit_sha, path = future_to_job[future]
            blame_cache[(commit_sha, path, blame_since)] = future.result()
            progress_postfix(future_iter, cache=len(blame_cache))


def prefetch_blame_line_ranges(
    repo_dir: Path,
    line_blame_cache: dict[tuple[str, str, int, str | None], BlameResult | None],
    jobs: set[tuple[str, str, int]],
    blame_workers: int,
    blame_since: str | None,
) -> None:
    missing_jobs = sorted(
        job for job in jobs if (job[0], job[1], job[2], blame_since) not in line_blame_cache
    )
    if not missing_jobs:
        return
    log(
        "Prefetching line-range blame for "
        f"{len(missing_jobs)} commit/path/line tuple(s) with {max(1, blame_workers)} worker(s)"
    )
    if blame_workers <= 1:
        job_iter = progress_iter(
            missing_jobs,
            total=len(missing_jobs),
            desc="Line-range blame",
            unit="line",
        )
        for commit_sha, path, line_number in job_iter:
            line_blame_cache[(commit_sha, path, line_number, blame_since)] = blame_file_lines(
                repo_dir,
                commit_sha,
                path,
                line_number=line_number,
                blame_since=blame_since,
            )
            progress_postfix(job_iter, cache=len(line_blame_cache))
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=blame_workers) as executor:
        future_to_job = {
            executor.submit(
                blame_file_lines,
                repo_dir,
                commit_sha,
                path,
                line_number,
                blame_since=blame_since,
            ): (commit_sha, path, line_number)
            for commit_sha, path, line_number in missing_jobs
        }
        future_iter = progress_iter(
            concurrent.futures.as_completed(future_to_job),
            total=len(future_to_job),
            desc="Line-range blame",
            unit="line",
        )
        for future in future_iter:
            commit_sha, path, line_number = future_to_job[future]
            line_blame_cache[(commit_sha, path, line_number, blame_since)] = future.result()
            progress_postfix(future_iter, cache=len(line_blame_cache))


def prefetch_diff_terminal_candidate_blames(
    contexts: list[dict[str, Any]],
    commit_graph: CommitGraph,
    repo_dir: Path,
    blame_cache: FileBlameCache,
    line_blame_cache: LineBlameCache,
    blame_workers: int,
    blame_since: str | None,
) -> None:
    candidate_infos = [
        candidate_info
        for context in contexts
        if (candidate_info := diff_terminal_candidate_info(context, commit_graph)) is not None
    ]
    line_jobs = {
        (
            candidate_info["commit_sha"],
            candidate_info["path"],
            candidate_info["current_line"],
        )
        for candidate_info in candidate_infos
        if isinstance(candidate_info["current_line"], int) and candidate_info["current_line"] > 0
    }
    prefetch_blame_line_ranges(
        repo_dir,
        line_blame_cache,
        line_jobs,
        blame_workers,
        blame_since,
    )

    full_jobs = {
        (candidate_info["commit_sha"], candidate_info["path"])
        for candidate_info in candidate_infos
    }
    prefetch_blame_file_lines(repo_dir, blame_cache, full_jobs, blame_workers, blame_since)


def terminal_key_for_context(context: dict[str, Any]) -> TerminalKey:
    line = context["line"]
    return (
        line.get("origin_file"),
        line["origin_commit_sha"],
        line["line_content"],
        context["required_occurrence"],
        context["end_sha"],
    )


def resolve_terminal_contexts(
    contexts: list[dict[str, Any]],
    commit_graph: CommitGraph,
    repo_dir: Path,
    blame_cache: FileBlameCache,
    line_blame_cache: LineBlameCache,
    terminal_cache: TerminalCache,
    blame_workers: int,
    blame_since: str | None,
) -> None:
    terminal_jobs: dict[TerminalKey, dict[str, Any]] = {}
    for context in contexts:
        terminal_key = terminal_key_for_context(context)
        if terminal_key not in terminal_cache and terminal_key not in terminal_jobs:
            terminal_jobs[terminal_key] = context
    if not terminal_jobs:
        return

    worker_count = max(1, blame_workers)
    log(
        "Resolving terminal commits for "
        f"{len(terminal_jobs)} unique unresolved line identity(s) "
        f"from {len(contexts)} unresolved line(s) with {worker_count} worker(s)"
    )
    if worker_count <= 1:
        job_iter = progress_iter(
            list(terminal_jobs.items()),
            total=len(terminal_jobs),
            desc="Resolving terminal commits",
            unit="key",
        )
        for terminal_key, context in job_iter:
            terminal_cache[terminal_key] = resolve_terminal_context(
                context,
                commit_graph,
                repo_dir,
                blame_cache,
                line_blame_cache,
                blame_since,
            )
            progress_postfix(
                job_iter,
                file_cache=len(blame_cache),
                line_cache=len(line_blame_cache),
            )
        return

    cache_lock = threading.Lock()
    file_blame_in_flight: dict[tuple[str, str, str | None], threading.Event] = {}
    line_blame_in_flight: dict[tuple[str, str, int, str | None], threading.Event] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_key = {
            executor.submit(
                resolve_terminal_context,
                context,
                commit_graph,
                repo_dir,
                blame_cache,
                line_blame_cache,
                blame_since,
                cache_lock,
                file_blame_in_flight,
                line_blame_in_flight,
            ): terminal_key
            for terminal_key, context in terminal_jobs.items()
        }
        future_iter = progress_iter(
            concurrent.futures.as_completed(future_to_key),
            total=len(future_to_key),
            desc="Resolving terminal commits",
            unit="key",
        )
        for future in future_iter:
            terminal_key = future_to_key[future]
            terminal_cache[terminal_key] = future.result()
            with cache_lock:
                file_cache_size = len(blame_cache)
                line_cache_size = len(line_blame_cache)
            progress_postfix(
                future_iter,
                file_cache=file_cache_size,
                line_cache=line_cache_size,
            )


def resolve_terminal_context(
    context: dict[str, Any],
    commit_graph: CommitGraph,
    repo_dir: Path,
    blame_cache: FileBlameCache,
    line_blame_cache: LineBlameCache,
    blame_since: str | None,
    cache_lock: Any = None,
    file_blame_in_flight: dict[tuple[str, str, str | None], threading.Event] | None = None,
    line_blame_in_flight: dict[tuple[str, str, int, str | None], threading.Event] | None = None,
) -> TerminalResult:
    line = context["line"]
    return (
        verified_diff_terminal_candidate(
            context,
            commit_graph,
            repo_dir,
            blame_cache,
            line_blame_cache,
            blame_since,
            cache_lock,
            file_blame_in_flight,
            line_blame_in_flight,
        )
        or find_first_missing_commit(
            line,
            commit_graph,
            context["end_sha"],
            repo_dir,
            blame_cache,
            line_blame_cache,
            context["required_occurrence"],
            blame_since,
            cache_lock,
            file_blame_in_flight,
            line_blame_in_flight,
        )
    )


def diff_terminal_candidate_info(
    context: dict[str, Any],
    commit_graph: CommitGraph,
) -> dict[str, Any] | None:
    line = context["line"]
    candidate_sha = line.get("terminal_commit_sha")
    if not is_between_commits(
        commit_graph,
        candidate_sha,
        line.get("origin_commit_sha"),
        context["end_sha"],
    ):
        return None
    candidate_commit = commit_graph.by_sha.get(candidate_sha)
    if candidate_commit is None:
        return None
    path = line.get("current_file") or line.get("origin_file")
    if path is None:
        return None
    current_line = line.get("terminal_line") or line.get("current_line") or line.get("origin_line")
    return {
        "commit": candidate_commit,
        "commit_sha": candidate_sha,
        "path": path,
        "current_line": current_line,
    }


def verified_diff_terminal_candidate(
    context: dict[str, Any],
    commit_graph: CommitGraph,
    repo_dir: Path,
    blame_cache: FileBlameCache,
    line_blame_cache: LineBlameCache,
    blame_since: str | None,
    cache_lock: Any = None,
    file_blame_in_flight: dict[tuple[str, str, str | None], threading.Event] | None = None,
    line_blame_in_flight: dict[tuple[str, str, int, str | None], threading.Event] | None = None,
) -> tuple[dict[str, Any] | None, str, str | None, int | None] | None:
    line = context["line"]
    candidate_info = diff_terminal_candidate_info(context, commit_graph)
    if candidate_info is None:
        return None
    candidate_commit = candidate_info["commit"]
    candidate_sha = candidate_info["commit_sha"]
    path = candidate_info["path"]
    current_line = candidate_info["current_line"]

    matched_line = blame_exact_line_at_hint(
        repo_dir,
        line_blame_cache,
        candidate_sha,
        path,
        current_line,
        line,
        context["required_occurrence"],
        blame_since,
        cache_lock,
        line_blame_in_flight,
    )
    if matched_line is not None:
        return None

    blamed_lines = cached_blame_file_lines(
        repo_dir,
        blame_cache,
        candidate_sha,
        path,
        blame_since,
        cache_lock=cache_lock,
        in_flight=file_blame_in_flight,
    )
    if blamed_lines is None:
        return candidate_commit, "deleted", path, current_line
    if survival_line_number(
        blamed_lines,
        line,
        context["required_occurrence"],
        allow_window_match=blame_since is not None,
        current_line_hint=current_line,
    ) is not None:
        return None

    status = line.get("status") if line.get("status") in {"modified", "deleted"} else "modified"
    return candidate_commit, status, path, current_line


def blame_exact_line_at_hint(
    repo_dir: Path,
    line_blame_cache: LineBlameCache,
    commit_sha: str,
    path: str,
    current_line: Any,
    line: dict[str, Any],
    required_occurrence: int,
    blame_since: str | None,
    cache_lock: Any = None,
    in_flight: dict[tuple[str, str, int, str | None], threading.Event] | None = None,
) -> int | None:
    if not isinstance(current_line, int) or current_line <= 0:
        return None
    hinted_blame = cached_blame_line_range(
        repo_dir,
        line_blame_cache,
        commit_sha,
        path,
        current_line,
        blame_since,
        cache_lock=cache_lock,
        in_flight=in_flight,
    )
    if hinted_blame is None:
        return None
    return survival_line_number(
        hinted_blame,
        line,
        required_occurrence,
        allow_window_match=blame_since is not None,
        current_line_hint=current_line,
    )


def find_first_missing_commit(
    line: dict[str, Any],
    commit_graph: CommitGraph,
    end_sha: str,
    repo_dir: Path,
    blame_cache: FileBlameCache,
    line_blame_cache: LineBlameCache,
    required_occurrence: int,
    blame_since: str | None,
    cache_lock: Any = None,
    file_blame_in_flight: dict[tuple[str, str, str | None], threading.Event] | None = None,
    line_blame_in_flight: dict[tuple[str, str, int, str | None], threading.Event] | None = None,
) -> tuple[dict[str, Any] | None, str, str | None, int | None]:
    origin_sha = line["origin_commit_sha"]
    path = line.get("origin_file")
    current_line = line.get("origin_line")

    for commit in commit_graph.ordered_commits:
        commit_sha = commit.get("sha")
        if not is_between_commits(commit_graph, commit_sha, origin_sha, end_sha):
            continue
        if path is None:
            return commit, "deleted", path, current_line

        path, touched, file_change = path_after_commit_with_touch(path, commit)
        if path is None:
            return commit, "deleted", path, current_line
        if not touched:
            continue

        matched_line = blame_exact_line_at_hint(
            repo_dir,
            line_blame_cache,
            commit["sha"],
            path,
            current_line,
            line,
            required_occurrence,
            blame_since,
            cache_lock,
            line_blame_in_flight,
        )
        if matched_line is not None:
            current_line = matched_line
            continue

        blamed_lines = cached_blame_file_lines(
            repo_dir,
            blame_cache,
            commit["sha"],
            path,
            blame_since,
            cache_lock=cache_lock,
            in_flight=file_blame_in_flight,
        )
        if blamed_lines is None:
            return commit, "deleted", path, current_line

        matched_line = survival_line_number(
            blamed_lines,
            line,
            required_occurrence,
            allow_window_match=blame_since is not None,
            current_line_hint=current_line,
        )
        if matched_line is None:
            return (
                commit,
                classify_missing_line_status(file_change, current_line),
                path,
                current_line,
            )
        current_line = matched_line

    return None, "survived", path, current_line


def path_at_end_of_window(
    origin_path: str | None,
    origin_sha: str | None,
    commit_graph: CommitGraph,
    end_sha: str,
) -> str | None:
    path = origin_path
    for commit in commit_graph.ordered_commits:
        commit_sha = commit.get("sha")
        if not is_between_commits(commit_graph, commit_sha, origin_sha, end_sha):
            continue
        if path is None:
            continue
        path = path_after_commit(path, commit)
    return path


def path_after_commit_with_touch(
    path: str,
    commit: dict[str, Any],
) -> tuple[str | None, bool, dict[str, Any] | None]:
    current_path: str | None = path
    touched = False
    touched_change: dict[str, Any] | None = None
    for file_change in commit.get("files") or []:
        previous_filename = file_change.get("previous_filename")
        filename = file_change.get("filename")
        status = file_change.get("status")
        if previous_filename and filename and current_path == previous_filename:
            current_path = filename
            touched = True
            touched_change = file_change
        elif filename == current_path:
            touched = True
            touched_change = file_change
            if status == "removed":
                current_path = None
    return current_path, touched, touched_change


def classify_missing_line_status(
    file_change: dict[str, Any] | None,
    current_line: int | None,
) -> str:
    if file_change is None:
        return "modified"
    if file_change.get("status") == "removed":
        return "deleted"
    if current_line is None:
        return "modified"

    hunks = sorted(file_change.get("hunks") or [], key=lambda hunk: hunk["old_start"])
    terminal_hunk = hunk_for_old_line(current_line, hunks)
    if terminal_hunk is None:
        return "modified"
    if terminal_hunk.get("added_lines"):
        return "modified"
    return "deleted"


def path_after_commit(path: str, commit: dict[str, Any]) -> str | None:
    current_path: str | None = path
    for file_change in commit.get("files") or []:
        previous_filename = file_change.get("previous_filename")
        filename = file_change.get("filename")
        status = file_change.get("status")
        if previous_filename and filename and current_path == previous_filename:
            current_path = filename
        elif status == "removed" and filename == current_path:
            current_path = None
    return current_path


def unique_nonempty_values(*values: str | None) -> list[str]:
    unique_values: list[str] = []
    seen_values: set[str] = set()
    for value in values:
        if not value or value in seen_values:
            continue
        unique_values.append(value)
        seen_values.add(value)
    return unique_values


def extension_for(path: str | None) -> str:
    if not path:
        return "(unknown)"
    suffix = Path(path).suffix.lower()
    return suffix or "(none)"


def normalize_extension(extension: str) -> str:
    normalized = extension.strip().lower()
    if not normalized:
        raise ValueError("extensions must not be empty")
    if normalized in {"(none)", "(unknown)"}:
        return normalized
    return normalized if normalized.startswith(".") else f".{normalized}"


def normalize_extension_values(values: list[str]) -> frozenset[str]:
    extensions: set[str] = set()
    for value in values:
        for part in value.split(","):
            if part.strip():
                extensions.add(normalize_extension(part))
    return frozenset(extensions)


def includes_extension(path: str | None, include_extensions: frozenset[str]) -> bool:
    if not include_extensions:
        return True
    return extension_for(path) in include_extensions


def is_test_file(path: str | None) -> bool:
    return bool(path and Path(path).name.startswith("test_"))


def exact_origin_line_number(
    blamed_lines: BlameResult,
    line: dict[str, Any],
    required_occurrence: int,
) -> int | None:
    origin_sha = line.get("origin_commit_sha")
    origin_line = line.get("origin_line")
    content = line.get("line_content")
    if origin_sha is None or content is None:
        return None
    if isinstance(origin_line, int):
        exact_matches = blamed_lines.identity_index.get((origin_sha, origin_line, content), [])
        if exact_matches:
            return exact_matches[0]
        return None

    fallback_matches = blamed_lines.sha_content_index.get((origin_sha, content), [])
    if len(fallback_matches) >= required_occurrence:
        return fallback_matches[required_occurrence - 1]
    return None


def survival_line_number(
    blamed_lines: BlameResult,
    line: dict[str, Any],
    required_occurrence: int,
    allow_window_match: bool = False,
    current_line_hint: Any = None,
) -> int | None:
    exact_line = exact_origin_line_number(blamed_lines, line, required_occurrence)
    if exact_line is not None:
        return exact_line
    if not allow_window_match:
        return None
    return window_content_line_number(blamed_lines, line, current_line_hint)


def window_content_line_number(
    blamed_lines: BlameResult,
    line: dict[str, Any],
    current_line_hint: Any = None,
) -> int | None:
    content = line.get("line_content")
    if not isinstance(content, str):
        return None

    matches = blamed_lines.content_index.get(content, [])
    if isinstance(current_line_hint, int) and current_line_hint > 0:
        if current_line_hint in matches:
            return current_line_hint

    if len(matches) == 1:
        return matches[0]
    return None


def blame_file_lines(
    repo_dir: Path,
    commit_sha: str,
    path: str,
    line_number: int | None = None,
    blame_since: str | None = None,
) -> BlameResult | None:
    try:
        output = run_git(
            repo_dir,
            blame_args(
                commit_sha,
                path,
                line_number=line_number,
                blame_since=blame_since,
            ),
        )
    except RuntimeError:
        return None
    return parse_blame_porcelain(output)


def blame_args(
    commit_sha: str,
    path: str,
    line_number: int | None = None,
    blame_since: str | None = None,
) -> list[str]:
    args = [
        "blame",
        "--line-porcelain",
        "-M",
    ]
    if blame_since is None:
        args.extend(["-C", "-C"])
    else:
        args.append(f"--since={blame_since}")
    if line_number is not None:
        args.extend(["-L", f"{line_number},{line_number}"])
    args.extend([commit_sha, "--", path])
    return args


def format_git_command(repo_dir: Path, args: list[str]) -> str:
    return shlex.join(["git", "-C", str(repo_dir), *args])


def parse_blame_porcelain(output: str) -> BlameResult:
    blamed_lines: list[dict[str, Any]] = []
    current_sha: str | None = None
    current_origin_line: int | None = None
    current_final_line: int | None = None
    current_filename: str | None = None

    for line in output.splitlines():
        if line.startswith("\t"):
            if current_sha is not None and current_final_line is not None:
                blamed_lines.append(
                    {
                        "sha": current_sha,
                        "origin_line": current_origin_line,
                        "final_line": current_final_line,
                        "filename": current_filename,
                        "content": line[1:],
                    }
                )
            current_sha = None
            current_origin_line = None
            current_final_line = None
            current_filename = None
            continue

        if line.startswith("filename "):
            current_filename = line.removeprefix("filename ")
            continue

        parts = line.split()
        if len(parts) >= 3 and re.fullmatch(r"\^?[0-9a-f]{40}", parts[0]):
            current_sha = parts[0].lstrip("^")
            current_origin_line = int(parts[1])
            current_final_line = int(parts[2])

    return build_blame_result(blamed_lines)


def build_blame_result(blamed_lines: list[dict[str, Any]]) -> BlameResult:
    identity_index: dict[tuple[str, int, str], list[int]] = {}
    sha_content_index: dict[tuple[str, str], list[int]] = {}
    content_index: dict[str, list[int]] = {}
    for blamed_line in blamed_lines:
        sha = blamed_line.get("sha")
        content = blamed_line.get("content")
        final_line = blamed_line.get("final_line")
        origin_line = blamed_line.get("origin_line")
        if not isinstance(sha, str) or not isinstance(content, str) or not isinstance(final_line, int):
            continue
        sha_content_index.setdefault((sha, content), []).append(final_line)
        content_index.setdefault(content, []).append(final_line)
        if isinstance(origin_line, int):
            identity_index.setdefault((sha, origin_line, content), []).append(final_line)
    return BlameResult(
        lines=blamed_lines,
        identity_index=identity_index,
        sha_content_index=sha_content_index,
        content_index=content_index,
    )


def write_outputs(
    commits: list[dict[str, Any]],
    output_dir: Path,
    repo_dir: Path | None = None,
    blame_workers: int = 4,
    include_extensions: frozenset[str] = frozenset(),
    exclude_test_files: bool = False,
    branch: str | None = None,
    branch_end_sha: str | None = None,
    blame_since: datetime | None = None,
    lifecycle_commits: list[dict[str, Any]] | None = None,
    lifecycle_origin_shas: set[str] | None = None,
) -> None:
    log(f"Preparing output data for {len(commits)} commits")
    output_dir.mkdir(parents=True, exist_ok=True)
    by_category = split_by_category(commits)
    file_changes = iter_file_changes(commits)
    log("Building line lifecycle")
    line_lifecycle = build_line_lifecycle(
        lifecycle_commits or commits,
        repo_dir,
        blame_workers=blame_workers,
        include_extensions=include_extensions,
        exclude_test_files=exclude_test_files,
        branch_end_sha=branch_end_sha,
        blame_since=blame_since,
        origin_commit_shas=lifecycle_origin_shas,
    )

    log(f"Writing outputs to {output_dir}")
    write_jsonl(output_dir / "all_commits.jsonl", commits)
    if lifecycle_commits is not None:
        write_jsonl(output_dir / "observation_commits.jsonl", lifecycle_commits)
    for category, category_commits in by_category.items():
        write_jsonl(output_dir / f"{category}_commits.jsonl", category_commits)
    write_jsonl(output_dir / "commit_file_changes.jsonl", file_changes)
    write_jsonl(output_dir / "line_lifecycle.jsonl", line_lifecycle)
    write_summary(
        output_dir / "summary.json",
        commits,
        by_category,
        line_lifecycle,
        include_extensions=include_extensions,
        exclude_test_files=exclude_test_files,
        branch=branch,
        branch_end_sha=branch_end_sha,
        blame_since=blame_since,
        observation_commits=len(lifecycle_commits or commits),
    )
    write_summary_csv(output_dir / "summary.csv", by_category)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as output_file:
        for row in rows:
            output_file.write(json.dumps(row, sort_keys=True) + "\n")


def write_summary(
    path: Path,
    commits: list[dict[str, Any]],
    by_category: dict[str, list[dict[str, Any]]],
    line_lifecycle: list[dict[str, Any]],
    include_extensions: frozenset[str] = frozenset(),
    exclude_test_files: bool = False,
    branch: str | None = None,
    branch_end_sha: str | None = None,
    blame_since: datetime | None = None,
    observation_commits: int | None = None,
) -> None:
    label_counts: dict[str, int] = {}
    for commit in commits:
        for label in commit["labels"] or ["none"]:
            label_counts[label] = label_counts.get(label, 0) + 1

    summary = {
        "branch": branch,
        "branch_end_sha": branch_end_sha,
        "blame_since": git_datetime_arg(blame_since) if blame_since is not None else None,
        "total_commits": len(commits),
        "observation_commits": observation_commits if observation_commits is not None else len(commits),
        "categories": {
            category: len(category_commits)
            for category, category_commits in by_category.items()
        },
        "labels": dict(sorted(label_counts.items())),
        "file_changes": sum(len(commit.get("files") or []) for commit in commits),
        "total_added_lines": sum(
            len(hunk.get("added_lines") or [])
            for commit in commits
            for file_change in commit.get("files") or []
            for hunk in file_change.get("hunks") or []
        ),
        "tracked_added_lines": len(line_lifecycle),
        "line_lifecycle_include_extensions": sorted(include_extensions),
        "line_lifecycle_exclude_test_files": exclude_test_files,
        "line_lifecycle": _count_by_key(line_lifecycle, "status"),
    }
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_summary_csv(path: Path, by_category: dict[str, list[dict[str, Any]]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=["category", "count"])
        writer.writeheader()
        for category, category_commits in by_category.items():
            writer.writerow({"category": category, "count": len(category_commits)})


def _count_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


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
    iterable: Any,
    total: int | None = None,
    desc: str | None = None,
    unit: str = "it",
) -> Any:
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


def progress_bar(total: int | None = None, desc: str | None = None, unit: str = "it") -> Any:
    if not progress_enabled():
        return None
    return _tqdm(
        total=total,
        desc=desc,
        unit=unit,
        dynamic_ncols=True,
        leave=False,
        file=sys.stderr,
    )


def progress_postfix(progress: Any, **values: Any) -> None:
    if hasattr(progress, "set_postfix"):
        progress.set_postfix(values, refresh=False)


def progress_close(progress: Any) -> None:
    if hasattr(progress, "close"):
        progress.close()


def log(message: str) -> None:
    formatted = f"[github_commit_scraper] {message}"
    if progress_enabled():
        _tqdm.write(formatted, file=sys.stderr)
    else:
        print(formatted, file=sys.stderr, flush=True)



def run_git(repo_dir: Path, args: list[str]) -> str:
    return run_command(["git", "-C", str(repo_dir), *args])


def run_command(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(args)}\n"
            f"{completed.stderr}"
        )
    return completed.stdout


def _next_link(headers: Any) -> str | None:
    link_header = headers.get("Link")
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.strip().partition(";")
        if 'rel="next"' in rel_part:
            return url_part.strip()[1:-1]
    return None


def _parse_dt(value: str | None, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None

    is_date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    if is_date_only:
        parsed_date = datetime.fromisoformat(value).date()
        parsed = datetime.combine(
            parsed_date,
            datetime_time.max if end_of_day else datetime_time.min,
            tzinfo=timezone.utc,
        )
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape commits reachable from one branch and compute branch-scoped "
            "line survival."
        )
    )
    parser.add_argument("--repo", required=True, help="Repository in owner/name form.")
    parser.add_argument("--output-dir", type=Path, default=Path("src/webscrape/output"))
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("src/webscrape/repos"),
        help="Directory where repos are cloned for local git analysis.",
    )
    parser.add_argument(
        "--local-repo",
        type=Path,
        help="Use an existing local git checkout instead of cloning.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Reuse an existing managed clone without fetching instead of deleting and recloning.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the managed clone under --clone-dir after scraping. Local repos are never removed.",
    )
    parser.add_argument(
        "--full-clone-for-blame",
        action="store_true",
        help="Clone all blobs instead of using a blobless clone. Faster for blame-heavy full lifecycle runs.",
    )
    parser.add_argument(
        "--blame-workers",
        type=int,
        default=4,
        help="Number of parallel git blame workers for independent blame jobs. Default: 4.",
    )
    parser.add_argument(
        "--include-extension",
        action="append",
        default=[],
        help=(
            "Only track line lifecycle rows whose origin file has this extension, "
            "e.g. .py. Repeat the option or pass comma-separated values."
        ),
    )
    parser.add_argument(
        "--exclude-test-files",
        action="store_true",
        help="Do not track line lifecycle rows born in files whose basename starts with test_.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Branch to scrape and use as the survival endpoint. Default: main.",
    )
    parser.add_argument(
        "--since",
        "--start-date",
        dest="since",
        help="Optional ISO timestamp/date lower bound, e.g. 2026-05-01.",
    )
    parser.add_argument(
        "--until",
        "--end-date",
        dest="until",
        help="Optional ISO timestamp/date upper bound, e.g. 2026-05-07.",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        help=(
            "Limit to the most recent N commits from the selected branch after "
            "date filtering. Intended for local samples."
        ),
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument(
        "--skip-github-api",
        action="store_true",
        help="Skip GitHub API metadata enrichment and use local git metadata only.",
    )
    parser.add_argument(
        "--progress",
        choices=["auto", "always", "never"],
        default=os.environ.get("GITHUB_COMMIT_SCRAPER_PROGRESS", "auto"),
        help=(
            "Progress bar mode. auto shows tqdm bars on interactive stderr, "
            "always forces them, and never disables them. Default: auto."
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bars. Alias for --progress never.",
    )
    args = parser.parse_args()
    if args.no_progress:
        args.progress = "never"
    if args.progress not in {"auto", "always", "never"}:
        parser.error("--progress must be one of: auto, always, never")
    configure_progress(args.progress)
    if args.progress != "never" and _tqdm is None:
        log("tqdm is not installed; progress bars disabled")
    try:
        include_extensions = normalize_extension_values(args.include_extension)
    except ValueError as exc:
        parser.error(str(exc))
    if args.max_commits is not None and args.max_commits < 0:
        parser.error("--max-commits must be non-negative")

    repo_output_dir = args.output_dir / args.repo.replace("/", "__")
    log(f"Starting scrape for {args.repo}")
    managed_clone = args.local_repo is None
    repo_dir = (
        args.local_repo
        if args.local_repo is not None
        else ensure_local_repo(
            args.repo,
            args.clone_dir,
            skip_fetch=args.skip_fetch,
            full_clone=args.full_clone_for_blame,
        )
    )
    try:
        if args.local_repo is not None:
            log(f"Using local repo: {repo_dir}")
        log(f"Discovering commits from local branch history: {args.branch}")
        since = _parse_dt(args.since)
        until = _parse_dt(args.until, end_of_day=True)
        if since is not None or until is not None:
            log(
                "Using time range: "
                f"since={since.isoformat() if since else 'none'}, "
                f"until={until.isoformat() if until else 'none'}"
            )
        branch_commits, branch_end_sha = fetch_branch_commits(
            args.repo,
            repo_dir=repo_dir,
            branch=args.branch,
            since=since,
        )
        origin_commits = select_origin_commits(
            branch_commits,
            until=until,
            max_commits=args.max_commits,
        )
        observation_commits = observation_commits_for_origins(branch_commits, origin_commits)
        origin_shas = {commit["sha"] for commit in origin_commits}
        log(
            "Discovered "
            f"{len(origin_commits)} selected origin commits and "
            f"{len(observation_commits)} observation commits"
        )
        if branch_end_sha:
            log(f"Using branch survival endpoint: {branch_end_sha}")
        if not args.skip_github_api:
            log("Enriching selected origin commits with GitHub API metadata")
            origin_commits = enrich_commits_with_github_api(
                GitHubClient(token=args.token),
                args.repo,
                origin_commits,
                args.branch,
                since=since,
                until=until,
            )
        else:
            log("Skipping GitHub API metadata enrichment")
        enriched_by_sha = {commit["sha"]: commit for commit in origin_commits}
        observation_commits = [
            enriched_by_sha.get(commit["sha"], commit)
            for commit in observation_commits
        ]
        observation_commits = add_git_file_changes(args.repo, repo_dir, observation_commits)
        detailed_observation_by_sha = {
            commit["sha"]: commit
            for commit in observation_commits
        }
        origin_commits = [
            detailed_observation_by_sha[commit["sha"]]
            for commit in origin_commits
        ]
        write_outputs(
            origin_commits,
            repo_output_dir,
            repo_dir,
            blame_workers=max(1, args.blame_workers),
            include_extensions=include_extensions,
            exclude_test_files=args.exclude_test_files,
            branch=args.branch,
            branch_end_sha=branch_end_sha,
            blame_since=since,
            lifecycle_commits=observation_commits,
            lifecycle_origin_shas=origin_shas,
        )

        by_category = split_by_category(origin_commits)
        print(f"repo: {args.repo}")
        print(f"branch: {args.branch}")
        print(f"branch_end_sha: {branch_end_sha or 'none'}")
        print(f"origin_commits: {len(origin_commits)}")
        print(f"observation_commits: {len(observation_commits)}")
        for category, category_commits in by_category.items():
            print(f"{category}: {len(category_commits)}")
        print(f"wrote {repo_output_dir}")
    finally:
        if managed_clone and not args.keep_clone:
            remove_cloned_repo_dir(repo_dir)


if __name__ == "__main__":
    main()
