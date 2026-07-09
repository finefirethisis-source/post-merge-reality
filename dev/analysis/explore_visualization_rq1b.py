from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import math
import os
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

DEFAULT_MPLCONFIGDIR = Path("/tmp/matplotlib-codex-cache")
os.environ.setdefault("MPLCONFIGDIR", str(DEFAULT_MPLCONFIGDIR))
DEFAULT_MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import to_hex, to_rgb
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator, PercentFormatter

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs


REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = REPO_ROOT / "font"

DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_rq1b_explore")
DEFAULT_ORIGIN_START = "2025-05"
DEFAULT_ORIGIN_END = "2026-05"
DEFAULT_OBSERVATION_END = "2026-05-31"
DEFAULT_MAX_TRACKED_LINES = 500_000

COMMIT_CLASSIFICATIONS_FILE = "commit_contribution_classifications.jsonl"
LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
RQ1B_EXPLORE_INPUT_FILES = (
    COMMIT_CLASSIFICATIONS_FILE,
    LINE_LIFECYCLE_FILE,
)

SUMMARY_CSV_NAME = "rq1b_origin_commit_terminal_maintenance_summary.csv"
LINE_COUNTS_CSV_NAME = "rq1b_line_terminal_maintenance_counts.csv"

ROLE_ORDER = ("AI", "Human")
ROLE_LABELS = {"AI": "Agentic", "Human": "Human"}
ROLE_COLORS = {"AI": "#F58F29", "Human": "#8789C0"}
RQ1B_DISTRIBUTION_FONT_SIZE = 13

MAINTENANCE_CLASS_ORDER = (
    "corrective",
    "adaptive",
    "perfective",
    "preventive",
    "management",
    "unknown",
    "mixed",
)
PLOTTED_MAINTENANCE_CLASS_ORDER = (
    "corrective",
    "adaptive",
    "perfective",
    "preventive",
    "management",
)
VALID_MAINTENANCE_CLASSES = set(MAINTENANCE_CLASS_ORDER)
MAINTENANCE_CLASS_LABELS = {
    "corrective": "Corrective",
    "adaptive": "Adaptive",
    "perfective": "Perfective",
    "preventive": "Preventive",
    "management": "Management",
    "unknown": "Unknown",
    "mixed": "Mixed",
}
MAINTENANCE_CLASS_TINTS = {
    "corrective": 0.00,
    "adaptive": 0.18,
    "perfective": 0.34,
    "preventive": 0.50,
    "management": 0.64,
}
MAINTENANCE_CLASS_HATCHES = {
    "corrective": "||",
    "adaptive": "//",
    "perfective": "\\\\",
    "preventive": "xx",
    "management": "..",
}

LOG = logging.getLogger(__name__)


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


def parse_time_bound(value: Any, *, is_end: bool) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "all", "open"}:
        return None

    if len(text) == 7 and text[4] == "-":
        try:
            year, month = map(int, text.split("-"))
        except ValueError:
            return None
        if is_end:
            last_day = calendar.monthrange(year, month)[1]
            return datetime.combine(
                datetime(year, month, last_day).date(),
                time(23, 59, 59, 999999),
                tzinfo=timezone.utc,
            )
        return datetime(year, month, 1, tzinfo=timezone.utc)

    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        suffix = "T23:59:59.999999+00:00" if is_end else "T00:00:00+00:00"
        return parse_datetime(f"{text}{suffix}")

    return parse_datetime(text)


def duration_days(start: datetime, end: datetime) -> float | None:
    days = (end - start).total_seconds() / 86_400
    if days < 0:
        return None
    return days


def safe_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(float(value))


def safe_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def origin_role_from_category(category: Any) -> str | None:
    if category == "ai_coauthored":
        return "AI"
    if category == "not_ai_coauthored":
        return "Human"
    return None


