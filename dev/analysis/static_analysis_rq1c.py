from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter, defaultdict, deque
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, StrMethodFormatter

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:  # Allows `python -m dev.analysis.static_analysis_rq1c`.
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs


DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_static_rq1c")
DEFAULT_CWE_1400_LOOKUP = Path("dev/analysis/cwe_1400_category_lookup_flat.json")
FONT_DIR = Path("font")
TOP_STATIC_CWE_LIMIT = 25
DEFAULT_MAX_COMMIT_CHURN = 500_000
STATIC_FINDING_RATE_SCALE = 100
COMPACT_COUNTS_CSV_NAME = "static_analysis_rq1c_compact_counts.csv"
COMPACT_SUMMARY_CSV_NAME = "static_analysis_rq1c_compact_summary.csv"
COMPACT_DURATIONS_CSV_NAME = "static_analysis_rq1c_lifecycle_durations.csv"

COMMIT_FILE = "all_commits.jsonl"
COMMIT_CLASSIFICATIONS_FILE = "commit_contribution_classifications.jsonl"
COMMIT_FILE_CLASSIFICATIONS_FILE = "commit_file_contribution_classifications.jsonl"
LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
STATIC_FINDINGS_FILE = "commit_static_findings.jsonl"
STATIC_SUMMARY_FILE = "commit_static_analysis_summary.jsonl"
SUPPLY_CHAIN_FINDINGS_FILE = "commit_supply_chain_findings.jsonl"
SUPPLY_CHAIN_SUMMARY_FILE = "commit_supply_chain_summary.jsonl"
SONARQUBE_SCANS_FILE = "sonarqube_snapshot_scans.jsonl"

STATIC_ANALYSIS_RQ1C_INPUT_FILES = (
    COMMIT_FILE,
    STATIC_FINDINGS_FILE,
    SUPPLY_CHAIN_FINDINGS_FILE,
)

ROLE_ORDER = ("AI", "Human")
ROLE_LABELS = {
    "AI": "Agentic",
    "Human": "Human",
}
FINDING_STATUS_ORDER = ("introduced", "removed")
FINDING_STATUS_LABELS = {
    "introduced": "Introduced",
    "removed": "Removed",
}
STATIC_FINDING_LIFECYCLE_OUTCOMES = (
    "removed_by_AI",
    "removed_by_Human",
    "removed_by_other_or_unknown",
    "not_observed_removed",
)
STATIC_FINDING_LIFECYCLE_LABELS = {
    "removed_by_AI": "Removed by Agentic",
    "removed_by_Human": "Removed by Human",
    "removed_by_other_or_unknown": "Removed by other/unknown",
    "not_observed_removed": "Not observed removed",
}
CWE_CODE_RE = re.compile(r"\bCWE-\d+\b", re.IGNORECASE)
SEMGREP_SEVERITY_ORDER = ("High", "Medium", "Low", "Unknown")
SEMGREP_SEVERITY_LEGEND_ORDER = ("Low", "Medium", "High")
SEMGREP_SEVERITY_GRAYS = {
    "High": "#525252",
    "Medium": "#969696",
    "Low": "#d9d9d9",
    "Unknown": "#f0f0f0",
}
STATUS_SEMGREP_SEVERITY_COLORS = {
    "introduced": {
        "High": "#55579A",
        "Medium": "#8789C0",
        "Low": "#C4C5E3",
        "Unknown": "#E4E5F2",
    },
    "removed": {
        "High": "#B85E0E",
        "Medium": "#F58F29",
        "Low": "#FBCB8C",
        "Unknown": "#FEE5C7",
    },
}
ROLE_SEMGREP_SEVERITY_COLORS = {
    "AI": STATUS_SEMGREP_SEVERITY_COLORS["removed"],
    "Human": STATUS_SEMGREP_SEVERITY_COLORS["introduced"],
}
OSV_SEVERITY_ORDER = ("Critical", "High", "Medium", "Low", "Unknown")
OSV_SEVERITY_LEGEND_ORDER = ("Low", "Medium", "High", "Critical")
OSV_SEVERITY_GRAYS = {
    "Critical": "#252525",
    "High": "#525252",
    "Medium": "#969696",
    "Low": "#d9d9d9",
    "Unknown": "#f0f0f0",
}
STATUS_OSV_SEVERITY_COLORS = {
    "introduced": {
        "Critical": "#373872",
        "High": "#55579A",
        "Medium": "#8789C0",
        "Low": "#C4C5E3",
        "Unknown": "#E4E5F2",
    },
    "removed": {
        "Critical": "#864005",
        "High": "#B85E0E",
        "Medium": "#F58F29",
        "Low": "#FBCB8C",
        "Unknown": "#FEE5C7",
    },
}
ROLE_OSV_SEVERITY_COLORS = {
    "AI": STATUS_OSV_SEVERITY_COLORS["removed"],
    "Human": STATUS_OSV_SEVERITY_COLORS["introduced"],
}
CWE_TOP_25_BUCKET = "CWE Top 25"
CWE_OTHER_BUCKET = "Other/No CWE"
CWE_BUCKET_ORDER = (CWE_TOP_25_BUCKET, CWE_OTHER_BUCKET)
CWE_BUCKET_LEGEND_ORDER = (CWE_OTHER_BUCKET, CWE_TOP_25_BUCKET)
CWE_BUCKET_GRAYS = {
    CWE_TOP_25_BUCKET: "#525252",
    CWE_OTHER_BUCKET: "#d9d9d9",
}
STATUS_CWE_BUCKET_COLORS = {
    "introduced": {
        CWE_TOP_25_BUCKET: "#6668AA",
        CWE_OTHER_BUCKET: "#C4C5E3",
    },
    "removed": {
        CWE_TOP_25_BUCKET: "#C96912",
        CWE_OTHER_BUCKET: "#FBCB8C",
    },
}
CWE_1400_NO_CWE = "No CWE"
CWE_1400_UNMAPPED = "Unmapped CWE"
CWE_1400_MULTIPLE = "Multiple CWE-1400 Categories"
CWE_1400_SPECIAL_ORDER = (
    CWE_1400_MULTIPLE,
    CWE_1400_UNMAPPED,
    CWE_1400_NO_CWE,
)
TOP_25_CWES = """
1	CWE-79	Improper Neutralization of Input During Web Page Generation ('Cross-site Scripting')	60.38	7	0
2	CWE-89	Improper Neutralization of Special Elements used in an SQL Command ('SQL Injection')	28.72	4	+1
3	CWE-352	Cross-Site Request Forgery (CSRF)	13.64	0	+1
4	CWE-862	Missing Authorization	13.28	0	+5
5	CWE-787	Out-of-bounds Write	12.68	12	-3
6	CWE-22	Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')	8.99	10	-1
7	CWE-416	Use After Free	8.47	14	+1
8	CWE-125	Out-of-bounds Read	7.88	3	-2
9	CWE-78	Improper Neutralization of Special Elements used in an OS Command ('OS Command Injection')	7.85	20	-2
10	CWE-94	Improper Control of Generation of Code ('Code Injection')	7.57	7	+1
11	CWE-120	Buffer Copy without Checking Size of Input ('Classic Buffer Overflow')	6.96	0	N/A
12	CWE-434	Unrestricted Upload of File with Dangerous Type	6.87	4	-2
13	CWE-476	NULL Pointer Dereference	6.41	0	+8
14	CWE-121	Stack-based Buffer Overflow	5.75	4	N/A
15	CWE-502	Deserialization of Untrusted Data	5.23	11	+1
16	CWE-122	Heap-based Buffer Overflow	5.21	6	N/A
17	CWE-863	Incorrect Authorization	4.14	4	+1
18	CWE-20	Improper Input Validation	4.09	2	-6
19	CWE-284	Improper Access Control	4.07	1	N/A
20	CWE-200	Exposure of Sensitive Information to an Unauthorized Actor	4.01	1	-3
21	CWE-306	Missing Authentication for Critical Function	3.47	11	+4
22	CWE-918	Server-Side Request Forgery (SSRF)	3.36	0	-3
23	CWE-77	Improper Neutralization of Special Elements used in a Command ('Command Injection')	3.15	2	-10
24	CWE-639	Authorization Bypass Through User-Controlled Key	2.62	0	+6
25	CWE-770	Allocation of Resources Without Limits or Throttling	2.54	0	+1
"""


