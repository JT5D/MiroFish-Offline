"""
Reusable LLM Client Wrapper
Extracted from MiroFish-Offline — requires `openai` package.

Provides a thin, opinionated wrapper around the OpenAI-compatible
chat completions API with:
  - Automatic Ollama num_ctx injection (prevents prompt truncation)
  - <think> tag stripping (for reasoning models)
  - JSON mode with markdown fence cleanup
  - Configurable via env vars or constructor args

Usage:
    from tools.llm_client import LLMClient

    client = LLMClient(base_url="http://localhost:11434/v1", api_key="ollama", model="qwen2.5:14b")
    text = client.chat([{"role": "user", "content": "Hello"}])
    data = client.chat_json([{"role": "user", "content": "Return JSON with key 'name'"}])
"""

import json
import os
import re
from typing import Optional, Dict, Any, List

from openai import OpenAI


class LLMClient:
    """
    Unified LLM client supporting OpenAI API and Ollama-compatible endpoints.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "ollama")
        self.base_url = base_url or os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
        self.model = model or os.environ.get("LLM_MODEL_NAME", "qwen2.5:14b")

        if not self.api_key:
            raise ValueError("LLM_API_KEY is not configured")

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)
        self._num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

    def _is_ollama(self) -> bool:
        return "11434" in (self.base_url or "")

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
    ) -> str:
        """Send a chat completion request, return the response text."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        # Ollama: inject num_ctx to prevent silent prompt truncation
        if self._is_ollama() and self._num_ctx:
            kwargs["extra_body"] = {"options": {"num_ctx": self._num_ctx}}

        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content

        # Strip <think> reasoning blocks some models emit
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        return content

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """Send a chat request with JSON mode and return parsed dict."""
        text = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        # Strip markdown code fences that some models wrap around JSON
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise ValueError(f"LLM returned invalid JSON: {cleaned[:200]}...")
