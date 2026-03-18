"""
LLM Provider — convenience factory for OpenAI-compatible clients.

Replaces raw OpenAI() construction throughout the codebase.
Uses LLMRouter to select the appropriate provider for each task type.
"""

from typing import Optional, Tuple

import httpx
from openai import OpenAI

from .llm_router import TaskType, ProviderConfig, get_router
from ..utils.logger import get_logger

logger = get_logger('mirofish.llm_provider')


def get_llm_client(
    task_type: TaskType = TaskType.DEFAULT,
    timeout: Optional[float] = None,
) -> Tuple[Optional[OpenAI], Optional[str]]:
    """
    Get an OpenAI client and model name for the given task type.

    Uses the LLMRouter to find the first healthy provider in the chain.

    Args:
        task_type: The type of LLM task (determines provider chain).
        timeout: Optional HTTP timeout override.

    Returns:
        (client, model_name) — or (None, None) if all providers are down.
    """
    router = get_router()
    provider = router.get_provider(task_type)

    if provider is None:
        logger.warning(f"No provider available for {task_type.value}")
        return None, None

    effective_timeout = timeout or 90.0

    client = OpenAI(
        api_key=provider.api_key,
        base_url=provider.base_url,
        timeout=httpx.Timeout(max(effective_timeout * 2, 180.0), connect=10.0),
        max_retries=0,
    )

    logger.debug(
        f"Provider for {task_type.value}: "
        f"{provider.name} ({provider.model} @ {provider.base_url})"
    )

    return client, provider.model


def get_provider_config(
    task_type: TaskType = TaskType.DEFAULT,
) -> Optional[ProviderConfig]:
    """
    Get the raw ProviderConfig for a task type without constructing a client.

    Useful when callers need api_key/base_url/model but construct their own client.
    """
    return get_router().get_provider(task_type)
