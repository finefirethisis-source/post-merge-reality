from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

INPUT = Path("dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv")
OUTPUT = Path("techledger_phase0/checkpoint_summary.json")


def main() -> None:
    row_count = 0
    schemas: list[str] = []
    distinct: dict[str, Counter[str]] = defaultdict(Counter)
    sample_rows: list[dict[str, str]] = []

    with INPUT.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        schemas = reader.fieldnames or []
        candidate_cols = [
            c for c in schemas
            if any(token in c.lower() for token in ("maintenance", "intent", "origin", "terminal", "role", "repo", "commit"))
        ]
        for row in reader:
            row_count += 1
            if len(sample_rows) < 3:
                sample_rows.append({k: row.get(k, "") for k in schemas})
            for col in candidate_cols:
                value = (row.get(col) or "").strip()
                if value and len(distinct[col]) < 50:
                    distinct[col][value] += 1

    summary = {
        "input": str(INPUT),
        "row_count": row_count,
        "schema": schemas,
        "candidate_join_and_taxonomy_columns": {
            col: dict(counter.most_common(20)) for col, counter in distinct.items()
        },
        "sample_rows": sample_rows,
    }
    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
