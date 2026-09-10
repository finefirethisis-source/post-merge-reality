from __future__ import annotations

import csv
import json
import shutil
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.webscrape import github_commit_scraper as gcs

RQ1A = Path('dev/analysis/output_rq1a_explore/rq1_origin_commit_maintenance_summary.csv')
RQ1B = Path('dev/analysis/output_rq1b_explore/rq1b_origin_commit_terminal_maintenance_summary.csv')
OUTPUT = Path('techledger_phase0/checkpoint_summary.json')
WORK = Path('/tmp/techledger_exact180_fastpilot')
REPO = 'BarthPaleologue/CosmosJourneyer'
HORIZON = 180.0
TARGETS = {
    '9028015cabb5fa6aef82365005438066c3e70154': {'packaged': 21, 'terminal_180': 14, 'corrective_180': 0},
    'dd593848da30990ce05a4226718eb503b99c6b59': {'packaged': 21, 'terminal_180': 2, 'corrective_180': 0},
    '839957fb6792ad99a1ab9450048247b249e71904': {'packaged': 23, 'terminal_180': 2, 'corrective_180': 0},
    '5b0b55bf857207f3f3c11156858ce86d6a118641': {'packaged': 24, 'terminal_180': 7, 'corrective_180': 1},
    '402772731d238ac639968de9315f9f40d64754c0': {'packaged': 25, 'terminal_180': 13, 'corrective_180': 0},
}
REFERENCE_SCRIPT_SECONDS = 1709.44
REFERENCE_FULL_FILE_BLAME_SECONDS = 1300.20
REFERENCE_DIFF_SECONDS = 265.70


