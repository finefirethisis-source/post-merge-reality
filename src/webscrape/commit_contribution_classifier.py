from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_NAME = "commit_contribution_classifications.jsonl"
DEFAULT_FILE_OUTPUT_NAME = "commit_file_contribution_classifications.jsonl"

MAINTENANCE_CLASS_ORDER = (
    "corrective",
    "adaptive",
    "perfective",
    "preventive",
    "management",
    "unknown",
)
OPERATIONAL_INTENT_ORDER = (
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
)
SECONDARY_OBJECT_TAG_ORDER = (
    "source_code",
    "tests",
    "docs",
    "resources",
    "config",
    "dependencies",
)
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".ex",
    ".exs",
    ".fs",
    ".fsx",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".m",
    ".mm",
    ".php",
    ".py",
    ".r",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
DOC_EXTENSIONS = {".adoc", ".md", ".mdx", ".rst", ".txt"}
CONFIG_EXTENSIONS = {".conf", ".cfg", ".ini", ".toml", ".yaml", ".yml"}
RESOURCE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".csv",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsonl",
    ".mo",
    ".parquet",
    ".plist",
    ".png",
    ".po",
    ".pot",
    ".properties",
    ".resx",
    ".strings",
    ".svg",
    ".tsv",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
}
RESOURCE_PATH_PARTS = {
    "asset",
    "assets",
    "catalog",
    "catalogs",
    "data",
    "dataset",
    "datasets",
    "fixture",
    "fixtures",
    "i18n",
    "lang",
    "locale",
    "locales",
    "model_catalog",
    "model_catalogs",
    "public",
    "resource",
    "resources",
    "static",
    "translation",
    "translations",
}
DEPENDENCY_FILENAMES = {
    "build.gradle",
    "cargo.lock",
    "cargo.toml",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "mix.exs",
    "mix.lock",
    "package-lock.json",
    "package.json",
    "packages.lock.json",
    "pipfile",
    "pipfile.lock",
    "pnpm-lock.yaml",
    "pom.xml",
    "poetry.lock",
    "pubspec.lock",
    "pubspec.yaml",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "setup.py",
    "uv.lock",
    "yarn.lock",
}
CONFIG_FILENAMES = {
    ".babelrc",
    ".dockerignore",
    ".editorconfig",
    ".env.example",
    ".eslintrc",
    ".eslintrc.js",
    ".flake8",
    ".gitattributes",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".prettierrc",
    "dockerfile",
    "makefile",
    "mypy.ini",
    "pyproject.toml",
    "ruff.toml",
    "setup.cfg",
    "tox.ini",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "webpack.config.js",
}
CI_FILENAMES = {
    ".gitlab-ci.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    "buildkite.yml",
    "jenkinsfile",
}
DOC_FILENAMES = {
    "changelog",
    "changelog.md",
    "code_of_conduct.md",
    "contributing.md",
    "license",
    "license.md",
    "readme",
    "readme.md",
}

CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<body>.*)$"
)
SCOPED_CONVENTIONAL_PREFIX_WITHOUT_COLON_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)\((?P<scope>[^)]+)\)!?\s+(?P<body>.*)$"
)
GITMOJI_SHORTCODE_PREFIX_RE = re.compile(r"^:[a-z0-9_+\-]+:\s*", re.IGNORECASE)
DEFAULT_MERGE_SUBJECT_RE = re.compile(
    r"^\s*merge\s+(pull request\s+#\d+\b|branch\b|remote-tracking branch\b)",
    re.IGNORECASE,
)
LEADING_FIX_SUBJECT_RE = re.compile(r"^\s*fix(?:\b|\()", re.IGNORECASE)
SECURITY_RE = re.compile(
    r"\b("
    r"cve-\d+|security|vulnerab|xss|csrf|sql injection|auth bypass|"
    r"permission|privilege escalation|sanitize|secret leak|token leak|"
    r"hardening|harden"
    r")\b",
    re.IGNORECASE,
)
BUG_RE = re.compile(
    r"\b("
    r"fix(?:e[sd])?|bugfix|bug|hotfix|regression|crash|exception|error|"
    r"failure|broken|incorrect|resolve[sd]?|repair|patch"
    r")\b|(?:fixe?s|close[sd]?|resolve[sd]?)\s+#\d+",
    re.IGNORECASE,
)
FEATURE_RE = re.compile(
    r"\b("
    r"add(?:s|ed)?|implement(?:s|ed)?|support(?:s|ed)?|enable[sd]?|"
    r"introduce[sd]?|create[sd]?|new|feature|integrate[sd]?|allow[sd]?|"
    r"expose[sd]?"
    r")\b",
    re.IGNORECASE,
)
REFACTOR_RE = re.compile(
    r"\b(refactor|cleanup|clean up|simplif|rename|reorganiz|remove unused|dead code)\b",
    re.IGNORECASE,
)
PERFORMANCE_RE = re.compile(
    r"\b(perf|performance|optimi[sz]e|speed|latency|throughput|cache|faster|slow)\b",
    re.IGNORECASE,
)
TEST_RE = re.compile(
    r"\b(tests?|testing|coverage|spec|unit test|integration test)\b",
    re.IGNORECASE,
)
DOC_RE = re.compile(
    r"\b(docs?|documentation|readme|changelog|guide|tutorial)\b",
    re.IGNORECASE,
)
RESOURCE_RE = re.compile(
    r"\b("
    r"assets?|resources?|static|datasets?|data files?|catalogs?|fixtures?|"
    r"locali[sz]ation|locales?|translations|translation files?|i18n"
    r")\b",
    re.IGNORECASE,
)
DEPENDENCY_RE = re.compile(
    r"\b(deps?|dependencies|dependency|bump|upgrade|renovate|dependabot)\b|"
    r"\bupdate\b.*\bto\b.*\d",
    re.IGNORECASE,
)
BUILD_CI_RE = re.compile(
    r"\b(ci|workflow|pipeline|github actions|build|docker|makefile|bazel|cmake|configure)\b",
    re.IGNORECASE,
)
STYLE_RE = re.compile(
    r"\b(format|formatting|lint|linter|prettier|black|gofmt|rustfmt|whitespace|style)\b",
    re.IGNORECASE,
)
LINT_STYLE_CONTEXT_RE = re.compile(
    r"\b("
    r"lint(?:ing|-?rules?)?|linter|eslint|tslint|flake8|ruff|pylint|"
    r"prettier|black|gofmt|rustfmt|stylelint|violations?"
    r")\b",
    re.IGNORECASE,
)
RELEASE_RE = re.compile(
    r"\b(release|changelog|merge pull request|merge branch|tag)\b|"
    r"\b((?:bump|update|set|prepare)\s+(?:project\s+)?version|version(?:ing| bump))\b",
    re.IGNORECASE,
)