LOG = logging.getLogger(__name__)


def parse_cwe_ids(value: Any) -> set[str]:
    return {match.upper() for match in CWE_CODE_RE.findall(str(value or ""))}


TOP_25_CWE_IDS = frozenset(parse_cwe_ids(TOP_25_CWES))


def setup_plot_style() -> None:
    regular_font = FONT_DIR / "NimbusRomNo9L-Reg.otf"
    nimbus_fonts = sorted(FONT_DIR.glob("NimbusRomNo9L-*.otf"))
    if regular_font.exists():
        for font_path in nimbus_fonts:
            font_manager.fontManager.addfont(str(font_path))
        font_name = font_manager.FontProperties(fname=str(regular_font)).get_name()
        plt.rcParams["font.family"] = font_name
    plt.rcParams.update(
        {
            "font.size": 13,
            "axes.titlesize": 14,
            "axes.labelsize": 13,
            "legend.fontsize": 11,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
        }
    )


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


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


def count_jsonl_rows(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_cwe_1400_lookup(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(
            f"CWE-1400 lookup not found: {path}. "
            "Generate it with dev/analysis/build_cwe_1400_lookup.py."
        )
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"CWE-1400 lookup must be a JSON object: {path}")

    if "lookup" in data and isinstance(data["lookup"], dict):
        out: dict[str, str] = {}
        for cwe_id, value in data["lookup"].items():
            if isinstance(value, dict):
                category = value.get("category")
            else:
                category = value
            if category:
                out[str(cwe_id).upper()] = str(category)
        return out

    return {str(cwe_id).upper(): str(category) for cwe_id, category in data.items()}


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


def safe_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def file_churn(row: dict[str, Any]) -> int:
    value = row.get("changes")
    if value in (None, ""):
        value = row.get("changed_lines")
    if value not in (None, ""):
        return safe_int(value)
    return safe_int(row.get("additions")) + safe_int(row.get("deletions"))


def commit_total_churn(row: dict[str, Any]) -> int:
    files = row.get("files")
    if isinstance(files, list):
        return sum(file_churn(file_row) for file_row in files if isinstance(file_row, dict))
    return file_churn(row)


def repo_from_dir(repo_dir: Path) -> str:
    return repo_dir.name.replace("__", "/")


def normalize_commit_role(category: Any) -> str | None:
    value = str(category or "").strip().lower()
    if value == "ai_coauthored":
        return "AI"
    if value == "not_ai_coauthored":
        return "Human"
    return None


def is_source_file_classification(row: dict[str, Any]) -> bool:
    tags = {str(tag) for tag in row.get("file_object_tags") or []}
    return row.get("file_primary_role") == "source_code" or "source_code" in tags


def normalize_finding_status(status: Any) -> str:
    return str(status or "unknown").strip().lower()


def parse_commit_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def static_finding_event_sort_key(
    row_index_and_row: tuple[int, dict[str, Any]],
) -> tuple[datetime, str, int, int]:
    row_index, row = row_index_and_row
    status_order = {
        "introduced": 0,
        "removed": 1,
    }.get(normalize_finding_status(row.get("finding_status")), 2)
    return (
        parse_commit_datetime(row.get("commit_date")),
        str(row.get("commit_sha") or ""),
        status_order,
        row_index,
    )


def normalize_semgrep_severity(severity: Any) -> str:
    value = str(severity or "").strip().upper()
    if value == "ERROR":
        return "High"
    if value == "WARNING":
        return "Medium"
    if value == "INFO":
        return "Low"
    return "Unknown"


def normalize_osv_severity(severity: Any) -> str:
    value = str(severity or "").strip().upper()
    if value == "CRITICAL":
        return "Critical"
    if value == "HIGH":
        return "High"
    if value in {"MODERATE", "MEDIUM"}:
        return "Medium"
    if value == "LOW":
        return "Low"
    return "Unknown"


def top_25_cwe_bucket(row: dict[str, Any]) -> str:
    cwe_ids = parse_cwe_ids(row.get("metadata_cwe"))
    if cwe_ids & TOP_25_CWE_IDS:
        return CWE_TOP_25_BUCKET
    return CWE_OTHER_BUCKET


def cwe_1400_category_for_finding(
    row: dict[str, Any],
    cwe_lookup: dict[str, str],
) -> str:
    cwe_ids = parse_cwe_ids(row.get("metadata_cwe"))
    if not cwe_ids:
        return CWE_1400_NO_CWE

    categories = {
        cwe_lookup[cwe_id]
        for cwe_id in cwe_ids
        if cwe_id in cwe_lookup
    }
    if not categories:
        return CWE_1400_UNMAPPED
    if len(categories) > 1:
        return CWE_1400_MULTIPLE
    return next(iter(categories))


def load_repo_static_analysis_rq1c_data(
    repo_dir: Path,
    cwe_lookup: dict[str, str],
    max_commit_churn: int,
) -> dict[str, Any]:
    """Stream per-repo inputs and return compact static-analysis RQ1c summaries."""
    load_counts = {
        "commits": 0,
        "commit_classifications": count_jsonl_rows(repo_dir / COMMIT_CLASSIFICATIONS_FILE),
        "commit_file_classifications": 0,
        "line_lifecycle": 0,
        "static_findings": 0,
        "static_summary": count_jsonl_rows(repo_dir / STATIC_SUMMARY_FILE),
        "supply_chain_findings": 0,
        "supply_chain_summary": count_jsonl_rows(repo_dir / SUPPLY_CHAIN_SUMMARY_FILE),
        "sonarqube_scans": count_jsonl_rows(repo_dir / SONARQUBE_SCANS_FILE),
        "outlier_commits": 0,
        "outlier_commit_churn": 0,
    }
    commit_counts_by_role: Counter[str] = Counter()
    commit_roles_by_sha: dict[str, str] = {}
    outlier_commits: set[str] = set()

    static_counts = {role: Counter() for role in ROLE_ORDER}
    static_summary: Counter[str] = Counter()
    static_included_rows = 0

    semgrep_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    cwe_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    severity_summary: Counter[str] = Counter()
    osv_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    osv_summary: Counter[str] = Counter()

    cwe_1400_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    cwe_1400_summary: Counter[str] = Counter()

    top_cwe_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    top_cwe_summary: Counter[str] = Counter()

    lifecycle_summary: Counter[str] = Counter()
    events_by_key: defaultdict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)

    for row in iter_jsonl(repo_dir / COMMIT_FILE):
        load_counts["commits"] += 1
        sha = str(row.get("sha") or row.get("commit_sha") or "").strip()
        churn = commit_total_churn(row)
        if max_commit_churn > 0 and churn >= max_commit_churn:
            if sha:
                outlier_commits.add(sha)
            load_counts["outlier_commits"] += 1
            load_counts["outlier_commit_churn"] += churn
            continue

        role = normalize_commit_role(row.get("category") or row.get("commit_category"))
        if role is not None:
            commit_counts_by_role[role] += 1
            if sha:
                commit_roles_by_sha[sha] = role
        else:
            commit_counts_by_role["skipped_unknown_role"] += 1

    for row_index, row in enumerate(iter_jsonl(repo_dir / STATIC_FINDINGS_FILE)):
        load_counts["static_findings"] += 1

        status = normalize_finding_status(row.get("finding_status"))
        role = normalize_commit_role(row.get("commit_category"))
        sha = str(row.get("commit_sha") or "").strip()

        static_summary["static_finding_rows"] += 1
        lifecycle_summary["static_finding_rows"] += 1
        if sha in outlier_commits:
            static_summary["skipped_outlier_commit"] += 1
            severity_summary["skipped_outlier_commit"] += 1
            cwe_1400_summary["skipped_outlier_commit"] += 1
            top_cwe_summary["skipped_outlier_commit"] += 1
            lifecycle_summary["skipped_outlier_commit"] += 1
            continue

        if status not in FINDING_STATUS_ORDER:
            static_summary["skipped_unknown_status"] += 1
        elif role is None:
            static_summary["skipped_unknown_role"] += 1
        else:
            static_counts[role][status] += 1
            static_summary["included_static_finding_rows"] += 1
            static_included_rows += 1

        if status not in FINDING_STATUS_ORDER:
            severity_summary["skipped_unknown_status"] += 1
            cwe_1400_summary["skipped_unknown_status"] += 1
            top_cwe_summary["skipped_unknown_status"] += 1
        elif role is None:
            severity_summary["skipped_unknown_role"] += 1
            cwe_1400_summary["skipped_unknown_role"] += 1
            top_cwe_summary["skipped_unknown_role"] += 1
        else:
            semgrep_severity = normalize_semgrep_severity(row.get("severity"))
            cwe_bucket = top_25_cwe_bucket(row)
            semgrep_counts[status][role][semgrep_severity] += 1
            cwe_counts[status][role][cwe_bucket] += 1
            severity_summary["included_static_finding_rows"] += 1

            category = cwe_1400_category_for_finding(row, cwe_lookup)
            cwe_1400_counts[status][role][category] += 1
            cwe_1400_summary["included_static_finding_rows"] += 1
            if category == CWE_1400_NO_CWE:
                cwe_1400_summary["no_cwe_rows"] += 1
            elif category == CWE_1400_UNMAPPED:
                cwe_1400_summary["unmapped_cwe_rows"] += 1
            elif category == CWE_1400_MULTIPLE:
                cwe_1400_summary["multiple_category_rows"] += 1

            cwe_ids = parse_cwe_ids(row.get("metadata_cwe"))
            if not cwe_ids:
                top_cwe_summary["no_cwe_rows"] += 1
            else:
                top_cwe_summary["included_static_finding_rows"] += 1
                for cwe_id in sorted(cwe_ids):
                    top_cwe_counts[status][role][cwe_id] += 1
                    top_cwe_summary["included_cwe_occurrences"] += 1

        if status not in FINDING_STATUS_ORDER:
            lifecycle_summary["skipped_unknown_status"] += 1
            continue

        match_key = str(row.get("match_key") or "").strip()
        if not match_key:
            lifecycle_summary["skipped_missing_match_key"] += 1
            continue

        events_by_key[match_key].append(
            (
                row_index,
                {
                    "finding_status": status,
                    "commit_category": row.get("commit_category"),
                    "commit_date": row.get("commit_date"),
                    "commit_sha": row.get("commit_sha"),
                },
            )
        )
        lifecycle_summary["included_static_finding_events"] += 1

    for row in iter_jsonl(repo_dir / SUPPLY_CHAIN_FINDINGS_FILE):
        load_counts["supply_chain_findings"] += 1
        status = normalize_finding_status(row.get("finding_status"))
        role = normalize_commit_role(row.get("commit_category"))
        sha = str(row.get("commit_sha") or "").strip()
        osv_summary["supply_chain_finding_rows"] += 1
        if sha in outlier_commits:
            osv_summary["skipped_outlier_commit"] += 1
        elif status not in FINDING_STATUS_ORDER:
            osv_summary["skipped_unknown_status"] += 1
        elif role is None:
            osv_summary["skipped_unknown_role"] += 1
        else:
            severity = normalize_osv_severity(row.get("severity"))
            osv_counts[status][role][severity] += 1
            osv_summary["included_supply_chain_finding_rows"] += 1

    if static_included_rows:
        static_summary["included_repos"] = 1

    static_summary["skipped_static_finding_rows"] = (
        static_summary["skipped_unknown_status"]
        + static_summary["skipped_unknown_role"]
        + static_summary["skipped_outlier_commit"]
    )
    severity_summary["skipped_static_finding_rows"] = (
        severity_summary["skipped_unknown_status"]
        + severity_summary["skipped_unknown_role"]
        + severity_summary["skipped_outlier_commit"]
    )
    osv_summary["skipped_supply_chain_finding_rows"] = (
        osv_summary["skipped_unknown_status"]
        + osv_summary["skipped_unknown_role"]
        + osv_summary["skipped_outlier_commit"]
    )
    cwe_1400_summary["skipped_static_finding_rows"] = (
        cwe_1400_summary["skipped_unknown_status"]
        + cwe_1400_summary["skipped_unknown_role"]
        + cwe_1400_summary["skipped_outlier_commit"]
    )
    top_cwe_summary["skipped_static_finding_rows"] = (
        top_cwe_summary["skipped_unknown_status"]
        + top_cwe_summary["skipped_unknown_role"]
        + top_cwe_summary["skipped_outlier_commit"]
        + top_cwe_summary["no_cwe_rows"]
    )

    lifecycle_counts = {role: Counter() for role in ROLE_ORDER}
    lifecycle_durations = {role: [] for role in ROLE_ORDER}
    lifecycle_summary["finding_identity_groups"] = len(events_by_key)
    for events in events_by_key.values():
        open_introductions: deque[dict[str, Any]] = deque()

        for _, row in sorted(events, key=static_finding_event_sort_key):
            status = normalize_finding_status(row.get("finding_status"))
            if status == "introduced":
                role = normalize_commit_role(row.get("commit_category"))
                if role is None:
                    lifecycle_summary["skipped_introduced_unknown_role"] += 1
                    continue
                open_introductions.append(row)
                lifecycle_summary["introduced_with_known_origin_role"] += 1
            elif status == "removed":
                if not open_introductions:
                    lifecycle_summary["orphan_removed_events"] += 1
                    continue

                intro = open_introductions.popleft()
                origin_role = normalize_commit_role(intro.get("commit_category"))
                if origin_role is None:
                    lifecycle_summary["skipped_introduced_unknown_role"] += 1
                    continue

                lifecycle_summary["paired_removed_events"] += 1
                intro_date = parse_commit_datetime(intro.get("commit_date"))
                removal_date = parse_commit_datetime(row.get("commit_date"))
                lifecycle_durations[origin_role].append(
                    max(0.0, (removal_date - intro_date).total_seconds() / 86_400)
                )

                remover_role = normalize_commit_role(row.get("commit_category"))
                if remover_role is None:
                    lifecycle_counts[origin_role]["removed_by_other_or_unknown"] += 1
                    lifecycle_summary["removed_by_unknown_or_other_role"] += 1
                else:
                    lifecycle_counts[origin_role][f"removed_by_{remover_role}"] += 1
                    lifecycle_summary[f"removed_by_{remover_role}"] += 1

        while open_introductions:
            intro = open_introductions.popleft()
            origin_role = normalize_commit_role(intro.get("commit_category"))
            if origin_role is None:
                lifecycle_summary["skipped_introduced_unknown_role"] += 1
                continue
            lifecycle_counts[origin_role]["not_observed_removed"] += 1
            lifecycle_summary["not_observed_removed_events"] += 1

    lifecycle_summary["skipped_static_finding_events"] = (
        lifecycle_summary["skipped_unknown_status"]
        + lifecycle_summary["skipped_missing_match_key"]
        + lifecycle_summary["skipped_outlier_commit"]
    )

    return {
        "repo_key": repo_dir.name,
        "repo": repo_from_dir(repo_dir),
        "repo_dir": str(repo_dir),
        "load_counts": load_counts,
        "commit_counts_by_role": commit_counts_by_role,
        "commit_counts_by_role": commit_counts_by_role,
        "static_counts_by_role_status": static_counts,
        "static_summary": static_summary,
        "semgrep_counts_by_status_role": semgrep_counts,
        "cwe_top25_counts_by_status_role": cwe_counts,
        "severity_summary": severity_summary,
        "osv_counts_by_status_role": osv_counts,
        "osv_summary": osv_summary,
        "cwe_1400_counts_by_status_role": cwe_1400_counts,
        "cwe_1400_summary": cwe_1400_summary,
        "top_cwe_counts_by_status_role": top_cwe_counts,
        "top_cwe_summary": top_cwe_summary,
        "lifecycle_counts_by_origin_role": lifecycle_counts,
        "lifecycle_summary": lifecycle_summary,
        "lifecycle_duration_summary": lifecycle_summary.copy(),
        "lifecycle_durations_by_origin_role": lifecycle_durations,
    }


