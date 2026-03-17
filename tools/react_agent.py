"""
Reusable ReACT Agent Framework
Extracted from MiroFish-Offline's report_agent.py.

Implements the ReACT (Reasoning + Acting) loop:
  Thought → Tool Selection → Tool Execution → Observation → (repeat) → Final Answer

Subclass ReACTAgent, register tools, and call run() with a query.

Usage:
    from tools.react_agent import ReACTAgent, Tool
    from tools.llm_client import LLMClient

    def search_graph(query: str) -> str:
        return f"Found 3 results for: {query}"

    def search_web(query: str) -> str:
        return f"Web results for: {query}"

    class ResearchAgent(ReACTAgent):
        def build_system_prompt(self):
            return "You are a research assistant. Use tools to gather info, then synthesize."

    client = LLMClient()
    agent = ResearchAgent(client, max_iterations=5)
    agent.register_tool(Tool("graph_search", "Search the knowledge graph", search_graph))
    agent.register_tool(Tool("web_search", "Search the web", search_web))

    result = agent.run("What is the relationship between Apple and spatial computing?")
    print(result["answer"])
    print(result["tool_calls"])  # List of all tool calls made
"""

import json
import re
import logging
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from .json_repair import try_parse_json

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    """A tool that the agent can invoke."""
    name: str
    description: str
    func: Callable[..., str]
    parameters: Optional[Dict[str, str]] = None  # param_name -> description

    def to_prompt_description(self) -> str:
        """Format tool for inclusion in system prompt."""
        params = ""
        if self.parameters:
            param_list = ", ".join(f"{k}: {v}" for k, v in self.parameters.items())
            params = f" Parameters: {param_list}"
        return f"- **{self.name}**: {self.description}{params}"


@dataclass
class ToolCall:
    """Record of a tool invocation."""
    tool_name: str
    args: Dict[str, Any]
    result: str
    iteration: int


class ReACTAgent(ABC):
    """
    Base class for ReACT (Reasoning + Acting) agents.

    The run() loop:
      1. Build system prompt with tool descriptions
      2. Send query + conversation history to LLM
      3. Parse response for tool calls (JSON format)
      4. Execute tool calls, append observations
      5. Repeat until LLM returns a final answer or max_iterations reached

    Subclasses must implement:
      - build_system_prompt() -> str  (base instructions, personality)

    The LLM is expected to respond in one of two formats:
      A. Tool call: {"thought": "...", "tool": "tool_name", "args": {...}}
      B. Final answer: {"thought": "...", "answer": "..."}
    """

    def __init__(
        self,
        llm_client,
        max_iterations: int = 5,
        temperature: float = 0.5,
    ):
        self.llm = llm_client
        self.max_iterations = max_iterations
        self.temperature = temperature
        self._tools: Dict[str, Tool] = {}

    @abstractmethod
    def build_system_prompt(self) -> str:
        """Return the base system prompt (agent personality, task description)."""
        ...

    def register_tool(self, tool: Tool):
        """Register a tool the agent can use."""
        self._tools[tool.name] = tool

    def _build_full_system_prompt(self) -> str:
        """Build system prompt with tool descriptions and response format."""
        base = self.build_system_prompt()
        tool_list = "\n".join(t.to_prompt_description() for t in self._tools.values())

        return f"""{base}

## Available Tools
{tool_list}

## Response Format

You must respond in JSON. Choose ONE of these formats:

**To use a tool:**
{{"thought": "your reasoning about what to do next", "tool": "tool_name", "args": {{"param": "value"}}}}

**To give a final answer (when you have enough information):**
{{"thought": "your final reasoning", "answer": "your comprehensive answer"}}

Rules:
- Always include a "thought" field explaining your reasoning
- Use tools to gather information before answering
- When you have enough context, provide a final "answer"
- Do NOT make up information — use tools to verify
"""

    def _parse_response(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse LLM response, handling common formatting issues."""
        result = try_parse_json(raw)
        if result:
            return result

        # Try to extract JSON from mixed text
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if json_match:
            return try_parse_json(json_match.group())

        return None

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Execute a registered tool and return its result as a string."""
        tool = self._tools.get(tool_name)
        if not tool:
            return f"Error: Unknown tool '{tool_name}'. Available: {list(self._tools.keys())}"

        try:
            result = tool.func(**args)
            return str(result)
        except Exception as e:
            logger.error("Tool '%s' failed: %s", tool_name, e)
            return f"Error executing {tool_name}: {e}"

    def run(
        self,
        query: str,
        context: Optional[str] = None,
        on_iteration: Optional[Callable[[int, Dict], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute the ReACT loop.

        Args:
            query:        The user's question or task.
            context:      Optional additional context to include.
            on_iteration: Optional callback(iteration, step_data) for logging.

        Returns:
            {
                "answer": str,           # Final answer
                "thought": str,          # Final reasoning
                "tool_calls": [ToolCall],# All tool calls made
                "iterations": int,       # Number of iterations used
            }
        """
        system_prompt = self._build_full_system_prompt()
        tool_calls: List[ToolCall] = []

        # Build initial messages
        messages = [{"role": "system", "content": system_prompt}]

        user_msg = f"## Query\n{query}"
        if context:
            user_msg += f"\n\n## Context\n{context}"
        messages.append({"role": "user", "content": user_msg})

        for iteration in range(self.max_iterations):
            # Call LLM
            try:
                raw = self.llm.chat(
                    messages=messages,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                )
            except Exception as e:
                logger.error("LLM call failed at iteration %d: %s", iteration, e)
                break

            parsed = self._parse_response(raw)
            if not parsed:
                logger.warning("Could not parse LLM response at iteration %d", iteration)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please respond in the required JSON format.",
                })
                continue

            thought = parsed.get("thought", "")

            if on_iteration:
                on_iteration(iteration, parsed)

            # Check for final answer
            if "answer" in parsed and parsed["answer"]:
                return {
                    "answer": parsed["answer"],
                    "thought": thought,
                    "tool_calls": tool_calls,
                    "iterations": iteration + 1,
                }

            # Execute tool call
            tool_name = parsed.get("tool", "")
            tool_args = parsed.get("args", {})

            if not tool_name:
                # No tool and no answer — nudge the LLM
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "Please either use a tool or provide a final answer.",
                })
                continue

            logger.info("Iteration %d: calling tool '%s'", iteration, tool_name)
            result = self._execute_tool(tool_name, tool_args)

            tool_call = ToolCall(
                tool_name=tool_name,
                args=tool_args,
                result=result,
                iteration=iteration,
            )
            tool_calls.append(tool_call)

            # Append to conversation
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"## Observation (from {tool_name})\n{result}\n\nContinue reasoning. Use another tool or provide your final answer.",
            })

        # Max iterations reached — synthesize what we have
        logger.warning("ReACT agent hit max iterations (%d)", self.max_iterations)
        return {
            "answer": f"Reached maximum iterations ({self.max_iterations}). Based on gathered information: {thought}",
            "thought": thought,
            "tool_calls": tool_calls,
            "iterations": self.max_iterations,
        }