def clean_maintenance_class(value: Any) -> str:
    maintenance_class = str(value or "unknown").strip() or "unknown"
    if maintenance_class not in VALID_MAINTENANCE_CLASSES:
        return "unknown"
    return maintenance_class


def terminal_line_count_field(maintenance_class: str) -> str:
    return f"terminal_{maintenance_class}_line_count"


def terminal_commit_count_field(maintenance_class: str) -> str:
    return f"terminal_{maintenance_class}_commit_count"


def read_commit_maintenance_classes(repo_dir: Path) -> dict[str, str]:
    classifications: dict[str, str] = {}
    for row in iter_jsonl(repo_dir / COMMIT_CLASSIFICATIONS_FILE):
        sha = row.get("sha") or row.get("commit_sha")
        if not isinstance(sha, str) or not sha:
            continue
        classifications[sha] = clean_maintenance_class(
            row.get("maintenance_class") or row.get("commit_maintenance_class")
        )
    return classifications


def new_commit_summary(
    *,
    repo_key: str,
    repo_name: str,
    origin_sha: str,
    origin_role: str,
    origin_category: str,
    origin_date: str,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "repo_key": repo_key,
        "repo": repo_name,
        "origin_commit_sha": origin_sha,
        "origin_role": origin_role,
        "origin_commit_category": origin_category,
        "origin_commit_date": origin_date,
        "tracked_line_count": 0,
        "survived_line_count": 0,
        "terminated_line_count": 0,
        "first_termination_date": "",
        "first_termination_days": "",
        "first_terminal_commit_sha": "",
        "first_terminal_maintenance_class": "",
        "mode_terminal_maintenance_class": "",
        "_terminal_commit_class_by_key": {},
    }
    for maintenance_class in MAINTENANCE_CLASS_ORDER:
        summary[terminal_line_count_field(maintenance_class)] = 0
        summary[terminal_commit_count_field(maintenance_class)] = 0
    return summary


def update_first_termination(
    summary: dict[str, Any],
    *,
    terminal_date: datetime,
    terminal_date_text: Any,
    terminal_sha: Any,
    terminal_maintenance_class: str,
    origin_date: datetime,
) -> None:
    current_date = parse_datetime(summary.get("first_termination_date"))
    is_first = current_date is None or terminal_date < current_date
    is_tie = current_date is not None and terminal_date == current_date
    if not is_first and not is_tie:
        return

    days = duration_days(origin_date, terminal_date)
    if is_first:
        summary["first_termination_date"] = str(terminal_date_text or "")
        summary["first_termination_days"] = days if days is not None else ""
        summary["first_terminal_commit_sha"] = (
            terminal_sha if isinstance(terminal_sha, str) else ""
        )
        summary["first_terminal_maintenance_class"] = terminal_maintenance_class
        return

    current_class = str(summary.get("first_terminal_maintenance_class") or "")
    if current_class and current_class != terminal_maintenance_class:
        summary["first_terminal_maintenance_class"] = "mixed"
        summary["first_terminal_commit_sha"] = "mixed"


def finalize_commit_summary(summary: dict[str, Any]) -> dict[str, Any]:
    tracked = safe_int(summary.get("tracked_line_count"))
    terminated = safe_int(summary.get("terminated_line_count"))
    terminal_commit_class_by_key = summary.pop("_terminal_commit_class_by_key", {})
    terminal_commit_counts = Counter(
        clean_maintenance_class(value)
        for value in terminal_commit_class_by_key.values()
    )
    for maintenance_class in MAINTENANCE_CLASS_ORDER:
        summary[terminal_commit_count_field(maintenance_class)] = terminal_commit_counts[
            maintenance_class
        ]
    summary["any_termination"] = 1 if terminated > 0 else 0
    summary["termination_rate"] = terminated / tracked if tracked else 0.0
    if terminated == 0:
        summary["first_terminal_maintenance_class"] = ""
        summary["mode_terminal_maintenance_class"] = ""
    else:
        summary["mode_terminal_maintenance_class"] = choose_mode_terminal_class(
            summary
        )
    return summary


