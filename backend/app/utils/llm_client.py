"""
LLM Client Wrapper — with guaranteed wall-clock timeout

Problem: Ollama streams tokens AND may queue requests when busy.
  Both stream creation (POST) and stream consumption (reading chunks)
  can block indefinitely, bypassing per-chunk httpx timeouts.

Solution: Run BOTH stream creation AND consumption in a daemon thread.
  The calling thread joins with a timeout. If the join times out, the
  daemon thread is abandoned. The caller gets None immediately.
"""

import json
import os
import re
import time
import threading
from typing import Optional, Dict, Any, List
import httpx
import openai
from openai import OpenAI

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.llm_client')

# Wall-clock timeout for the entire LLM call (creation + generation).
_LLM_WALL_TIMEOUT = float(os.environ.get('MIROFISH_LLM_TIMEOUT', '90'))


class LLMClient:
    """LLM Client — supports task-type-aware provider routing."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        task_type: Optional[str] = None,
    ):
        # If task_type is set and no explicit params, use the router
        if task_type and not (api_key or base_url or model):
            from .llm_router import TaskType, get_router
            try:
                tt = TaskType(task_type)
            except ValueError:
                tt = TaskType.DEFAULT
            provider = get_router().get_provider(tt)
            if provider:
                api_key = provider.api_key
                base_url = provider.base_url
                model = provider.model

        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY is not configured")

        self._wall_timeout = timeout or _LLM_WALL_TIMEOUT

        # httpx timeout: generous because the real enforcement is the
        # thread-join timeout below. But not infinite — acts as a safety net.
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(max(self._wall_timeout * 2, 180.0), connect=10.0),
            max_retries=0,
        )

        self._num_ctx = int(os.environ.get('OLLAMA_NUM_CTX', '8192'))

    def _is_ollama(self) -> bool:
        """Check if we're talking to an Ollama server."""
        return '11434' in (self.base_url or '')

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None
    ) -> str:
        """
        Send a chat request with hard wall-clock timeout.

        BOTH stream creation and consumption run in a daemon thread.
        This ensures that even if Ollama is busy and queues the request,
        the caller returns within self._wall_timeout seconds.

        Returns:
            Model response text, or None on timeout/error
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if response_format:
            kwargs["response_format"] = response_format

        if self._is_ollama() and self._num_ctx:
            kwargs["extra_body"] = {
                "options": {"num_ctx": self._num_ctx}
            }

        start = time.monotonic()
        logger.info(f"LLM call: timeout={self._wall_timeout}s max_tokens={max_tokens}")

        # Shared state between main thread and worker
        chunks = []
        error = [None]

        def _create_and_consume():
            """Create stream AND consume it — all inside the daemon thread."""
            stream = None
            try:
                stream = self.client.chat.completions.create(**kwargs)
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        chunks.append(delta.content)
            except (httpx.ReadTimeout, httpx.ConnectTimeout,
                    openai.APITimeoutError) as e:
                error[0] = e
            except openai.APIConnectionError as e:
                error[0] = e
            except Exception as e:
                error[0] = e
            finally:
                if stream:
                    try:
                        stream.close()
                    except Exception:
                        pass

        worker = threading.Thread(target=_create_and_consume, daemon=True)
        worker.start()
        worker.join(timeout=self._wall_timeout)

        elapsed = time.monotonic() - start
        timed_out = worker.is_alive()

        if timed_out:
            logger.warning(
                f"WALL-CLOCK TIMEOUT: {elapsed:.1f}s elapsed, "
                f"{len(chunks)} chunks — abandoning worker thread"
            )
        elif error[0]:
            logger.warning(f"LLM error after {elapsed:.1f}s: {error[0]}")
        else:
            logger.info(f"LLM complete: {len(chunks)} chunks in {elapsed:.1f}s")

        if not chunks:
            return None

        content = "".join(chunks)

        if timed_out and len(content) < 100:
            logger.warning(f"Partial too short ({len(content)} chars), returning None")
            return None

        if timed_out:
            logger.info(f"Returning partial content ({len(content)} chars)")

        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        return content

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096
    ) -> Dict[str, Any]:
        """Send a chat request and return JSON."""
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        if response is None:
            raise ValueError("LLM call timed out (wall-clock timeout)")
        cleaned_response = response.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            return json.loads(cleaned_response)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON returned by LLM: {cleaned_response}")
