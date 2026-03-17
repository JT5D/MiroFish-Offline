"""
Reusable Retry Toolkit
Extracted from MiroFish-Offline — standalone, zero-dependency module.

Provides:
  - @retry_with_backoff       — sync decorator
  - @retry_with_backoff_async — async decorator
  - RetryableAPIClient        — OOP wrapper with batch support

Usage:
    from tools.retry import retry_with_backoff, RetryableAPIClient

    @retry_with_backoff(max_retries=3, backoff_factor=2.0)
    def call_api():
        ...

    client = RetryableAPIClient(max_retries=5)
    result = client.call_with_retry(call_api)
    results, failures = client.call_batch_with_retry(items, process_fn)
"""

import time
import random
import logging
import functools
from typing import Callable, Any, Optional, Type, Tuple, List, Dict

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
):
    """
    Retry decorator with exponential backoff.

    Args:
        max_retries:    Maximum number of retry attempts.
        initial_delay:  First delay in seconds.
        max_delay:      Ceiling for delay.
        backoff_factor: Multiplier applied after each retry.
        jitter:         Add randomness to delay (avoids thundering herd).
        exceptions:     Exception types that trigger a retry.
        on_retry:       Optional callback(exception, attempt_number).
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            "%s failed after %d retries: %s",
                            func.__name__, max_retries, e,
                        )
                        raise

                    current_delay = min(delay, max_delay)
                    if jitter:
                        current_delay *= 0.5 + random.random()

                    logger.warning(
                        "%s attempt %d failed: %s — retrying in %.1fs",
                        func.__name__, attempt + 1, e, current_delay,
                    )

                    if on_retry:
                        on_retry(e, attempt + 1)

                    time.sleep(current_delay)
                    delay *= backoff_factor

            raise last_exception
        return wrapper
    return decorator


def retry_with_backoff_async(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[Exception, int], None]] = None,
):
    """Async version of retry_with_backoff."""
    import asyncio

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            "Async %s failed after %d retries: %s",
                            func.__name__, max_retries, e,
                        )
                        raise

                    current_delay = min(delay, max_delay)
                    if jitter:
                        current_delay *= 0.5 + random.random()

                    logger.warning(
                        "Async %s attempt %d failed: %s — retrying in %.1fs",
                        func.__name__, attempt + 1, e, current_delay,
                    )

                    if on_retry:
                        on_retry(e, attempt + 1)

                    await asyncio.sleep(current_delay)
                    delay *= backoff_factor

            raise last_exception
        return wrapper
    return decorator


class RetryableAPIClient:
    """
    OOP retry wrapper — useful when you want instance-level config
    rather than per-function decorators.
    """

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    def call_with_retry(
        self,
        func: Callable,
        *args,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
        **kwargs,
    ) -> Any:
        """Execute func(*args, **kwargs) with retry on failure."""
        last_exception = None
        delay = self.initial_delay

        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except exceptions as e:
                last_exception = e
                if attempt == self.max_retries:
                    logger.error("API call failed after %d retries: %s", self.max_retries, e)
                    raise

                current_delay = min(delay, self.max_delay) * (0.5 + random.random())
                logger.warning(
                    "API call attempt %d failed: %s — retrying in %.1fs",
                    attempt + 1, e, current_delay,
                )
                time.sleep(current_delay)
                delay *= self.backoff_factor

        raise last_exception

    def call_batch_with_retry(
        self,
        items: List,
        process_func: Callable,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
        continue_on_failure: bool = True,
    ) -> Tuple[List, List[Dict]]:
        """
        Process a list of items with per-item retry.

        Returns:
            (successful_results, failures) where failures is a list of
            {"index": int, "item": Any, "error": str}.
        """
        results: List = []
        failures: List[Dict] = []

        for idx, item in enumerate(items):
            try:
                result = self.call_with_retry(process_func, item, exceptions=exceptions)
                results.append(result)
            except Exception as e:
                logger.error("Item %d failed: %s", idx + 1, e)
                failures.append({"index": idx, "item": item, "error": str(e)})
                if not continue_on_failure:
                    raise

        return results, failures