def static_analysis_rq1c_repo_data_summary(
    repo_dir: Path,
    repo_data: dict[str, Any],
) -> str:
    counts = repo_data.get("load_counts") or {}
    return (
        f"commits={int(counts.get('commits') or 0):,} "
        f"intent_commits={int(counts.get('commit_classifications') or 0):,} "
        f"files={int(counts.get('commit_file_classifications') or 0):,} "
        f"lifecycle_lines_loaded={int(counts.get('line_lifecycle') or 0):,} "
        f"static_findings={int(counts.get('static_findings') or 0):,} "
        f"static_summary={int(counts.get('static_summary') or 0):,} "
        f"supply_chain_findings={int(counts.get('supply_chain_findings') or 0):,} "
        f"supply_chain_summary={int(counts.get('supply_chain_summary') or 0):,} "
        f"sonar_scans={int(counts.get('sonarqube_scans') or 0):,} "
        f"outlier_commits={int(counts.get('outlier_commits') or 0):,}"
    )


def to_counter(value: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    if isinstance(value, Counter):
        counter.update(value)
    elif isinstance(value, dict):
        counter.update({str(key): int(count or 0) for key, count in value.items()})
    return counter


def merge_counter_field(repo_data: list[dict[str, Any]], field: str) -> Counter[str]:
    merged: Counter[str] = Counter()
    for repo in repo_data:
        merged.update(to_counter(repo.get(field)))
    return merged


def merge_role_counters(
    repo_data: list[dict[str, Any]],
    field: str,
) -> dict[str, Counter[str]]:
    merged = {role: Counter() for role in ROLE_ORDER}
    for repo in repo_data:
        values = repo.get(field) or {}
        for role in ROLE_ORDER:
            merged[role].update(to_counter(values.get(role)))
    return merged


def merge_status_role_counters(
    repo_data: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, Counter[str]]]:
    merged = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    for repo in repo_data:
        values = repo.get(field) or {}
        for status in FINDING_STATUS_ORDER:
            status_values = values.get(status) or {}
            for role in ROLE_ORDER:
                merged[status][role].update(to_counter(status_values.get(role)))
    return merged


