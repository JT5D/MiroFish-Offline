"""
Reusable JSON Repair Toolkit
Extracted from MiroFish-Offline — standalone, zero-dependency module.

Handles the messy reality of LLM-generated JSON:
  - Truncated output (finish_reason == "length")
  - Unclosed strings, brackets, braces
  - Newlines/control chars inside string values
  - Markdown code fences wrapping JSON
  - Partial field extraction as last resort

Usage:
    from tools.json_repair import fix_truncated_json, try_parse_json, extract_field

    # Fix and parse in one shot:
    data = try_parse_json(raw_llm_output)
    if data is not None:
        print(data)

    # Extract a specific field from broken JSON:
    bio = extract_field(raw_llm_output, "bio")
"""

import json
import re
from typing import Any, Dict, Optional


def fix_truncated_json(content: str) -> str:
    """
    Attempt to close a truncated JSON string.

    Handles unclosed strings, arrays, and objects by counting
    open/close delimiters and appending the missing closers.
    """
    content = content.strip()
    if not content:
        return "{}"

    # Strip markdown code fences
    content = re.sub(r"^```(?:json)?\s*\n?", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\n?```\s*$", "", content)
    content = content.strip()

    # Close unclosed string (heuristic: last char is not a JSON structural char)
    if content and content[-1] not in '",}]':
        content += '"'

    # Count and close unclosed brackets/braces
    open_brackets = content.count("[") - content.count("]")
    open_braces = content.count("{") - content.count("}")
    content += "]" * max(0, open_brackets)
    content += "}" * max(0, open_braces)

    return content


def _fix_string_newlines(content: str) -> str:
    """Replace literal newlines inside JSON string values with spaces."""
    def _replace(match):
        s = match.group(0)
        s = s.replace("\n", " ").replace("\r", " ")
        s = re.sub(r"\s+", " ", s)
        return s

    return re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', _replace, content)


def try_parse_json(content: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort JSON parsing with multi-stage repair.

    Stages:
      1. Direct parse
      2. Strip markdown fences + direct parse
      3. Fix truncation + parse
      4. Fix newlines in strings + parse
      5. Strip all control characters + parse

    Returns None if all stages fail.
    """
    if not content or not content.strip():
        return None

    raw = content.strip()

    # Stage 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Stage 2: strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Stage 3: fix truncation
    fixed = fix_truncated_json(cleaned)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Stage 4: extract JSON object + fix newlines in strings
    json_match = re.search(r"\{[\s\S]*\}", fixed)
    if json_match:
        json_str = _fix_string_newlines(json_match.group())
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # Stage 5: nuclear option — strip all control characters
        json_str = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", json_str)
        json_str = re.sub(r"\s+", " ", json_str)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    return None


def extract_field(content: str, field_name: str) -> Optional[str]:
    """
    Last-resort extraction of a single string field from broken JSON.

    Useful when the JSON is too corrupted to parse but you need
    one specific value (e.g., "bio" or "persona").
    """
    # Try quoted value
    pattern = rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)"'
    match = re.search(pattern, content)
    if match:
        return match.group(1)

    # Try unquoted/truncated value
    pattern = rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)'
    match = re.search(pattern, content)
    if match:
        return match.group(1)

    return None
