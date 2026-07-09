from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
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
import matplotlib.patheffects as path_effects
from matplotlib import font_manager
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator, PercentFormatter

try:
    from repo_loader import load_repo_data_parallel, resolve_repo_dirs
except ModuleNotFoundError:
    from dev.analysis.repo_loader import load_repo_data_parallel, resolve_repo_dirs


REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = REPO_ROOT / "font"

DEFAULT_INPUT_DIR = Path("src/webscrape/output")
DEFAULT_OUTPUT_DIR = Path("dev/analysis/output_rq1c_explore")
DEFAULT_ORIGIN_START = "2025-05"
DEFAULT_ORIGIN_END = "2026-05"
DEFAULT_OBSERVATION_END = "2026-05-31"
DEFAULT_MAX_TRACKED_LINES = 500_000

COMMIT_CLASSIFICATIONS_FILE = "commit_contribution_classifications.jsonl"
LINE_LIFECYCLE_FILE = "line_lifecycle.jsonl"
RQ1C_EXPLORE_INPUT_FILES = (
    COMMIT_CLASSIFICATIONS_FILE,
    LINE_LIFECYCLE_FILE,
)

COUNTS_CSV_NAME = "rq1c_line_corrective_terminal_intent_counts.csv"
BUG_FIX_ACTOR_CSV_NAME = "rq1c_bug_fix_terminal_actor_counts.csv"

ROLE_ORDER = ("AI", "Human")
ROLE_LABELS = {"AI": "Agentic", "Human": "Human"}
TERMINAL_ROLE_LABELS = {
    "AI": "Terminated by Agentic",
    "Human": "Terminated by Human",
}
ROLE_COLORS = {"AI": "#F58F29", "Human": "#8789C0"}