def aggregate_static_findings_by_role(
    repo_data: list[dict[str, Any]],
) -> tuple[dict[str, Counter[str]], Counter[str]]:
    return (
        merge_role_counters(repo_data, "static_counts_by_role_status"),
        merge_counter_field(repo_data, "static_summary"),
    )


def count_commits_by_role(repo_data: list[dict[str, Any]]) -> Counter[str]:
    return merge_counter_field(repo_data, "commit_counts_by_role")


def aggregate_static_severity_views_by_role(
    repo_data: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    Counter[str],
    Counter[str],
]:
    return (
        merge_status_role_counters(repo_data, "semgrep_counts_by_status_role"),
        merge_status_role_counters(repo_data, "cwe_top25_counts_by_status_role"),
        merge_status_role_counters(repo_data, "osv_counts_by_status_role"),
        merge_counter_field(repo_data, "severity_summary"),
        merge_counter_field(repo_data, "osv_summary"),
    )


def aggregate_static_cwe_1400_categories_by_role(
    repo_data: list[dict[str, Any]],
    cwe_lookup: dict[str, str],
) -> tuple[dict[str, dict[str, Counter[str]]], Counter[str]]:
    _ = cwe_lookup
    return (
        merge_status_role_counters(repo_data, "cwe_1400_counts_by_status_role"),
        merge_counter_field(repo_data, "cwe_1400_summary"),
    )


def aggregate_static_cwe_ids_by_role(
    repo_data: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Counter[str]]], Counter[str]]:
    return (
        merge_status_role_counters(repo_data, "top_cwe_counts_by_status_role"),
        merge_counter_field(repo_data, "top_cwe_summary"),
    )


def aggregate_static_finding_lifecycle_outcomes_by_origin_role(
    repo_data: list[dict[str, Any]],
) -> tuple[dict[str, Counter[str]], Counter[str]]:
    return (
        merge_role_counters(repo_data, "lifecycle_counts_by_origin_role"),
        merge_counter_field(repo_data, "lifecycle_summary"),
    )


def aggregate_static_finding_lifecycle_removal_days_by_origin_role(
    repo_data: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], Counter[str]]:
    durations = {role: [] for role in ROLE_ORDER}
    for repo in repo_data:
        values = repo.get("lifecycle_durations_by_origin_role") or {}
        for role in ROLE_ORDER:
            durations[role].extend(float(value) for value in values.get(role, []))
    summary = merge_counter_field(repo_data, "lifecycle_duration_summary")
    return durations, summary


def merged_load_counts(repo_data: list[dict[str, Any]]) -> Counter[str]:
    merged: Counter[str] = Counter()
    for repo in repo_data:
        merged.update(to_counter(repo.get("load_counts")))
    merged["loaded_repos"] = len(repo_data)
    return merged


def add_compact_count_row(
    rows: list[dict[str, Any]],
    *,
    dataset: str,
    status: str = "",
    role: str = "",
    category: str = "",
    count: int,
) -> None:
    if count:
        rows.append(
            {
                "dataset": dataset,
                "status": status,
                "role": role,
                "category": category,
                "count": int(count),
            }
        )


