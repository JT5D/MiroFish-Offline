"""
LLM Router — multi-provider routing with health-aware fallback chains.

Design principles:
- KB-first: Local Ollama = full functionality with zero cloud tokens.
  Cloud providers are optional accelerators, not requirements.
- Non-blocking: Health checks cached 30s, never block pipeline.
  Provider down = skip to next in chain.
- Backward compatible: No new env vars = identical behavior to today.

Each task type can have its own provider chain configured via env vars:
  {PREFIX}_LLM_API_KEY, {PREFIX}_LLM_BASE_URL, {PREFIX}_LLM_MODEL_NAME

Every chain ends with the default Ollama provider as last-resort fallback.
"""

import os
import time
import threading
from enum import Enum
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field

import httpx

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.llm_router')

# Health check cache TTL in seconds
_HEALTH_CACHE_TTL = 30.0


class TaskType(Enum):
    """LLM task types — each can have its own provider chain."""
    DEFAULT = "default"
    PROFILE_GEN = "profile"
    SIM_CONFIG = "sim_config"
    REPORT = "report"
    SIMULATION = "simulation"
    SIM_BOOST = "sim_boost"
    ENRICHMENT = "enrichment"
    GRAPH_TOOLS = "graph_tools"


# Env var prefix mapping for each task type
_TASK_ENV_PREFIX: Dict[TaskType, str] = {
    TaskType.PROFILE_GEN: "PROFILE",
    TaskType.SIM_CONFIG: "SIM_CONFIG",
    TaskType.REPORT: "REPORT",
    TaskType.SIMULATION: "SIMULATION",
    TaskType.SIM_BOOST: "SIM_BOOST",
    TaskType.ENRICHMENT: "ENRICHMENT",
    TaskType.GRAPH_TOOLS: "GRAPH_TOOLS",
}


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""
    name: str
    api_key: str
    base_url: str
    model: str

    def __repr__(self):
        return f"ProviderConfig(name={self.name!r}, base_url={self.base_url!r}, model={self.model!r})"


@dataclass
class ProviderHealth:
    """Cached health status for a provider base_url."""
    is_healthy: bool = True
    last_checked: float = 0.0
    last_latency_ms: float = 0.0
    consecutive_failures: int = 0