def choose_mode_terminal_class(summary: dict[str, Any]) -> str:
    class_counts = {
        maintenance_class: safe_int(
            summary.get(terminal_commit_count_field(maintenance_class))
        )
        for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER
    }
    max_count = max(class_counts.values(), default=0)
    if max_count <= 0:
        return ""
    tied_classes = {
        maintenance_class
        for maintenance_class, count in class_counts.items()
        if count == max_count
    }
    first_class = clean_maintenance_class(
        summary.get("first_terminal_maintenance_class")
    )
    if first_class in tied_classes:
        return first_class
    for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER:
        if maintenance_class in tied_classes:
            return maintenance_class
    return ""


def load_repo_rq1b_explore_data(
    repo_dir: Path,
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    max_tracked_lines: int | None,
) -> dict[str, Any]:
    repo_key = repo_dir.name
    repo_name = repo_from_dir(repo_dir)
    commit_maintenance_classes = read_commit_maintenance_classes(repo_dir)

    summaries_by_sha: dict[str, dict[str, Any]] = {}
    skipped: Counter[str] = Counter()
    streamed_lines = 0

    for line in iter_jsonl(repo_dir / LINE_LIFECYCLE_FILE):
        streamed_lines += 1
        origin_category = line.get("origin_commit_category")
        origin_role = origin_role_from_category(origin_category)
        if origin_role is None:
            skipped["unsupported_origin_role"] += 1
            continue

        origin_sha = line.get("origin_commit_sha")
        if not isinstance(origin_sha, str) or not origin_sha:
            skipped["missing_origin_commit_sha"] += 1
            continue

        origin_date_text = line.get("origin_commit_date")
        origin_date = parse_datetime(origin_date_text)
        if origin_date is None:
            skipped["missing_origin_date"] += 1
            continue
        if origin_start is not None and origin_date < origin_start:
            skipped["before_origin_window"] += 1
            continue
        if origin_end is not None and origin_date > origin_end:
            skipped["after_origin_window"] += 1
            continue
        if origin_date > observation_end:
            skipped["after_observation_end"] += 1
            continue

        summary = summaries_by_sha.get(origin_sha)
        if summary is None:
            summary = new_commit_summary(
                repo_key=repo_key,
                repo_name=repo_name,
                origin_sha=origin_sha,
                origin_role=origin_role,
                origin_category=str(origin_category),
                origin_date=str(origin_date_text),
            )
            summaries_by_sha[origin_sha] = summary

        summary["tracked_line_count"] += 1
        terminal_date_text = line.get("terminal_commit_date")
        terminal_date = parse_datetime(terminal_date_text)
        is_event = terminal_date is not None and terminal_date <= observation_end
        if not is_event:
            summary["survived_line_count"] += 1
            continue

        terminal_sha = line.get("terminal_commit_sha")
        terminal_maintenance_class = clean_maintenance_class(
            commit_maintenance_classes.get(terminal_sha)
            if isinstance(terminal_sha, str)
            else None
        )
        terminal_key = (
            terminal_sha
            if isinstance(terminal_sha, str) and terminal_sha
            else f"missing:{terminal_date_text}:{terminal_maintenance_class}"
        )
        summary["_terminal_commit_class_by_key"].setdefault(
            terminal_key,
            terminal_maintenance_class,
        )
        summary["terminated_line_count"] += 1
        summary[terminal_line_count_field(terminal_maintenance_class)] += 1
        update_first_termination(
            summary,
            terminal_date=terminal_date,
            terminal_date_text=terminal_date_text,
            terminal_sha=terminal_sha,
            terminal_maintenance_class=terminal_maintenance_class,
            origin_date=origin_date,
        )

    summaries = [
        finalize_commit_summary(summary)
        for summary in summaries_by_sha.values()
        if max_tracked_lines is None
        or safe_int(summary.get("tracked_line_count")) < max_tracked_lines
    ]
    outlier_count = len(summaries_by_sha) - len(summaries)

    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "streamed_lifecycle_lines": streamed_lines,
        "origin_commit_summaries": summaries,
        "skipped": dict(skipped),
        "outlier_origin_commits": outlier_count,
        "commit_classification_count": len(commit_maintenance_classes),
    }


