"""Utilities for parallel and sequential task execution with optional progress tracking."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from time import perf_counter
from typing import TypeVar

from tqdm.contrib.logging import logging_redirect_tqdm

from findpapers.utils.progress import make_progress_bar

T = TypeVar("T")
R = TypeVar("R")

ProgressUpdate = Callable[[T, R | None, Exception | None], int]


def execute_tasks(
    items: Iterable[T],
    task: Callable[[T], R],
    *,
    num_workers: int | None,
    timeout: float | None,
    progress_total: int | None = None,
    progress_unit: str = "item",
    progress_desc: str | None = None,
    progress_update: ProgressUpdate | None = None,
    use_progress: bool = True,
    stop_on_timeout: bool = True,
) -> Iterator[tuple[T, R | None, Exception | None]]:
    """Execute tasks sequentially or in parallel with optional progress tracking.

    Parameters
    ----------
    items : Iterable[T]
        Items to process.
    task : Callable[[T], R]
        Task function to execute for each item.
    num_workers : int | None
        Number of workers. ``None`` or ``1`` runs sequentially.
    timeout : float | None
        Global timeout in seconds. ``None`` means no timeout.
    progress_total : int | None
        Total number of progress units for the progress bar.
    progress_unit : str
        Unit label displayed in the progress bar.
    progress_desc : str | None
        Short description label shown before the progress bar.  ``None``
        omits the label.
    progress_update : ProgressUpdate | None
        Optional callback returning the progress increment per completed item.
        When ``None``, each completed item counts as 1.
    use_progress : bool
        Whether to display a tqdm progress bar.
    stop_on_timeout : bool
        When ``True``, stop processing remaining items after a timeout.

    Yields
    ------
    Iterator[tuple[T, R | None, Exception | None]]
        ``(item, result, error)`` tuples for each completed task.
        *result* is ``None`` and *error* is set on failure.

    Raises
    ------
    None
        Errors are surfaced as the third element of the yielded tuple, never
        raised directly.
    """
    total = progress_total
    if total is None and hasattr(items, "__len__"):
        total = len(items)  # type: ignore[arg-type]

    progress_bar = (
        make_progress_bar(
            desc=progress_desc,
            total=total,
            unit=progress_unit,
            disable=not use_progress,
        )
        if total is not None
        else None
    )

    def _update_progress(item: T, result: R | None, error: Exception | None) -> None:
        if progress_bar is None:
            return
        increment = 1 if progress_update is None else progress_update(item, result, error)
        if increment:
            progress_bar.update(increment)

    start = perf_counter()
    try:
        if num_workers is None or num_workers <= 1:
            # Sequential path.
            for item in items:
                if timeout is not None and (perf_counter() - start) > timeout:
                    timeout_error = TimeoutError("Global timeout exceeded.")
                    _update_progress(item, None, timeout_error)
                    yield item, None, timeout_error
                    if stop_on_timeout:
                        break
                    continue
                try:
                    result: R | None = task(item)
                    error: Exception | None = None
                except Exception as exc:
                    result = None
                    error = exc
                _update_progress(item, result, error)
                yield item, result, error
        else:
            # Parallel path: submit all tasks and iterate results as they complete.
            # logging_redirect_tqdm ensures log records emitted by worker
            # threads are written via tqdm.write() instead of directly to
            # stderr, preventing them from corrupting active progress bars.
            with logging_redirect_tqdm(), ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {executor.submit(task, item): item for item in items}
                remaining = None if timeout is None else max(timeout - (perf_counter() - start), 0)
                yielded_futures: set[object] = set()
                try:
                    for future in as_completed(futures, timeout=remaining):
                        item = futures[future]
                        yielded_futures.add(future)
                        try:
                            fut_result: R | None = future.result()
                            fut_error: Exception | None = None
                        except Exception as exc:
                            fut_result = None
                            fut_error = exc
                        _update_progress(item, fut_result, fut_error)
                        yield item, fut_result, fut_error
                except FuturesTimeoutError:
                    # Global timeout reached: cancel pending work so the
                    # executor doesn't block on shutdown waiting for futures.
                    timeout_error = TimeoutError("Global timeout exceeded.")
                    for future, item in futures.items():
                        if future in yielded_futures:
                            continue
                        if not future.done():
                            future.cancel()
                            _update_progress(item, None, timeout_error)
                            yield item, None, timeout_error
                            continue
                        try:
                            fut_result = future.result()
                            fut_error = None
                        except Exception as exc:
                            fut_result = None
                            fut_error = exc
                        _update_progress(item, fut_result, fut_error)
                        yield item, fut_result, fut_error
                    # Shut down without waiting for cancelled/running futures.
                    executor.shutdown(wait=False, cancel_futures=True)
    finally:
        if progress_bar is not None:
            progress_bar.close()
