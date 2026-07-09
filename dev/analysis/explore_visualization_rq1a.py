from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import math
import os
from collections import Counter, defaultdict
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
from matplotlib.patches import PathPatch, Rectangle
from matplotlib.path import Path as MplPath
from matplotlib.ticker import PercentFormatter

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:  # Allows `python -m dev.analysis.explore_visualization_rq1`.
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs


REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = REPO_ROOT / "font"

DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_rq1a_explore")
DEFAULT_ORIGIN_START = "2025-05"
DEFAULT_ORIGIN_END = "2026-05"
DEFAULT_OBSERVATION_END = "2026-05-31"
DEFAULT_MAX_TRACKED_LINES = 500_000

COMMIT_CLASSIFICATIONS_FILE = "commit_contribution_classifications.jsonl"
LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
RQ1_EXPLORE_INPUT_FILES = (
    COMMIT_CLASSIFICATIONS_FILE,
    LINE_LIFECYCLE_FILE,
)

SUMMARY_CSV_NAME = "rq1_origin_commit_maintenance_summary.csv"
FLOW_CSV_NAME = "rq1_origin_to_terminal_actor_flow.csv"

ROLE_ORDER = ("AI", "Human")
SANKEY_BAND_ROLE_ORDER = ("Human", "AI")
ROLE_LABELS = {"AI": "Agentic", "Human": "Human"}
ROLE_COLORS = {"AI": "#F58F29", "Human": "#8789C0"}
TERMINAL_ACTOR_ORDER = (
    "terminated_by_ai",
    "terminated_by_human",
    "terminated_by_bot",
    "survived",
    "terminated_by_unknown",
)
SANKEY_BAND_ACTOR_ORDER = (
    "terminated_by_ai",
    "terminated_by_human",
    "terminated_by_bot",
    "terminated_by_unknown",
    "survived",
)
TERMINAL_ACTOR_LABELS = {
    "survived": "Survived",
    "terminated_by_ai": "Terminated by Agentic commit",
    "terminated_by_human": "Terminated by Human commit",
    "terminated_by_bot": "Terminated by bot commit",
    "terminated_by_unknown": "Terminated by unknown commit",
}
TERMINAL_ACTOR_SHORT_LABELS = {
    "survived": "",
    "terminated_by_ai": "",
    "terminated_by_human": "",
    "terminated_by_bot": "",
    "terminated_by_unknown": "",
}
SANKEY_LEGEND_LABELS = {
    "terminated_by_ai": "Agentic",
    "terminated_by_human": "Human",
    "terminated_by_bot": "Bot",
    "survived": "Survived",
    "terminated_by_unknown": "Unknown",
}
TERMINAL_ACTOR_COLORS = {
    "survived": "#59A14F",
    "terminated_by_ai": "#F58F29",
    "terminated_by_human": "#8789C0",
    "terminated_by_bot": "#8C8C8C",
    "terminated_by_unknown": "#C7C7C7",
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
            "font.size": 15,
            "axes.titlesize": 17,
            "axes.labelsize": 16,
            "legend.fontsize": 14,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
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


def date_in_window(
    value: Any,
    *,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    parsed = parse_datetime(value)
    if parsed is None:
        return False
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


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


def primary_intent_from_classification(row: dict[str, Any]) -> str | None:
    value = row.get("primary_operational_intent")
    if isinstance(value, str) and value:
        return value
    return None


def maintenance_class_from_classification(row: dict[str, Any]) -> str | None:
    value = row.get("maintenance_class")
    if isinstance(value, str) and value:
        return value
    return None


def terminal_actor_from_category(category: Any) -> str:
    if category == "ai_coauthored":
        return "terminated_by_ai"
    if category == "not_ai_coauthored":
        return "terminated_by_human"
    if category == "other_bot":
        return "terminated_by_bot"
    return "terminated_by_unknown"


def read_commit_classifications(repo_dir: Path) -> dict[str, dict[str, str | None]]:
    classifications: dict[str, dict[str, str | None]] = {}
    for row in iter_jsonl(repo_dir / COMMIT_CLASSIFICATIONS_FILE):
        sha = row.get("sha")
        if not isinstance(sha, str) or not sha:
            continue
        classifications[sha] = {
            "primary_operational_intent": primary_intent_from_classification(row),
            "maintenance_class": maintenance_class_from_classification(row),
            "date": row.get("date") if isinstance(row.get("date"), str) else None,
        }
    return classifications


def new_commit_summary(
    *,
    repo_key: str,
    repo_name: str,
    origin_sha: str,
    origin_role: str,
    origin_category: str,
    origin_date: str,
    origin_classification: dict[str, str | None] | None,
) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "origin_commit_sha": origin_sha,
        "origin_role": origin_role,
        "origin_commit_category": origin_category,
        "origin_commit_date": origin_date,
        "origin_primary_operational_intent": (
            (origin_classification or {}).get("primary_operational_intent") or "unknown"
        ),
        "origin_maintenance_class": (
            (origin_classification or {}).get("maintenance_class") or "unknown"
        ),
        "tracked_line_count": 0,
        "survived_line_count": 0,
        "modified_line_count": 0,
        "deleted_line_count": 0,
        "terminated_line_count": 0,
        "terminated_by_ai_line_count": 0,
        "terminated_by_human_line_count": 0,
        "terminated_by_bot_line_count": 0,
        "terminated_by_unknown_line_count": 0,
        "first_termination_days": None,
    }


def finalize_commit_summary(summary: dict[str, Any]) -> dict[str, Any]:
    tracked = int(summary["tracked_line_count"])
    terminated = int(summary["terminated_line_count"])
    survived = int(summary["survived_line_count"])
    summary["survival_rate"] = survived / tracked if tracked else 0.0
    summary["termination_rate"] = terminated / tracked if tracked else 0.0
    summary["any_termination"] = 1 if terminated > 0 else 0
    return summary


def load_repo_rq1_explore_data(
    repo_dir: Path,
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    max_tracked_lines: int | None,
) -> dict[str, Any]:
    repo_key = repo_dir.name
    repo_name = repo_from_dir(repo_dir)
    commit_classifications = read_commit_classifications(repo_dir)

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
                origin_classification=commit_classifications.get(origin_sha),
            )
            summaries_by_sha[origin_sha] = summary

        summary["tracked_line_count"] += 1
        status = str(line.get("status") or "").lower()
        terminal_date = parse_datetime(line.get("terminal_commit_date"))
        is_event = terminal_date is not None and terminal_date <= observation_end

        if not is_event:
            summary["survived_line_count"] += 1
            continue

        summary["terminated_line_count"] += 1
        if status == "deleted":
            summary["deleted_line_count"] += 1
        else:
            summary["modified_line_count"] += 1

        days = duration_days(origin_date, terminal_date)
        if days is not None:
            current_first = summary.get("first_termination_days")
            if current_first is None or days < current_first:
                summary["first_termination_days"] = days

        terminal_actor = terminal_actor_from_category(line.get("terminal_commit_category"))
        summary[f"{terminal_actor}_line_count"] += 1

    summaries = [
        finalize_commit_summary(summary)
        for summary in summaries_by_sha.values()
        if max_tracked_lines is None
        or int(summary["tracked_line_count"]) < max_tracked_lines
    ]
    outlier_count = len(summaries_by_sha) - len(summaries)

    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "streamed_lifecycle_lines": streamed_lines,
        "origin_commit_summaries": summaries,
        "skipped": dict(skipped),
        "outlier_origin_commits": outlier_count,
        "commit_classification_count": len(commit_classifications),
    }


