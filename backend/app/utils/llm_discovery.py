"""
LLM Discovery — auto-detect local LLM providers and choose optimal model assignments.

Scans known ports for Ollama, LM Studio, and other OpenAI-compatible servers.
Ranks models by capability and assigns them to task types for best throughput.

Usage:
    from app.utils.llm_discovery import discover_and_configure
    changes = discover_and_configure()  # Returns dict of env var changes made
"""

import os
import re
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import httpx

from ..utils.logger import get_logger

logger = get_logger('mirofish.llm_discovery')


# Known local LLM server ports and their probe strategies
_KNOWN_PORTS = [
    (11434, "ollama"),       # Ollama default
    (1234, "lmstudio"),      # LM Studio default
    (8080, "llamacpp"),      # llama.cpp server
    (4891, "gpt4all"),       # GPT4All
    (8000, "vllm"),          # vLLM
    (3001, "jan"),           # Jan AI
    (5001, None),            # Skip — that's our own backend
]

_PROBE_TIMEOUT = 2.0  # seconds per probe


@dataclass
class DiscoveredModel:
    """A model found on a local provider."""
    model_id: str
    provider_name: str
    base_url: str
    api_key: str = "local"
    # Estimated capability tier (higher = better for complex tasks)
    tier: int = 1
    # Estimated parameter count in billions (0 = unknown)
    param_b: float = 0.0
    is_embedding: bool = False


@dataclass
class DiscoveredProvider:
    """A discovered local LLM provider."""
    name: str
    base_url: str
    api_key: str = "local"
    models: List[DiscoveredModel] = field(default_factory=list)
    latency_ms: float = 0.0


def _estimate_model_params(model_id: str) -> Tuple[float, int, bool]:
    """
    Estimate parameter count (B), capability tier, and whether it's an embedding model.

    Returns (param_b, tier, is_embedding).
    Tier: 1=small (<4B), 2=medium (4-10B), 3=large (10-30B), 4=xlarge (30B+)
    """
    mid = model_id.lower()

    # Embedding models
    if any(kw in mid for kw in ['embed', 'embedding', 'bge', 'e5-', 'nomic-embed']):
        return 0.0, 0, True

    # Extract parameter count from model name
    param_b = 0.0
    # Match patterns like "7b", "14b", "70b", "1.5b", "0.5b"
    m = re.search(r'(\d+\.?\d*)\s*[bB]', mid)
    if m:
        param_b = float(m.group(1))
    # Common model size aliases
    elif 'small' in mid:
        param_b = 1.0
    elif 'mini' in mid:
        param_b = 3.5
    elif 'medium' in mid:
        param_b = 7.0
    elif 'large' in mid or 'next' in mid:
        param_b = 14.0
    elif 'xl' in mid:
        param_b = 30.0

    # Determine tier
    if param_b >= 30:
        tier = 4
    elif param_b >= 10:
        tier = 3
    elif param_b >= 4:
        tier = 2
    elif param_b > 0:
        tier = 1
    else:
        # Unknown size — default to medium
        tier = 2
        param_b = 7.0

    # Bonus tier for known high-quality model families
    quality_families = ['qwen3', 'qwen2.5', 'llama3', 'mistral', 'gemma2', 'phi-3', 'deepseek']
    if any(f in mid for f in quality_families):
        tier = min(tier + 1, 4) if param_b >= 7 else tier

    # Coder models get a boost for structured output tasks
    if 'coder' in mid or 'code' in mid:
        tier = min(tier + 1, 4)

    # Large context window models (Nemotron, etc.) get tier boost for report tasks
    if any(kw in mid for kw in ['nemotron', '128k', '131k', 'yarn']):
        tier = min(tier + 1, 4)

    return param_b, tier, False