def pdt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_metadata_and_labels():
    selected = {}
    labels = {}
    with RQ1A.open(newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            repo = (r.get('repo') or r.get('repo_key') or '').strip()
            sha = (r.get('origin_commit_sha') or '').strip()
            klass = (r.get('origin_maintenance_class') or '').strip()
            if repo and sha and klass:
                labels[(repo, sha)] = klass
            if repo == REPO and sha in TARGETS:
                selected[sha] = {
                    'origin_date': pdt(r.get('origin_commit_date')),
                    'tracked': int(float(r.get('tracked_line_count') or 0)),
                    'role': (r.get('origin_role') or '').strip(),
                }
    with RQ1B.open(newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            repo = (r.get('repo') or r.get('repo_key') or '').strip()
            sha = (r.get('first_terminal_commit_sha') or '').strip()
            klass = (r.get('first_terminal_maintenance_class') or '').strip()
            if repo and sha and sha != 'mixed' and klass and klass != 'mixed':
                labels.setdefault((repo, sha), klass)
    return selected, labels


def measure(lines, selected, labels):
    raw = Counter()
    terminal = Counter()
    corrective = Counter()
    labeled = Counter()
    unlabeled = Counter()
    for row in lines:
        sha = row.get('origin_commit_sha')
        if sha not in selected:
            continue
        raw[sha] += 1
        td = pdt(row.get('terminal_commit_date'))
        if td is None:
            continue
        age = (td - selected[sha]['origin_date']).total_seconds() / 86400.0
        if not 0 <= age <= HORIZON:
            continue
        terminal[sha] += 1
        tsha = (row.get('terminal_commit_sha') or '').strip()
        klass = labels.get((REPO, tsha)) if tsha else None
        if klass:
            labeled[sha] += 1
            if klass == 'corrective':
                corrective[sha] += 1
        else:
            unlabeled[sha] += 1

    assets = []
    for sha in TARGETS:
        actual = {
            'sha': sha,
            'packaged': selected[sha]['tracked'],
            'reconstructed': raw[sha],
            'terminal_180': terminal[sha],
            'corrective_180': corrective[sha],
        }
        expected = TARGETS[sha]
        actual['tracked_matches_blame_reference'] = actual['reconstructed'] == expected['packaged']
        actual['terminal_matches_blame_reference'] = actual['terminal_180'] == expected['terminal_180']
        actual['corrective_matches_blame_reference'] = actual['corrective_180'] == expected['corrective_180']
        assets.append(actual)

    total_terminal = sum(terminal.values())
    total_labeled = sum(labeled.values())
    total_unlabeled = sum(unlabeled.values())
    return {
        'assets': assets,
        'all_tracked_counts_match': all(a['tracked_matches_blame_reference'] for a in assets),
        'all_terminal_counts_match': all(a['terminal_matches_blame_reference'] for a in assets),
        'all_corrective_counts_match': all(a['corrective_matches_blame_reference'] for a in assets),
        'terminal_lines_180': total_terminal,
        'corrective_lines_180': sum(corrective.values()),
        'terminal_label_coverage': total_labeled / (total_labeled + total_unlabeled) if (total_labeled + total_unlabeled) else None,
    }


def main():
    total_start = time.monotonic()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    timings = {}
    failure = None
    result = None
    repo_dir = None

    try:
        t = time.monotonic()
        selected, labels = load_metadata_and_labels()
        timings['load_packaged_data_seconds'] = time.monotonic() - t
        if set(selected) != set(TARGETS):
            raise RuntimeError(f'Expected five target SHAs; found {len(selected)}')
        for sha, expected in TARGETS.items():
            if selected[sha]['tracked'] != expected['packaged']:
                raise RuntimeError(f'Packaged tracked count changed for {sha}')

        t = time.monotonic()
        repo_dir = gcs.ensure_local_repo(REPO, WORK / 'repos', full_clone=False)
        timings['clone_seconds'] = time.monotonic() - t
        branch = gcs.run_git(repo_dir, ['rev-parse', '--abbrev-ref', 'HEAD']).strip()
        reachable = set(gcs.run_git(repo_dir, ['rev-list', 'HEAD']).splitlines())
        if not set(TARGETS).issubset(reachable):
            raise RuntimeError('One or more validated target SHAs are no longer reachable')

        earliest = min(v['origin_date'] for v in selected.values())
        latest = max(v['origin_date'] for v in selected.values())
        horizon_end = latest + timedelta(days=HORIZON, seconds=1)

        t = time.monotonic()
        commits, _ = gcs.fetch_branch_commits(REPO, repo_dir, branch, since=earliest - timedelta(days=2))
        obs = [c for c in commits if gcs.commit_datetime(c) <= horizon_end]
        timings['enumerate_commit_metadata_seconds'] = time.monotonic() - t
        if not set(TARGETS).issubset({c['sha'] for c in obs}):
            raise RuntimeError('Target SHAs missing from dated branch enumeration')

        t = time.monotonic()
        detailed = gcs.add_git_file_changes(REPO, repo_dir, obs)
        timings['extract_all_window_diffs_seconds'] = time.monotonic() - t

        t = time.monotonic()
        # Same authors' forward lifecycle logic; deliberately omit repo_dir so
        # verify_line_lifecycle_with_blame() is not invoked.
        lines = gcs.build_line_lifecycle(
            detailed,
            repo_dir=None,
            origin_commit_shas=set(TARGETS),
        )
        timings['diff_only_lifecycle_seconds'] = time.monotonic() - t

        result = measure(lines, selected, labels)
        result.update({
            'repo': REPO,
            'branch': branch,
            'observation_commits': len(obs),
            'reconstructed_lines': len(lines),
        })
    except Exception as exc:
        failure = {
            'error': str(exc),
            'traceback': traceback.format_exc()[-10000:],
        }
    finally:
        if repo_dir is not None:
            gcs.remove_cloned_repo_dir(repo_dir)

    timings['total_script_compute_seconds'] = time.monotonic() - total_start
    exact_match = bool(result and result['all_tracked_counts_match'] and result['all_terminal_counts_match'] and result['all_corrective_counts_match'])
    speedup = REFERENCE_SCRIPT_SECONDS / timings['total_script_compute_seconds'] if timings['total_script_compute_seconds'] else None
    summary = {
        'purpose': 'Test whether exact-180 per-asset burden can be reconstructed materially faster by using the authors forward diff lifecycle without expensive full-file blame verification.',
        'method_change': 'Keep the same commit window, parsed diffs, line-birth rules, rename handling, line-position mapping, and first-terminal logic; skip verify_line_lifecycle_with_blame and use the forward diff result directly.',
        'same_five_assets_as_validated_blame_pilot': list(TARGETS),
        'reference_blame_pilot': {
            'script_compute_seconds': REFERENCE_SCRIPT_SECONDS,
            'full_file_blame_prefetch_seconds': REFERENCE_FULL_FILE_BLAME_SECONDS,
            'diff_extraction_seconds': REFERENCE_DIFF_SECONDS,
            'terminal_lines_180': 38,
            'corrective_lines_180': 1,
        },
        'optimized_timings': timings,
        'optimized_result': result,
        'failure': failure,
        'exact_outcome_match_to_blame_pilot': exact_match,
        'measured_speedup_vs_blame_pilot': speedup,
        'acceptance': 'PASS only if all five reconstructed line counts, terminal_180 counts, and corrective_180 counts exactly match the blame-based pilot and runtime is materially lower.',
        'pilot_pass': exact_match and speedup is not None and speedup >= 3.0,
        'guardrail': 'A pass validates this faster route only on the same five assets. Before a full 18k-asset rerun, validate on a larger and more diverse repository sample, especially assets involving renames/moves.',
    }
    OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2, sort_keys=True))
    if failure:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