INTENT_TO_CLASSES = {
    "bug_fix": ("corrective",),
    "security_fix": ("corrective",),
    "feature": ("perfective",),
    "refactor_cleanup": ("preventive",),
    "performance": ("perfective",),
    "test": ("preventive",),
    "documentation": ("perfective",),
    "resource": ("perfective",),
    "build_config_ci": ("management",),
    "dependency_update": ("adaptive",),
    "style_formatting": ("preventive",),
    "merge_release_versioning": ("management",),
    "revert": ("corrective",),
}
CORE_SOURCE_INTENTS = {
    "bug_fix",
    "security_fix",
    "feature",
    "refactor_cleanup",
    "performance",
}
ACCOMPANYING_INTENTS = {"test", "documentation", "resource", "build_config_ci", "style_formatting"}
REFACTOR_SUPPORT_INTENTS = ACCOMPANYING_INTENTS | {"dependency_update"}
FILE_ROLE_TO_INTENT = {
    "dependency_update": "dependency_update",
    "test": "test",
    "documentation": "documentation",
    "resource_file": "resource",
    "build_config_ci": "build_config_ci",
}
CONVENTIONAL_TYPE_TO_INTENT = {
    "build": "build_config_ci",
    "ci": "build_config_ci",
    "docs": "documentation",
    "feat": "feature",
    "fix": "bug_fix",
    "perf": "performance",
    "ref": "refactor_cleanup",
    "refactor": "refactor_cleanup",
    "style": "style_formatting",
    "test": "test",
    "tests": "test",
    "revert": "revert",
}
CHORE_SCOPE_INTENT_PATTERNS = (
    ("dependency_update", re.compile(r"\b(deps?|dependenc(?:y|ies)|packages?|lockfiles?)\b", re.IGNORECASE)),
    ("build_config_ci", re.compile(r"\b(ci|workflow|workflows|actions?|pipeline|build|config|docker|tooling)\b", re.IGNORECASE)),
    ("style_formatting", re.compile(r"\b(lint|lint rules?|format|formatting|style|prettier|eslint|ruff|black)\b", re.IGNORECASE)),
    ("test", re.compile(r"\b(tests?|testing|coverage|specs?)\b", re.IGNORECASE)),
    ("documentation", re.compile(r"\b(docs?|documentation|readme|changelog)\b", re.IGNORECASE)),
    ("refactor_cleanup", re.compile(r"\b(cleanup|clean up|refactor|rename|remove|delete|drop|deprecat|prune)\b", re.IGNORECASE)),
    ("merge_release_versioning", re.compile(r"\b(release|version|tag)\b", re.IGNORECASE)),
)
CHORE_BODY_INTENT_PATTERNS = (
    ("refactor_cleanup", re.compile(r"^\s*(remove|delete|drop|clean\s*up|cleanup|rename|refactor|deprecat\w*|retire|prune)\b", re.IGNORECASE)),
    ("dependency_update", re.compile(r"^\s*(bump|upgrade)\b|^\s*update\s+(deps?|dependencies|packages?|lock(?:file)?s?)\b", re.IGNORECASE)),
    ("merge_release_versioning", re.compile(r"^\s*(release|tag|version)\b|^\s*prepare\s+release\b|^\s*(bump|update|set|prepare)\s+version\b", re.IGNORECASE)),
    ("build_config_ci", re.compile(r"^\s*(update|fix|add|remove|configure|enable|disable)\s+(ci|workflow|workflows|pipeline|build|docker|github actions?)\b", re.IGNORECASE)),
    ("style_formatting", re.compile(r"^\s*(format|lint|prettify)\b|^\s*(enable|disable|update|fix)\s+.*\b(lint|eslint|prettier|formatting|style)\b", re.IGNORECASE)),
    ("test", re.compile(r"^\s*(add|update|fix|remove)\s+(tests?|testing|coverage|specs?)\b", re.IGNORECASE)),
    ("documentation", re.compile(r"^\s*(add|update|fix|remove)\s+(docs?|documentation|readme|changelog)\b", re.IGNORECASE)),
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON in {path}:{line_number}: {exc}", file=sys.stderr)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def normalize_repo_filter(repo: str) -> str:
    return repo.replace("/", "__")


def discover_repo_dirs(input_dir: Path, repo_filters: set[str] | None = None) -> list[Path]:
    if not input_dir.exists():
        raise FileNotFoundError(f"input directory not found: {input_dir}")
    repo_dirs = [
        path
        for path in input_dir.iterdir()
        if path.is_dir() and (path / "all_commits.jsonl").exists()
    ]
    if repo_filters:
        repo_dirs = [path for path in repo_dirs if path.name in repo_filters]
    return sorted(repo_dirs, key=lambda path: path.name)


def safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def basename(path: str | None) -> str:
    return Path((path or "").strip()).name.lower()


def normalized_path(path: str | None) -> str:
    return (path or "").strip().replace("\\", "/").lower()


def path_parts(path: str | None) -> list[str]:
    return [part for part in normalized_path(path).split("/") if part]


def extension_for(path: str | None) -> str:
    return Path((path or "").strip()).suffix.lower()


def is_test_path(path: str | None) -> bool:
    parts = path_parts(path)
    name = basename(path)
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts)
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
        or name.endswith("_spec.rb")
        or ".test." in name
        or ".spec." in name
    )