def discover_providers() -> List[DiscoveredProvider]:
    """
    Scan known ports for local LLM providers.

    Non-blocking, fast: 2s timeout per port, skips unresponsive ones.
    """
    providers = []

    for port, hint in _KNOWN_PORTS:
        if port == 5001:
            continue  # Skip our own backend

        base_url_v1 = f"http://localhost:{port}/v1"
        base_url_raw = f"http://localhost:{port}"
        api_key = "local"

        # Special handling for Ollama (needs "ollama" as api_key)
        if hint == "ollama":
            api_key = "ollama"

        # Probe OpenAI-compatible /v1/models
        try:
            start = time.monotonic()
            r = httpx.get(
                f"{base_url_v1}/models",
                timeout=httpx.Timeout(_PROBE_TIMEOUT, connect=1.0),
            )
            latency = (time.monotonic() - start) * 1000

            if r.status_code == 200:
                data = r.json()
                model_list = data.get("data", [])
                if model_list:
                    provider = DiscoveredProvider(
                        name=hint or f"openai_compat_{port}",
                        base_url=base_url_v1,
                        api_key=api_key,
                        latency_ms=latency,
                    )
                    for m in model_list:
                        mid = m.get("id", "")
                        if not mid:
                            continue
                        param_b, tier, is_emb = _estimate_model_params(mid)
                        provider.models.append(DiscoveredModel(
                            model_id=mid,
                            provider_name=provider.name,
                            base_url=base_url_v1,
                            api_key=api_key,
                            tier=tier,
                            param_b=param_b,
                            is_embedding=is_emb,
                        ))
                    providers.append(provider)
                    logger.info(
                        f"Discovered {provider.name} at port {port}: "
                        f"{len(provider.models)} models, {latency:.0f}ms"
                    )
                    continue
        except Exception:
            pass

        # Probe Ollama-native /api/tags as fallback
        try:
            start = time.monotonic()
            r = httpx.get(
                f"{base_url_raw}/api/tags",
                timeout=httpx.Timeout(_PROBE_TIMEOUT, connect=1.0),
            )
            latency = (time.monotonic() - start) * 1000

            if r.status_code == 200:
                data = r.json()
                model_list = data.get("models", [])
                if model_list:
                    provider = DiscoveredProvider(
                        name=hint or f"ollama_{port}",
                        base_url=base_url_v1,  # Use /v1 for OpenAI compat
                        api_key="ollama",
                        latency_ms=latency,
                    )
                    for m in model_list:
                        mid = m.get("name", m.get("model", ""))
                        if not mid:
                            continue
                        param_b, tier, is_emb = _estimate_model_params(mid)
                        provider.models.append(DiscoveredModel(
                            model_id=mid,
                            provider_name=provider.name,
                            base_url=base_url_v1,
                            api_key="ollama",
                            tier=tier,
                            param_b=param_b,
                            is_embedding=is_emb,
                        ))
                    providers.append(provider)
                    logger.info(
                        f"Discovered {provider.name} (Ollama) at port {port}: "
                        f"{len(provider.models)} models, {latency:.0f}ms"
                    )
        except Exception:
            pass

    return providers


