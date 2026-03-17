"""
Reusable LLM Agent Base Class
Extracted from MiroFish-Offline.

Captures the common pattern used across all LLM-backed services:
  system prompt → user message → LLM call → JSON parse → repair → rule-based fallback

Subclass LLMAgent and override build_system_prompt(), build_user_prompt(),
and fallback() to create domain-specific agents.

Usage:
    from tools.llm_agent import LLMAgent
    from tools.llm_client import LLMClient

    class ProfileAgent(LLMAgent):
        def build_system_prompt(self, context):
            return "You are a profile generation expert..."

        def build_user_prompt(self, context):
            return f"Generate a profile for {context['name']}..."

        def fallback(self, context, error):
            return {"bio": f"Default bio for {context['name']}"}

    client = LLMClient()
    agent = ProfileAgent(client, max_retries=3)
    result = agent.run({"name": "Alice", "type": "Person"})
"""

import json
import logging
from typing import Any, Dict, List, Optional, Callable
from abc import ABC, abstractmethod

from .json_repair import try_parse_json, fix_truncated_json

logger = logging.getLogger(__name__)


class LLMAgent(ABC):
    """
    Base class for LLM-backed agents that produce structured JSON output.

    The run() method implements the full retry + repair + fallback pipeline:
      1. Build system + user prompts from context
      2. Call LLM with JSON mode
      3. Parse response, repairing truncation/corruption
      4. Validate required fields
      5. On total failure, invoke rule-based fallback

    Subclasses must implement:
      - build_system_prompt(context) -> str
      - build_user_prompt(context) -> str
      - fallback(context, error) -> dict
    """

    def __init__(
        self,
        llm_client,
        max_retries: int = 3,
        initial_temperature: float = 0.7,
        temperature_decay: float = 0.1,
        required_fields: Optional[List[str]] = None,
        on_retry: Optional[Callable[[Exception, int], None]] = None,
    ):
        """
        Args:
            llm_client:          An LLMClient instance (from tools.llm_client).
            max_retries:         Number of LLM call attempts before falling back.
            initial_temperature: Starting temperature for generation.
            temperature_decay:   How much to lower temperature on each retry.
            required_fields:     Fields that must be present in the JSON output.
            on_retry:            Optional callback(exception, attempt_number).
        """
        self.llm = llm_client
        self.max_retries = max_retries
        self.initial_temperature = initial_temperature
        self.temperature_decay = temperature_decay
        self.required_fields = required_fields or []
        self.on_retry = on_retry

    @abstractmethod
    def build_system_prompt(self, context: Dict[str, Any]) -> str:
        """Build the system prompt from the given context."""
        ...

    @abstractmethod
    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        """Build the user prompt from the given context."""
        ...

    @abstractmethod
    def fallback(self, context: Dict[str, Any], error: Exception) -> Dict[str, Any]:
        """
        Rule-based fallback when all LLM attempts fail.
        Should return a valid result dict.
        """
        ...

    def validate(self, result: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Optional post-processing hook. Override to add domain-specific
        validation or field correction. Called after successful JSON parse.

        Default implementation checks required_fields and fills missing
        ones with empty strings.
        """
        for field in self.required_fields:
            if field not in result or not result[field]:
                result[field] = ""
        return result

    def run(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the full agent pipeline:
          prompt → LLM → parse → validate → fallback

        Args:
            context: Arbitrary dict passed to prompt builders and fallback.

        Returns:
            Parsed and validated JSON dict from LLM, or fallback result.
        """
        system_prompt = self.build_system_prompt(context)
        user_prompt = self.build_user_prompt(context)
        last_error = None

        for attempt in range(self.max_retries):
            temperature = max(0.1, self.initial_temperature - (attempt * self.temperature_decay))

            try:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]

                # Use JSON mode
                raw = self.llm.chat(
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )

                # Parse with repair
                result = try_parse_json(raw)
                if result is None:
                    raise ValueError(f"Could not parse LLM output as JSON: {raw[:200]}...")

                # Validate and post-process
                result = self.validate(result, context)
                return result

            except Exception as e:
                last_error = e
                logger.warning(
                    "%s attempt %d failed: %s",
                    self.__class__.__name__, attempt + 1, str(e)[:100],
                )
                if self.on_retry:
                    self.on_retry(e, attempt + 1)

        # All attempts failed — use fallback
        logger.warning(
            "%s falling back to rule-based generation after %d attempts: %s",
            self.__class__.__name__, self.max_retries, last_error,
        )
        return self.fallback(context, last_error)


class StepwiseAgent:
    """
    Orchestrates multi-step LLM generation where each step may use
    a different prompt and produces a different part of the final output.

    Models the pattern from SimulationConfigGenerator:
      Step 1: Generate time config
      Step 2: Generate event config
      Step 3-N: Generate agent configs (batched)
      Step N+1: Generate platform config

    Usage:
        agent = StepwiseAgent(llm_client)
        agent.add_step("time_config", time_agent)
        agent.add_step("event_config", event_agent)
        results = agent.run(context, progress_callback=my_callback)
        # results = {"time_config": {...}, "event_config": {...}}
    """

    def __init__(self, llm_client=None):
        self.llm = llm_client
        self._steps: List[tuple] = []  # (name, agent_or_callable)

    def add_step(self, name: str, agent: LLMAgent):
        """Add a named step to the pipeline."""
        self._steps.append((name, agent))

    def run(
        self,
        context: Dict[str, Any],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Run all steps sequentially, collecting results.

        Each step's result is added to the context under its name,
        so later steps can reference earlier outputs.

        Args:
            context:           Shared context dict (mutated with step results).
            progress_callback: Optional (current_step, total_steps, message) callback.

        Returns:
            Dict mapping step names to their results.
        """
        total = len(self._steps)
        results = {}

        for i, (name, agent) in enumerate(self._steps):
            if progress_callback:
                progress_callback(i + 1, total, f"Running step: {name}")

            logger.info("StepwiseAgent: step %d/%d — %s", i + 1, total, name)
            result = agent.run(context)
            results[name] = result
            context[name] = result  # Make available to subsequent steps

        return results