def is_doc_path(path: str | None) -> bool:
    parts = path_parts(path)
    name = basename(path)
    return (
        "docs" in parts
        or "doc" in parts
        or name in DOC_FILENAMES
        or extension_for(path) in DOC_EXTENSIONS
    )


def is_ci_path(path: str | None) -> bool:
    path_value = normalized_path(path)
    name = basename(path)
    return (
        path_value.startswith(".github/workflows/")
        or path_value.startswith(".circleci/")
        or path_value.startswith(".buildkite/")
        or name in CI_FILENAMES
    )


def is_dependency_path(path: str | None) -> bool:
    name = basename(path)
    return name in DEPENDENCY_FILENAMES or name.startswith("requirements-") and name.endswith(".txt")


def is_config_path(path: str | None) -> bool:
    name = basename(path)
    return (
        name in CONFIG_FILENAMES
        or is_ci_path(path)
        or extension_for(path) in CONFIG_EXTENSIONS
        or name.endswith("config.js")
        or name.endswith("config.ts")
        or name.endswith(".config")
    )


def is_source_path(path: str | None) -> bool:
    return extension_for(path) in SOURCE_EXTENSIONS and not is_test_path(path)


def is_resource_path(path: str | None) -> bool:
    parts = path_parts(path)
    return (
        extension_for(path) in RESOURCE_EXTENSIONS
        or any(part in RESOURCE_PATH_PARTS for part in parts)
    )


def add_evidence(
    evidence: list[dict[str, Any]],
    source: str,
    rule: str,
    detail: str,
    weight: int | None = None,
) -> None:
    item: dict[str, Any] = {"source": source, "rule": rule, "detail": detail}
    if weight is not None:
        item["weight"] = weight
    if item not in evidence:
        evidence.append(item)


def add_score(
    scores: Counter[str],
    evidence: list[dict[str, Any]],
    intent: str,
    amount: int,
    source: str,
    rule: str,
    detail: str,
) -> None:
    scores[intent] += amount
    add_evidence(evidence, source, rule, detail, amount)


def add_inferred_intent_score(
    scores: Counter[str],
    evidence: list[dict[str, Any]],
    intent: str,
    amount: int,
    source: str,
    rule: str,
    detail: str,
) -> None:
    scores[intent] += amount
    item: dict[str, Any] = {
        "source": source,
        "rule": rule,
        "detail": detail,
        "intent": intent,
        "weight": amount,
    }
    if item not in evidence:
        evidence.append(item)


def has_conventional_type(evidence: list[dict[str, Any]], conventional_type: str) -> bool:
    return any(
        item.get("rule") == "conventional_commit_type"
        and item.get("detail") == conventional_type
        for item in evidence
    )


def conventional_intent(evidence: list[dict[str, Any]]) -> str | None:
    for item in evidence:
        if item.get("rule") in {"chore_scope_intent", "chore_leading_verb_intent"}:
            intent = str(item.get("intent") or "")
            if intent in OPERATIONAL_INTENT_ORDER:
                return intent
        if item.get("rule") != "conventional_commit_type":
            continue
        intent = CONVENTIONAL_TYPE_TO_INTENT.get(str(item.get("detail") or ""))
        if intent:
            return intent
    return None


