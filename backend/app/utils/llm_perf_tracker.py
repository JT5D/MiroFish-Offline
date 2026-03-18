"""
LLM Performance Tracker — auto-learns and tunes provider/model assignments.

Tracks per-provider per-task metrics (latency, success rate, tokens/sec),
and dynamically re-routes tasks to optimal providers based on observed performance.

Zero overhead when disabled. Light overhead when enabled (~1KB per call record).

Usage:
    from app.utils.llm_perf_tracker import get_tracker
    tracker = get_tracker()
    tracker.record_call(task_type, provider_name, latency_s, success, tokens)
    recommendations = tracker.get_recommendations()
"""

import os
import time
import threading
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from ..utils.logger import get_logger

logger = get_logger('mirofish.perf_tracker')

# Keep last N records per provider+task combo
_MAX_RECORDS = 50
# Re-evaluate routing every N calls
_EVAL_INTERVAL = 10
# Minimum calls before making recommendations
_MIN_CALLS_FOR_RECOMMENDATION = 5


@dataclass
class CallRecord:
    """Single LLM call record."""
    timestamp: float
    latency_s: float
    success: bool
    tokens: int = 0
    error_type: str = ""


@dataclass
class ProviderTaskStats:
    """Aggregated stats for a provider+task combination."""
    total_calls: int = 0
    success_count: int = 0
    total_latency_s: float = 0.0
    total_tokens: int = 0
    min_latency_s: float = float('inf')
    max_latency_s: float = 0.0
    recent_records: List[CallRecord] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.total_calls, 1)

    @property
    def avg_latency_s(self) -> float:
        return self.total_latency_s / max(self.success_count, 1)

    @property
    def tokens_per_sec(self) -> float:
        if self.total_latency_s <= 0:
            return 0.0
        return self.total_tokens / self.total_latency_s

    @property
    def recent_success_rate(self) -> float:
        """Success rate over last 10 calls."""
        recent = self.recent_records[-10:]
        if not recent:
            return 1.0
        return sum(1 for r in recent if r.success) / len(recent)

    @property
    def recent_avg_latency_s(self) -> float:
        """Average latency over last 10 successful calls."""
        recent = [r for r in self.recent_records[-10:] if r.success]
        if not recent:
            return self.avg_latency_s
        return sum(r.latency_s for r in recent) / len(recent)


