"""
Dynamic Configuration — auto-tune parallelism, memory, and concurrency based on system resources.

Runs at startup and periodically to adapt to changing conditions.
Priority: never slow down the computer or other processes.

Usage:
    from app.utils.dynamic_config import get_dynamic_config
    config = get_dynamic_config()
    workers = config.profile_gen_workers
"""

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from ..utils.logger import get_logger

logger = get_logger('mirofish.dynamic_config')


@dataclass
class DynamicConfig:
    """Runtime-tuned configuration values."""
    # Profile generation parallelism (1-4)
    profile_gen_workers: int = 2
    # Simulation batch size
    sim_batch_size: int = 5
    # LLM request concurrency limit
    llm_concurrency: int = 2
    # Neo4j connection pool size
    neo4j_pool_size: int = 50
    # Available system memory GB
    available_memory_gb: float = 0.0
    # CPU count
    cpu_count: int = 1
    # Whether system is under pressure
    system_under_pressure: bool = False
    # Last evaluation time
    last_evaluated: float = 0.0


def _get_available_memory_gb() -> float:
    """Get available system memory in GB. macOS + Linux compatible."""
    try:
        import subprocess
        if os.uname().sysname == 'Darwin':
            # macOS: use vm_stat
            result = subprocess.run(
                ['vm_stat'], capture_output=True, text=True, timeout=2
            )
            lines = result.stdout.strip().split('\n')
            page_size = 16384  # Default on Apple Silicon
            free_pages = 0
            for line in lines:
                if 'page size' in line.lower():
                    try:
                        page_size = int(''.join(c for c in line.split()[-1] if c.isdigit()))
                    except (ValueError, IndexError):
                        pass
                if 'Pages free' in line:
                    free_pages += int(line.split(':')[1].strip().rstrip('.'))
                if 'Pages inactive' in line:
                    free_pages += int(line.split(':')[1].strip().rstrip('.'))
            return (free_pages * page_size) / (1024 ** 3)
        else:
            # Linux: /proc/meminfo
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass
    return 8.0  # Assume 8GB if detection fails


def _get_load_average() -> float:
    """Get 1-minute load average."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def evaluate_config() -> DynamicConfig:
    """
    Evaluate system resources and return tuned configuration.

    Priority: never slow down the computer.
    """
    config = DynamicConfig()
    config.last_evaluated = time.monotonic()

    try:
        config.cpu_count = os.cpu_count() or 1
        config.available_memory_gb = _get_available_memory_gb()
        load_avg = _get_load_average()

        # System pressure: load > 80% of CPUs or <2GB free memory
        load_ratio = load_avg / max(config.cpu_count, 1)
        config.system_under_pressure = (
            load_ratio > 0.8 or config.available_memory_gb < 2.0
        )

        if config.system_under_pressure:
            # Back off: minimal resource usage
            config.profile_gen_workers = 1
            config.sim_batch_size = 3
            config.llm_concurrency = 1
            config.neo4j_pool_size = 25
            logger.info(
                f"System under pressure (load={load_avg:.1f}/{config.cpu_count}cpu, "
                f"mem={config.available_memory_gb:.1f}GB) — reducing concurrency"
            )
        elif config.available_memory_gb > 8.0 and load_ratio < 0.4:
            # System has headroom — increase parallelism
            config.profile_gen_workers = min(3, config.cpu_count // 2)
            config.sim_batch_size = 8
            config.llm_concurrency = 3
            config.neo4j_pool_size = 100
            logger.info(
                f"System has headroom (load={load_avg:.1f}/{config.cpu_count}cpu, "
                f"mem={config.available_memory_gb:.1f}GB) — increased concurrency"
            )
        else:
            # Normal: default conservative settings
            config.profile_gen_workers = 2
            config.sim_batch_size = 5
            config.llm_concurrency = 2
            config.neo4j_pool_size = 50

    except Exception as e:
        logger.warning(f"Dynamic config evaluation failed: {e}")

    return config


# ── Singleton with periodic re-evaluation ─────────────────────────────

_config_instance: Optional[DynamicConfig] = None
_config_lock = threading.Lock()
_EVAL_INTERVAL = 60.0  # Re-evaluate every 60 seconds


def get_dynamic_config() -> DynamicConfig:
    """Get current dynamic configuration. Re-evaluates periodically."""
    global _config_instance
    now = time.monotonic()

    if _config_instance is None or (now - _config_instance.last_evaluated) > _EVAL_INTERVAL:
        with _config_lock:
            if _config_instance is None or (now - _config_instance.last_evaluated) > _EVAL_INTERVAL:
                _config_instance = evaluate_config()

    return _config_instance