CORRECTIVE_PRIMARY_INTENTS = ("bug_fix", "security_fix", "revert")
INTENT_LABELS = {
    "bug_fix": "Bug fix",
    "security_fix": "Security fix",
    "revert": "Revert",
}
INTENT_SHADE_COLORS = {
    "AI": {
        "bug_fix": "#B85E0E",
        "security_fix": "#F58F29",
        "revert": "#FBCB8C",
    },
    "Human": {
        "bug_fix": "#55579A",
        "security_fix": "#8789C0",
        "revert": "#C4C5E3",
    },
}
INTENT_LEGEND_COLORS = {
    "bug_fix": "#525252",
    "security_fix": "#969696",
    "revert": "#d9d9d9",
}
INTENT_TEXT_COLORS = {
    "bug_fix": "white",
    "security_fix": "black",
    "revert": "black",
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
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def origin_role_from_category(category: Any) -> str | None:
    if category == "ai_coauthored":
        return "AI"
    if category == "not_ai_coauthored":
        return "Human"
    return None


def terminal_role_from_category(category: Any) -> str | None:
    if category == "ai_coauthored":
        return "AI"
    if category == "not_ai_coauthored":
        return "Human"
    return None


def primary_intent_from_classification(classification: dict[str, Any]) -> str | None:
    primary_intent = classification.get("primary_operational_intent")
    if isinstance(primary_intent, str) and primary_intent:
        return primary_intent
    return None


def read_commit_primary_intents(repo_dir: Path) -> dict[str, str | None]:
    intents: dict[str, str | None] = {}
    for row in iter_jsonl(repo_dir / COMMIT_CLASSIFICATIONS_FILE):
        sha = row.get("sha")
        if isinstance(sha, str) and sha:
            intents[sha] = primary_intent_from_classification(row)
    return intents


def line_origin_context(
    line: dict[str, Any],
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    skipped: Counter[str] | None = None,
) -> tuple[str, str] | None:
    origin_role = origin_role_from_category(line.get("origin_commit_category"))
    if origin_role is None:
        if skipped is not None:
            skipped["unsupported_origin_role"] += 1
        return None

    origin_sha = line.get("origin_commit_sha")
    if not isinstance(origin_sha, str) or not origin_sha:
        if skipped is not None:
            skipped["missing_origin_commit_sha"] += 1
        return None

    origin_date = parse_datetime(line.get("origin_commit_date"))
    if origin_date is None:
        if skipped is not None:
            skipped["missing_origin_date"] += 1
        return None
    if origin_start is not None and origin_date < origin_start:
        if skipped is not None:
            skipped["before_origin_window"] += 1
        return None
    if origin_end is not None and origin_date > origin_end:
        if skipped is not None:
            skipped["after_origin_window"] += 1
        return None
    if origin_date > observation_end:
        if skipped is not None:
            skipped["after_observation_end"] += 1
        return None

    return origin_role, origin_sha


def compute_outlier_origin_shas(
    repo_dir: Path,
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    max_tracked_lines: int | None,
) -> set[str]:
    if max_tracked_lines is None:
        return set()

    origin_line_counts: Counter[str] = Counter()
    for line in iter_jsonl(repo_dir / LINE_LIFECYCLE_FILE):
        context = line_origin_context(
            line,
            origin_start=origin_start,
            origin_end=origin_end,
            observation_end=observation_end,
        )
        if context is None:
            continue
        _origin_role, origin_sha = context
        origin_line_counts[origin_sha] += 1

    return {
        origin_sha
        for origin_sha, tracked_lines in origin_line_counts.items()
        if tracked_lines >= max_tracked_lines
    }


def load_repo_rq1c_explore_data(
    repo_dir: Path,
    *,
    origin_start: datetime | None,
    origin_end: datetime | None,
    observation_end: datetime,
    max_tracked_lines: int | None,
) -> dict[str, Any]:
    repo_key = repo_dir.name
    repo_name = repo_from_dir(repo_dir)
    commit_primary_intents = read_commit_primary_intents(repo_dir)
    outlier_origin_shas = compute_outlier_origin_shas(
        repo_dir,
        origin_start=origin_start,
        origin_end=origin_end,
        observation_end=observation_end,
        max_tracked_lines=max_tracked_lines,
    )

    tracked_totals: Counter[str] = Counter()
    corrective_counts: Counter[tuple[str, str]] = Counter()
    bug_fix_actor_counts: Counter[tuple[str, str]] = Counter()
    skipped: Counter[str] = Counter()
    streamed_lines = 0

    for line in iter_jsonl(repo_dir / LINE_LIFECYCLE_FILE):
        streamed_lines += 1
        context = line_origin_context(
            line,
            origin_start=origin_start,
            origin_end=origin_end,
            observation_end=observation_end,
            skipped=skipped,
        )
        if context is None:
            continue
        origin_role, origin_sha = context
        if origin_sha in outlier_origin_shas:
            skipped["outlier_origin_commit"] += 1
            continue

        tracked_totals[origin_role] += 1

        terminal_sha = line.get("terminal_commit_sha")
        if not isinstance(terminal_sha, str) or not terminal_sha:
            skipped["no_terminal_commit"] += 1
            continue

        terminal_date = parse_datetime(line.get("terminal_commit_date"))
        if terminal_date is None:
            skipped["missing_terminal_date"] += 1
            continue
        if terminal_date > observation_end:
            skipped["terminal_after_observation_end"] += 1
            continue

        if terminal_sha not in commit_primary_intents:
            skipped["missing_terminal_classification"] += 1
            continue

        terminal_intent = commit_primary_intents.get(terminal_sha)
        if terminal_intent in CORRECTIVE_PRIMARY_INTENTS:
            corrective_counts[(origin_role, str(terminal_intent))] += 1
            if terminal_intent == "bug_fix":
                terminal_role = terminal_role_from_category(
                    line.get("terminal_commit_category")
                )
                if terminal_role in ROLE_ORDER:
                    bug_fix_actor_counts[(origin_role, terminal_role)] += 1
                else:
                    skipped["bug_fix_non_ai_human_terminal_actor"] += 1
        elif terminal_intent == "mixed":
            skipped["mixed_terminal_primary_intent"] += 1
        elif terminal_intent in (None, "", "unknown"):
            skipped["missing_or_unknown_terminal_primary_intent"] += 1
        else:
            skipped["non_corrective_primary_intent"] += 1

    return {
        "repo_key": repo_key,
        "repo": repo_name,
        "streamed_lifecycle_lines": streamed_lines,
        "tracked_totals": dict(tracked_totals),
        "corrective_counts": {
            f"{role}|{intent}": count
            for (role, intent), count in corrective_counts.items()
        },
        "bug_fix_actor_counts": {
            f"{origin_role}|{terminal_role}": count
            for (origin_role, terminal_role), count in bug_fix_actor_counts.items()
        },
        "skipped": dict(skipped),
        "outlier_origin_commits": len(outlier_origin_shas),
        "commit_classification_count": len(commit_primary_intents),
    }


def rq1c_explore_repo_data_summary(repo_dir: Path, repo_data: dict[str, Any]) -> str:
    tracked_total = sum(int(value) for value in (repo_data.get("tracked_totals") or {}).values())
    corrective_total = sum(
        int(value) for value in (repo_data.get("corrective_counts") or {}).values()
    )
    return (
        f"intent_commits={int(repo_data.get('commit_classification_count') or 0):,} "
        f"lifecycle_lines_streamed={int(repo_data.get('streamed_lifecycle_lines') or 0):,} "
        f"tracked_lines={tracked_total:,} "
        f"corrective_terminal_lines={corrective_total:,} "
        f"outlier_origin_commits={int(repo_data.get('outlier_origin_commits') or 0):,}"
    )


def load_rq1c_explore_data(
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
        required_files=RQ1C_EXPLORE_INPUT_FILES,
    )
    if not repo_dirs:
        raise FileNotFoundError(f"No repo output directories found under {input_dir}")

    return load_repo_data_parallel(
        repo_dirs,
        load_repo_rq1c_explore_data,
        workers=workers,
        default_worker_limit=4,
        task_label="RQ1c exploratory input data",
        logger=logger or LOG,
        result_summary=rq1c_explore_repo_data_summary,
        preserve_order=True,
        origin_start=origin_start,
        origin_end=origin_end,
        observation_end=observation_end,
        max_tracked_lines=max_tracked_lines,
    )


def merge_repo_counts(
    repo_data: list[dict[str, Any]],
) -> tuple[
    Counter[str],
    Counter[tuple[str, str]],
    Counter[tuple[str, str]],
    Counter[str],
]:
    tracked_totals: Counter[str] = Counter()
    corrective_counts: Counter[tuple[str, str]] = Counter()
    bug_fix_actor_counts: Counter[tuple[str, str]] = Counter()
    skipped: Counter[str] = Counter()

    for repo in repo_data:
        for role, value in (repo.get("tracked_totals") or {}).items():
            tracked_totals[str(role)] += int(value)
        for key, value in (repo.get("corrective_counts") or {}).items():
            role, intent = str(key).split("|", 1)
            corrective_counts[(role, intent)] += int(value)
        for key, value in (repo.get("bug_fix_actor_counts") or {}).items():
            origin_role, terminal_role = str(key).split("|", 1)
            bug_fix_actor_counts[(origin_role, terminal_role)] += int(value)
        skipped.update(repo.get("skipped") or {})

    return tracked_totals, corrective_counts, bug_fix_actor_counts, skipped


def counts_csv_rows(
    tracked_totals: Counter[str],
    corrective_counts: Counter[tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role in ROLE_ORDER:
        tracked_line_count = tracked_totals[role]
        total_corrective_line_count = sum(
            corrective_counts[(role, intent)] for intent in CORRECTIVE_PRIMARY_INTENTS
        )
        for intent in CORRECTIVE_PRIMARY_INTENTS:
            corrective_line_count = corrective_counts[(role, intent)]
            rows.append(
                {
                    "origin_role": role,
                    "terminal_primary_operational_intent": intent,
                    "corrective_line_count": corrective_line_count,
                    "total_corrective_line_count": total_corrective_line_count,
                    "tracked_line_count": tracked_line_count,
                    "share_of_corrective_lines": (
                        corrective_line_count / total_corrective_line_count
                        if total_corrective_line_count
                        else 0.0
                    ),
                    "rate_per_1000_tracked_lines": (
                        corrective_line_count / tracked_line_count * 1000
                        if tracked_line_count
                        else 0.0
                    ),
                }
            )
    return rows


def write_counts_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "origin_role",
        "terminal_primary_operational_intent",
        "corrective_line_count",
        "total_corrective_line_count",
        "tracked_line_count",
        "share_of_corrective_lines",
        "rate_per_1000_tracked_lines",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def bug_fix_actor_csv_rows(
    bug_fix_actor_counts: Counter[tuple[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for origin_role in ROLE_ORDER:
        total_bug_fix_ai_human_actor_lines = sum(
            bug_fix_actor_counts[(origin_role, terminal_role)]
            for terminal_role in ROLE_ORDER
        )
        for terminal_role in ROLE_ORDER:
            line_count = bug_fix_actor_counts[(origin_role, terminal_role)]
            rows.append(
                {
                    "origin_role": origin_role,
                    "terminal_role": terminal_role,
                    "line_count": line_count,
                    "total_bug_fix_ai_human_actor_lines": (
                        total_bug_fix_ai_human_actor_lines
                    ),
                    "share": (
                        line_count / total_bug_fix_ai_human_actor_lines
                        if total_bug_fix_ai_human_actor_lines
                        else 0.0
                    ),
                }
            )
    return rows


def write_bug_fix_actor_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "origin_role",
        "terminal_role",
        "line_count",
        "total_bug_fix_ai_human_actor_lines",
        "share",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_counts_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "origin_role": row.get("origin_role") or "",
                    "terminal_primary_operational_intent": (
                        row.get("terminal_primary_operational_intent") or ""
                    ),
                    "corrective_line_count": safe_int(row.get("corrective_line_count")),
                    "total_corrective_line_count": safe_int(
                        row.get("total_corrective_line_count")
                    ),
                    "tracked_line_count": safe_int(row.get("tracked_line_count")),
                    "share_of_corrective_lines": safe_float(
                        row.get("share_of_corrective_lines")
                    ),
                    "rate_per_1000_tracked_lines": safe_float(
                        row.get("rate_per_1000_tracked_lines")
                    ),
                }
            )
    return rows


def read_bug_fix_actor_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(
                {
                    "origin_role": row.get("origin_role") or "",
                    "terminal_role": row.get("terminal_role") or "",
                    "line_count": safe_int(row.get("line_count")),
                    "total_bug_fix_ai_human_actor_lines": safe_int(
                        row.get("total_bug_fix_ai_human_actor_lines")
                    ),
                    "share": safe_float(row.get("share")),
                }
            )
    return rows


def row_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (
            str(row.get("origin_role") or ""),
            str(row.get("terminal_primary_operational_intent") or ""),
        ): row
        for row in rows
    }


def actor_row_lookup(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (
            str(row.get("origin_role") or ""),
            str(row.get("terminal_role") or ""),
        ): row
        for row in rows
    }


def draw_empty_plot(output_path: Path, title: str, message: str) -> None:
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.axis("off")
    ax.set_title(title)
    ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)


def has_corrective_data(rows: list[dict[str, Any]]) -> bool:
    return any(safe_int(row.get("corrective_line_count")) > 0 for row in rows)


def plot_corrective_intent_distribution(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    if not has_corrective_data(rows):
        draw_empty_plot(
            output_path,
            "RQ1c: Corrective Terminal Intent Distribution",
            "No corrective terminal line intents available.",
        )
        return

    lookup = row_lookup(rows)
    fig, ax = plt.subplots(figsize=(4.4, 3.2))
    x_positions = list(range(len(ROLE_ORDER)))
    bar_width = 0.58

    for role_index, role in enumerate(ROLE_ORDER):
        total = sum(
            safe_int(lookup.get((role, intent), {}).get("corrective_line_count"))
            for intent in CORRECTIVE_PRIMARY_INTENTS
        )
        bottom = 0.0
        for intent in CORRECTIVE_PRIMARY_INTENTS:
            count = safe_int(
                lookup.get((role, intent), {}).get("corrective_line_count")
            )
            share = count / total if total else 0.0
            if share <= 0:
                continue
            ax.bar(
                role_index,
                share,
                bottom=bottom,
                width=bar_width,
                color=INTENT_SHADE_COLORS[role][intent],
                edgecolor="white",
                linewidth=0.8,
            )
            if share >= 0.04:
                ax.text(
                    role_index,
                    bottom + share / 2,
                    f"{share * 100:.0f}%",
                    ha="center",
                    va="center",
                    fontsize=18,
                    color=INTENT_TEXT_COLORS[intent],
                )
            bottom += share

    ax.set_xticks(x_positions)
    ax.set_xticklabels([ROLE_LABELS[role] for role in ROLE_ORDER], fontsize=18)
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=1, decimals=0))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
    ax.set_ylabel("Corrective termination lines", labelpad=2)
    ax.set_xlim(-0.55, len(ROLE_ORDER) - 0.45)
    ax.set_ylim(0, 1)
    ax.margins(x=0.01)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    legend_handles = [
        Patch(
            facecolor=INTENT_LEGEND_COLORS[intent],
            edgecolor="none",
            label=INTENT_LABELS[intent],
        )
        for intent in CORRECTIVE_PRIMARY_INTENTS
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.22),
        ncols=3,
        frameon=False,
        borderaxespad=0.2,
        columnspacing=0.7,
        handlelength=1.5,
        handleheight=1.0,
        handletextpad=0.35,
        fontsize=13,
    )
    fig.subplots_adjust(left=0.15, right=0.98, top=0.86, bottom=0.12)
    fig.savefig(output_path)
    plt.close(fig)