def choose_best_assignment(providers: List[DiscoveredProvider]) -> Dict[str, Dict[str, str]]:
    """
    Given discovered providers, choose the best model for each task type.

    Strategy:
    - Quality-sensitive tasks (report, graph_tools) → highest tier model
    - Throughput-sensitive tasks (profile_gen, sim_config) → fast model on lowest-latency provider
    - Default → best overall model
    - Embedding → dedicated embedding model if available

    Returns dict of {task_prefix: {"api_key": ..., "base_url": ..., "model": ...}}
    """
    # Collect all generation models (non-embedding) sorted by tier desc, then param desc
    all_gen = []
    for p in providers:
        for m in p.models:
            if not m.is_embedding:
                all_gen.append((m, p))

    if not all_gen:
        logger.warning("No generation models found across any provider")
        return {}

    # Sort by tier descending, then param_b descending, then provider latency ascending
    all_gen.sort(key=lambda x: (-x[0].tier, -x[0].param_b, x[1].latency_ms))

    best_quality = all_gen[0]  # Highest tier/params
    # Best throughput: prefer smaller model on fastest provider
    all_gen_by_speed = sorted(all_gen, key=lambda x: (x[1].latency_ms, x[0].param_b))
    best_throughput = all_gen_by_speed[0]

    # If we have multiple providers, spread load
    assignments = {}

    def _assign(prefix: str, model: DiscoveredModel, provider: DiscoveredProvider):
        assignments[prefix] = {
            "api_key": model.api_key,
            "base_url": model.base_url,
            "model": model.model_id,
            "provider": provider.name,
            "tier": model.tier,
            "param_b": model.param_b,
        }

    # Default = best quality model
    _assign("LLM", best_quality[0], best_quality[1])

    # Quality-sensitive tasks → best quality
    _assign("REPORT_LLM", best_quality[0], best_quality[1])
    _assign("GRAPH_TOOLS_LLM", best_quality[0], best_quality[1])

    # Throughput-sensitive tasks → fastest (can be smaller model)
    _assign("PROFILE_LLM", best_throughput[0], best_throughput[1])
    _assign("SIM_CONFIG_LLM", best_throughput[0], best_throughput[1])
    _assign("ENRICHMENT_LLM", best_throughput[0], best_throughput[1])

    # If there are multiple providers, try to spread quality tasks
    # to a different provider than throughput tasks
    if len(providers) > 1:
        # Find best model on a DIFFERENT provider than throughput
        throughput_provider = best_throughput[1].name
        other_quality = [
            (m, p) for m, p in all_gen
            if p.name != throughput_provider
        ]
        if other_quality:
            alt = other_quality[0]
            if alt[0].tier >= best_quality[0].tier - 1:
                # Good enough quality on another provider — spread the load
                _assign("REPORT_LLM", alt[0], alt[1])
                _assign("GRAPH_TOOLS_LLM", alt[0], alt[1])
                logger.info(
                    f"Load-spreading: quality tasks → {alt[1].name}, "
                    f"throughput tasks → {throughput_provider}"
                )

    return assignments


def apply_assignments(assignments: Dict[str, Dict[str, str]], dry_run: bool = False) -> Dict[str, str]:
    """
    Apply model assignments as environment variables.

    Returns dict of {env_var: value} that were set.
    """
    changes = {}

    for prefix, config in assignments.items():
        api_key_var = f"{prefix}_API_KEY"
        base_url_var = f"{prefix}_BASE_URL"
        model_var = f"{prefix}_MODEL_NAME"

        api_key = config["api_key"]
        base_url = config["base_url"]
        model = config["model"]

        if not dry_run:
            os.environ[api_key_var] = api_key
            os.environ[base_url_var] = base_url
            os.environ[model_var] = model

        changes[api_key_var] = api_key
        changes[base_url_var] = base_url
        changes[model_var] = model

    return changes