def compact_count_rows(
    *,
    commit_counts: Counter[str],
    static_counts: dict[str, Counter[str]],
    semgrep_counts: dict[str, dict[str, Counter[str]]],
    cwe_counts: dict[str, dict[str, Counter[str]]],
    osv_counts: dict[str, dict[str, Counter[str]]],
    cwe_1400_counts: dict[str, dict[str, Counter[str]]],
    top_cwe_counts: dict[str, dict[str, Counter[str]]],
    lifecycle_counts: dict[str, Counter[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for role, count in commit_counts.items():
        add_compact_count_row(
            rows,
            dataset="commit_counts",
            role=str(role),
            category="commits",
            count=count,
        )

    for role, role_counts in static_counts.items():
        for status, count in role_counts.items():
            add_compact_count_row(
                rows,
                dataset="static_counts",
                status=str(status),
                role=str(role),
                category="all",
                count=count,
            )

    for dataset, values in (
        ("semgrep_severity", semgrep_counts),
        ("cwe_top25_bucket", cwe_counts),
        ("osv_severity", osv_counts),
        ("cwe_1400_category", cwe_1400_counts),
        ("top_cwe", top_cwe_counts),
    ):
        for status, status_values in values.items():
            for role, role_counts in status_values.items():
                for category, count in role_counts.items():
                    add_compact_count_row(
                        rows,
                        dataset=dataset,
                        status=str(status),
                        role=str(role),
                        category=str(category),
                        count=count,
                    )

    for role, role_counts in lifecycle_counts.items():
        for outcome, count in role_counts.items():
            add_compact_count_row(
                rows,
                dataset="lifecycle_outcome",
                role=str(role),
                category=str(outcome),
                count=count,
            )

    return rows


def compact_summary_rows(
    *,
    load_counts: Counter[str],
    static_summary: Counter[str],
    severity_summary: Counter[str],
    osv_summary: Counter[str],
    cwe_1400_summary: Counter[str],
    top_cwe_summary: Counter[str],
    lifecycle_summary: Counter[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dataset, values in (
        ("load_counts", load_counts),
        ("static_summary", static_summary),
        ("severity_summary", severity_summary),
        ("osv_summary", osv_summary),
        ("cwe_1400_summary", cwe_1400_summary),
        ("top_cwe_summary", top_cwe_summary),
        ("lifecycle_summary", lifecycle_summary),
    ):
        for key, count in values.items():
            rows.append(
                {
                    "dataset": dataset,
                    "key": str(key),
                    "count": int(count),
                }
            )
    return rows


def compact_duration_rows(
    lifecycle_durations: dict[str, list[float]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role in ROLE_ORDER:
        for duration_days in lifecycle_durations.get(role, []):
            rows.append(
                {
                    "role": role,
                    "duration_days": f"{float(duration_days):.6f}",
                }
            )
    return rows


def write_compact_counts_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("dataset", "status", "role", "category", "count"),
        )
        writer.writeheader()
        writer.writerows(rows)


def write_compact_summary_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("dataset", "key", "count"))
        writer.writeheader()
        writer.writerows(rows)


def write_compact_durations_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("role", "duration_days"))
        writer.writeheader()
        writer.writerows(rows)


def read_compact_counts_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "dataset": row.get("dataset") or "",
                    "status": row.get("status") or "",
                    "role": row.get("role") or "",
                    "category": row.get("category") or "",
                    "count": int(row.get("count") or 0),
                }
            )
    return rows


def read_compact_summary_csv(path: Path) -> dict[str, Counter[str]]:
    summaries: dict[str, Counter[str]] = defaultdict(Counter)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            dataset = row.get("dataset") or ""
            key = row.get("key") or ""
            if dataset and key:
                summaries[dataset][key] += int(row.get("count") or 0)
    return dict(summaries)


def read_compact_durations_csv(path: Path) -> dict[str, list[float]]:
    durations = {role: [] for role in ROLE_ORDER}
    if not path.exists():
        return durations
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            role = row.get("role") or ""
            if role not in durations:
                continue
            try:
                durations[role].append(float(row.get("duration_days") or 0.0))
            except ValueError:
                continue
    return durations


def compact_counts_to_aggregates(
    rows: list[dict[str, Any]],
) -> tuple[
    Counter[str],
    dict[str, Counter[str]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, dict[str, Counter[str]]],
    dict[str, Counter[str]],
]:
    commit_counts: Counter[str] = Counter()
    static_counts = {role: Counter() for role in ROLE_ORDER}
    semgrep_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    cwe_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    osv_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    cwe_1400_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    top_cwe_counts = {
        status: {role: Counter() for role in ROLE_ORDER}
        for status in FINDING_STATUS_ORDER
    }
    lifecycle_counts = {role: Counter() for role in ROLE_ORDER}

    nested_datasets = {
        "semgrep_severity": semgrep_counts,
        "cwe_top25_bucket": cwe_counts,
        "osv_severity": osv_counts,
        "cwe_1400_category": cwe_1400_counts,
        "top_cwe": top_cwe_counts,
    }

    for row in rows:
        dataset = str(row.get("dataset") or "")
        status = str(row.get("status") or "")
        role = str(row.get("role") or "")
        category = str(row.get("category") or "")
        count = int(row.get("count") or 0)

        if dataset == "commit_counts":
            commit_counts[role] += count
        elif dataset == "static_counts" and role in static_counts:
            static_counts[role][status] += count
        elif dataset in nested_datasets and status in FINDING_STATUS_ORDER and role in ROLE_ORDER:
            nested_datasets[dataset][status][role][category] += count
        elif dataset == "lifecycle_outcome" and role in lifecycle_counts:
            lifecycle_counts[role][category] += count

    return (
        commit_counts,
        static_counts,
        semgrep_counts,
        cwe_counts,
        osv_counts,
        cwe_1400_counts,
        top_cwe_counts,
        lifecycle_counts,
    )


def finding_rate_per_100_commits(
    counts: dict[str, Counter[str]],
    commit_counts: Counter[str],
    role: str,
    status: str,
) -> float:
    denominator = commit_counts.get(role, 0)
    if denominator <= 0:
        return 0.0
    return counts.get(role, Counter()).get(status, 0) / denominator * STATIC_FINDING_RATE_SCALE


def nested_finding_rate_per_100_commits(
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    status: str,
    role: str,
    category: str,
) -> float:
    denominator = commit_counts.get(role, 0)
    if denominator <= 0:
        return 0.0
    return (
        counts.get(status, {}).get(role, Counter()).get(category, 0)
        / denominator
        * STATIC_FINDING_RATE_SCALE
    )


def categories_with_counts(
    counts: dict[str, dict[str, Counter[str]]],
    preferred_order: tuple[str, ...],
) -> list[str]:
    categories = {
        category
        for values_by_role in counts.values()
        for role_counts in values_by_role.values()
        for category, count in role_counts.items()
        if count
    }
    ordered = [category for category in preferred_order if category in categories]
    ordered.extend(sorted(categories - set(preferred_order)))
    return ordered


def categories_by_total_count(
    counts: dict[str, dict[str, Counter[str]]],
) -> list[str]:
    totals: Counter[str] = Counter()
    for values_by_role in counts.values():
        for role_counts in values_by_role.values():
            totals.update(role_counts)
    return [
        category
        for category, count in sorted(
            totals.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if count
    ]


def plot_side_by_side_stacked_rate_subplot(
    ax: plt.Axes,
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    categories: list[str],
    colors_by_role: dict[str, dict[str, str]],
    title: str,
) -> float:
    x_positions = list(range(len(ROLE_ORDER)))
    bar_width = 0.32
    offsets = {
        "introduced": -bar_width / 2,
        "removed": bar_width / 2,
    }
    max_total = 0.0

    for status in FINDING_STATUS_ORDER:
        for role_index, role in enumerate(ROLE_ORDER):
            x_position = x_positions[role_index] + offsets[status]
            bottom = 0.0
            for category in categories:
                value = nested_finding_rate_per_100_commits(
                    counts,
                    commit_counts,
                    status,
                    role,
                    category,
                )
                if value <= 0:
                    continue
                ax.bar(
                    x_position,
                    value,
                    bottom=bottom,
                    color=colors_by_role.get(role, {}).get(category, "#9d9d9d"),
                    width=bar_width,
                )
                bottom += value

            max_total = max(max_total, bottom)
            if bottom:
                ax.text(
                    x_position,
                    bottom,
                    f"{bottom:.1f}",
                    ha="center",
                    va="bottom",
                    fontsize=18,
                )

    status_label_y = -0.012
    role_label_y = -0.062
    for role_index in x_positions:
        label = ax.text(
            role_index + offsets["introduced"],
            status_label_y,
            "Intro.",
            ha="center",
            va="top",
            fontsize=20,
            transform=ax.get_xaxis_transform(),
            color="#000000",
            clip_on=False,
        )
        label.set_in_layout(False)
        label = ax.text(
            role_index + offsets["removed"],
            status_label_y,
            "Rem.",
            ha="center",
            va="top",
            fontsize=20,
            transform=ax.get_xaxis_transform(),
            color="#000000",
            clip_on=False,
        )
        label.set_in_layout(False)
        label = ax.text(
            role_index,
            role_label_y,
            role_label(ROLE_ORDER[role_index]),
            ha="center",
            va="top",
            fontsize=20,
            transform=ax.get_xaxis_transform(),
            color="#111111",
            clip_on=False,
        )
        label.set_in_layout(False)

    ax.set_xlim(-0.55, len(ROLE_ORDER) - 0.45)
    ax.set_xticks(x_positions)
    ax.set_xticklabels([])
    ax.tick_params(axis="x", length=0)
    ax.tick_params(axis="y", labelsize=13)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:.0f}"))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    return max_total


def add_grayscale_colorbar_legend(
    ax: plt.Axes,
    title: str,
    labels: tuple[str, ...],
    colors: dict[str, str],
    *,
    location: str = "upper_right",
) -> None:
    inset_width = 0.12 * max(len(labels), 2)
    inset_width = min(inset_width, 0.48)
    inset_x = 0.04 if location == "upper_left" else 0.96 - inset_width
    inset = ax.inset_axes([inset_x, 0.86, inset_width, 0.07])
    for index, label in enumerate(labels):
        inset.add_patch(
            plt.Rectangle(
                (index, 0),
                1,
                1,
                color=colors.get(label, "#d9d9d9"),
            )
        )
    inset.set_xlim(0, len(labels))
    inset.set_ylim(0, 1)
    inset.set_yticks([])
    inset.set_xticks([index + 0.5 for index in range(len(labels))])
    inset.set_xticklabels(labels, fontsize=15)
    inset.set_title(title, fontsize=16, pad=2)
    for spine in inset.spines.values():
        spine.set_color("#777777")
        spine.set_linewidth(0.6)


def plot_static_findings_severity_by_role(
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    category_order: tuple[str, ...],
    colors_by_role: dict[str, dict[str, str]],
    legend_labels: tuple[str, ...],
    legend_colors: dict[str, str],
    title: str,
    output_path: Path,
    *,
    legend_location: str = "upper_right",
    y_axis_min: float | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    categories = categories_with_counts(counts, category_order)
    total = sum(
        counts.get(status, {}).get(role, Counter()).get(category, 0)
        for status in FINDING_STATUS_ORDER
        for role in ROLE_ORDER
        for category in categories
    )

    fig, ax = plt.subplots(figsize=(6.2, 5.2))
    if total == 0:
        ax.text(
            0.5,
            0.5,
            f"No introduced or removed {title} findings for Agentic/Human commits.",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.set_axis_off()
    else:
        max_total = plot_side_by_side_stacked_rate_subplot(
            ax,
            counts,
            commit_counts,
            categories,
            colors_by_role,
            title,
        )
        y_axis_max = max_total * 1.2 if max_total else 1
        if y_axis_min is not None:
            y_axis_max = max(y_axis_max, y_axis_min)
        ax.set_ylim(0, y_axis_max)
        ax.set_ylabel("Findings per 100 commits", fontsize=22)
        add_grayscale_colorbar_legend(
            ax,
            "Severity shade",
            legend_labels,
            legend_colors,
            location=legend_location,
        )

    fig.subplots_adjust(left=0.12, right=0.98, top=0.96, bottom=0.11)
    fig.savefig(output_path, dpi=200)
    plt.close(fig)


def load_static_analysis_rq1c_data(
    input_dir: Path = DEFAULT_INPUT_DIR,
    *,
    cwe_lookup: dict[str, str],
    repos: list[str] | None = None,
    repo_file: Path | None = None,
    workers: int | None = None,
    max_commit_churn: int = DEFAULT_MAX_COMMIT_CHURN,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Resolve repo output dirs and stream compact static-analysis RQ1c summaries."""
    repo_dirs = resolve_repo_dirs(
        input_dir,
        repo_file=repo_file,
        repos=repos or [],
        required_files=STATIC_ANALYSIS_RQ1C_INPUT_FILES,
    )
    if not repo_dirs:
        raise FileNotFoundError(f"No repo output directories found under {input_dir}")

    return load_repo_data_parallel(
        repo_dirs,
        load_repo_static_analysis_rq1c_data,
        cwe_lookup,
        max_commit_churn,
        workers=workers,
        default_worker_limit=4,
        task_label="static-analysis RQ1c input data",
        logger=logger or LOG,
        result_summary=static_analysis_rq1c_repo_data_summary,
        preserve_order=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream per-repo inputs for RQ1c static-analysis exploration, "
            "write compact CSVs, and draw summary figures."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Folder containing per-repo webscrape output directories.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Limit to owner/repo or owner__repo. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "--repo-file",
        type=Path,
        help="Optional text file containing repos to include, one per line.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Number of worker processes for parallel per-repo loading.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where static-analysis RQ1c outputs will be written.",
    )
    parser.add_argument(
        "--compact-counts-csv",
        type=Path,
        help=(
            "Optional compact count CSV path. "
            f"Default: output-dir/{COMPACT_COUNTS_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--compact-summary-csv",
        type=Path,
        help=(
            "Optional compact summary CSV path. "
            f"Default: output-dir/{COMPACT_SUMMARY_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--compact-durations-csv",
        type=Path,
        help=(
            "Optional compact lifecycle duration CSV path. "
            f"Default: output-dir/{COMPACT_DURATIONS_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--use-compact-csvs",
        action="store_true",
        help="Skip raw JSONL loading and draw/print from the compact CSVs.",
    )
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Export/print compact summaries but do not draw plots.",
    )
    parser.add_argument(
        "--max-commit-churn",
        type=int,
        default=DEFAULT_MAX_COMMIT_CHURN,
        help=(
            "Exclude commits with total additions+deletions churn greater than or "
            "equal to this value from static-finding numerators and source-code "
            "changed-line denominators. Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--cwe-1400-lookup",
        type=Path,
        default=DEFAULT_CWE_1400_LOOKUP,
        help=(
            "JSON lookup mapping CWE IDs to MITRE CWE-1400 categories. "
            "Generate with dev/analysis/build_cwe_1400_lookup.py."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-repo loading progress.",
    )
    return parser.parse_args()


def print_loaded_totals(repo_data: list[dict[str, Any]]) -> None:
    row_keys = (
        "commits",
        "outlier_commits",
        "outlier_commit_churn",
        "commit_classifications",
        "commit_file_classifications",
        "line_lifecycle",
        "static_findings",
        "static_summary",
        "supply_chain_findings",
        "supply_chain_summary",
        "sonarqube_scans",
    )
    print(f"loaded_repos: {len(repo_data):,}")
    for key in row_keys:
        print(
            f"{key}: "
            f"{sum(int((repo.get('load_counts') or {}).get(key) or 0) for repo in repo_data):,}"
        )
    print("line_lifecycle note: rows are not loaded by this script.")


def print_compact_loaded_totals(load_counts: Counter[str]) -> None:
    row_keys = (
        "commits",
        "outlier_commits",
        "outlier_commit_churn",
        "commit_classifications",
        "commit_file_classifications",
        "line_lifecycle",
        "static_findings",
        "static_summary",
        "supply_chain_findings",
        "supply_chain_summary",
        "sonarqube_scans",
    )
    print(f"loaded_repos: {load_counts['loaded_repos']:,}")
    for key in row_keys:
        print(f"{key}: {load_counts[key]:,}")
    print("line_lifecycle note: rows are not loaded by this script.")


def print_static_finding_role_status_summary(
    counts: dict[str, Counter[str]],
    commit_counts: Counter[str],
    summary: Counter[str],
) -> None:
    print("static_findings_by_role_status:")
    for role in ROLE_ORDER:
        introduced = counts.get(role, Counter()).get("introduced", 0)
        removed = counts.get(role, Counter()).get("removed", 0)
        denominator = commit_counts.get(role, 0)
        introduced_rate = finding_rate_per_100_commits(
            counts,
            commit_counts,
            role,
            "introduced",
        )
        removed_rate = finding_rate_per_100_commits(
            counts,
            commit_counts,
            role,
            "removed",
        )
        print(
            f"  {role_label(role)}: introduced={introduced:,} removed={removed:,} "
            f"total={introduced + removed:,} commits={denominator:,} "
            f"introduced_per_100_commits={introduced_rate:.3f} "
            f"removed_per_100_commits={removed_rate:.3f}"
        )
    print(f"included_static_finding_rows: {summary['included_static_finding_rows']:,}")
    print(f"included_static_finding_repos: {summary['included_repos']:,}")
    print(f"skipped_static_finding_rows: {summary['skipped_static_finding_rows']:,}")
    print(f"  skipped_unknown_status: {summary['skipped_unknown_status']:,}")
    print(f"  skipped_unknown_role: {summary['skipped_unknown_role']:,}")
    print(f"  skipped_outlier_commit: {summary['skipped_outlier_commit']:,}")


def print_static_finding_lifecycle_removal_outcome_summary(
    counts: dict[str, Counter[str]],
    summary: Counter[str],
) -> None:
    print("static_finding_lifecycle_removal_outcome_sankey:")
    for role in ROLE_ORDER:
        removed_by_ai = counts.get(role, Counter()).get("removed_by_AI", 0)
        removed_by_human = counts.get(role, Counter()).get("removed_by_Human", 0)
        removed_by_other = counts.get(role, Counter()).get(
            "removed_by_other_or_unknown",
            0,
        )
        removed = removed_by_ai + removed_by_human + removed_by_other
        not_observed_removed = counts.get(role, Counter()).get(
            "not_observed_removed",
            0,
        )
        introduced = removed + not_observed_removed
        removal_rate = removed / introduced * 100 if introduced else 0.0
        print(
            f"  {role_label(role)}: introduced={introduced:,} later_removed={removed:,} "
            f"removed_by_Agentic={removed_by_ai:,} "
            f"removed_by_Human={removed_by_human:,} "
            f"removed_by_other_or_unknown={removed_by_other:,} "
            f"not_observed_removed={not_observed_removed:,} "
            f"removal_rate={removal_rate:.1f}%"
        )

    print(f"static_finding_rows: {summary['static_finding_rows']:,}")
    print(
        "included_static_finding_events: "
        f"{summary['included_static_finding_events']:,}"
    )
    print(f"finding_identity_groups: {summary['finding_identity_groups']:,}")
    print(
        "introduced_with_known_origin_role: "
        f"{summary['introduced_with_known_origin_role']:,}"
    )
    print(f"paired_removed_events: {summary['paired_removed_events']:,}")
    print(
        "not_observed_removed_events: "
        f"{summary['not_observed_removed_events']:,}"
    )
    print(f"orphan_removed_events: {summary['orphan_removed_events']:,}")
    print(
        "removed_by_Agentic: "
        f"{summary['removed_by_AI']:,} removed_by_Human: {summary['removed_by_Human']:,} "
        f"removed_by_unknown_or_other_role: {summary['removed_by_unknown_or_other_role']:,}"
    )
    print(
        "skipped_static_finding_events: "
        f"{summary['skipped_static_finding_events']:,}"
    )
    print(f"  skipped_unknown_status: {summary['skipped_unknown_status']:,}")
    print(f"  skipped_missing_match_key: {summary['skipped_missing_match_key']:,}")
    print(f"  skipped_outlier_commit: {summary['skipped_outlier_commit']:,}")
    print(
        "  skipped_introduced_unknown_role: "
        f"{summary['skipped_introduced_unknown_role']:,}"
    )


def print_static_finding_lifecycle_time_to_removal_summary(
    durations_by_role: dict[str, list[float]],
    summary: Counter[str],
) -> None:
    print("static_finding_lifecycle_time_to_removal:")
    for role in ROLE_ORDER:
        values = sorted(durations_by_role.get(role, []))
        if values:
            print(
                f"  {role}: paired_removed={len(values):,} "
                f"median_days={median(values):.2f} "
                f"min_days={values[0]:.2f} max_days={values[-1]:.2f}"
            )
        else:
            print(f"  {role}: paired_removed=0")
    print(f"paired_removed_events: {summary['paired_removed_events']:,}")
    print(
        "not_observed_removed_events: "
        f"{summary['not_observed_removed_events']:,}"
    )
    print(f"orphan_removed_events: {summary['orphan_removed_events']:,}")
    print(
        "skipped_static_finding_events: "
        f"{summary['skipped_static_finding_events']:,}"
    )


def format_nested_rate_summary(
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    status: str,
    role: str,
    categories: list[str],
) -> str:
    parts = []
    for category in categories:
        raw_count = counts.get(status, {}).get(role, Counter()).get(category, 0)
        rate = nested_finding_rate_per_100_commits(
            counts,
            commit_counts,
            status,
            role,
            category,
        )
        if raw_count or rate:
            parts.append(f"{category}={raw_count:,} ({rate:.3f}/100 commits)")
    return ", ".join(parts) if parts else "none"


def print_static_severity_overview_summary(
    semgrep_counts: dict[str, dict[str, Counter[str]]],
    osv_counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    summary: Counter[str],
    osv_summary: Counter[str],
) -> None:
    semgrep_categories = categories_with_counts(semgrep_counts, SEMGREP_SEVERITY_ORDER)
    osv_categories = categories_with_counts(osv_counts, OSV_SEVERITY_ORDER)

    print("static_findings_semgrep_severity_by_role_status:")
    for status in FINDING_STATUS_ORDER:
        print(f"  {FINDING_STATUS_LABELS[status]}:")
        for role in ROLE_ORDER:
            print(
                f"    {role_label(role)}: "
                + format_nested_rate_summary(
                    semgrep_counts,
                    commit_counts,
                    status,
                    role,
                    semgrep_categories,
                )
            )

    print("osv_findings_severity_by_role_status:")
    for status in FINDING_STATUS_ORDER:
        print(f"  {FINDING_STATUS_LABELS[status]}:")
        for role in ROLE_ORDER:
            print(
                f"    {role_label(role)}: "
                + format_nested_rate_summary(
                    osv_counts,
                    commit_counts,
                    status,
                    role,
                    osv_categories,
                )
            )
    print(
        "included_static_severity_rows: "
        f"{summary['included_static_finding_rows']:,}"
    )
    print(
        "skipped_static_severity_rows: "
        f"{summary['skipped_static_finding_rows']:,}"
    )
    print(
        "  skipped_static_severity_outlier_commit: "
        f"{summary['skipped_outlier_commit']:,}"
    )
    print(
        "included_osv_finding_rows: "
        f"{osv_summary['included_supply_chain_finding_rows']:,}"
    )
    print(
        "skipped_osv_finding_rows: "
        f"{osv_summary['skipped_supply_chain_finding_rows']:,}"
    )
    print(
        "  skipped_osv_outlier_commit: "
        f"{osv_summary['skipped_outlier_commit']:,}"
    )


def print_static_cwe_1400_category_summary(
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    summary: Counter[str],
) -> None:
    categories = categories_by_total_count(counts)
    print("static_findings_cwe_1400_category_by_role_status:")
    for category in categories:
        total = sum(
            counts.get(status, {}).get(role, Counter()).get(category, 0)
            for status in FINDING_STATUS_ORDER
            for role in ROLE_ORDER
        )
        print(f"  {category}: total={total:,}")
        for status in FINDING_STATUS_ORDER:
            values = []
            for role in ROLE_ORDER:
                raw_count = counts.get(status, {}).get(role, Counter()).get(category, 0)
                rate = nested_finding_rate_per_100_commits(
                    counts,
                    commit_counts,
                    status,
                    role,
                    category,
                )
                if raw_count or rate:
                    values.append(f"{role}={raw_count:,} ({rate:.3f}/100 commits)")
            if values:
                print(f"    {FINDING_STATUS_LABELS[status]}: " + ", ".join(values))
    print(
        "included_static_cwe_1400_rows: "
        f"{summary['included_static_finding_rows']:,}"
    )
    print(f"no_cwe_rows: {summary['no_cwe_rows']:,}")
    print(f"unmapped_cwe_rows: {summary['unmapped_cwe_rows']:,}")
    print(f"multiple_cwe_1400_category_rows: {summary['multiple_category_rows']:,}")
    print(
        "skipped_static_cwe_1400_rows: "
        f"{summary['skipped_static_finding_rows']:,}"
    )
    print(f"  skipped_outlier_commit: {summary['skipped_outlier_commit']:,}")


def print_static_top_cwe_summary(
    counts: dict[str, dict[str, Counter[str]]],
    commit_counts: Counter[str],
    summary: Counter[str],
    *,
    top_n: int = TOP_STATIC_CWE_LIMIT,
) -> None:
    cwe_ids = categories_by_total_count(counts)[:top_n]
    print(f"static_findings_top_{len(cwe_ids)}_cwe_by_role_status:")
    for cwe_id in cwe_ids:
        total = sum(
            counts.get(status, {}).get(role, Counter()).get(cwe_id, 0)
            for status in FINDING_STATUS_ORDER
            for role in ROLE_ORDER
        )
        print(f"  {cwe_id}: total={total:,}")
        for status in FINDING_STATUS_ORDER:
            values = []
            for role in ROLE_ORDER:
                raw_count = counts.get(status, {}).get(role, Counter()).get(cwe_id, 0)
                rate = nested_finding_rate_per_100_commits(
                    counts,
                    commit_counts,
                    status,
                    role,
                    cwe_id,
                )
                if raw_count or rate:
                    values.append(f"{role}={raw_count:,} ({rate:.3f}/100 commits)")
            if values:
                print(f"    {FINDING_STATUS_LABELS[status]}: " + ", ".join(values))
    print(
        "included_static_cwe_rows: "
        f"{summary['included_static_finding_rows']:,}"
    )
    print(f"included_cwe_occurrences: {summary['included_cwe_occurrences']:,}")
    print(f"no_cwe_rows: {summary['no_cwe_rows']:,}")
    print(
        "skipped_static_cwe_rows: "
        f"{summary['skipped_static_finding_rows']:,}"
    )
    print(f"  skipped_outlier_commit: {summary['skipped_outlier_commit']:,}")


def main() -> None:
    setup_plot_style()
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.workers is not None and args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.max_commit_churn < 0:
        raise ValueError("--max-commit-churn must be non-negative")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    compact_counts_csv = args.compact_counts_csv or (
        args.output_dir / COMPACT_COUNTS_CSV_NAME
    )
    compact_summary_csv = args.compact_summary_csv or (
        args.output_dir / COMPACT_SUMMARY_CSV_NAME
    )
    compact_durations_csv = args.compact_durations_csv or (
        args.output_dir / COMPACT_DURATIONS_CSV_NAME
    )

    if args.use_compact_csvs:
        compact_rows = read_compact_counts_csv(compact_counts_csv)
        compact_summaries = read_compact_summary_csv(compact_summary_csv)
        lifecycle_durations = read_compact_durations_csv(compact_durations_csv)
        (
            commit_counts,
            static_counts,
            semgrep_counts,
            cwe_counts,
            osv_counts,
            cwe_1400_counts,
            top_cwe_counts,
            lifecycle_counts,
        ) = compact_counts_to_aggregates(compact_rows)
        load_counts = compact_summaries.get("load_counts", Counter())
        static_summary = compact_summaries.get("static_summary", Counter())
        severity_summary = compact_summaries.get("severity_summary", Counter())
        osv_summary = compact_summaries.get("osv_summary", Counter())
        cwe_1400_summary = compact_summaries.get("cwe_1400_summary", Counter())
        top_cwe_summary = compact_summaries.get("top_cwe_summary", Counter())
        lifecycle_summary = compact_summaries.get("lifecycle_summary", Counter())
        lifecycle_duration_summary = lifecycle_summary
        print(f"loaded_compact_counts_csv: {compact_counts_csv}")
        print(f"loaded_compact_summary_csv: {compact_summary_csv}")
        print(f"loaded_compact_durations_csv: {compact_durations_csv}")
        print_compact_loaded_totals(load_counts)
        if not any(commit_counts.get(role, 0) for role in ROLE_ORDER):
            print(
                "warning: compact counts contain no commit_counts denominator; "
                "regenerate compact CSVs without --use-compact-csvs for per-commit rates."
            )
    else:
        cwe_1400_lookup = load_cwe_1400_lookup(args.cwe_1400_lookup)
        repo_data = load_static_analysis_rq1c_data(
            args.input_dir,
            cwe_lookup=cwe_1400_lookup,
            repos=split_csv(args.repo),
            repo_file=args.repo_file,
            workers=args.workers,
            max_commit_churn=args.max_commit_churn,
            logger=LOG,
        )
        print_loaded_totals(repo_data)
        load_counts = merged_load_counts(repo_data)
        static_counts, static_summary = aggregate_static_findings_by_role(repo_data)
        commit_counts = count_commits_by_role(repo_data)
        lifecycle_counts, lifecycle_summary = (
            aggregate_static_finding_lifecycle_outcomes_by_origin_role(repo_data)
        )
        lifecycle_durations, lifecycle_duration_summary = (
            aggregate_static_finding_lifecycle_removal_days_by_origin_role(repo_data)
        )
        semgrep_counts, cwe_counts, osv_counts, severity_summary, osv_summary = (
            aggregate_static_severity_views_by_role(repo_data)
        )
        cwe_1400_counts, cwe_1400_summary = (
            aggregate_static_cwe_1400_categories_by_role(
                repo_data,
                cwe_1400_lookup,
            )
        )
        top_cwe_counts, top_cwe_summary = aggregate_static_cwe_ids_by_role(repo_data)

        write_compact_counts_csv(
            compact_count_rows(
                commit_counts=commit_counts,
                static_counts=static_counts,
                semgrep_counts=semgrep_counts,
                cwe_counts=cwe_counts,
                osv_counts=osv_counts,
                cwe_1400_counts=cwe_1400_counts,
                top_cwe_counts=top_cwe_counts,
                lifecycle_counts=lifecycle_counts,
            ),
            compact_counts_csv,
        )
        write_compact_summary_csv(
            compact_summary_rows(
                load_counts=load_counts,
                static_summary=static_summary,
                severity_summary=severity_summary,
                osv_summary=osv_summary,
                cwe_1400_summary=cwe_1400_summary,
                top_cwe_summary=top_cwe_summary,
                lifecycle_summary=lifecycle_summary,
            ),
            compact_summary_csv,
        )
        write_compact_durations_csv(
            compact_duration_rows(lifecycle_durations),
            compact_durations_csv,
        )
        print(f"wrote_compact_counts_csv: {compact_counts_csv}")
        print(f"wrote_compact_summary_csv: {compact_summary_csv}")
        print(f"wrote_compact_durations_csv: {compact_durations_csv}")

    print_static_finding_role_status_summary(
        static_counts,
        commit_counts,
        static_summary,
    )

    print_static_finding_lifecycle_removal_outcome_summary(
        lifecycle_counts,
        lifecycle_summary,
    )

    print_static_finding_lifecycle_time_to_removal_summary(
        lifecycle_durations,
        lifecycle_duration_summary,
    )

    semgrep_severity_output_path = (
        args.output_dir
        / "static_findings_semgrep_severity_overview_by_role_per_100_commits.pdf"
    )
    osv_severity_output_path = (
        args.output_dir
        / "static_findings_osv_severity_overview_by_role_per_100_commits.pdf"
    )
    print_static_severity_overview_summary(
        semgrep_counts,
        osv_counts,
        commit_counts,
        severity_summary,
        osv_summary,
    )
    if not args.skip_plots:
        plot_static_findings_severity_by_role(
            semgrep_counts,
            commit_counts,
            SEMGREP_SEVERITY_ORDER,
            ROLE_SEMGREP_SEVERITY_COLORS,
            SEMGREP_SEVERITY_LEGEND_ORDER,
            SEMGREP_SEVERITY_GRAYS,
            "Semgrep Severity",
            semgrep_severity_output_path,
            y_axis_min=10,
        )
        print(f"wrote: {semgrep_severity_output_path}")
        plot_static_findings_severity_by_role(
            osv_counts,
            commit_counts,
            OSV_SEVERITY_ORDER,
            ROLE_OSV_SEVERITY_COLORS,
            OSV_SEVERITY_LEGEND_ORDER,
            OSV_SEVERITY_GRAYS,
            "OSV Severity",
            osv_severity_output_path,
            legend_location="upper_right",
            y_axis_min=18.5,
        )
        print(f"wrote: {osv_severity_output_path}")

    print_static_cwe_1400_category_summary(
        cwe_1400_counts,
        commit_counts,
        cwe_1400_summary,
    )

    print_static_top_cwe_summary(
        top_cwe_counts,
        commit_counts,
        top_cwe_summary,
    )


if __name__ == "__main__":
    main()