class LLMRouter:
    """
    Multi-provider LLM router with health-aware fallback.

    Singleton — use get_router() to obtain the instance.

    For each task type, builds a provider chain from env vars.
    The chain always ends with the default provider (Ollama).
    get_provider() walks the chain and returns the first healthy provider.
    """

    def __init__(self):
        self._health: Dict[str, ProviderHealth] = {}
        self._lock = threading.Lock()
        self._chains: Dict[TaskType, List[ProviderConfig]] = {}
        self._default_provider: Optional[ProviderConfig] = None

        self._build_chains()

    def _build_chains(self):
        """Build provider chains from environment variables."""
        # Default provider (always Ollama unless overridden)
        default_key = Config.LLM_API_KEY or "ollama"
        default_url = Config.LLM_BASE_URL or "http://localhost:11434/v1"
        default_model = Config.LLM_MODEL_NAME or "qwen2.5:14b"

        self._default_provider = ProviderConfig(
            name="default",
            api_key=default_key,
            base_url=default_url,
            model=default_model,
        )

        # Build per-task chains
        for task_type, prefix in _TASK_ENV_PREFIX.items():
            chain = self._build_chain_for_prefix(prefix)
            # Always end with default
            if not chain or chain[-1].base_url != default_url:
                chain.append(self._default_provider)
            self._chains[task_type] = chain

        # DEFAULT task type just uses the default provider
        self._chains[TaskType.DEFAULT] = [self._default_provider]

        # Log chain summary
        for task_type, chain in self._chains.items():
            names = [p.name for p in chain]
            if len(names) > 1 or names[0] != "default":
                logger.info(f"Router chain {task_type.value}: {' -> '.join(names)}")

    def _build_chain_for_prefix(self, prefix: str) -> List[ProviderConfig]:
        """Build a provider chain from env vars with the given prefix."""
        chain = []

        api_key = os.environ.get(f"{prefix}_LLM_API_KEY", "")
        base_url = os.environ.get(f"{prefix}_LLM_BASE_URL", "")
        model = os.environ.get(f"{prefix}_LLM_MODEL_NAME", "")

        if api_key and base_url and model:
            chain.append(ProviderConfig(
                name=prefix.lower(),
                api_key=api_key,
                base_url=base_url,
                model=model,
            ))

        return chain

    def get_provider(self, task_type: TaskType = TaskType.DEFAULT) -> Optional[ProviderConfig]:
        """
        Get the first healthy provider for the given task type.

        Walks the chain in order, returns first healthy provider.
        Returns None only if ALL providers (including default Ollama) are down.
        """
        chain = self._chains.get(task_type, [self._default_provider])

        for provider in chain:
            if self._is_healthy(provider.base_url):
                return provider

        # All down — return default anyway (let the caller handle the error)
        logger.warning(f"All providers unhealthy for {task_type.value}, returning default")
        return self._default_provider

    def _is_healthy(self, base_url: str) -> bool:
        """
        Check if a provider is healthy. Uses cached result if fresh enough.

        Optimistic on first check: returns True immediately and probes in background.
        """
        now = time.monotonic()

        with self._lock:
            health = self._health.get(base_url)

            if health is None:
                # First time seeing this URL — optimistic, probe in background
                self._health[base_url] = ProviderHealth(
                    is_healthy=True,
                    last_checked=now,
                )
                self._probe_background(base_url)
                return True

            if (now - health.last_checked) < _HEALTH_CACHE_TTL:
                return health.is_healthy

        # Cache expired — probe in background, return last known state
        self._probe_background(base_url)
        return health.is_healthy

    def _probe_background(self, base_url: str):
        """Run a health probe in a background thread."""
        thread = threading.Thread(
            target=self._probe_sync, args=(base_url,), daemon=True
        )
        thread.start()

    def _probe_sync(self, base_url: str):
        """Synchronous health probe. Updates cached health state."""
        start = time.monotonic()
        is_healthy = False

        try:
            # Try OpenAI-compatible endpoint first
            probe_url = base_url.rstrip("/")
            if "/v1" in probe_url:
                probe_url = probe_url.split("/v1")[0]

            # Try Ollama /api/tags first, then /v1/models
            for path in ["/api/tags", "/v1/models"]:
                try:
                    r = httpx.get(
                        f"{probe_url}{path}",
                        timeout=httpx.Timeout(5.0, connect=2.0),
                    )
                    if r.status_code < 500:
                        is_healthy = True
                        break
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"Health probe failed for {base_url}: {e}")

        elapsed_ms = (time.monotonic() - start) * 1000

        with self._lock:
            health = self._health.get(base_url)
            if health is None:
                health = ProviderHealth()
                self._health[base_url] = health

            health.is_healthy = is_healthy
            health.last_checked = time.monotonic()
            health.last_latency_ms = elapsed_ms

            if is_healthy:
                health.consecutive_failures = 0
            else:
                health.consecutive_failures += 1
                logger.warning(
                    f"Provider unhealthy: {base_url} "
                    f"(failures={health.consecutive_failures})"
                )

    def get_status(self) -> Dict[str, Dict]:
        """Get health status of all known providers. For monitoring."""
        with self._lock:
            status = {}
            for url, health in self._health.items():
                status[url] = {
                    "is_healthy": health.is_healthy,
                    "last_latency_ms": round(health.last_latency_ms, 1),
                    "consecutive_failures": health.consecutive_failures,
                    "cache_age_s": round(time.monotonic() - health.last_checked, 1),
                }
            return status

    def get_chains(self) -> Dict[str, List[str]]:
        """Get chain configuration for all task types. For debugging."""
        return {
            task_type.value: [f"{p.name}({p.model})" for p in chain]
            for task_type, chain in self._chains.items()
        }


# ── Singleton ──────────────────────────────────────────────────────────

_router_instance: Optional[LLMRouter] = None
_router_lock = threading.Lock()


def get_router() -> LLMRouter:
    """Get or create the singleton LLMRouter instance."""
    global _router_instance
    if _router_instance is None:
        with _router_lock:
            if _router_instance is None:
                _router_instance = LLMRouter()
                logger.info("LLMRouter initialized")
    return _router_instance
