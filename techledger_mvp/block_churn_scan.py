"""Build a commit-level function/method rework dataset from five public repositories.

MVP definition:
- block: named function/method on the default branch
- admission: a first-parent commit that adds or modifies at least one surviving block
- horizon: 180 days after admission
- rework count: later distinct commits touching any admitted block
- rework volume: later changed lines inside those blocks
- rework intensity: rework volume / admitted block LOC

This is deliberately an illustrative scanner, not a production lineage engine.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from block_parser import Block, extract_blocks, language_for_path


TARGET_REPOS = [
    "BarthPaleologue/CosmosJourneyer",
    "gameknife/gkNextEngine",
    "MudBlazor/MudBlazor",
    "skeletonlabs/skeleton",
    "PrimeIntellect-ai/verifiers",
]

ADMISSION_START = datetime(2025, 5, 1, tzinfo=timezone.utc)
ADMISSION_END = datetime(2025, 12, 2, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_START = ADMISSION_START - timedelta(days=180)
OBSERVATION_END = ADMISSION_END + timedelta(days=180)
HORIZON_DAYS = 180

ROOT = Path(__file__).resolve().parents[1]
WORK = Path("/tmp/techledger_block_churn")
OUTPUT = ROOT / "techledger_mvp" / "output"
RQ1A = ROOT / "dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv"

HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)
ISSUE_RE = re.compile(r"(?:#\d+|https?://\S+/(?:issues|pull)/\d+)", re.I)

MESSAGE_MARKERS = {
    "message_fix": re.compile(r"\b(fix|bug|repair|correct|regression|hotfix)\b", re.I),
    "message_refactor": re.compile(r"\b(refactor|cleanup|clean up|rework|simplif)\w*\b", re.I),
    "message_test": re.compile(r"\b(test|spec|coverage)\w*\b", re.I),
    "message_perf": re.compile(r"\b(perf|performance|optimi[sz]|faster|speed)\w*\b", re.I),
    "message_docs": re.compile(r"\b(doc|docs|readme|comment)\w*\b", re.I),
    "message_feature": re.compile(r"\b(feat|feature|add|implement|support|introduce)\w*\b", re.I),
    "message_revert": re.compile(r"\b(revert|rollback|back out)\b", re.I),
    "message_dependency": re.compile(r"\b(dependenc|upgrade|bump|version|package)\w*\b", re.I),
}


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class FileChange:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class CommitInfo:
    sha: str
    parents: list[str]
    date: datetime
    author_email: str
    author_name: str
    subject: str


@dataclass
class Event:
    repo: str
    sha: str
    date: datetime
    author_email: str
    block_changes: dict[str, int]
    block_metrics: dict[str, Block]
    markers: dict[str, object]


def git(repo_dir: Path, args: list[str], check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr[-2000:]}")
    return proc.stdout


def clone_repo(slug: str) -> Path:
    target = WORK / slug.replace("/", "__")
    if target.exists():
        shutil.rmtree(target)
    subprocess.run(
        [
            "git", "clone", "--quiet", "--filter=blob:none", "--no-tags",
            f"https://github.com/{slug}.git", str(target),
        ],
        check=True,
    )
    return target


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc)


def list_commits(repo_dir: Path) -> list[CommitInfo]:
    fmt = "%H%x1f%P%x1f%aI%x1f%ae%x1f%an%x1f%s%x1e"
    raw = git(
        repo_dir,
        [
            "log", "--first-parent", "--reverse",
            f"--since={HISTORY_START.isoformat()}",
            f"--until={OBSERVATION_END.isoformat()}",
            f"--format={fmt}", "HEAD",
        ],
    )
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) != 6:
            continue
        sha, parents, date, email, name, subject = parts
        commits.append(
            CommitInfo(
                sha=sha,
                parents=[p for p in parents.split() if p],
                date=parse_dt(date),
                author_email=email.lower().strip(),
                author_name=name.strip(),
                subject=subject.strip(),
            )
        )
    return commits


def parse_diff(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    current: FileChange | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if not match:
                current = None
                continue
            current = FileChange(match.group(1), match.group(2))
            changes.append(current)
            continue
        if current is None:
            continue
        if line.startswith("rename from "):
            current.old_path = line[len("rename from ") :]
            continue
        if line.startswith("rename to "):
            current.new_path = line[len("rename to ") :]
            continue
        match = HUNK_RE.match(line)
        if match:
            current.hunks.append(
                Hunk(
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2) or 1),
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4) or 1),
                )
            )
    return changes


def file_text(repo_dir: Path, sha: str, path: str) -> str:
    if path == "/dev/null":
        return ""
    return git(repo_dir, ["show", f"{sha}:{path}"], check=False)


def canonical_path(path_aliases: dict[str, str], old_path: str, new_path: str) -> str:
    existing = path_aliases.get(old_path) or path_aliases.get(new_path)
    canonical = existing or old_path or new_path
    if old_path:
        path_aliases[old_path] = canonical
    if new_path:
        path_aliases[new_path] = canonical
    return canonical


def allocate_range(blocks: list[Block], start: int, count: int) -> Counter[str]:
    allocation: Counter[str] = Counter()
    if count <= 0:
        return allocation
    for line_no in range(start, start + count):
        candidates = [b for b in blocks if b.start_line <= line_no <= b.end_line]
        if not candidates:
            continue
        # Attribute nested edits to the smallest named block containing the line.
        chosen = min(candidates, key=lambda b: (b.loc, b.name))
        allocation[chosen.name] += 1
    return allocation


def load_packaged_labels() -> dict[tuple[str, str], dict[str, str]]:
    labels: dict[tuple[str, str], dict[str, str]] = {}
    if not RQ1A.exists():
        return labels
    with RQ1A.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            if not repo or not sha:
                continue
            labels[(repo, sha)] = {
                "origin_role": (row.get("origin_role") or "").strip(),
                "origin_intent": (row.get("origin_primary_operational_intent") or "").strip(),
                "origin_maintenance_class": (row.get("origin_maintenance_class") or "").strip(),
            }
    return labels


def message_markers(subject: str) -> dict[str, object]:
    words = re.findall(r"[A-Za-z0-9_]+", subject)
    result: dict[str, object] = {
        "message_chars": len(subject),
        "message_words": len(words),
        "message_has_issue_ref": int(bool(ISSUE_RE.search(subject))),
        "message_has_question": int("?" in subject),
        "message_has_exclamation": int("!" in subject),
    }
    for name, pattern in MESSAGE_MARKERS.items():
        result[name] = int(bool(pattern.search(subject)))
    return result


def scan_repo(slug: str, packaged_labels: dict[tuple[str, str], dict[str, str]]) -> tuple[list[Event], dict[str, object]]:
    repo_dir = clone_repo(slug)
    commits = list_commits(repo_dir)
    path_aliases: dict[str, str] = {}
    block_history: dict[str, list[tuple[datetime, str, int, str]]] = defaultdict(list)
    author_first_seen: dict[str, datetime] = {}
    author_commit_count: Counter[str] = Counter()
    events: list[Event] = []

    repo_first_date_raw = git(repo_dir, ["log", "--reverse", "--format=%aI", "-1", "HEAD"]).strip()
    repo_first_date = parse_dt(repo_first_date_raw) if repo_first_date_raw else commits[0].date

    for index, commit in enumerate(commits, start=1):
        if not commit.parents:
            continue
        parent = commit.parents[0]
        diff = git(repo_dir, ["diff", "--unified=0", "--find-renames", parent, commit.sha, "--"])
        changes = parse_diff(diff)

        block_changes: Counter[str] = Counter()
        block_metrics: dict[str, Block] = {}
        new_blocks: set[str] = set()
        touched_code_files: set[str] = set()
        touched_test_files: set[str] = set()
        additions = deletions = 0

        for change in changes:
            path = change.new_path if change.new_path != "/dev/null" else change.old_path
            if language_for_path(path) is None:
                continue
            touched_code_files.add(path)
            if re.search(r"(^|/)(test|tests|spec|specs)(/|_)|\.(test|spec)\.", path, re.I):
                touched_test_files.add(path)

            canonical = canonical_path(path_aliases, change.old_path, change.new_path)
            before = file_text(repo_dir, parent, change.old_path)
            after = file_text(repo_dir, commit.sha, change.new_path)
            before_blocks = extract_blocks(change.old_path, before)
            after_blocks = extract_blocks(change.new_path, after)
            before_by_name = {b.name: b for b in before_blocks}
            after_by_name = {b.name: b for b in after_blocks}

            local_changes: Counter[str] = Counter()
            for hunk in change.hunks:
                old_alloc = allocate_range(before_blocks, hunk.old_start, hunk.old_count)
                new_alloc = allocate_range(after_blocks, hunk.new_start, hunk.new_count)
                deletions += hunk.old_count
                additions += hunk.new_count
                for name, amount in old_alloc.items():
                    local_changes[name] += amount
                for name, amount in new_alloc.items():
                    local_changes[name] += amount

            for name, amount in local_changes.items():
                key = f"{canonical}::{name}"
                block_changes[key] += amount
                metric = after_by_name.get(name) or before_by_name.get(name)
                if metric:
                    block_metrics[key] = metric
                if name in after_by_name and name not in before_by_name:
                    new_blocks.add(key)

        if not block_changes:
            author_first_seen.setdefault(commit.author_email, commit.date)
            author_commit_count[commit.author_email] += 1
            continue

        touched_keys = set(block_changes)
        prior_30 = []
        prior_90 = []
        last_dates = []
        for key in touched_keys:
            history = block_history.get(key, [])
            for item in history:
                age = (commit.date - item[0]).days
                if 0 <= age <= 90:
                    prior_90.append(item)
                    if age <= 30:
                        prior_30.append(item)
            if history:
                last_dates.append(history[-1][0])

        post_keys = {key for key in touched_keys if key in block_metrics and block_metrics[key].loc > 0}
        admitted_loc = sum(block_metrics[key].loc for key in post_keys)
        complexity_values = [block_metrics[key].complexity_proxy for key in post_keys]
        comment_values = [block_metrics[key].comment_fraction for key in post_keys]
        change_density_values = [block_changes[key] / max(1, block_metrics[key].loc) for key in post_keys]

        author_first = author_first_seen.get(commit.author_email, commit.date)
        markers: dict[str, object] = {
            "repo": slug,
            "sha": commit.sha,
            "date": commit.date.isoformat(),
            "author_email": commit.author_email,
            "subject": commit.subject,
            "parent_count": len(commit.parents),
            "is_merge": int(len(commit.parents) > 1),
            "weekday": commit.date.weekday(),
            "is_weekend": int(commit.date.weekday() >= 5),
            "author_hour_utc": commit.date.hour,
            "repo_age_days": (commit.date - repo_first_date).days,
            "author_prior_commits": author_commit_count[commit.author_email],
            "author_tenure_days": max(0, (commit.date - author_first).days),
            "code_additions": additions,
            "code_deletions": deletions,
            "code_churn": additions + deletions,
            "code_files_touched": len(touched_code_files),
            "test_files_touched": len(touched_test_files),
            "test_file_fraction": len(touched_test_files) / len(touched_code_files) if touched_code_files else 0.0,
            "blocks_touched": len(touched_keys),
            "new_blocks": len(new_blocks),
            "new_block_fraction": len(new_blocks) / len(touched_keys) if touched_keys else 0.0,
            "admitted_block_loc": admitted_loc,
            "mean_block_loc": (admitted_loc / len(post_keys)) if post_keys else 0.0,
            "max_block_loc": max((block_metrics[k].loc for k in post_keys), default=0),
            "mean_complexity_proxy": sum(complexity_values) / len(complexity_values) if complexity_values else 0.0,
            "max_complexity_proxy": max(complexity_values, default=0),
            "mean_comment_fraction": sum(comment_values) / len(comment_values) if comment_values else 0.0,
            "mean_change_density": sum(change_density_values) / len(change_density_values) if change_density_values else 0.0,
            "prior_block_commits_30d": len({item[1] for item in prior_30}),
            "prior_block_commits_90d": len({item[1] for item in prior_90}),
            "prior_block_changed_lines_30d": sum(item[2] for item in prior_30),
            "prior_block_changed_lines_90d": sum(item[2] for item in prior_90),
            "prior_block_authors_90d": len({item[3] for item in prior_90}),
            "days_since_any_block_touch": min((commit.date - d).days for d in last_dates) if last_dates else math.nan,
            "hot_block_fraction_30d": sum(bool([i for i in block_history.get(k, []) if 0 <= (commit.date - i[0]).days <= 30]) for k in touched_keys) / len(touched_keys),
        }
        markers.update(message_markers(commit.subject))
        markers.update(packaged_labels.get((slug, commit.sha), {}))

        events.append(
            Event(
                repo=slug,
                sha=commit.sha,
                date=commit.date,
                author_email=commit.author_email,
                block_changes=dict(block_changes),
                block_metrics=block_metrics,
                markers=markers,
            )
        )

        for key, amount in block_changes.items():
            block_history[key].append((commit.date, commit.sha, amount, commit.author_email))
        author_first_seen.setdefault(commit.author_email, commit.date)
        author_commit_count[commit.author_email] += 1

        if index % 250 == 0:
            print(f"{slug}: scanned {index}/{len(commits)} first-parent commits", flush=True)

    return events, {"repo": slug, "commits_examined": len(commits), "events_with_named_blocks": len(events)}


def add_outcomes(events: list[Event]) -> list[dict[str, object]]:
    block_index: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        for key in event.block_changes:
            block_index[key].append(event)

    rows: list[dict[str, object]] = []
    for event in events:
        if not (ADMISSION_START <= event.date <= ADMISSION_END):
            continue
        admitted_keys = {key for key in event.block_changes if key in event.block_metrics}
        admitted_loc = sum(event.block_metrics[key].loc for key in admitted_keys)
        if not admitted_keys or admitted_loc <= 0:
            continue

        downstream_commits: set[str] = set()
        downstream_volume = 0
        for key in admitted_keys:
            for later in block_index.get(key, []):
                if later.date <= event.date:
                    continue
                age_days = (later.date - event.date).total_seconds() / 86400.0
                if age_days > HORIZON_DAYS:
                    break
                downstream_commits.add(later.sha)
                downstream_volume += later.block_changes.get(key, 0)

        row = dict(event.markers)
        row["outcome_rework_count_180d"] = len(downstream_commits)
        row["outcome_rework_volume_180d"] = downstream_volume
        row["outcome_rework_intensity_180d"] = downstream_volume / admitted_loc
        rows.append(row)
    return rows


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    packaged_labels = load_packaged_labels()
    all_events: list[Event] = []
    repo_summary = []

    for slug in TARGET_REPOS:
        print(f"Scanning {slug}", flush=True)
        events, summary = scan_repo(slug, packaged_labels)
        all_events.extend(events)
        repo_summary.append(summary)

    rows = add_outcomes(all_events)
    write_csv(rows, OUTPUT / "commit_metrics.csv")
    summary = {
        "purpose": "Illustrate function/method-level downstream rework and admission-time predictors across five repositories.",
        "block_definition": "Named function/method symbol on the default branch; file renames are followed, function renames are new identities.",
        "admission_window": [ADMISSION_START.isoformat(), ADMISSION_END.isoformat()],
        "horizon_days": HORIZON_DAYS,
        "repos": repo_summary,
        "admission_rows": len(rows),
        "outcomes": [
            "outcome_rework_count_180d",
            "outcome_rework_volume_180d",
            "outcome_rework_intensity_180d",
        ],
        "limitations": [
            "Illustrative five-repository sample; not a validation dataset.",
            "Named functions/methods only; code outside named blocks is excluded.",
            "Function renames are treated as new identities; file renames are followed.",
            "Default-branch first-parent history only.",
        ],
    }
    (OUTPUT / "scan_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
