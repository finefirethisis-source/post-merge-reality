from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeVar


T = TypeVar("T")


def normalize_repo_key(repo: str) -> str:
    return repo.strip().replace("/", "__")


def read_repo_list_file(path: Path) -> list[str]:
    """Read owner/repo or owner__repo entries from a text file.

    Blank lines and comments are ignored. Inline comments are also supported, so
    a line like ``owner/repo  # note`` resolves to ``owner/repo``.
    """
    if not path.exists():
        raise FileNotFoundError(f"repo list file not found: {path}")

    repos: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            repo = line.strip()
            if not repo or repo.startswith("#"):
                continue
            if "#" in repo:
                repo = repo.split("#", 1)[0].strip()
            if repo:
                repos.append(repo)
            else:
                logging.getLogger(__name__).warning(
                    "skipping empty repo entry after comment removal in %s:%s",
                    path,
                    line_number,
                )
    return repos


def repo_dir_has_required_files(
    repo_dir: Path,
    required_files: Sequence[str] | None,
) -> bool:
    if not required_files:
        return True
    return any((repo_dir / filename).exists() for filename in required_files)


def resolve_repo_dirs(
    input_dir: Path,
    repo_file: Path | None = None,
    repos: Sequence[str] | None = None,
    required_files: Sequence[str] | None = ("all_commits.jsonl",),
) -> list[Path]:
    """Resolve repository output directories under ``input_dir``.

    When ``repos`` or ``repo_file`` entries are provided, each entry may be either
    ``owner/repo`` or ``owner__repo``. The returned paths follow the input order
    with duplicates removed. When neither is provided, all matching repo output
    directories under ``input_dir`` are discovered and returned sorted by name.
    """
    if not input_dir.exists():
        raise FileNotFoundError(f"repo output directory not found: {input_dir}")

    repo_entries = list(repos or [])
    if repo_file is not None:
        repo_entries.extend(read_repo_list_file(repo_file))

    if repo_entries:
        repo_dirs: list[Path] = []
        seen: set[str] = set()
        missing: list[str] = []
        invalid: list[str] = []
        for repo in repo_entries:
            repo_key = normalize_repo_key(repo)
            if not repo_key or repo_key in seen:
                continue
            seen.add(repo_key)
            repo_dir = input_dir / repo_key
            if not repo_dir.is_dir():
                missing.append(repo_key)
                continue
            if not repo_dir_has_required_files(repo_dir, required_files):
                invalid.append(repo_key)
                continue
            repo_dirs.append(repo_dir)

        problems = []
        if missing:
            problems.append(f"missing repo dirs: {', '.join(missing)}")
        if invalid:
            problems.append(
                "repo dirs missing required file(s) "
                f"{list(required_files or [])}: {', '.join(invalid)}"
            )
        if problems:
            raise FileNotFoundError("; ".join(problems))
        return repo_dirs

    return sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_dir() and repo_dir_has_required_files(path, required_files)
        ),
        key=lambda path: path.name,
    )


def resolve_worker_count(
    workers: int | None,
    item_count: int,
    default_worker_limit: int = 4,
) -> int:
    if item_count <= 1:
        return 1
    if workers is None:
        return min(item_count, max(1, min(default_worker_limit, os.cpu_count() or 1)))
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    return min(workers, item_count)


def load_repo_data_parallel(
    repo_dirs: Sequence[Path],
    loader: Callable[..., T],
    *loader_args: Any,
    workers: int | None = None,
    default_worker_limit: int = 4,
    task_label: str = "repo",
    logger: logging.Logger | None = None,
    result_summary: Callable[[Path, T], str] | None = None,
    preserve_order: bool = True,
    **loader_kwargs: Any,
) -> list[T]:
    """Run a picklable per-repo loader across repo output directories.

    The loader is called as ``loader(repo_dir, *loader_args, **loader_kwargs)``.
    Use a top-level function for ``loader`` when ``workers`` can be greater than
    one, because process pools must pickle the callable.
    """
    repo_dirs = list(repo_dirs)
    worker_count = resolve_worker_count(
        workers,
        len(repo_dirs),
        default_worker_limit=default_worker_limit,
    )
    log = logger or logging.getLogger(__name__)
    log.info(
        "loading %s repo(s) for %s with %s worker process(es)",
        f"{len(repo_dirs):,}",
        task_label,
        worker_count,
    )

    start_time = time.perf_counter()
    if worker_count == 1:
        results = [
            loader(repo_dir, *loader_args, **loader_kwargs)
            for repo_dir in repo_dirs
        ]
        log.info(
            "finished single-process %s in %.2fs",
            task_label,
            time.perf_counter() - start_time,
        )
        return results

    completed = 0
    ordered_results: dict[int, T] = {}
    unordered_results: list[T] = []
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        future_to_repo = {
            executor.submit(loader, repo_dir, *loader_args, **loader_kwargs): (
                index,
                repo_dir,
            )
            for index, repo_dir in enumerate(repo_dirs)
        }
        log.info(
            "submitted %s per-repo %s job(s)",
            f"{len(future_to_repo):,}",
            task_label,
        )
        for future in as_completed(future_to_repo):
            index, repo_dir = future_to_repo[future]
            try:
                result = future.result()
            except Exception as exc:
                raise RuntimeError(f"Failed to load {task_label} {repo_dir}") from exc

            completed += 1
            if preserve_order:
                ordered_results[index] = result
            else:
                unordered_results.append(result)

            summary = result_summary(repo_dir, result) if result_summary else ""
            log.info(
                "completed %s %s/%s repo=%s%s",
                task_label,
                f"{completed:,}",
                f"{len(repo_dirs):,}",
                repo_dir.name,
                f" {summary}" if summary else "",
            )

    log.info(
        "finished multiprocess %s in %.2fs",
        task_label,
        time.perf_counter() - start_time,
    )
    if preserve_order:
        return [ordered_results[index] for index in range(len(repo_dirs))]
    return unordered_results