def rq1b_explore_repo_data_summary(repo_dir: Path, repo_data: dict[str, Any]) -> str:
    return (
        f"intent_commits={int(repo_data.get('commit_classification_count') or 0):,} "
        f"lifecycle_lines_streamed={int(repo_data.get('streamed_lifecycle_lines') or 0):,} "
        f"origin_commit_summaries="
        f"{len(repo_data.get('origin_commit_summaries') or []):,} "
        f"outlier_origin_commits={int(repo_data.get('outlier_origin_commits') or 0):,}"
    )


def load_rq1b_explore_data(
    input_dir: Path = DEFAULT_INPUT_DIR,
    *,
    repos: list[str] | None = None,
    repo_file: Path | None = None,
    workers: int | None = None,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    max_tracked_lines: int | None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    repo_dirs = resolve_repo_dirs(
        input_dir,
        repo_file=repo_file,
        repos=repos or [],
        required_files=RQ1B_EXPLORE_INPUT_FILES,
    )
    if not repo_dirs:
        raise FileNotFoundError(f"No repo output directories found under {input_dir}")

    return load_repo_data_parallel(
        repo_dirs,
        load_repo_rq1b_explore_data,
        workers=workers,
        default_worker_limit=4,
        task_label="RQ1b exploratory input data",
        logger=logger or LOG,
        result_summary=rq1b_explore_repo_data_summary,
        preserve_order=True,
        origin_start=origin_start,
        origin_end=origin_end,
        observation_end=observation_end,
        max_tracked_lines=max_tracked_lines,
    )


def flatten_commit_summaries(repo_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo in repo_data:
        rows.extend(repo.get("origin_commit_summaries") or [])
    return rows


def line_counts_from_summaries(
    summaries: list[dict[str, Any]],
) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for summary in summaries:
        role = str(summary.get("origin_role") or "")
        if role not in ROLE_ORDER:
            continue
        for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER:
            counts[(role, maintenance_class)] += safe_int(
                summary.get(terminal_line_count_field(maintenance_class))
            )
    return counts


SUMMARY_FIELDNAMES = [
    "repo_key",
    "repo",
    "origin_commit_sha",
    "origin_role",
    "origin_commit_category",
    "origin_commit_date",
    "tracked_line_count",
    "survived_line_count",
    "terminated_line_count",
    "any_termination",
    "termination_rate",
    "first_termination_date",
    "first_termination_days",
    "first_terminal_commit_sha",
    "first_terminal_maintenance_class",
    "mode_terminal_maintenance_class",
] + [terminal_line_count_field(value) for value in MAINTENANCE_CLASS_ORDER]
SUMMARY_FIELDNAMES += [
    terminal_commit_count_field(value) for value in MAINTENANCE_CLASS_ORDER
]


def write_origin_commit_summary_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: row.get(field, "")
                    if row.get(field, "") is not None
                    else ""
                    for field in SUMMARY_FIELDNAMES
                }
            )


def read_origin_commit_summary_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    int_fields = {
        "tracked_line_count",
        "survived_line_count",
        "terminated_line_count",
        "any_termination",
        *{terminal_line_count_field(value) for value in MAINTENANCE_CLASS_ORDER},
        *{terminal_commit_count_field(value) for value in MAINTENANCE_CLASS_ORDER},
    }
    float_fields = {"termination_rate", "first_termination_days"}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row: dict[str, Any] = dict(raw)
            for field in int_fields:
                row[field] = safe_int(row.get(field))
            for field in float_fields:
                row[field] = safe_float(row.get(field))
            rows.append(row)
    return rows