def rq1_explore_repo_data_summary(repo_dir: Path, repo_data: dict[str, Any]) -> str:
    return (
        f"intent_commits={int(repo_data.get('commit_classification_count') or 0):,} "
        f"lifecycle_lines_streamed={int(repo_data.get('streamed_lifecycle_lines') or 0):,} "
        f"origin_commit_summaries="
        f"{len(repo_data.get('origin_commit_summaries') or []):,} "
        f"outlier_origin_commits={int(repo_data.get('outlier_origin_commits') or 0):,}"
    )


def load_rq1_explore_data(
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
        required_files=RQ1_EXPLORE_INPUT_FILES,
    )
    if not repo_dirs:
        raise FileNotFoundError(f"No repo output directories found under {input_dir}")

    return load_repo_data_parallel(
        repo_dirs,
        load_repo_rq1_explore_data,
        workers=workers,
        default_worker_limit=4,
        task_label="RQ1 exploratory input data",
        logger=logger or LOG,
        result_summary=rq1_explore_repo_data_summary,
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


def flow_counts_from_summaries(
    summaries: list[dict[str, Any]],
) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for summary in summaries:
        role = str(summary.get("origin_role") or "")
        if role not in ROLE_ORDER:
            continue
        counts[(role, "survived")] += safe_int(summary.get("survived_line_count"))
        for terminal_actor in TERMINAL_ACTOR_ORDER:
            if terminal_actor == "survived":
                continue
            counts[(role, terminal_actor)] += safe_int(
                summary.get(f"{terminal_actor}_line_count")
            )
    return counts


SUMMARY_FIELDNAMES = [
    "repo_key",
    "repo",
    "origin_commit_sha",
    "origin_role",
    "origin_commit_category",
    "origin_commit_date",
    "origin_primary_operational_intent",
    "origin_maintenance_class",
    "tracked_line_count",
    "survived_line_count",
    "modified_line_count",
    "deleted_line_count",
    "terminated_line_count",
    "terminated_by_ai_line_count",
    "terminated_by_human_line_count",
    "terminated_by_bot_line_count",
    "terminated_by_unknown_line_count",
    "survival_rate",
    "termination_rate",
    "any_termination",
    "first_termination_days",
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
        "modified_line_count",
        "deleted_line_count",
        "terminated_line_count",
        "terminated_by_ai_line_count",
        "terminated_by_human_line_count",
        "terminated_by_bot_line_count",
        "terminated_by_unknown_line_count",
        "any_termination",
    }
    float_fields = {
        "survival_rate",
        "termination_rate",
        "first_termination_days",
    }
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


def write_flow_counts_csv(
    counts: Counter[tuple[str, str]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    role_totals = {
        role: sum(counts[(role, actor)] for actor in TERMINAL_ACTOR_ORDER)
        for role in ROLE_ORDER
    }
    actor_totals = {
        actor: sum(counts[(role, actor)] for role in ROLE_ORDER)
        for actor in TERMINAL_ACTOR_ORDER
    }
    grand_total = sum(role_totals.values())
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "origin_role",
                "terminal_actor",
                "terminal_actor_label",
                "line_count",
                "role_total_line_count",
                "terminal_actor_total_line_count",
                "grand_total_line_count",
                "percent_within_origin_role",
                "percent_within_terminal_actor",
                "percent_of_all_tracked_lines",
            ],
        )
        writer.writeheader()
        for role in ROLE_ORDER:
            role_total = role_totals[role]
            for actor in TERMINAL_ACTOR_ORDER:
                count = counts[(role, actor)]
                actor_total = actor_totals[actor]
                writer.writerow(
                    {
                        "origin_role": role,
                        "terminal_actor": actor,
                        "terminal_actor_label": TERMINAL_ACTOR_LABELS[actor],
                        "line_count": count,
                        "role_total_line_count": role_total,
                        "terminal_actor_total_line_count": actor_total,
                        "grand_total_line_count": grand_total,
                        "percent_within_origin_role": (
                            count / role_total if role_total else 0.0
                        ),
                        "percent_within_terminal_actor": (
                            count / actor_total if actor_total else 0.0
                        ),
                        "percent_of_all_tracked_lines": (
                            count / grand_total if grand_total else 0.0
                        ),
                    }
                )


def read_flow_counts_csv(path: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            role = row.get("origin_role")
            actor = row.get("terminal_actor")
            if role in ROLE_ORDER and actor in TERMINAL_ACTOR_ORDER:
                counts[(role, actor)] += safe_int(row.get("line_count"))
    return counts


def draw_empty_plot(output_path: Path, title: str, message: str) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.axis("off")
    ax.set_title(title)
    ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def sankey_node_positions(
    node_order: tuple[str, ...],
    totals: Counter[str],
    *,
    total_flow: int,
    flow_scale: float | None = None,
    y_top: float = 0.88,
    y_bottom: float = 0.12,
    gap: float = 0.035,
) -> dict[str, tuple[float, float]]:
    nodes = [node for node in node_order if totals[node] > 0]
    if not nodes or total_flow <= 0:
        return {}
    if flow_scale is None:
        available_height = y_top - y_bottom - gap * (len(nodes) - 1)
        flow_scale = available_height / total_flow
    current_top = y_top
    positions: dict[str, tuple[float, float]] = {}
    for node in nodes:
        height = totals[node] * flow_scale
        bottom = current_top - height
        positions[node] = (bottom, current_top)
        current_top = bottom - gap
    return positions


def draw_sankey_band(
    ax: Any,
    *,
    x_left: float,
    x_right: float,
    left_bottom: float,
    left_top: float,
    right_bottom: float,
    right_top: float,
    color: str,
    alpha: float = 0.48,
) -> None:
    curve = (x_right - x_left) * 0.42
    vertices = [
        (x_left, left_top),
        (x_left + curve, left_top),
        (x_right - curve, right_top),
        (x_right, right_top),
        (x_right, right_bottom),
        (x_right - curve, right_bottom),
        (x_left + curve, left_bottom),
        (x_left, left_bottom),
        (x_left, left_top),
    ]
    codes = [
        MplPath.MOVETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CLOSEPOLY,
    ]
    ax.add_patch(
        PathPatch(
            MplPath(vertices, codes),
            facecolor=color,
            edgecolor="none",
            alpha=alpha,
            zorder=1,
        )
    )


def compact_line_count(value: int) -> str:
    if value >= 1_000_000:
        formatted = f"{value / 1_000_000:.1f}M"
        return formatted.replace(".0M", "M")
    if value >= 1_000:
        formatted = f"{value / 1_000:.1f}K"
        return formatted.replace(".0K", "K")
    return str(value)


def draw_sankey_side_count(
    ax: Any,
    *,
    x: float,
    y: float,
    ha: str,
    count: int,
) -> None:
    if count <= 0:
        return
    ax.text(
        x,
        y,
        compact_line_count(count),
        ha=ha,
        va="center",
        fontsize=15.0,
        color="#222222",
        zorder=5,
    )


def plot_origin_to_terminal_actor_sankey(
    flows: Counter[tuple[str, str]],
    output_path: Path,
) -> None:
    total_flow = sum(flows.values())
    title = "RQ1: Origin Code Flow to Survival or Terminal Actor"
    if total_flow == 0:
        draw_empty_plot(output_path, title, "No line flow data available.")
        return

    origin_totals: Counter[str] = Counter()
    terminal_totals: Counter[str] = Counter()
    for (origin_role, terminal_actor), count in flows.items():
        origin_totals[origin_role] += count
        terminal_totals[terminal_actor] += count

    left_node_count = sum(1 for role in ROLE_ORDER if origin_totals[role] > 0)
    right_node_count = sum(
        1 for actor in TERMINAL_ACTOR_ORDER if terminal_totals[actor] > 0
    )
    y_top = 0.91
    y_bottom = 0.02
    gap = 0.03
    left_available = y_top - y_bottom - gap * max(left_node_count - 1, 0)
    right_available = y_top - y_bottom - gap * max(right_node_count - 1, 0)
    flow_scale = min(left_available, right_available) / total_flow

    left_positions = sankey_node_positions(
        ROLE_ORDER,
        origin_totals,
        total_flow=total_flow,
        flow_scale=flow_scale,
        y_top=y_top,
        y_bottom=y_bottom,
        gap=gap,
    )
    right_positions = sankey_node_positions(
        TERMINAL_ACTOR_ORDER,
        terminal_totals,
        total_flow=total_flow,
        flow_scale=flow_scale,
        y_top=y_top,
        y_bottom=y_bottom,
        gap=gap,
    )

    fig, ax = plt.subplots(figsize=(4, 5))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    x_left = 0.02
    x_right = 0.93
    node_width = 0.055
    left_cursor = {node: left_positions[node][0] for node in left_positions}
    right_cursor = {node: right_positions[node][0] for node in right_positions}

    for origin_role in SANKEY_BAND_ROLE_ORDER:
        for terminal_actor in SANKEY_BAND_ACTOR_ORDER:
            count = flows[(origin_role, terminal_actor)]
            if count <= 0:
                continue
            height = count * flow_scale
            left_bottom = left_cursor[origin_role]
            left_top = left_bottom + height
            left_cursor[origin_role] = left_top
            right_bottom = right_cursor[terminal_actor]
            right_top = right_bottom + height
            right_cursor[terminal_actor] = right_top
            draw_sankey_band(
                ax,
                x_left=x_left + node_width,
                x_right=x_right,
                left_bottom=left_bottom,
                left_top=left_top,
                right_bottom=right_bottom,
                right_top=right_top,
                color=ROLE_COLORS[origin_role],
            )

    for role, (bottom, top) in left_positions.items():
        ax.add_patch(
            Rectangle(
                (x_left, bottom),
                node_width,
                top - bottom,
                facecolor=ROLE_COLORS[role],
                edgecolor="white",
                linewidth=1,
                zorder=3,
            )
        )
        draw_sankey_side_count(
            ax,
            x=x_left + node_width + 0.012,
            y=(bottom + top) / 2,
            ha="left",
            count=origin_totals[role],
        )

    for actor, (bottom, top) in right_positions.items():
        ax.add_patch(
            Rectangle(
                (x_right, bottom),
                node_width,
                top - bottom,
                facecolor=TERMINAL_ACTOR_COLORS[actor],
                edgecolor="white",
                linewidth=1,
                zorder=3,
            )
        )
        draw_sankey_side_count(
            ax,
            x=x_right - 0.012,
            y=(bottom + top) / 2,
            ha="right",
            count=terminal_totals[actor],
        )
        short_label = TERMINAL_ACTOR_SHORT_LABELS[actor]
        if short_label:
            ax.text(
                x_right + node_width + 0.02,
                (bottom + top) / 2,
                short_label,
                ha="left",
                va="center",
                fontsize=8,
            )

    legend_actors = [
        actor for actor in TERMINAL_ACTOR_ORDER if terminal_totals[actor] > 0
    ]
    legend_handles = [
        Rectangle(
            (0, 0),
            1,
            1,
            facecolor=TERMINAL_ACTOR_COLORS[actor],
            edgecolor="none",
        )
        for actor in legend_actors
    ]
    ax.legend(
        legend_handles,
        [SANKEY_LEGEND_LABELS[actor] for actor in legend_actors],
        loc="upper center",
        bbox_to_anchor=(0.5, 1.01),
        ncols=max(len(legend_actors), 1),
        frameon=False,
        fontsize=14.0,
        handlelength=0.9,
        handletextpad=0.35,
        columnspacing=0.55,
        borderaxespad=0,
    )
    fig.subplots_adjust(left=0.01, right=0.99, top=0.96, bottom=0.05)
    fig.savefig(output_path, dpi=220, pad_inches=0.015)
    plt.close(fig)


def smoothed_density(
    values: list[float],
    *,
    grid_size: int = 101,
) -> tuple[list[float], list[float]]:
    if not values:
        return [], []
    clean_values = [min(1.0, max(0.0, value)) for value in values if math.isfinite(value)]
    if not clean_values:
        return [], []
    n = len(clean_values)
    mean = sum(clean_values) / n
    variance = sum((value - mean) ** 2 for value in clean_values) / max(n - 1, 1)
    std = math.sqrt(max(variance, 0.0))
    bandwidth = 1.06 * std * (n ** -0.2) if n > 1 and std > 0 else 0.045
    bandwidth = min(0.16, max(0.035, bandwidth))
    x_values = [index / (grid_size - 1) for index in range(grid_size)]
    normalizer = n * bandwidth * math.sqrt(2 * math.pi)
    density = [
        sum(
            math.exp(-0.5 * ((x_value - value) / bandwidth) ** 2)
            for value in clean_values
        )
        / normalizer
        for x_value in x_values
    ]
    return x_values, density


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def repo_short_name(repo: str) -> str:
    return repo.rsplit("/", 1)[-1] if "/" in repo else repo


def repo_label_font_size(label: str) -> float:
    target_chars = 13
    base_size = 13.0
    min_size = 8.5
    if len(label) <= target_chars:
        return base_size
    return max(min_size, base_size * target_chars / len(label))


def ridgeline_repo_series(
    summaries: list[dict[str, Any]],
    *,
    min_ai_commits: int,
    min_human_commits: int,
    max_repos: int | None,
    center_min: float,
    center_max: float,
) -> list[tuple[str, dict[str, list[float]]]]:
    repo_role_stats: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: {
            role: {
                "survival_rates": [],
                "tracked_lines": 0,
                "terminated_lines": 0,
            }
            for role in ROLE_ORDER
        }
    )
    for summary in summaries:
        role = str(summary.get("origin_role") or "")
        repo = str(summary.get("repo") or summary.get("repo_key") or "")
        if role not in ROLE_ORDER or not repo:
            continue
        tracked_lines = safe_int(summary.get("tracked_line_count"))
        if tracked_lines <= 0:
            continue
        stats = repo_role_stats[repo][role]
        stats["survival_rates"].append(safe_float(summary.get("survival_rate")))
        stats["tracked_lines"] += tracked_lines
        stats["terminated_lines"] += safe_int(summary.get("terminated_line_count"))

    def role_termination_rate(stats: dict[str, Any]) -> float:
        tracked_lines = safe_int(stats.get("tracked_lines"))
        if tracked_lines <= 0:
            return 0.0
        return safe_int(stats.get("terminated_lines")) / tracked_lines

    eligible: list[tuple[str, dict[str, list[float]], float, float]] = []
    for repo, stats_by_role in repo_role_stats.items():
        values_by_role = {
            role: list(stats_by_role[role]["survival_rates"]) for role in ROLE_ORDER
        }
        if len(values_by_role["AI"]) < min_ai_commits:
            continue
        if len(values_by_role["Human"]) < min_human_commits:
            continue
        medians_by_role = {
            role: median(values_by_role[role]) for role in ROLE_ORDER
        }
        if any(
            value <= center_min or value >= center_max
            for value in medians_by_role.values()
        ):
            continue
        eligible.append(
            (
                repo,
                values_by_role,
                role_termination_rate(stats_by_role["AI"]),
                role_termination_rate(stats_by_role["Human"]),
            )
        )
    eligible.sort(
        key=lambda item: (
            abs(item[2] - item[3]),
            len(item[1]["AI"]) + len(item[1]["Human"]),
            item[0],
        ),
        reverse=True,
    )
    if max_repos is not None and max_repos > 0:
        eligible = eligible[:max_repos]
    return [(repo, values) for repo, values, _ai_rate, _human_rate in eligible]


def plot_repo_survival_rate_ridgeline(
    summaries: list[dict[str, Any]],
    output_path: Path,
    *,
    min_ai_commits: int,
    min_human_commits: int,
    max_repos: int | None,
    center_min: float,
    center_max: float,
) -> None:
    series = ridgeline_repo_series(
        summaries,
        min_ai_commits=min_ai_commits,
        min_human_commits=min_human_commits,
        max_repos=max_repos,
        center_min=center_min,
        center_max=center_max,
    )
    title = "RQ1: Per-Commit Survival Rate Distribution by Repo"
    if not series:
        draw_empty_plot(
            output_path,
            title,
            "No repos pass the ridgeline commit-count and center filters.",
        )
        return

    fig, ax = plt.subplots(figsize=(4, 5))
    ridge_height = 0.74
    for index, (repo, values_by_role) in enumerate(series):
        y_base = len(series) - index - 1
        density_by_role: dict[str, tuple[list[float], list[float]]] = {}
        max_density = 0.0
        for role in ROLE_ORDER:
            x_values, density = smoothed_density(values_by_role[role])
            if not x_values:
                continue
            density_by_role[role] = (x_values, density)
            max_density = max(max_density, max(density, default=0.0))
        if max_density <= 0:
            continue
        for role in ROLE_ORDER:
            if role not in density_by_role:
                continue
            x_values, density = density_by_role[role]
            y_values = [
                y_base + (value / max_density) * ridge_height
                for value in density
            ]
            ax.fill_between(
                x_values,
                [y_base] * len(x_values),
                y_values,
                color=ROLE_COLORS[role],
                alpha=0.42,
                linewidth=0,
            )
            ax.plot(
                x_values,
                y_values,
                color=ROLE_COLORS[role],
                linewidth=1.2,
                label=ROLE_LABELS[role] if index == 0 else None,
            )

    ax.set_yticks(range(len(series)))
    ax.set_yticklabels(
        [repo_short_name(repo) for repo, _values in reversed(series)],
    )
    for tick_label in ax.get_yticklabels():
        tick_label.set_fontsize(repo_label_font_size(tick_label.get_text()))
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.04, max(len(series) - 1 + ridge_height + 0.04, 1))
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=1))
    ax.set_xlabel("Origin commit survival rate")
    ax.set_ylabel("")
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        fig.legend(
            handles,
            labels,
            loc="upper center",
            bbox_to_anchor=(0.58, 0.985),
            ncols=2,
            frameon=False,
            fontsize=15,
        )
    fig.subplots_adjust(left=0.34, right=0.94, top=0.92, bottom=0.12)
    fig.savefig(output_path, dpi=220)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build compact RQ1 exploratory maintenance CSVs and survival/"
            "terminal-actor visualizations."
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
        "--flow-csv",
        type=Path,
        help=(
            "Origin-to-terminal-actor flow CSV path. Defaults to "
            f"--output-dir/{FLOW_CSV_NAME}."
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
        "--ridgeline-min-ai-commits",
        type=int,
        default=1,
        help="Minimum Agentic commits required for a repo to appear in the ridgeline.",
    )
    parser.add_argument(
        "--ridgeline-min-human-commits",
        type=int,
        default=1,
        help="Minimum Human commits required for a repo to appear in the ridgeline.",
    )
    parser.add_argument(
        "--ridgeline-max-repos",
        type=int,
        default=10,
        help=(
            "Maximum repos to show in the ridgeline, selected by largest "
            "AI-vs-Human aggregate termination-rate gap. Use 0 for all "
            "eligible repos. Default: 10."
        ),
    )
    parser.add_argument(
        "--ridgeline-center-min",
        type=float,
        default=0.05,
        help=(
            "Exclude repos where either AI or Human median commit survival rate "
            "is at or below this value. Default: 0.05."
        ),
    )
    parser.add_argument(
        "--ridgeline-center-max",
        type=float,
        default=0.95,
        help=(
            "Exclude repos where either AI or Human median commit survival rate "
            "is at or above this value. Default: 0.95."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-repo loading progress.",
    )
    return parser.parse_args()


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
    flow_counts: Counter[tuple[str, str]],
) -> None:
    print(f"compact_origin_commits: {len(summaries):,}")
    for role in ROLE_ORDER:
        commit_count = sum(1 for row in summaries if row.get("origin_role") == role)
        tracked_lines = sum(
            safe_int(row.get("tracked_line_count"))
            for row in summaries
            if row.get("origin_role") == role
        )
        terminated_lines = sum(
            flow_counts[(role, actor)]
            for actor in TERMINAL_ACTOR_ORDER
            if actor != "survived"
        )
        survived_lines = flow_counts[(role, "survived")]
        print(
            f"{role}: commits={commit_count:,} tracked_lines={tracked_lines:,} "
            f"survived_lines={survived_lines:,} "
            f"terminated_lines={terminated_lines:,} "
            f"termination_rate={(terminated_lines / tracked_lines if tracked_lines else 0):.2%}"
        )


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
    if args.ridgeline_min_ai_commits < 0 or args.ridgeline_min_human_commits < 0:
        raise ValueError("ridgeline minimum commit counts must be non-negative")
    if not (0 <= args.ridgeline_center_min < args.ridgeline_center_max <= 1):
        raise ValueError(
            "--ridgeline-center-min and --ridgeline-center-max must satisfy "
            "0 <= min < max <= 1"
        )

    setup_plot_style()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = args.summary_csv or args.output_dir / SUMMARY_CSV_NAME
    flow_csv = args.flow_csv or args.output_dir / FLOW_CSV_NAME

    if args.use_compact_csvs:
        summaries = read_origin_commit_summary_csv(summary_csv)
        flow_counts = (
            read_flow_counts_csv(flow_csv)
            if flow_csv.exists()
            else flow_counts_from_summaries(summaries)
        )
        print(f"loaded_summary_csv: {summary_csv}")
        if flow_csv.exists():
            print(f"loaded_flow_csv: {flow_csv}")
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
        repo_data = load_rq1_explore_data(
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
        flow_counts = flow_counts_from_summaries(summaries)
        write_origin_commit_summary_csv(summaries, summary_csv)
        write_flow_counts_csv(flow_counts, flow_csv)
        print(f"wrote_summary_csv: {summary_csv}")
        print(f"wrote_flow_csv: {flow_csv}")

    print_compact_totals(summaries, flow_counts)

    if args.skip_plots:
        return

    sankey_plot = args.output_dir / "rq1a_origin_to_terminal_actor_sankey.pdf"
    ridgeline_plot = args.output_dir / "rq1a_repo_survival_rate_ridgeline.pdf"
    plot_origin_to_terminal_actor_sankey(flow_counts, sankey_plot)
    plot_repo_survival_rate_ridgeline(
        summaries,
        ridgeline_plot,
        min_ai_commits=args.ridgeline_min_ai_commits,
        min_human_commits=args.ridgeline_min_human_commits,
        max_repos=(
            args.ridgeline_max_repos if args.ridgeline_max_repos > 0 else None
        ),
        center_min=args.ridgeline_center_min,
        center_max=args.ridgeline_center_max,
    )
    print(f"wrote {sankey_plot}")
    print(f"wrote {ridgeline_plot}")


if __name__ == "__main__":
    main()