def plot_corrective_intent_rates(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    if not has_corrective_data(rows):
        draw_empty_plot(
            output_path,
            "RQ1c: Corrective Terminal Intent Rates",
            "No corrective terminal line intents available.",
        )
        return

    lookup = row_lookup(rows)
    fig, ax = plt.subplots(figsize=(5, 3.6))
    x_positions = list(range(len(CORRECTIVE_PRIMARY_INTENTS)))
    offsets = {"AI": -0.12, "Human": 0.12}
    max_y = 0.0

    for index, intent in enumerate(CORRECTIVE_PRIMARY_INTENTS):
        role_values = [
            safe_float(
                lookup.get((role, intent), {}).get("rate_per_1000_tracked_lines")
            )
            for role in ROLE_ORDER
        ]
        max_y = max(max_y, max(role_values, default=0.0))
        ax.plot(
            [index + offsets["AI"], index + offsets["Human"]],
            role_values,
            color="#B8B8B8",
            linewidth=1.1,
            zorder=1,
        )

    for role in ROLE_ORDER:
        values = [
            safe_float(
                lookup.get((role, intent), {}).get("rate_per_1000_tracked_lines")
            )
            for intent in CORRECTIVE_PRIMARY_INTENTS
        ]
        ax.scatter(
            [position + offsets[role] for position in x_positions],
            values,
            s=72,
            color=ROLE_COLORS[role],
            edgecolor="white",
            linewidth=0.8,
            label=ROLE_LABELS[role],
            zorder=2,
        )

    ax.set_xticks(x_positions)
    ax.set_xticklabels(
        [INTENT_LABELS[intent] for intent in CORRECTIVE_PRIMARY_INTENTS],
        rotation=30,
        ha="right",
    )
    ax.set_ylabel("Lines per 1,000 tracked lines")
    ax.set_ylim(0, max_y * 1.15 + 0.01)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.7, alpha=0.55)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(
        loc="lower center",
        bbox_to_anchor=(0.5, 1.02),
        ncols=2,
        frameon=False,
        borderaxespad=0.2,
        columnspacing=0.9,
        handlelength=1.1,
        handletextpad=0.35,
        fontsize=13,
    )
    fig.subplots_adjust(left=0.21, right=0.99, top=0.86, bottom=0.23)
    fig.savefig(output_path)
    plt.close(fig)