def write_line_counts_csv(
    counts: Counter[tuple[str, str]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    role_totals = {
        role: sum(
            counts[(role, maintenance_class)]
            for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER
        )
        for role in ROLE_ORDER
    }
    class_totals = {
        maintenance_class: sum(counts[(role, maintenance_class)] for role in ROLE_ORDER)
        for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER
    }
    grand_total = sum(role_totals.values())
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "origin_role",
                "maintenance_class",
                "maintenance_class_label",
                "line_count",
                "role_total_line_count",
                "maintenance_class_total_line_count",
                "grand_total_line_count",
                "percent_within_origin_role",
                "percent_of_all_terminal_lines",
            ],
        )
        writer.writeheader()
        for role in ROLE_ORDER:
            role_total = role_totals[role]
            for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER:
                count = counts[(role, maintenance_class)]
                writer.writerow(
                    {
                        "origin_role": role,
                        "maintenance_class": maintenance_class,
                        "maintenance_class_label": MAINTENANCE_CLASS_LABELS[maintenance_class],
                        "line_count": count,
                        "role_total_line_count": role_total,
                        "maintenance_class_total_line_count": class_totals[maintenance_class],
                        "grand_total_line_count": grand_total,
                        "percent_within_origin_role": count / role_total if role_total else 0.0,
                        "percent_of_all_terminal_lines": count / grand_total if grand_total else 0.0,
                    }
                )


def read_line_counts_csv(path: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            role = row.get("origin_role")
            maintenance_class = row.get("maintenance_class")
            if role in ROLE_ORDER and maintenance_class in VALID_MAINTENANCE_CLASSES:
                counts[(role, maintenance_class)] += safe_int(row.get("line_count"))
    return counts


def draw_empty_plot(output_path: Path, title: str, message: str) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.axis("off")
    ax.set_title(title)
    ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)


def visible_maintenance_classes(counts: Counter[tuple[str, str]]) -> list[str]:
    return [
        maintenance_class
        for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER
        if sum(counts[(role, maintenance_class)] for role in ROLE_ORDER) > 0
    ]


def tinted_role_color(role: str, maintenance_class: str) -> str:
    base = to_rgb(ROLE_COLORS[role])
    tint = MAINTENANCE_CLASS_TINTS.get(maintenance_class, 0.0)
    mixed = tuple(channel * (1 - tint) + tint for channel in base)
    return to_hex(mixed)