def has_leading_fix_subject(evidence: list[dict[str, Any]]) -> bool:
    return any(
        item.get("source") == "message"
        and item.get("rule") == "bug_fix_keyword"
        and LEADING_FIX_SUBJECT_RE.search(
            conventional_subject(str(item.get("detail") or ""))
        )
        for item in evidence
    )


def has_refactor_message_signal(evidence: list[dict[str, Any]]) -> bool:
    return any(
        item.get("source") == "message" and item.get("rule") == "refactor_keyword"
        for item in evidence
    )


def is_broad_low_churn_change(stats: dict[str, int] | None) -> bool:
    if not stats:
        return False
    files_changed = safe_int(stats.get("files_changed"))
    changed_lines = safe_int(stats.get("changed_lines"))
    return files_changed >= 5 and changed_lines <= files_changed * 8


def commit_files(commit: dict[str, Any], changes_by_sha: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    files = commit.get("files")
    if isinstance(files, list) and files:
        return [file_change for file_change in files if isinstance(file_change, dict)]
    sha = commit.get("sha")
    if isinstance(sha, str):
        return changes_by_sha.get(sha, [])
    return []


def file_change_path(file_change: dict[str, Any]) -> str:
    return str(file_change.get("filename") or file_change.get("previous_filename") or "")


def analyze_file_tags(files: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> tuple[Counter[str], dict[str, int]]:
    tag_counts: Counter[str] = Counter()
    stats = {
        "files_changed": len(files),
        "additions": 0,
        "deletions": 0,
        "changed_lines": 0,
    }
    tag_example_counts: Counter[str] = Counter()

    for file_change in files:
        path = file_change_path(file_change)
        stats["additions"] += safe_int(file_change.get("additions"))
        stats["deletions"] += safe_int(file_change.get("deletions"))
        stats["changed_lines"] += safe_int(file_change.get("changes")) or (
            safe_int(file_change.get("additions")) + safe_int(file_change.get("deletions"))
        )

        tags: list[str] = []
        if is_source_path(path):
            tags.append("source_code")
        if is_test_path(path):
            tags.append("tests")
        if is_doc_path(path):
            tags.append("docs")
        if is_config_path(path):
            tags.append("config")
        if is_dependency_path(path):
            tags.append("dependencies")
        if not tags and is_resource_path(path):
            tags.append("resources")

        for tag in tags:
            tag_counts[tag] += 1
            if tag_example_counts[tag] < 3:
                add_evidence(evidence, "path", f"{tag}_path", path)
                tag_example_counts[tag] += 1

    return tag_counts, stats


def conventional_subject(message: str) -> str:
    subject = message.strip()
    while subject:
        stripped = GITMOJI_SHORTCODE_PREFIX_RE.sub("", subject, count=1).lstrip()
        if stripped != subject:
            subject = stripped
            continue
        if not re.match(r"[A-Za-z]", subject[0]):
            subject = subject[1:].lstrip()
            continue
        break
    return subject


def conventional_parts(message: str) -> tuple[str | None, str | None, str]:
    subject = conventional_subject(message)
    match = CONVENTIONAL_PREFIX_RE.match(subject)
    if not match:
        match = SCOPED_CONVENTIONAL_PREFIX_WITHOUT_COLON_RE.match(subject)
    if not match:
        return None, None, subject
    return (
        match.group("type").lower(),
        (match.group("scope") or "").strip().lower() or None,
        (match.group("body") or "").strip(),
    )


def conventional_type(message: str) -> str | None:
    return conventional_parts(message)[0]


def chore_scope_intent(scope: str | None) -> str | None:
    if not scope:
        return None
    normalized_scope = re.sub(r"[_\-/.]+", " ", scope.lower())
    for intent, pattern in CHORE_SCOPE_INTENT_PATTERNS:
        if pattern.search(normalized_scope):
            return intent
    return None


def chore_body_intent(body: str) -> str | None:
    for intent, pattern in CHORE_BODY_INTENT_PATTERNS:
        if pattern.search(body):
            return intent
    return None


def score_message_intents(message: str, commit: dict[str, Any], scores: Counter[str], evidence: list[dict[str, Any]]) -> None:
    subject = message.strip()
    if not subject:
        return
    if DEFAULT_MERGE_SUBJECT_RE.search(subject):
        add_score(
            scores,
            evidence,
            "merge_release_versioning",
            5,
            "message",
            "default_merge_subject",
            subject,
        )
        return

    prefix, scope, body = conventional_parts(subject)
    if prefix in CONVENTIONAL_TYPE_TO_INTENT:
        add_score(
            scores,
            evidence,
            CONVENTIONAL_TYPE_TO_INTENT[prefix],
            4,
            "message",
            "conventional_commit_type",
            prefix,
        )
    elif prefix == "chore":
        add_evidence(evidence, "message", "conventional_commit_type", prefix)
        scope_intent = chore_scope_intent(scope)
        body_intent = chore_body_intent(body)
        if scope_intent:
            add_inferred_intent_score(
                scores,
                evidence,
                scope_intent,
                4,
                "message",
                "chore_scope_intent",
                f"{scope} -> {scope_intent}",
            )
        elif body_intent:
            add_inferred_intent_score(
                scores,
                evidence,
                body_intent,
                4,
                "message",
                "chore_leading_verb_intent",
                f"{body} -> {body_intent}",
            )

    lint_style_context = bool(LINT_STYLE_CONTEXT_RE.search(subject))
    if subject.lower().startswith("revert") or "this reverts commit" in subject.lower():
        add_score(scores, evidence, "revert", 5, "message", "revert_keyword", subject)
    if SECURITY_RE.search(subject):
        add_score(scores, evidence, "security_fix", 4, "message", "security_keyword", subject)
    if BUG_RE.search(subject) and not lint_style_context:
        add_score(scores, evidence, "bug_fix", 3, "message", "bug_fix_keyword", subject)
    if DEPENDENCY_RE.search(subject):
        add_score(scores, evidence, "dependency_update", 3, "message", "dependency_keyword", subject)
    if PERFORMANCE_RE.search(subject):
        add_score(scores, evidence, "performance", 3, "message", "performance_keyword", subject)
    if FEATURE_RE.search(subject) and not lint_style_context:
        add_score(scores, evidence, "feature", 3, "message", "feature_keyword", subject)
    if REFACTOR_RE.search(subject):
        add_score(scores, evidence, "refactor_cleanup", 3, "message", "refactor_keyword", subject)
    if TEST_RE.search(subject):
        add_score(scores, evidence, "test", 2, "message", "test_keyword", subject)
    if DOC_RE.search(subject):
        add_score(scores, evidence, "documentation", 2, "message", "documentation_keyword", subject)
    if RESOURCE_RE.search(subject):
        add_score(scores, evidence, "resource", 2, "message", "resource_keyword", subject)
    if BUILD_CI_RE.search(subject):
        add_score(scores, evidence, "build_config_ci", 2, "message", "build_ci_keyword", subject)
    if STYLE_RE.search(subject):
        add_score(scores, evidence, "style_formatting", 3, "message", "style_keyword", subject)
    if RELEASE_RE.search(subject) or subject.lower().startswith("merge "):
        add_score(scores, evidence, "merge_release_versioning", 3, "message", "release_merge_keyword", subject)

    labels = commit.get("labels") or []
    author_bits = " ".join(
        str(value or "")
        for value in (
            commit.get("author_login"),
            commit.get("committer_login"),
            commit.get("git_author_name"),
            commit.get("git_author_email"),
        )
    )
    metadata_text = " ".join([author_bits, " ".join(str(label) for label in labels)])
    if re.search(r"\b(dependabot|renovate)\b", metadata_text, re.IGNORECASE):
        add_score(
            scores,
            evidence,
            "dependency_update",
            3,
            "metadata",
            "dependency_bot",
            metadata_text.strip(),
        )


def score_path_intents(
    files: list[dict[str, Any]],
    tag_counts: Counter[str],
    scores: Counter[str],
    evidence: list[dict[str, Any]],
) -> None:
    file_count = len(files)
    if file_count == 0:
        return

    only_docs = tag_counts["docs"] == file_count
    only_tests = tag_counts["tests"] == file_count
    only_dependencies = tag_counts["dependencies"] == file_count
    only_config = tag_counts["config"] == file_count
    only_resources = tag_counts["resources"] == file_count

    if only_dependencies:
        add_score(scores, evidence, "dependency_update", 4, "path", "only_dependency_files", "all changed files are dependency manifests or locks")
    elif tag_counts["dependencies"]:
        add_score(scores, evidence, "dependency_update", 2, "path", "dependency_files", f"{tag_counts['dependencies']} dependency file(s)")

    if only_tests:
        add_score(scores, evidence, "test", 4, "path", "only_test_files", "all changed files are tests")
    elif tag_counts["tests"]:
        add_score(scores, evidence, "test", 2, "path", "test_files", f"{tag_counts['tests']} test file(s)")

    if only_docs:
        add_score(scores, evidence, "documentation", 4, "path", "only_doc_files", "all changed files are documentation")
    elif tag_counts["docs"]:
        add_score(scores, evidence, "documentation", 2, "path", "doc_files", f"{tag_counts['docs']} documentation file(s)")

    if only_resources:
        add_score(scores, evidence, "resource", 4, "path", "only_resource_files", "all changed files are resources or data files")
    elif tag_counts["resources"]:
        add_score(scores, evidence, "resource", 2, "path", "resource_files", f"{tag_counts['resources']} resource file(s)")

    if only_config:
        add_score(scores, evidence, "build_config_ci", 3, "path", "only_config_files", "all changed files are config")
    elif tag_counts["config"]:
        add_score(scores, evidence, "build_config_ci", 2, "path", "config_files", "config files changed")


def choose_operational_intents(
    scores: Counter[str],
    evidence: list[dict[str, Any]],
    tag_counts: Counter[str],
    stats: dict[str, int] | None = None,
) -> tuple[str, list[str]]:
    intents = [
        intent
        for intent in OPERATIONAL_INTENT_ORDER
        if intent not in {"mixed", "unknown"} and scores[intent] >= 2
    ]
    if not intents:
        return "unknown", ["unknown"]

    ranked = sorted(
        intents,
        key=lambda intent: (-scores[intent], OPERATIONAL_INTENT_ORDER.index(intent)),
    )
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None

    primary = top
    if second is not None and scores[top] == scores[second]:
        top_is_core = top in CORE_SOURCE_INTENTS
        second_is_accompanying = second in ACCOMPANYING_INTENTS
        support_intent_competition = top in ACCOMPANYING_INTENTS and second in ACCOMPANYING_INTENTS
        chore_tie_breaker = has_conventional_type(evidence, "chore") and support_intent_competition
        conventional_top_tie_breaker = conventional_intent(evidence) == top
        source_present = tag_counts["source_code"] > 0
        leading_fix_feature_tie_breaker = (
            {top, second} == {"bug_fix", "feature"}
            and scores["bug_fix"] == scores["feature"]
            and has_leading_fix_subject(evidence)
        )
        broad_refactor_rename_tie_breaker = (
            top == "refactor_cleanup"
            and second in REFACTOR_SUPPORT_INTENTS
            and source_present
            and has_refactor_message_signal(evidence)
            and is_broad_low_churn_change(stats)
        )
        top_is_decisive = top in {"security_fix", "revert", "dependency_update", "merge_release_versioning"}
        if conventional_top_tie_breaker:
            add_evidence(
                evidence,
                "classifier",
                "conventional_commit_tie_breaker",
                f"conventional commit prefix selects top intent: {top}={scores[top]}, {second}={scores[second]}",
            )
        elif chore_tie_breaker:
            add_evidence(
                evidence,
                "classifier",
                "chore_support_intent_tie_breaker",
                f"chore prefix selects highest scoring support intent: {top}={scores[top]}, {second}={scores[second]}",
            )
        elif leading_fix_feature_tie_breaker:
            primary = "bug_fix"
            add_evidence(
                evidence,
                "classifier",
                "leading_fix_feature_tie_breaker",
                f"leading Fix subject selects bug fix over feature: bug_fix={scores['bug_fix']}, feature={scores['feature']}",
            )
        elif broad_refactor_rename_tie_breaker:
            add_evidence(
                evidence,
                "classifier",
                "broad_low_churn_refactor_tie_breaker",
                f"broad low-churn refactor keeps refactor primary: refactor_cleanup={scores['refactor_cleanup']}, {second}={scores[second]}",
            )
        elif not (top_is_decisive or (top_is_core and second_is_accompanying and source_present)):
            primary = "mixed"
            add_evidence(
                evidence,
                "classifier",
                "mixed_intent",
                f"competing intents: {top}={scores[top]}, {second}={scores[second]}",
            )

    if primary == "mixed":
        return primary, sorted(set(intents + ["mixed"]), key=OPERATIONAL_INTENT_ORDER.index)
    return primary, sorted(set(intents), key=OPERATIONAL_INTENT_ORDER.index)


def choose_maintenance_classes(primary_intent: str, operational_intents: list[str], scores: Counter[str]) -> tuple[str, list[str]]:
    class_scores: Counter[str] = Counter()
    for intent in operational_intents:
        if intent in {"mixed", "unknown"}:
            continue
        for maintenance_class in INTENT_TO_CLASSES.get(intent, ()):
            class_scores[maintenance_class] += max(scores[intent], 1)

    if not class_scores:
        return "unknown", ["unknown"]

    maintenance_classes = sorted(class_scores, key=lambda item: (-class_scores[item], MAINTENANCE_CLASS_ORDER.index(item)))
    primary_classes = INTENT_TO_CLASSES.get(primary_intent, ())
    if primary_classes:
        maintenance_class = primary_classes[0]
    else:
        maintenance_class = maintenance_classes[0]
    return maintenance_class, maintenance_classes


def confidence_for(
    primary_intent: str,
    scores: Counter[str],
    evidence: list[dict[str, Any]],
    tag_counts: Counter[str],
) -> str:
    if primary_intent == "unknown":
        return "low"
    if primary_intent == "mixed":
        return "medium"

    score = scores[primary_intent]
    evidence_sources = {item.get("source") for item in evidence if item.get("source") != "classifier"}
    has_conventional = any(item.get("rule") == "conventional_commit_type" for item in evidence)
    if score >= 5 and len(evidence_sources) >= 2:
        return "high"
    if has_conventional and score >= 4:
        return "high"
    if score >= 4 and "path" in evidence_sources:
        return "high"
    if primary_intent in CORE_SOURCE_INTENTS and score >= 3 and tag_counts["source_code"]:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def ordered_tags(tag_counts: Counter[str]) -> list[str]:
    return [
        tag
        for tag in SECONDARY_OBJECT_TAG_ORDER
        if tag_counts[tag]
    ]


def primary_file_role(tag_counts: Counter[str]) -> str:
    if tag_counts["dependencies"]:
        return "dependency_update"
    if tag_counts["tests"]:
        return "test"
    if tag_counts["docs"]:
        return "documentation"
    if tag_counts["config"]:
        return "build_config_ci"
    if tag_counts["source_code"]:
        return "source_code"
    if tag_counts["resources"]:
        return "resource_file"
    return "unknown"


def add_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def is_real_commit_intent(intent: Any) -> bool:
    return isinstance(intent, str) and intent not in {"", "unknown", "mixed"}


def file_context_intents(
    file_role: str,
    file_tags: list[str],
    commit_classification: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[str]:
    intents: list[str] = []
    role_intent = FILE_ROLE_TO_INTENT.get(file_role)
    if role_intent:
        add_unique(intents, role_intent)

    commit_primary = commit_classification.get("primary_operational_intent")
    commit_intents = [
        intent
        for intent in commit_classification.get("operational_intents", [])
        if is_real_commit_intent(intent)
    ]
    source_like = "source_code" in file_tags

    if source_like:
        if is_real_commit_intent(commit_primary):
            add_unique(intents, str(commit_primary))
            add_evidence(
                evidence,
                "commit_context",
                "source_file_commit_intent",
                str(commit_primary),
            )
        elif commit_primary == "mixed":
            for intent in commit_intents:
                if intent in CORE_SOURCE_INTENTS:
                    add_unique(intents, intent)
            if any(intent in CORE_SOURCE_INTENTS for intent in commit_intents):
                add_evidence(
                    evidence,
                    "commit_context",
                    "source_file_mixed_commit_intents",
                    ", ".join(intent for intent in commit_intents if intent in CORE_SOURCE_INTENTS),
                )
    elif is_real_commit_intent(commit_primary) and role_intent != commit_primary:
        supporting_intent = f"supporting_{commit_primary}"
        add_unique(intents, supporting_intent)
        add_evidence(
            evidence,
            "commit_context",
            "supporting_commit_intent",
            supporting_intent,
        )

    if not intents:
        add_unique(intents, "unknown")
    return intents


def file_confidence_for(
    file_role: str,
    file_context: list[str],
    commit_classification: dict[str, Any],
) -> str:
    if file_role == "unknown":
        return "low"
    if file_role in {"test", "documentation", "resource_file", "build_config_ci", "dependency_update"}:
        return "high"
    if file_role == "source_code":
        commit_confidence = str(commit_classification.get("confidence") or "low")
        if any(intent in CORE_SOURCE_INTENTS or intent == "security_fix" for intent in file_context):
            return commit_confidence if commit_confidence in {"high", "medium"} else "medium"
        return "medium"
    return "low"


def classify_commit(
    commit: dict[str, Any],
    files: list[dict[str, Any]] | None = None,
    repo_key: str | None = None,
    is_origin_commit: bool | None = None,
    is_observation_commit: bool | None = None,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    scores: Counter[str] = Counter()
    file_changes = files if files is not None else commit.get("files") or []
    tag_counts, stats = analyze_file_tags(file_changes, evidence)
    score_path_intents(file_changes, tag_counts, scores, evidence)
    score_message_intents(str(commit.get("message_subject") or ""), commit, scores, evidence)

    primary_intent, operational_intents = choose_operational_intents(
        scores,
        evidence,
        tag_counts,
        stats,
    )
    maintenance_class, maintenance_classes = choose_maintenance_classes(primary_intent, operational_intents, scores)
    confidence = confidence_for(primary_intent, scores, evidence, tag_counts)
    repo = commit.get("repo")
    if not repo and repo_key:
        repo = repo_key.replace("__", "/")

    return {
        "repo": repo,
        "repo_key": repo_key,
        "sha": commit.get("sha"),
        "date": commit.get("date"),
        "message_subject": commit.get("message_subject"),
        "is_origin_commit": bool(is_origin_commit),
        "is_observation_commit": bool(is_observation_commit),
        "maintenance_class": maintenance_class,
        "maintenance_classes": maintenance_classes,
        "primary_operational_intent": primary_intent,
        "operational_intents": operational_intents,
        "secondary_object_tags": ordered_tags(tag_counts),
        "confidence": confidence,
        "evidence": evidence,
        "stats": stats,
    }


def classify_file_change(
    commit: dict[str, Any],
    file_change: dict[str, Any],
    commit_classification: dict[str, Any],
    repo_key: str | None = None,
    is_origin_commit: bool | None = None,
    is_observation_commit: bool | None = None,
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    tag_counts, stats = analyze_file_tags([file_change], evidence)
    file_tags = ordered_tags(tag_counts)
    file_role = primary_file_role(tag_counts)
    context_intents = file_context_intents(file_role, file_tags, commit_classification, evidence)
    confidence = file_confidence_for(file_role, context_intents, commit_classification)
    repo = commit.get("repo") or commit_classification.get("repo")
    if not repo and repo_key:
        repo = repo_key.replace("__", "/")

    return {
        "repo": repo,
        "repo_key": repo_key,
        "commit_sha": commit.get("sha"),
        "date": commit.get("date"),
        "message_subject": commit.get("message_subject"),
        "is_origin_commit": bool(is_origin_commit),
        "is_observation_commit": bool(is_observation_commit),
        "filename": file_change.get("filename"),
        "previous_filename": file_change.get("previous_filename"),
        "status": file_change.get("status"),
        "additions": stats["additions"],
        "deletions": stats["deletions"],
        "changed_lines": stats["changed_lines"],
        "file_primary_role": file_role,
        "file_context_intents": context_intents,
        "file_object_tags": file_tags,
        "file_confidence": confidence,
        "commit_maintenance_class": commit_classification.get("maintenance_class"),
        "commit_maintenance_classes": commit_classification.get("maintenance_classes"),
        "commit_primary_operational_intent": commit_classification.get("primary_operational_intent"),
        "commit_operational_intents": commit_classification.get("operational_intents"),
        "commit_confidence": commit_classification.get("confidence"),
        "evidence": evidence,
    }


def changes_by_commit(repo_dir: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file_change in read_jsonl(repo_dir / "commit_file_changes.jsonl"):
        sha = file_change.get("commit_sha")
        if isinstance(sha, str):
            grouped[sha].append(file_change)
    return grouped


def select_commit_rows(repo_dir: Path, commit_source: str) -> tuple[list[dict[str, Any]], str]:
    if commit_source == "all":
        return read_jsonl(repo_dir / "all_commits.jsonl"), "all_commits.jsonl"
    if commit_source == "observation":
        return read_jsonl(repo_dir / "observation_commits.jsonl"), "observation_commits.jsonl"

    observation_path = repo_dir / "observation_commits.jsonl"
    if observation_path.exists():
        return read_jsonl(observation_path), "observation_commits.jsonl"
    return read_jsonl(repo_dir / "all_commits.jsonl"), "all_commits.jsonl"


def classify_repo_dir(
    repo_dir: Path,
    output_name: str = DEFAULT_OUTPUT_NAME,
    file_output_name: str = DEFAULT_FILE_OUTPUT_NAME,
    commit_source: str = "auto",
) -> tuple[Path, int]:
    origin_commits = read_jsonl(repo_dir / "all_commits.jsonl")
    origin_shas = {
        commit.get("sha")
        for commit in origin_commits
        if isinstance(commit.get("sha"), str)
    }
    observation_shas = {
        commit.get("sha")
        for commit in read_jsonl(repo_dir / "observation_commits.jsonl")
        if isinstance(commit.get("sha"), str)
    }
    commits, source_name = select_commit_rows(repo_dir, commit_source)
    if not observation_shas:
        observation_shas = {
            commit.get("sha")
            for commit in commits
            if isinstance(commit.get("sha"), str)
        }

    grouped_changes = changes_by_commit(repo_dir)
    rows: list[dict[str, Any]] = []
    file_rows: list[dict[str, Any]] = []
    for commit in commits:
        file_changes = commit_files(commit, grouped_changes)
        is_origin = commit.get("sha") in origin_shas
        is_observation = commit.get("sha") in observation_shas
        row = classify_commit(
            commit,
            files=file_changes,
            repo_key=repo_dir.name,
            is_origin_commit=is_origin,
            is_observation_commit=is_observation,
        )
        row["input_commit_file"] = source_name
        rows.append(row)
        for file_change in file_changes:
            file_row = classify_file_change(
                commit,
                file_change,
                row,
                repo_key=repo_dir.name,
                is_origin_commit=is_origin,
                is_observation_commit=is_observation,
            )
            file_row["input_commit_file"] = source_name
            file_rows.append(file_row)

    output_path = repo_dir / output_name
    file_output_path = repo_dir / file_output_name
    write_jsonl(output_path, rows)
    write_jsonl(file_output_path, file_rows)
    return output_path, len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify contribution types for existing github_commit_scraper outputs."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"Directory containing per-repo scraper outputs. Default: {DEFAULT_INPUT_DIR}",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit classification to a repo, either owner/repo or owner__repo. Can be repeated.",
    )
    parser.add_argument(
        "--output-name",
        default=DEFAULT_OUTPUT_NAME,
        help=f"Output JSONL filename written inside each repo directory. Default: {DEFAULT_OUTPUT_NAME}",
    )
    parser.add_argument(
        "--file-output-name",
        default=DEFAULT_FILE_OUTPUT_NAME,
        help=(
            "Per-file output JSONL filename written inside each repo directory. "
            f"Default: {DEFAULT_FILE_OUTPUT_NAME}"
        ),
    )
    parser.add_argument(
        "--commit-source",
        choices=("auto", "all", "observation"),
        default="auto",
        help=(
            "Commit file to classify. auto uses observation_commits.jsonl when present, "
            "otherwise all_commits.jsonl. Default: auto."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_filters = {normalize_repo_filter(repo) for repo in args.repo} if args.repo else None
    repo_dirs = discover_repo_dirs(args.input_dir, repo_filters)
    if not repo_dirs:
        requested = f" matching {sorted(repo_filters)}" if repo_filters else ""
        raise SystemExit(f"No scraper result directories found in {args.input_dir}{requested}.")

    for repo_dir in repo_dirs:
        output_path, row_count = classify_repo_dir(
            repo_dir,
            output_name=args.output_name,
            file_output_name=args.file_output_name,
            commit_source=args.commit_source,
        )
        print(f"wrote {row_count} classifications to {output_path}")
        file_output_path = repo_dir / args.file_output_name
        file_count = len(read_jsonl(file_output_path))
        print(f"wrote {file_count} file classifications to {file_output_path}")


if __name__ == "__main__":
    main()