def plot_bug_fix_terminal_actor_donuts(
    rows: list[dict[str, Any]],
    output_path: Path,
) -> None:
    if not any(safe_int(row.get("line_count")) > 0 for row in rows):
        draw_empty_plot(
            output_path,
            "RQ1c: Bug-Fix Terminal Actors",
            "No Agentic/Human-authored bug-fix terminal actors available.",
        )
        return

    lookup = actor_row_lookup(rows)
    fig, axes = plt.subplots(1, len(ROLE_ORDER), figsize=(5, 3.6))
    if len(ROLE_ORDER) == 1:
        axes = [axes]

    def autopct(value: float) -> str:
        return f"{value:.0f}%" if value >= 4 else ""

    for ax, origin_role in zip(axes, ROLE_ORDER):
        values = [
            safe_int(lookup.get((origin_role, terminal_role), {}).get("line_count"))
            for terminal_role in ROLE_ORDER
        ]
        total = sum(values)
        ax.set_aspect("equal")
        if total <= 0:
            ax.axis("off")
            ax.text(0.5, 0.5, "No data", ha="center", va="center")
            ax.set_title(f"{ROLE_LABELS[origin_role]}\nbug-fix only", pad=4, fontsize=18)
            continue
        wedges, _texts, autotexts = ax.pie(
            values,
            colors=[ROLE_COLORS[terminal_role] for terminal_role in ROLE_ORDER],
            explode=[0.08, 0.0],
            radius=1.18,
            startangle=90,
            counterclock=False,
            autopct=autopct,
            pctdistance=0.72,
            wedgeprops={
                "edgecolor": "white",
                "linewidth": 1.0,
            },
            textprops={"fontsize": 24, "color": "black"},
        )
        if wedges:
            wedges[0].set_path_effects(
                [
                    path_effects.withSimplePatchShadow(
                        offset=(2.0, -2.0),
                        shadow_rgbFace=(0, 0, 0),
                        alpha=0.28,
                    ),
                    path_effects.Normal(),
                ]
            )
        for text in autotexts:
            text.set_fontsize(24)
        ax.set_title(
            f"{ROLE_LABELS[origin_role]}\nbug-fix only, n={total:,}",
            pad=4,
            fontsize=18,
        )

    handles = [
        plt.Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor=ROLE_COLORS[terminal_role],
            markeredgecolor="none",
            markersize=13,
        )
        for terminal_role in ROLE_ORDER
    ]
    fig.legend(
        handles,
        [TERMINAL_ROLE_LABELS[terminal_role] for terminal_role in ROLE_ORDER],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.02),
        ncols=2,
        frameon=False,
        fontsize=14,
        handletextpad=0.35,
        columnspacing=0.9,
    )
    fig.subplots_adjust(left=0.02, right=0.98, top=0.88, bottom=0.18, wspace=0.06)
    fig.savefig(output_path)
    plt.close(fig)