class PerfTracker:
    """
    Tracks LLM call performance and generates routing recommendations.

    Thread-safe. Records are kept in memory (no disk I/O).
    """

    def __init__(self):
        self._stats: Dict[str, ProviderTaskStats] = defaultdict(ProviderTaskStats)
        self._lock = threading.Lock()
        self._call_count = 0
        self._last_eval_time = time.monotonic()
        self._recommendations: Dict[str, Dict] = {}
        self._system_metrics: Dict[str, float] = {}

    def _key(self, task_type: str, provider_name: str) -> str:
        return f"{task_type}:{provider_name}"

    def record_call(
        self,
        task_type: str,
        provider_name: str,
        latency_s: float,
        success: bool,
        tokens: int = 0,
        error_type: str = "",
    ):
        """Record a single LLM call's performance metrics."""
        record = CallRecord(
            timestamp=time.monotonic(),
            latency_s=latency_s,
            success=success,
            tokens=tokens,
            error_type=error_type,
        )

        key = self._key(task_type, provider_name)

        with self._lock:
            stats = self._stats[key]
            stats.total_calls += 1
            if success:
                stats.success_count += 1
                stats.total_latency_s += latency_s
                stats.total_tokens += tokens
                stats.min_latency_s = min(stats.min_latency_s, latency_s)
                stats.max_latency_s = max(stats.max_latency_s, latency_s)

            stats.recent_records.append(record)
            if len(stats.recent_records) > _MAX_RECORDS:
                stats.recent_records = stats.recent_records[-_MAX_RECORDS:]

            self._call_count += 1

            # Auto-evaluate periodically
            if self._call_count % _EVAL_INTERVAL == 0:
                self._evaluate_unlocked()

    def _evaluate_unlocked(self):
        """Evaluate performance and generate recommendations. Caller holds lock."""
        self._last_eval_time = time.monotonic()

        # Group stats by task type
        task_providers: Dict[str, List[Tuple[str, ProviderTaskStats]]] = defaultdict(list)
        for key, stats in self._stats.items():
            parts = key.split(":", 1)
            if len(parts) == 2:
                task_type, provider = parts
                task_providers[task_type].append((provider, stats))

        recommendations = {}

        for task_type, providers in task_providers.items():
            if len(providers) < 2:
                continue  # Need at least 2 providers to compare

            # Score each provider: balance success rate, latency, throughput
            scored = []
            for provider_name, stats in providers:
                if stats.total_calls < _MIN_CALLS_FOR_RECOMMENDATION:
                    continue

                # Score: weighted combination (higher = better)
                # Success rate is most important, then latency, then throughput
                success_score = stats.recent_success_rate * 40
                latency_score = max(0, 30 - stats.recent_avg_latency_s * 2)  # Penalize slow
                throughput_score = min(stats.tokens_per_sec / 10, 20)  # Cap at 20

                total_score = success_score + latency_score + throughput_score

                scored.append({
                    "provider": provider_name,
                    "score": round(total_score, 1),
                    "success_rate": round(stats.recent_success_rate, 3),
                    "avg_latency_s": round(stats.recent_avg_latency_s, 2),
                    "tokens_per_sec": round(stats.tokens_per_sec, 1),
                    "total_calls": stats.total_calls,
                })

            if scored:
                scored.sort(key=lambda x: -x["score"])
                best = scored[0]
                current_best = recommendations.get(task_type, {}).get("provider")

                recommendations[task_type] = {
                    "recommended": best["provider"],
                    "score": best["score"],
                    "all_providers": scored,
                    "should_switch": (
                        current_best is not None
                        and current_best != best["provider"]
                    ),
                }

        self._recommendations = recommendations

    def get_recommendations(self) -> Dict[str, Dict]:
        """Get current routing recommendations."""
        with self._lock:
            return dict(self._recommendations)

    def get_stats(self) -> Dict[str, Dict]:
        """Get all performance stats. For monitoring."""
        with self._lock:
            result = {}
            for key, stats in self._stats.items():
                result[key] = {
                    "total_calls": stats.total_calls,
                    "success_rate": round(stats.success_rate, 3),
                    "avg_latency_s": round(stats.avg_latency_s, 2),
                    "tokens_per_sec": round(stats.tokens_per_sec, 1),
                    "min_latency_s": round(stats.min_latency_s, 2) if stats.min_latency_s != float('inf') else 0,
                    "max_latency_s": round(stats.max_latency_s, 2),
                    "recent_success_rate": round(stats.recent_success_rate, 3),
                    "recent_avg_latency_s": round(stats.recent_avg_latency_s, 2),
                }
            return result

    def get_summary(self) -> Dict:
        """Compact summary for status endpoints."""
        with self._lock:
            return {
                "total_calls": self._call_count,
                "providers_tracked": len(set(
                    k.split(":")[1] for k in self._stats.keys() if ":" in k
                )),
                "tasks_tracked": len(set(
                    k.split(":")[0] for k in self._stats.keys() if ":" in k
                )),
                "recommendations": {
                    k: v["recommended"]
                    for k, v in self._recommendations.items()
                },
            }

    def get_bottlenecks(self) -> List[Dict]:
        """Identify current bottlenecks — slow or failing providers."""
        bottlenecks = []

        with self._lock:
            for key, stats in self._stats.items():
                if stats.total_calls < 3:
                    continue

                parts = key.split(":", 1)
                if len(parts) != 2:
                    continue
                task_type, provider = parts

                # Flag: low success rate
                if stats.recent_success_rate < 0.7:
                    bottlenecks.append({
                        "type": "low_success_rate",
                        "task": task_type,
                        "provider": provider,
                        "value": round(stats.recent_success_rate, 2),
                        "severity": "high" if stats.recent_success_rate < 0.5 else "medium",
                        "action": f"Consider switching {task_type} away from {provider}",
                    })

                # Flag: high latency (>30s average)
                if stats.recent_avg_latency_s > 30:
                    bottlenecks.append({
                        "type": "high_latency",
                        "task": task_type,
                        "provider": provider,
                        "value": round(stats.recent_avg_latency_s, 1),
                        "severity": "high" if stats.recent_avg_latency_s > 60 else "medium",
                        "action": f"Use smaller model or faster provider for {task_type}",
                    })

                # Flag: low throughput (<5 tokens/sec)
                if stats.tokens_per_sec > 0 and stats.tokens_per_sec < 5:
                    bottlenecks.append({
                        "type": "low_throughput",
                        "task": task_type,
                        "provider": provider,
                        "value": round(stats.tokens_per_sec, 1),
                        "severity": "medium",
                        "action": f"Consider smaller model or GPU offloading for {task_type}",
                    })

        return sorted(bottlenecks, key=lambda x: 0 if x["severity"] == "high" else 1)


# ── Singleton ──────────────────────────────────────────────────────────

_tracker_instance: Optional[PerfTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> PerfTracker:
    """Get or create the singleton PerfTracker."""
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_lock:
            if _tracker_instance is None:
                _tracker_instance = PerfTracker()
    return _tracker_instance
