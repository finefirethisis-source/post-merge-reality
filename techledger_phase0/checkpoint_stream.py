from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

OUTPUT = Path("techledger_phase0/checkpoint_summary.json")
ROOTS = [Path("."), Path("dev/analysis")]

IDENTIFIER_TOKENS = (
    "origin_commit_sha",
    "commit_sha",
    "sha",
    "repo",
    "repo_key",
    "repo_slug",
)
OUTCOME_TOKENS = (
    "terminal",
    "termination",
    "survival",
    "corrective",
    "adaptive",
    "perfective",
    "preventive",
    "maintenance",
    "lifecycle",
    "duration",
    "hazard",
    "outcome",
)
POTENTIAL_T0_TOKENS = (
    "author",
    "actor",
    "role",
    "date",
    "intent",
    "category",
    "tracked",
    "line",
    "file",
    "add",
    "delete",
    "change",
    "review",
    "comment",
    "pr",
    "language",
    "star",
    "fork",
    "watch",
    "complex",
    "coverage",
    "dependency",
    "security",
    "static",
    "sonar",
    "cognitive",
    "duplication",
)


def open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def header(path: Path) -> list[str]:
    try:
        with open_text(path) as handle:
            reader = csv.reader(handle)
            return next(reader, [])
    except Exception:
        return []


def classify_columns(columns: list[str]) -> dict:
    ids = []
    outcomes = []
    possible_t0 = []
    other = []
    for col in columns:
        low = col.lower()
        if low in IDENTIFIER_TOKENS or any(low.endswith(token) for token in IDENTIFIER_TOKENS):
            ids.append(col)
        elif any(token in low for token in OUTCOME_TOKENS):
            outcomes.append(col)
        elif any(token in low for token in POTENTIAL_T0_TOKENS):
            possible_t0.append(col)
        else:
            other.append(col)
    return {
        "identifier_columns": ids,
        "possible_admission_time_columns": possible_t0,
        "obvious_outcome_or_post_admission_columns": outcomes,
        "other_columns": other,
    }


def main() -> None:
    paths: set[Path] = set()
    for root in ROOTS:
        if not root.exists():
            continue
        for pattern in ("*.csv", "*.csv.gz"):
            paths.update(root.rglob(pattern))

    tables = []
    for path in sorted(paths):
        if "techledger_phase0" in path.parts:
            continue
        columns = header(path)
        if not columns:
            continue
        classification = classify_columns(columns)
        has_asset_id = "origin_commit_sha" in columns
        has_commit_id = any(c in columns for c in ("commit_sha", "sha"))
        has_repo = any(c in columns for c in ("repo", "repo_key", "repo_slug"))
        tables.append({
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "column_count": len(columns),
            "has_origin_commit_sha": has_asset_id,
            "has_commit_level_id": has_commit_id,
            "has_repo_id": has_repo,
            **classification,
        })

    asset_level = [t for t in tables if t["has_origin_commit_sha"] and t["has_repo_id"]]
    generic_commit_level = [t for t in tables if t["has_commit_level_id"] and t["has_repo_id"] and not t["has_origin_commit_sha"]]
    repo_level = [t for t in tables if t["has_repo_id"] and not t["has_origin_commit_sha"] and not t["has_commit_level_id"]]

    summary = {
        "purpose": "Identify packaged tables that could supply information knowable at asset admission time without revisiting original repositories.",
        "files_scanned": len(tables),
        "asset_level_tables": asset_level,
        "generic_commit_level_tables": generic_commit_level,
        "repo_level_tables": repo_level,
        "interpretation_rule": {
            "usable_asset_predictor_source": "must identify the origin asset/commit and contain fields knowable at or before admission",
            "warning": "Columns are classified by names only in this checkpoint; no field is accepted as causal/predictive yet.",
        },
    }

    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