def plot_line_terminal_maintenance_distribution(
    counts: Counter[tuple[str, str]],
    output_path: Path,
) -> None:
    classes = visible_maintenance_classes(counts)
    if not classes:
        draw_empty_plot(
            output_path,
            "RQ1b: Terminal Maintenance Class Distribution",
            "No terminal line maintenance classes available.",
        )
        return

    fig, ax = plt.subplots(figsize=(6.4, 2.05))
    y_positions = list(range(len(ROLE_ORDER)))
    bar_height = 0.44
    for role in ROLE_ORDER:
        total = sum(counts[(role, maintenance_class)] for maintenance_class in classes)
        left = 0.0
        y_position = y_positions[ROLE_ORDER.index(role)]
        for maintenance_class in classes:
            percentage = counts[(role, maintenance_class)] / total if total else 0.0
            ax.barh(
                y_position,
                percentage,
                left=left,
                height=bar_height,
                color=tinted_role_color(role, maintenance_class),
                edgecolor="white",
                linewidth=0.85,
                hatch=MAINTENANCE_CLASS_HATCHES.get(maintenance_class, ""),
                label=MAINTENANCE_CLASS_LABELS[maintenance_class]
                if role == ROLE_ORDER[0]
                else None,
            )
            if percentage >= 0.08:
                ax.text(
                    left + percentage / 2,
                    y_position,
                    f"{percentage * 100:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=RQ1B_DISTRIBUTION_FONT_SIZE,
                    color="black",
                )
            left += percentage

    ax.set_yticks(y_positions)
    ax.set_yticklabels([ROLE_LABELS[role] for role in ROLE_ORDER])
    ax.tick_params(axis="both", labelsize=RQ1B_DISTRIBUTION_FONT_SIZE)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.set_xlabel("Terminal line share")
    ax.xaxis.label.set_size(RQ1B_DISTRIBUTION_FONT_SIZE)
    ax.set_xlim(0, 1)
    ax.grid(False)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    legend_handles = [
        Patch(
            facecolor=tinted_role_color("AI", maintenance_class),
            edgecolor="white",
            hatch=MAINTENANCE_CLASS_HATCHES.get(maintenance_class, ""),
            label=MAINTENANCE_CLASS_LABELS[maintenance_class],
        )
        for maintenance_class in classes
    ]
    fig.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.98),
        ncols=min(len(classes), 5),
        frameon=False,
        columnspacing=0.55,
        handlelength=1.1,
        handletextpad=0.35,
        fontsize=RQ1B_DISTRIBUTION_FONT_SIZE,
    )
    fig.subplots_adjust(left=0.12, right=0.955, top=0.72, bottom=0.27)
    fig.savefig(output_path)
    plt.close(fig)


def print_loaded_totals(repo_data: list[dict[str, Any]]) -> None:
    print(f"loaded_repos: {len(repo_data):,}")
    print(
        "line_lifecycle_streamed: "
        f"{sum(int(repo.get('streamed_lifecycle_lines') or 0) for repo in repo_data):,}"
    )
    print(
        "origin_commit_summaries: "
        f"{sum(len(repo.get('origin_commit_summaries') or []) for repo in repo_data):,}"
    )
    print(
        "outlier_origin_commits: "
        f"{sum(int(repo.get('outlier_origin_commits') or 0) for repo in repo_data):,}"
    )
    print("line_lifecycle note: rows are streamed per repo and not retained in memory.")


