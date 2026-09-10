"""Fast TechLedger smell test: downstream rework of the same function/method.

This scanner intentionally stays high level. For each repository it makes one
streaming first-parent `git log -p --function-context` pass. Git supplies the
function-context patch; Python extracts a stable file+function name, admission
markers, and later touches. No historical checkout, `git show` loop, blame, or
native parser is used.

Outcomes over a fixed 180-day horizon:
- rework count: later distinct commits touching the same admitted functions
- rework volume: later changed lines in those functions
- rework intensity: rework volume / admitted post-change function LOC

This is an illustrative five-repository smell test, not validation data.
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

SUPPORTED_GLOBS = [
    "*.py", "*.pyi", "*.ts", "*.tsx", "*.js", "*.jsx", "*.cpp", "*.cc",
    "*.cxx", "*.c", "*.hpp", "*.hh", "*.hxx", "*.h", "*.cs",
]
SUPPORTED_SUFFIXES = tuple(glob[1:] for glob in SUPPORTED_GLOBS)
COMMIT_PREFIX = "@@@TECHLEDGER_COMMIT@@@"
COMMIT_SEP = "\x1f"

ISSUE_RE = re.compile(r"(?:#\d+|https?://\S+/(?:issues|pull)/\d+)", re.I)
HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<context>.*)$")
CONTROL_NAMES = {"if", "for", "while", "switch", "catch", "foreach", "using", "lock", "return", "throw", "new", "sizeof", "typeof", "nameof", "checked", "unchecked", "fixed", "do", "else", "try", "with", "match"}

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

PY_DEF_RE = re.compile(r"\b(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
JS_FUNCTION_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
ARROW_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=.*?=>")
GENERAL_CALLABLE_RE = re.compile(r"(?:^|\s|::|\.)([A-Za-z_$~][\w$~]*)\s*\([^;{}]*\)")
BRANCH_RE = re.compile(r"\b(if|for|while|case|catch|except|elif|switch|match)\b")
BOOL_RE = re.compile(r"&&|\|\||\band\b|\bor\b")
EXCEPTION_RE = re.compile(r"\b(try|catch|except|throw|raise|finally)\b")
ASSERT_RE = re.compile(r"\b(assert|expect)\b")
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.I)
IMPORT_RE = re.compile(r"^\s*(import\b|from\s+\S+\s+import\b|#include\b|using\s+\S+|require\s*\(|.*\bfrom\s+['\"])")
COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|<!--)")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass
class BlockTouch:
    key: str
    name: str
    path: str
    changed_lines: int = 0
    post_loc: int = 0
    complexity_proxy: int = 0
    comment_fraction: float = 0.0
    signature_added: bool = False


@dataclass
class Event:
    repo: str
    sha: str
    date: datetime
    author_email: str
    blocks: dict[str, BlockTouch]
    markers: dict[str, object]


@dataclass
class CommitBuilder:
    repo: str
    sha: str
    parents: list[str]
    date: datetime
    author_email: str
    author_name: str
    subject: str
    blocks: dict[str, BlockTouch] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    test_files: set[str] = field(default_factory=set)
    suffixes: set[str] = field(default_factory=set)
    path_depths: list[int] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    total_changed_lines: int = 0
    attributed_changed_lines: int = 0
    changed_text: list[str] = field(default_factory=list)
    current_old_path: str = ""
    current_new_path: str = ""
    hunk_header: str = ""
    hunk_old_count: int = 0
    hunk_new_count: int = 0
    hunk_body: list[str] = field(default_factory=list)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr[-3000:]}")
    return proc


def clone_repo(slug: str) -> Path:
    target = WORK / slug.replace("/", "__")
    if target.exists():
        shutil.rmtree(target)
    shallow = [
        "git", "clone", "--quiet", "--single-branch", "--no-tags",
        f"--shallow-since={HISTORY_START.date().isoformat()}",
        f"https://github.com/{slug}.git", str(target),
    ]
    proc = run(shallow, check=False)
    if proc.returncode != 0:
        if target.exists():
            shutil.rmtree(target)
        run(["git", "clone", "--quiet", "--single-branch", "--no-tags", f"https://github.com/{slug}.git", str(target)])
    return target


def repo_first_date(repo_dir: Path) -> datetime:
    roots = run(["git", "-C", str(repo_dir), "rev-list", "--max-parents=0", "HEAD"]).stdout.split()
    dates = []
    for sha in roots:
        value = run(["git", "-C", str(repo_dir), "show", "-s", "--format=%aI", sha]).stdout.strip()
        if value:
            dates.append(parse_dt(value))
    return min(dates) if dates else HISTORY_START


def normalize_path(path: str) -> str:
    path = path.strip()
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def canonical_path(aliases: dict[str, str], old_path: str, new_path: str) -> str:
    old_path = normalize_path(old_path)
    new_path = normalize_path(new_path)
    canonical = aliases.get(old_path) or aliases.get(new_path) or old_path or new_path
    if old_path and old_path != "/dev/null":
        aliases[old_path] = canonical
    if new_path and new_path != "/dev/null":
        aliases[new_path] = canonical
    return canonical


def callable_name(text: str) -> str | None:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return None
    for pattern in (PY_DEF_RE, JS_FUNCTION_RE, ARROW_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    matches = list(GENERAL_CALLABLE_RE.finditer(text))
    for match in reversed(matches):
        name = match.group(1)
        if name.lower() not in CONTROL_NAMES:
            return name
    return None


def is_code_path(path: str) -> bool:
    return path.lower().endswith(SUPPORTED_SUFFIXES)


def is_test_path(path: str) -> bool:
    return bool(re.search(r"(^|/)(test|tests|spec|specs)(/|_)|\.(test|spec)\.", path, re.I))


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


def load_packaged_labels() -> dict[tuple[str, str], dict[str, str]]:
    labels: dict[tuple[str, str], dict[str, str]] = {}
    if not RQ1A.exists():
        return labels
    with RQ1A.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo") or row.get("repo_key") or "").strip()
            sha = (row.get("origin_commit_sha") or "").strip()
            if repo and sha:
                labels[(repo, sha)] = {
                    "origin_role": (row.get("origin_role") or "").strip(),
                    "origin_intent": (row.get("origin_primary_operational_intent") or "").strip(),
                    "origin_maintenance_class": (row.get("origin_maintenance_class") or "").strip(),
                }
    return labels


def post_image_lines(hunk_body: list[str]) -> list[str]:
    result = []
    for line in hunk_body:
        if line.startswith("\\ No newline") or line.startswith("-"):
            continue
        if line.startswith("+") or line.startswith(" "):
            result.append(line[1:])
    return result


def changed_lines(hunk_body: list[str]) -> tuple[list[str], list[str]]:
    added, deleted = [], []
    for line in hunk_body:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            deleted.append(line[1:])
    return added, deleted


def identify_hunk_name(header_context: str, body: list[str]) -> str | None:
    name = callable_name(header_context)
    if name:
        return name
    # Function-context hunks usually contain the declaration. Prefer unchanged or
    # added lines near the start of the hunk to avoid matching calls in the body.
    candidates = []
    for line in body[:40]:
        if line.startswith((" ", "+", "-")):
            candidates.append(line[1:])
    for line in candidates:
        name = callable_name(line)
        if name:
            return name
    return None


def finalize_hunk(builder: CommitBuilder, aliases: dict[str, str]) -> None:
    if not builder.hunk_body:
        builder.hunk_header = ""
        return
    path = builder.current_new_path if builder.current_new_path != "/dev/null" else builder.current_old_path
    path = normalize_path(path)
    added, deleted = changed_lines(builder.hunk_body)
    changed = added + deleted
    builder.additions += len(added)
    builder.deletions += len(deleted)
    builder.total_changed_lines += len(changed)
    builder.changed_text.extend(changed)

    if not path or not is_code_path(path):
        builder.hunk_body = []
        builder.hunk_header = ""
        return

    canonical = canonical_path(aliases, builder.current_old_path, builder.current_new_path)
    name = identify_hunk_name(builder.hunk_header, builder.hunk_body)
    if not name:
        builder.hunk_body = []
        builder.hunk_header = ""
        return

    post_lines = post_image_lines(builder.hunk_body)
    post_text = "\n".join(post_lines)
    post_loc = max(1, len(post_lines))
    complexity = len(BRANCH_RE.findall(post_text)) + len(BOOL_RE.findall(post_text))
    comment_fraction = sum(bool(COMMENT_RE.match(line)) for line in post_lines) / post_loc
    signature_added = any(callable_name(line) == name for line in added[:12])
    key = f"{builder.repo}::{canonical}::{name}"

    touch = builder.blocks.get(key)
    if touch is None:
        touch = BlockTouch(key=key, name=name, path=canonical)
        builder.blocks[key] = touch
    touch.changed_lines += len(changed)
    touch.post_loc = max(touch.post_loc, post_loc)
    touch.complexity_proxy = max(touch.complexity_proxy, complexity)
    touch.comment_fraction = max(touch.comment_fraction, comment_fraction)
    touch.signature_added = touch.signature_added or signature_added
    builder.attributed_changed_lines += len(changed)

    builder.hunk_body = []
    builder.hunk_header = ""


def finalize_file(builder: CommitBuilder) -> None:
    path = builder.current_new_path if builder.current_new_path != "/dev/null" else builder.current_old_path
    path = normalize_path(path)
    if path and is_code_path(path):
        builder.files.add(path)
        if is_test_path(path):
            builder.test_files.add(path)
        suffix = Path(path).suffix.lower()
        if suffix:
            builder.suffixes.add(suffix)
        builder.path_depths.append(path.count("/"))


def content_markers(lines: list[str]) -> dict[str, float | int]:
    nonblank = [line for line in lines if line.strip()]
    n = max(1, len(nonblank))
    joined = "\n".join(nonblank)
    return {
        "changed_nonblank_lines": len(nonblank),
        "changed_mean_line_chars": (sum(len(line) for line in nonblank) / n),
        "changed_long_line_fraction": (sum(len(line) > 120 for line in nonblank) / n),
        "changed_comment_fraction": (sum(bool(COMMENT_RE.match(line)) for line in nonblank) / n),
        "changed_branch_keyword_count": len(BRANCH_RE.findall(joined)),
        "changed_boolean_operator_count": len(BOOL_RE.findall(joined)),
        "changed_exception_keyword_count": len(EXCEPTION_RE.findall(joined)),
        "changed_assert_count": len(ASSERT_RE.findall(joined)),
        "changed_todo_fixme_count": len(TODO_RE.findall(joined)),
        "changed_import_dependency_lines": sum(bool(IMPORT_RE.match(line)) for line in nonblank),
        "changed_async_keyword_count": len(re.findall(r"\b(async|await)\b", joined)),
        "changed_numeric_literal_count": len(NUMBER_RE.findall(joined)),
    }


def build_event(
    builder: CommitBuilder,
    packaged_labels: dict[tuple[str, str], dict[str, str]],
    block_history: dict[str, list[tuple[datetime, str, int, str]]],
    author_first_seen: dict[str, datetime],
    author_commit_count: Counter[str],
    repo_first: datetime,
    prior_code_events: int,
) -> Event | None:
    if not builder.blocks:
        return None

    touched = set(builder.blocks)
    prior30, prior90, last_dates = [], [], []
    for key in touched:
        history = block_history.get(key, [])
        for item in history:
            age = (builder.date - item[0]).days
            if 0 <= age <= 90:
                prior90.append(item)
                if age <= 30:
                    prior30.append(item)
        if history:
            last_dates.append(history[-1][0])

    locs = [touch.post_loc for touch in builder.blocks.values()]
    complexities = [touch.complexity_proxy for touch in builder.blocks.values()]
    comments = [touch.comment_fraction for touch in builder.blocks.values()]
    densities = [touch.changed_lines / max(1, touch.post_loc) for touch in builder.blocks.values()]
    admitted_loc = sum(locs)
    author_first = author_first_seen.get(builder.author_email, builder.date)
    addition_fraction = builder.additions / max(1, builder.additions + builder.deletions)

    markers: dict[str, object] = {
        "repo": builder.repo,
        "sha": builder.sha,
        "date": builder.date.isoformat(),
        "author_email": builder.author_email,
        "subject": builder.subject,
        "parent_count": len(builder.parents),
        "is_merge": int(len(builder.parents) > 1),
        "weekday": builder.date.weekday(),
        "is_weekend": int(builder.date.weekday() >= 5),
        "author_hour_utc": builder.date.hour,
        "repo_age_days": max(0, (builder.date - repo_first).days),
        "author_prior_code_commits": author_commit_count[builder.author_email],
        "author_tenure_days": max(0, (builder.date - author_first).days),
        "author_prior_commit_share": author_commit_count[builder.author_email] / max(1, prior_code_events),
        "code_additions": builder.additions,
        "code_deletions": builder.deletions,
        "code_churn": builder.additions + builder.deletions,
        "code_addition_fraction": addition_fraction,
        "code_files_touched": len(builder.files),
        "test_files_touched": len(builder.test_files),
        "test_file_fraction": len(builder.test_files) / max(1, len(builder.files)),
        "file_extension_count": len(builder.suffixes),
        "mean_path_depth": sum(builder.path_depths) / max(1, len(builder.path_depths)),
        "blocks_touched": len(builder.blocks),
        "new_blocks": sum(t.signature_added for t in builder.blocks.values()),
        "new_block_fraction": sum(t.signature_added for t in builder.blocks.values()) / max(1, len(builder.blocks)),
        "admitted_block_loc": admitted_loc,
        "mean_block_loc": admitted_loc / max(1, len(locs)),
        "max_block_loc": max(locs),
        "mean_complexity_proxy": sum(complexities) / max(1, len(complexities)),
        "max_complexity_proxy": max(complexities),
        "mean_comment_fraction": sum(comments) / max(1, len(comments)),
        "mean_change_density": sum(densities) / max(1, len(densities)),
        "max_change_density": max(densities),
        "churn_per_block": (builder.additions + builder.deletions) / max(1, len(builder.blocks)),
        "blocks_per_file": len(builder.blocks) / max(1, len(builder.files)),
        "function_attribution_share": builder.attributed_changed_lines / max(1, builder.total_changed_lines),
        "prior_block_commits_30d": len({item[1] for item in prior30}),
        "prior_block_commits_90d": len({item[1] for item in prior90}),
        "prior_block_changed_lines_30d": sum(item[2] for item in prior30),
        "prior_block_changed_lines_90d": sum(item[2] for item in prior90),
        "prior_block_authors_90d": len({item[3] for item in prior90}),
        "days_since_any_block_touch": min((builder.date - d).days for d in last_dates) if last_dates else math.nan,
        "hot_block_fraction_30d": sum(any(0 <= (builder.date - item[0]).days <= 30 for item in block_history.get(key, [])) for key in touched) / max(1, len(touched)),
    }
    markers.update(message_markers(builder.subject))
    markers.update(content_markers(builder.changed_text))
    markers.update(packaged_labels.get((builder.repo, builder.sha), {}))
    return Event(builder.repo, builder.sha, builder.date, builder.author_email, dict(builder.blocks), markers)


def parse_commit_header(line: str, repo: str) -> CommitBuilder:
    payload = line[len(COMMIT_PREFIX):].rstrip("\n")
    parts = payload.split(COMMIT_SEP)
    if len(parts) != 6:
        raise ValueError(f"Malformed commit header with {len(parts)} fields")
    sha, parents, date, email, name, subject = parts
    return CommitBuilder(repo, sha, parents.split(), parse_dt(date), email.lower().strip(), name.strip(), subject.strip())


def stream_repo(slug: str, packaged_labels: dict[tuple[str, str], dict[str, str]]) -> tuple[list[Event], dict[str, object]]:
    repo_dir = clone_repo(slug)
    first_date = repo_first_date(repo_dir)
    aliases: dict[str, str] = {}
    block_history: dict[str, list[tuple[datetime, str, int, str]]] = defaultdict(list)
    author_first_seen: dict[str, datetime] = {}
    author_commit_count: Counter[str] = Counter()
    events: list[Event] = []
    current: CommitBuilder | None = None
    prior_code_events = 0
    commits_seen = 0
    total_changed = 0
    total_attributed = 0

    def finish_current() -> None:
        nonlocal current, prior_code_events, commits_seen, total_changed, total_attributed
        if current is None:
            return
        finalize_hunk(current, aliases)
        finalize_file(current)
        commits_seen += 1
        total_changed += current.total_changed_lines
        total_attributed += current.attributed_changed_lines
        event = build_event(current, packaged_labels, block_history, author_first_seen, author_commit_count, first_date, prior_code_events)
        if event is not None:
            events.append(event)
            for key, touch in event.blocks.items():
                block_history[key].append((event.date, event.sha, touch.changed_lines, event.author_email))
        author_first_seen.setdefault(current.author_email, current.date)
        author_commit_count[current.author_email] += 1
        prior_code_events += 1
        current = None

    fmt = f"{COMMIT_PREFIX}%H%x1f%P%x1f%aI%x1f%ae%x1f%an%x1f%s"
    cmd = [
        "git", "-C", str(repo_dir), "log", "--first-parent", "--reverse",
        f"--since={HISTORY_START.isoformat()}", f"--until={OBSERVATION_END.isoformat()}",
        f"--format={fmt}", "--patch", "--function-context", "--find-renames",
        "--no-color", "--no-ext-diff", "--no-textconv", "HEAD", "--", *SUPPORTED_GLOBS,
    ]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)
    assert proc.stdout is not None

    for line in proc.stdout:
        if line.startswith(COMMIT_PREFIX):
            finish_current()
            current = parse_commit_header(line, slug)
            continue
        if current is None:
            continue
        if line.startswith("diff --git "):
            finalize_hunk(current, aliases)
            finalize_file(current)
            match = re.match(r"diff --git a/(.*) b/(.*)$", line.rstrip("\n"))
            if match:
                current.current_old_path = match.group(1)
                current.current_new_path = match.group(2)
            continue
        if line.startswith("rename from "):
            current.current_old_path = line[len("rename from "):].rstrip("\n")
            continue
        if line.startswith("rename to "):
            current.current_new_path = line[len("rename to "):].rstrip("\n")
            continue
        if line.startswith("--- "):
            current.current_old_path = normalize_path(line[4:].rstrip("\n"))
            continue
        if line.startswith("+++ "):
            current.current_new_path = normalize_path(line[4:].rstrip("\n"))
            continue
        match = HUNK_RE.match(line.rstrip("\n"))
        if match:
            finalize_hunk(current, aliases)
            current.hunk_old_count = int(match.group("old_count") or 1)
            current.hunk_new_count = int(match.group("new_count") or 1)
            current.hunk_header = match.group("context").strip()
            continue
        if current.hunk_header or current.hunk_body:
            if line.startswith((" ", "+", "-", "\\")):
                current.hunk_body.append(line.rstrip("\n"))

    finish_current()
    stderr = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(f"streaming git log failed for {slug}: {stderr[-3000:]}")

    admissions = [e for e in events if ADMISSION_START <= e.date <= ADMISSION_END]
    summary = {
        "repo": slug,
        "code_commits_seen": commits_seen,
        "events_with_attributed_functions": len(events),
        "admission_events": len(admissions),
        "changed_lines": total_changed,
        "attributed_changed_lines": total_attributed,
        "function_attribution_share": total_attributed / max(1, total_changed),
    }
    print(json.dumps(summary), flush=True)
    return events, summary


def add_outcomes(events: list[Event]) -> list[dict[str, object]]:
    block_index: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        for key in event.blocks:
            block_index[key].append(event)

    rows: list[dict[str, object]] = []
    for event in events:
        if not (ADMISSION_START <= event.date <= ADMISSION_END):
            continue
        admitted_loc = sum(touch.post_loc for touch in event.blocks.values())
        if admitted_loc <= 0:
            continue
        downstream_commits: set[str] = set()
        downstream_volume = 0
        for key in event.blocks:
            for later in block_index[key]:
                if later.date <= event.date:
                    continue
                age = (later.date - event.date).total_seconds() / 86400.0
                if age > HORIZON_DAYS:
                    break
                downstream_commits.add(later.sha)
                downstream_volume += later.blocks[key].changed_lines
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
    repo_summaries = []
    for slug in TARGET_REPOS:
        print(f"Scanning {slug}", flush=True)
        events, summary = stream_repo(slug, packaged_labels)
        all_events.extend(events)
        repo_summaries.append(summary)

    rows = add_outcomes(all_events)
    if not rows:
        raise RuntimeError("Scanner produced no admission rows")
    write_csv(rows, OUTPUT / "commit_metrics.csv")
    summary = {
        "purpose": "Five-repository illustration of admission-time markers versus 180-day function/method rework.",
        "method": "One streaming first-parent git log -p --function-context pass per repository; block identity is canonical file path plus extracted function/method name.",
        "block_definition": "Named function/method inferred from Git function-context hunks. Same file+name is the same block; function renames become new identities.",
        "admission_window": [ADMISSION_START.isoformat(), ADMISSION_END.isoformat()],
        "horizon_days": HORIZON_DAYS,
        "repos": repo_summaries,
        "admission_rows": len(rows),
        "outcomes": ["outcome_rework_count_180d", "outcome_rework_volume_180d", "outcome_rework_intensity_180d"],
        "limitations": [
            "Illustrative five-repository convenience sample; not inferential validation.",
            "Only changes attributable to named function/method context are included in block outcomes.",
            "File renames are followed; function renames are new identities; overloads with the same file+name are intentionally collapsed for this smell test.",
            "Default-branch first-parent history only.",
            "Function-context extraction is heuristic across languages; per-repository attribution share is reported so coverage is visible.",
        ],
    }
    (OUTPUT / "scan_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