def print_loaded_totals(repo_data: list[dict[str, Any]]) -> None:
    print(f"loaded_repos: {len(repo_data):,}")
    print(
        "line_lifecycle_streamed: "
        f"{sum(int(repo.get('streamed_lifecycle_lines') or 0) for repo in repo_data):,}"
    )
    print(
        "outlier_origin_commits: "
        f"{sum(int(repo.get('outlier_origin_commits') or 0) for repo in repo_data):,}"
    )
    print("line_lifecycle note: rows are streamed per repo and not retained in memory.")


def print_compact_totals(rows: list[dict[str, Any]], skipped: Counter[str] | None = None) -> None:
    lookup = row_lookup(rows)
    print("rq1c_line_corrective_terminal_intent_counts:")
    for role in ROLE_ORDER:
        tracked = max(
            safe_int(lookup.get((role, intent), {}).get("tracked_line_count"))
            for intent in CORRECTIVE_PRIMARY_INTENTS
        )
        corrective = max(
            safe_int(
                lookup.get((role, intent), {}).get("total_corrective_line_count")
            )
            for intent in CORRECTIVE_PRIMARY_INTENTS
        )
        print(
            f"  {role}: tracked_lines={tracked:,} "
            f"corrective_terminal_lines={corrective:,}"
        )
        for intent in CORRECTIVE_PRIMARY_INTENTS:
            row = lookup.get((role, intent), {})
            print(
                "    "
                f"{intent}: lines={safe_int(row.get('corrective_line_count')):,} "
                f"share={safe_float(row.get('share_of_corrective_lines')):.2%} "
                f"rate_per_1000={safe_float(row.get('rate_per_1000_tracked_lines')):.4f}"
            )
    if skipped:
        print("skipped_line_reasons:")
        for reason, count in skipped.most_common():
            print(f"  {reason}: {count:,}")


