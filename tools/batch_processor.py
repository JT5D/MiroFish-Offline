"""
Reusable Parallel Batch Processor
Extracted from MiroFish-Offline — requires only stdlib.

Processes a list of items through a worker function using a thread pool,
with per-item error isolation, progress callbacks, optional real-time
file output, and order preservation.

Usage:
    from tools.batch_processor import BatchProcessor

    def generate_profile(item):
        # ... call LLM or do work ...
        return {"name": item["name"], "bio": "..."}

    processor = BatchProcessor(
        worker_fn=generate_profile,
        parallel_count=4,
        progress_callback=lambda cur, total, msg: print(f"{cur}/{total}: {msg}"),
    )

    results, failures = processor.run(items)
"""

import json
import logging
import concurrent.futures
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BatchProcessor:
    """
    Parallel batch processor with per-item error isolation.

    Features:
      - ThreadPoolExecutor with configurable parallelism
      - Order-preserving results (pre-allocated list)
      - Per-item fallback on failure (no item blocks others)
      - Progress callbacks (current, total, message)
      - Optional real-time JSON file output after each item completes
      - Thread-safe accumulation
    """

    def __init__(
        self,
        worker_fn: Callable[[Any], Any],
        parallel_count: int = 4,
        fallback_fn: Optional[Callable[[Any, Exception], Any]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        realtime_output_path: Optional[str] = None,
        result_serializer: Optional[Callable[[Any], Any]] = None,
    ):
        """
        Args:
            worker_fn:            Function that processes a single item.
            parallel_count:       Number of concurrent workers.
            fallback_fn:          Called with (item, exception) when worker_fn fails.
                                  If None, failed items are recorded in failures list.
            progress_callback:    Called with (completed, total, message) after each item.
            realtime_output_path: If set, writes accumulated results to this JSON file
                                  after each item completes.
            result_serializer:    Converts a result to a JSON-serializable dict for
                                  real-time output. If None, results are written as-is.
        """
        self.worker_fn = worker_fn
        self.parallel_count = parallel_count
        self.fallback_fn = fallback_fn
        self.progress_callback = progress_callback
        self.realtime_output_path = realtime_output_path
        self.result_serializer = result_serializer

    def run(self, items: List[Any]) -> Tuple[List[Any], List[Dict]]:
        """
        Process all items in parallel.

        Returns:
            (results, failures) where:
              - results: list aligned with input items (None for failed items without fallback)
              - failures: list of {"index": int, "item": Any, "error": str}
        """
        total = len(items)
        results = [None] * total
        failures: List[Dict] = []
        completed_count = [0]
        lock = Lock()

        def _save_realtime():
            if not self.realtime_output_path:
                return
            with lock:
                existing = [r for r in results if r is not None]
                if not existing:
                    return
                try:
                    serialized = (
                        [self.result_serializer(r) for r in existing]
                        if self.result_serializer
                        else existing
                    )
                    with open(self.realtime_output_path, "w", encoding="utf-8") as f:
                        json.dump(serialized, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.warning("Real-time save failed: %s", e)

        def _process_item(idx: int, item: Any) -> Tuple[int, Any, Optional[str]]:
            try:
                result = self.worker_fn(item)
                return idx, result, None
            except Exception as e:
                if self.fallback_fn:
                    try:
                        fallback_result = self.fallback_fn(item, e)
                        return idx, fallback_result, str(e)
                    except Exception as fallback_err:
                        return idx, None, f"Worker: {e}; Fallback: {fallback_err}"
                return idx, None, str(e)

        logger.info(
            "BatchProcessor: processing %d items with %d workers",
            total, self.parallel_count,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.parallel_count) as executor:
            future_to_idx = {
                executor.submit(_process_item, idx, item): idx
                for idx, item in enumerate(items)
            }

            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    result_idx, result, error = future.result()
                    results[result_idx] = result

                    with lock:
                        completed_count[0] += 1
                        current = completed_count[0]

                    _save_realtime()

                    if error:
                        failures.append({"index": idx, "item": items[idx], "error": error})
                        if self.progress_callback:
                            self.progress_callback(current, total, f"Item {idx} fallback: {error[:80]}")
                    else:
                        if self.progress_callback:
                            self.progress_callback(current, total, f"Completed {current}/{total}")

                except Exception as e:
                    logger.error("Unexpected error for item %d: %s", idx, e)
                    with lock:
                        completed_count[0] += 1
                    failures.append({"index": idx, "item": items[idx], "error": str(e)})

        logger.info(
            "BatchProcessor: done. %d succeeded, %d failed.",
            total - len(failures), len(failures),
        )
        return results, failures