def print_compact_totals(
    summaries: list[dict[str, Any]],
    line_counts: Counter[tuple[str, str]],
) -> None:
    print(f"compact_origin_commits: {len(summaries):,}")
    for role in ROLE_ORDER:
        commit_count = sum(1 for row in summaries if row.get("origin_role") == role)
        terminated_commits = sum(
            1
            for row in summaries
            if row.get("origin_role") == role
            and safe_int(row.get("terminated_line_count")) > 0
        )
        terminal_lines = sum(
            line_counts[(role, maintenance_class)]
            for maintenance_class in PLOTTED_MAINTENANCE_CLASS_ORDER
        )
        print(
            f"{role}: commits={commit_count:,} "
            f"terminated_commits={terminated_commits:,} "
            f"terminal_lines={terminal_lines:,}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build compact RQ1b exploratory maintenance-type CSVs and plots."
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
        help="Directory where compact CSVs and exploratory plots are written.",
    )
    parser.add_argument(
        "--summary-csv",
        type=Path,
        help=(
            "Origin-commit summary CSV path. Defaults to "
            f"--output-dir/{SUMMARY_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--line-counts-csv",
        type=Path,
        help=(
            "Line-level terminal maintenance class counts CSV path. Defaults "
            f"to --output-dir/{LINE_COUNTS_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--use-compact-csvs",
        action="store_true",
        help="Skip raw JSONL loading and plot from existing compact CSVs.",
    )
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Only write/load compact CSVs; do not generate plots.",
    )
    parser.add_argument(
        "--origin-start",
        default=DEFAULT_ORIGIN_START,
        help=(
            "Inclusive lower bound for origin commit dates. Accepts YYYY-MM, "
            "YYYY-MM-DD, ISO datetime, or none/all/open. "
            f"Default: {DEFAULT_ORIGIN_START}."
        ),
    )
    parser.add_argument(
        "--origin-end",
        default=DEFAULT_ORIGIN_END,
        help=(
            "Inclusive upper bound for origin commit dates. Accepts YYYY-MM, "
            "YYYY-MM-DD, ISO datetime, or none/all/open. "
            f"Default: {DEFAULT_ORIGIN_END}."
        ),
    )
    parser.add_argument(
        "--observation-end",
        default=DEFAULT_OBSERVATION_END,
        help=(
            "Lines terminated after this date are treated as survived through "
            f"the observation window. Default: {DEFAULT_OBSERVATION_END}."
        ),
    )
    parser.add_argument(
        "--max-tracked-lines",
        type=int,
        default=DEFAULT_MAX_TRACKED_LINES,
        help=(
            "Exclude origin commits with at least this many tracked lifecycle "
            "lines. Use 0 to disable. "
            f"Default: {DEFAULT_MAX_TRACKED_LINES:,}."
        ),
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
    if args.max_tracked_lines < 0:
        raise ValueError("--max-tracked-lines must be non-negative")

    setup_plot_style()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = args.summary_csv or args.output_dir / SUMMARY_CSV_NAME
    line_counts_csv = args.line_counts_csv or args.output_dir / LINE_COUNTS_CSV_NAME

    if args.use_compact_csvs:
        summaries = read_origin_commit_summary_csv(summary_csv)
        line_counts = (
            read_line_counts_csv(line_counts_csv)
            if line_counts_csv.exists()
            else line_counts_from_summaries(summaries)
        )
        print(f"loaded_summary_csv: {summary_csv}")
        if line_counts_csv.exists():
            print(f"loaded_line_counts_csv: {line_counts_csv}")
    else:
        origin_start = parse_time_bound(args.origin_start, is_end=False)
        origin_end = parse_time_bound(args.origin_end, is_end=True)
        observation_end = parse_time_bound(args.observation_end, is_end=True)
        if observation_end is None:
            raise ValueError(f"invalid --observation-end: {args.observation_end}")
        if origin_start is not None and origin_end is not None and origin_start > origin_end:
            raise ValueError("--origin-start must be <= --origin-end")
        max_tracked_lines = (
            args.max_tracked_lines if args.max_tracked_lines > 0 else None
        )

        print(
            "analysis_window: "
            f"origin_start={origin_start.isoformat() if origin_start else 'open'} "
            f"origin_end={origin_end.isoformat() if origin_end else 'open'} "
            f"observation_end={observation_end.isoformat()} "
            f"max_tracked_lines={max_tracked_lines if max_tracked_lines else 'disabled'}"
        )
        repo_data = load_rq1b_explore_data(
            args.input_dir,
            repos=split_csv(args.repo),
            repo_file=args.repo_file,
            workers=args.workers,
            origin_start=origin_start,
            origin_end=origin_end,
            observation_end=observation_end,
            max_tracked_lines=max_tracked_lines,
            logger=LOG,
        )
        print_loaded_totals(repo_data)
        summaries = flatten_commit_summaries(repo_data)
        line_counts = line_counts_from_summaries(summaries)
        write_origin_commit_summary_csv(summaries, summary_csv)
        write_line_counts_csv(line_counts, line_counts_csv)
        print(f"wrote_summary_csv: {summary_csv}")
        print(f"wrote_line_counts_csv: {line_counts_csv}")

    print_compact_totals(summaries, line_counts)

    if args.skip_plots:
        return

    line_distribution_plot = (
        args.output_dir / "rq1b_line_terminal_maintenance_distribution.pdf"
    )
    plot_line_terminal_maintenance_distribution(line_counts, line_distribution_plot)
    print(f"wrote {line_distribution_plot}")


if __name__ == "__main__":
    main()