def print_bug_fix_actor_totals(rows: list[dict[str, Any]]) -> None:
    lookup = actor_row_lookup(rows)
    print("rq1c_bug_fix_terminal_actor_counts:")
    for origin_role in ROLE_ORDER:
        total = max(
            safe_int(
                lookup.get((origin_role, terminal_role), {}).get(
                    "total_bug_fix_ai_human_actor_lines"
                )
            )
            for terminal_role in ROLE_ORDER
        )
        print(f"  {origin_role}: ai_human_actor_bug_fix_lines={total:,}")
        for terminal_role in ROLE_ORDER:
            row = lookup.get((origin_role, terminal_role), {})
            print(
                "    "
                f"{terminal_role}: lines={safe_int(row.get('line_count')):,} "
                f"share={safe_float(row.get('share')):.2%}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build compact RQ1c exploratory defect-resolution CSVs and plots."
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
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for generated CSVs and plots. Default: {DEFAULT_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--counts-csv",
        type=Path,
        help=f"Optional compact counts CSV path. Default: output-dir/{COUNTS_CSV_NAME}.",
    )
    parser.add_argument(
        "--bug-fix-actor-csv",
        type=Path,
        help=(
            "Optional compact bug-fix terminal actor CSV path. "
            f"Default: output-dir/{BUG_FIX_ACTOR_CSV_NAME}."
        ),
    )
    parser.add_argument(
        "--use-compact-csv",
        action="store_true",
        help="Skip JSONL loading and regenerate plots from the compact counts CSV.",
    )
    parser.add_argument(
        "--skip-plots",
        action="store_true",
        help="Only export/print compact counts; do not draw plots.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Number of worker processes for per-repo loading.",
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
    counts_csv = args.counts_csv or args.output_dir / COUNTS_CSV_NAME
    bug_fix_actor_csv = (
        args.bug_fix_actor_csv or args.output_dir / BUG_FIX_ACTOR_CSV_NAME
    )
    skipped: Counter[str] | None = None

    if args.use_compact_csv:
        rows = read_counts_csv(counts_csv)
        print(f"loaded_counts_csv: {counts_csv}")
        if bug_fix_actor_csv.exists():
            bug_fix_actor_rows = read_bug_fix_actor_csv(bug_fix_actor_csv)
            print(f"loaded_bug_fix_actor_csv: {bug_fix_actor_csv}")
        else:
            bug_fix_actor_rows = []
            print(f"missing_bug_fix_actor_csv: {bug_fix_actor_csv}")
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
        repo_data = load_rq1c_explore_data(
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
        tracked_totals, corrective_counts, bug_fix_actor_counts, skipped = (
            merge_repo_counts(repo_data)
        )
        rows = counts_csv_rows(tracked_totals, corrective_counts)
        bug_fix_actor_rows = bug_fix_actor_csv_rows(bug_fix_actor_counts)
        write_counts_csv(rows, counts_csv)
        write_bug_fix_actor_csv(bug_fix_actor_rows, bug_fix_actor_csv)
        print(f"wrote_counts_csv: {counts_csv}")
        print(f"wrote_bug_fix_actor_csv: {bug_fix_actor_csv}")

    print_compact_totals(rows, skipped)
    print_bug_fix_actor_totals(bug_fix_actor_rows)

    if args.skip_plots:
        return

    distribution_plot = (
        args.output_dir / "rq1c_line_corrective_terminal_intent_distribution.pdf"
    )
    bug_fix_actor_plot = args.output_dir / "rq1c_bug_fix_terminal_actor_donut.pdf"
    plot_corrective_intent_distribution(rows, distribution_plot)
    plot_bug_fix_terminal_actor_donuts(bug_fix_actor_rows, bug_fix_actor_plot)
    print(f"wrote {distribution_plot}")
    print(f"wrote {bug_fix_actor_plot}")


if __name__ == "__main__":
    main()
