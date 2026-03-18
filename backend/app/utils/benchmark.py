"""
Benchmark — persistent performance tracking across models, configs, simulations.

Stores every LLM call + simulation run in SQLite. Survives restarts.
Answers: which model/provider/config is best for each task type?

Usage:
    from app.utils.benchmark import get_bench
    bench = get_bench()
    bench.log_llm_call(task, provider, model, latency, success, tokens)
    bench.log_sim_run(sim_id, config, metrics)
    leaderboard = bench.leaderboard()
"""

import os
import time
import json
import sqlite3
import threading
from typing import Dict, List, Optional
from contextlib import contextmanager

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.benchmark')

_DB_NAME = "benchmark.db"


class Benchmark:
    """Persistent performance tracker backed by SQLite."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or os.path.join(
            Config.UPLOAD_FOLDER, _DB_NAME
        )
        self._local = threading.local()
        self._init_db()

    @contextmanager
    def _conn(self):
        """Thread-local SQLite connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield self._local.conn
        except Exception:
            self._local.conn.rollback()
            raise

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    task_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT DEFAULT '',
                    latency_s REAL NOT NULL,
                    success INTEGER NOT NULL,
                    tokens INTEGER DEFAULT 0,
                    error_type TEXT DEFAULT '',
                    sim_id TEXT DEFAULT '',
                    config_hash TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sim_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    simulation_id TEXT NOT NULL,
                    project_id TEXT DEFAULT '',
                    platform TEXT DEFAULT '',
                    total_rounds INTEGER DEFAULT 0,
                    total_actions INTEGER DEFAULT 0,
                    twitter_actions INTEGER DEFAULT 0,
                    reddit_actions INTEGER DEFAULT 0,
                    duration_s REAL DEFAULT 0,
                    entities_count INTEGER DEFAULT 0,
                    model_default TEXT DEFAULT '',
                    model_profile TEXT DEFAULT '',
                    model_sim TEXT DEFAULT '',
                    model_report TEXT DEFAULT '',
                    config_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS model_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    score REAL NOT NULL,
                    detail TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_llm_task ON llm_calls(task_type);
                CREATE INDEX IF NOT EXISTS idx_llm_model ON llm_calls(model);
                CREATE INDEX IF NOT EXISTS idx_sim_id ON sim_runs(simulation_id);
            """)
            conn.commit()

    # ── LLM Call Logging ──────────────────────────────────────────────

    def log_llm_call(
        self,
        task_type: str,
        provider: str,
        model: str,
        latency_s: float,
        success: bool,
        tokens: int = 0,
        error_type: str = "",
        base_url: str = "",
        sim_id: str = "",
    ):
        """Record a single LLM call."""
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO llm_calls
                       (ts, task_type, provider, model, base_url, latency_s, success, tokens, error_type, sim_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), task_type, provider, model, base_url,
                     latency_s, 1 if success else 0, tokens, error_type, sim_id),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Benchmark log_llm_call failed: {e}")

    # ── Simulation Run Logging ────────────────────────────────────────

    def log_sim_run(
        self,
        simulation_id: str,
        project_id: str = "",
        platform: str = "",
        total_rounds: int = 0,
        total_actions: int = 0,
        twitter_actions: int = 0,
        reddit_actions: int = 0,
        duration_s: float = 0,
        entities_count: int = 0,
        models: Optional[Dict[str, str]] = None,
        config: Optional[Dict] = None,
    ):
        """Record a simulation run with its configuration."""
        models = models or {}
        config = config or {}
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO sim_runs
                       (ts, simulation_id, project_id, platform, total_rounds,
                        total_actions, twitter_actions, reddit_actions, duration_s,
                        entities_count, model_default, model_profile, model_sim,
                        model_report, config_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (time.time(), simulation_id, project_id, platform,
                     total_rounds, total_actions, twitter_actions, reddit_actions,
                     duration_s, entities_count,
                     models.get('default', ''), models.get('profile', ''),
                     models.get('simulation', ''), models.get('report', ''),
                     json.dumps(config)),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"Benchmark log_sim_run failed: {e}")

    # ── Queries ───────────────────────────────────────────────────────

    def leaderboard(self, task_type: Optional[str] = None, limit: int = 10) -> List[Dict]:
        """
        Model leaderboard — ranked by composite score (success rate * speed).

        Returns best-performing models per task type.
        """
        where = "WHERE task_type = ?" if task_type else ""
        params = (task_type,) if task_type else ()

        try:
            with self._conn() as conn:
                rows = conn.execute(f"""
                    SELECT
                        model,
                        provider,
                        task_type,
                        COUNT(*) as total_calls,
                        SUM(success) as successes,
                        ROUND(AVG(CASE WHEN success=1 THEN latency_s END), 2) as avg_latency,
                        ROUND(CAST(SUM(success) AS REAL) / COUNT(*), 3) as success_rate,
                        SUM(tokens) as total_tokens,
                        ROUND(SUM(tokens) * 1.0 / NULLIF(SUM(CASE WHEN success=1 THEN latency_s END), 0), 1) as tokens_per_sec,
                        MIN(CASE WHEN success=1 THEN latency_s END) as best_latency,
                        MAX(CASE WHEN success=1 THEN latency_s END) as worst_latency
                    FROM llm_calls
                    {where}
                    GROUP BY model, provider, task_type
                    HAVING total_calls >= 3
                    ORDER BY success_rate DESC, avg_latency ASC
                    LIMIT ?
                """, (*params, limit)).fetchall()

                return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"Benchmark leaderboard failed: {e}")
            return []

    def sim_leaderboard(self, limit: int = 10) -> List[Dict]:
        """Simulation leaderboard — ranked by actions per round (engagement density)."""
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT
                        simulation_id,
                        project_id,
                        platform,
                        total_rounds,
                        total_actions,
                        entities_count,
                        ROUND(duration_s, 1) as duration_s,
                        ROUND(total_actions * 1.0 / NULLIF(total_rounds, 0), 1) as actions_per_round,
                        ROUND(total_actions * 1.0 / NULLIF(duration_s, 0) * 60, 1) as actions_per_min,
                        model_default,
                        model_profile,
                        model_sim,
                        datetime(ts, 'unixepoch', 'localtime') as ran_at
                    FROM sim_runs
                    ORDER BY actions_per_round DESC
                    LIMIT ?
                """, (limit,)).fetchall()

                return [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"Benchmark sim_leaderboard failed: {e}")
            return []

    def model_comparison(self, model_a: str, model_b: str) -> Dict:
        """Head-to-head comparison of two models across all task types."""
        try:
            with self._conn() as conn:
                result = {}
                for model in (model_a, model_b):
                    rows = conn.execute("""
                        SELECT
                            task_type,
                            COUNT(*) as calls,
                            ROUND(AVG(CASE WHEN success=1 THEN latency_s END), 2) as avg_latency,
                            ROUND(CAST(SUM(success) AS REAL) / COUNT(*), 3) as success_rate,
                            ROUND(SUM(tokens) * 1.0 / NULLIF(SUM(CASE WHEN success=1 THEN latency_s END), 0), 1) as tokens_per_sec
                        FROM llm_calls
                        WHERE model = ?
                        GROUP BY task_type
                    """, (model,)).fetchall()
                    result[model] = [dict(r) for r in rows]
                return result
        except Exception as e:
            logger.debug(f"Benchmark comparison failed: {e}")
            return {}

    def trends(self, hours: int = 24) -> Dict:
        """Performance trends over the last N hours."""
        cutoff = time.time() - (hours * 3600)
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT
                        task_type,
                        model,
                        provider,
                        COUNT(*) as calls,
                        ROUND(AVG(CASE WHEN success=1 THEN latency_s END), 2) as avg_latency,
                        ROUND(CAST(SUM(success) AS REAL) / COUNT(*), 3) as success_rate
                    FROM llm_calls
                    WHERE ts > ?
                    GROUP BY task_type, model, provider
                    ORDER BY calls DESC
                """, (cutoff,)).fetchall()

                return {
                    "period_hours": hours,
                    "data": [dict(r) for r in rows],
                    "total_calls": sum(dict(r)["calls"] for r in rows),
                }
        except Exception as e:
            logger.debug(f"Benchmark trends failed: {e}")
            return {"period_hours": hours, "data": [], "total_calls": 0}

    def summary(self) -> Dict:
        """Compact summary for status endpoints."""
        try:
            with self._conn() as conn:
                total = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
                sim_total = conn.execute("SELECT COUNT(*) FROM sim_runs").fetchone()[0]
                models = conn.execute("SELECT DISTINCT model FROM llm_calls").fetchall()
                return {
                    "total_llm_calls": total,
                    "total_sim_runs": sim_total,
                    "models_tracked": [r[0] for r in models],
                }
        except Exception as e:
            return {"error": str(e)}


# ── Singleton ──────────────────────────────────────────────────────────

_bench_instance: Optional[Benchmark] = None
_bench_lock = threading.Lock()


def get_bench() -> Benchmark:
    """Get or create the singleton Benchmark."""
    global _bench_instance
    if _bench_instance is None:
        with _bench_lock:
            if _bench_instance is None:
                _bench_instance = Benchmark()
                logger.info(f"Benchmark DB initialized: {_bench_instance._db_path}")
    return _bench_instance