def suggest_models() -> List[Dict[str, str]]:
    """
    Suggest models to download based on current provider capabilities and gaps.

    Analyzes what's available vs what would improve the pipeline, and returns
    actionable recommendations with download commands.
    """
    providers = discover_providers()

    all_models = []
    for p in providers:
        for m in p.models:
            all_models.append(m)

    gen_models = [m for m in all_models if not m.is_embedding]
    emb_models = [m for m in all_models if m.is_embedding]

    suggestions = []

    # Check for large context window model (reports, complex reasoning)
    has_large_ctx = any(
        kw in m.model_id.lower()
        for m in gen_models
        for kw in ['nemotron', '128k', '131k', 'long', 'yarn']
    )
    if not has_large_ctx:
        suggestions.append({
            "model": "nvidia/nemotron-mini:latest",
            "reason": "Large context window (128K tokens) for report generation and complex multi-section analysis. Handles full simulation transcripts without chunking.",
            "task": "REPORT",
            "download": "ollama pull nvidia/nemotron-mini",
            "priority": "high",
        })

    # Check for fast small model (throughput)
    has_fast = any(m.param_b <= 4 and m.param_b > 0 for m in gen_models)
    if not has_fast:
        suggestions.append({
            "model": "qwen2.5:3b",
            "reason": "Fast 3B model for high-throughput tasks (profile gen batch). 3-5x faster than 14B with acceptable quality for structured output.",
            "task": "PROFILE_GEN",
            "download": "ollama pull qwen2.5:3b",
            "priority": "medium",
        })

    # Check for coding-specialized model
    has_coder = any('coder' in m.model_id.lower() or 'code' in m.model_id.lower() for m in gen_models)
    if not has_coder:
        suggestions.append({
            "model": "qwen2.5-coder:14b",
            "reason": "Code-specialized model for structured JSON output, NER extraction, and ontology generation. Better at following JSON schemas.",
            "task": "ENRICHMENT, SIM_CONFIG",
            "download": "ollama pull qwen2.5-coder:14b",
            "priority": "medium",
        })

    # Check for embedding model
    if not emb_models:
        suggestions.append({
            "model": "nomic-embed-text",
            "reason": "Required for graph vector search. 768-dim embeddings, fast and accurate.",
            "task": "EMBEDDING",
            "download": "ollama pull nomic-embed-text",
            "priority": "critical",
        })

    # Check for high-quality large model (if only small models available)
    max_param = max((m.param_b for m in gen_models), default=0)
    if max_param < 10:
        suggestions.append({
            "model": "qwen2.5:14b",
            "reason": "14B parameter model for quality-sensitive tasks. Significant quality improvement over smaller models for reports and analysis.",
            "task": "REPORT, GRAPH_TOOLS",
            "download": "ollama pull qwen2.5:14b",
            "priority": "high",
        })

    # Always suggest Nemotron if not present (large context is a game-changer for reports)
    has_nemotron = any('nemotron' in m.model_id.lower() for m in gen_models)
    if not has_nemotron and has_large_ctx:
        pass  # Already have a large-ctx model
    elif not has_nemotron:
        # Already added above in large_ctx check
        pass

    return suggestions


def discover_and_configure(dry_run: bool = False) -> Dict:
    """
    Full discovery + configuration pipeline.

    1. Scan for local providers
    2. Choose optimal model assignments
    3. Set environment variables
    4. Return summary

    Args:
        dry_run: If True, don't actually set env vars, just return what would change.

    Returns dict with discovery results and assignments.
    """
    logger.info("Starting LLM provider discovery...")
    providers = discover_providers()

    if not providers:
        logger.warning("No local LLM providers found")
        return {"providers": [], "assignments": {}, "changes": {}}

    assignments = choose_best_assignment(providers)
    changes = apply_assignments(assignments, dry_run=dry_run)

    # Generate model suggestions
    suggestions = suggest_models()

    summary = {
        "providers": [
            {
                "name": p.name,
                "base_url": p.base_url,
                "latency_ms": round(p.latency_ms, 1),
                "models": [
                    {
                        "id": m.model_id,
                        "tier": m.tier,
                        "param_b": m.param_b,
                        "is_embedding": m.is_embedding,
                    }
                    for m in p.models
                ],
            }
            for p in providers
        ],
        "assignments": {
            prefix: {
                "model": c["model"],
                "provider": c["provider"],
                "tier": c["tier"],
            }
            for prefix, c in assignments.items()
        },
        "suggestions": suggestions,
        "changes": changes,
        "dry_run": dry_run,
    }

    if not dry_run:
        logger.info(f"Applied {len(changes)} env var changes from {len(providers)} providers")
        for prefix, config in assignments.items():
            logger.info(f"  {prefix}: {config['model']} @ {config['provider']} (tier {config['tier']})")

    return summary
