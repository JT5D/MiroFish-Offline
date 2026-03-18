"""
MiroFish Backend - Flask Application Factory
"""

import os
import warnings

# Suppress multiprocessing resource_tracker warnings (from third-party libraries like transformers)
# Must be set before all other imports
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


def create_app(config_class=Config):
    """Flask application factory function"""
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Set JSON encoding: ensure Chinese characters are displayed directly (instead of \uXXXX format)
    # Flask >= 2.3 uses app.json.ensure_ascii, older versions use JSON_AS_ASCII config
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False

    # Set up logging
    logger = setup_logger('mirofish')

    # Only print startup info in the reloader subprocess (avoid printing twice in debug mode)
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process

    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish-Offline Backend starting...")
        logger.info("=" * 50)

    # Enable CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # --- Initialize Neo4jStorage singleton (DI via app.extensions) ---
    from .storage import Neo4jStorage
    try:
        neo4j_storage = Neo4jStorage()
        app.extensions['neo4j_storage'] = neo4j_storage
        if should_log_startup:
            logger.info("Neo4jStorage initialized (connected to %s)", Config.NEO4J_URI)
    except Exception as e:
        logger.error("Neo4jStorage initialization failed: %s", e)
        # Store None so endpoints can return 503 gracefully
        app.extensions['neo4j_storage'] = None

    # Register simulation process cleanup function (ensure all simulation processes are terminated when server shuts down)
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("Simulation process cleanup function registered")

    # Request logging middleware
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"Request: {request.method} {request.path}")
        if request.content_type and 'json' in request.content_type:
            logger.debug(f"Request body: {request.get_json(silent=True)}")

    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"Response: {response.status_code}")
        return response

    # Register blueprints
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')

    # Auto-discover LLM providers and configure routing on startup (background, non-blocking)
    def _startup_discovery():
        try:
            from .utils.llm_discovery import discover_and_configure
            result = discover_and_configure()
            prov_count = len(result.get('providers', []))
            change_count = len(result.get('changes', {}))
            if prov_count > 0:
                logger.info(f"Auto-discovery: {prov_count} providers, {change_count} env vars configured")
                # Reload router with new env vars
                from .utils.llm_router import get_router
                router = get_router()
                router._build_chains()
                logger.info("LLM router reloaded with discovered providers")
            suggestions = result.get('suggestions', [])
            if suggestions:
                logger.info(f"Model suggestions ({len(suggestions)}):")
                for s in suggestions[:3]:
                    logger.info(f"  [{s.get('priority','')}] {s.get('model','')}: {s.get('reason','')[:80]}")
        except Exception as e:
            logger.debug(f"Auto-discovery skipped: {e}")

    import threading
    threading.Thread(target=_startup_discovery, daemon=True).start()

    # Initialize dynamic config (system resource check)
    try:
        from .utils.dynamic_config import get_dynamic_config
        dconf = get_dynamic_config()
        if should_log_startup:
            logger.info(
                f"Dynamic config: workers={dconf.profile_gen_workers}, "
                f"mem={dconf.available_memory_gb:.1f}GB, pressure={dconf.system_under_pressure}"
            )
    except Exception as e:
        logger.debug(f"Dynamic config init skipped: {e}")

    # Health check
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish-Offline Backend'}

    # Performance and tuning status endpoint
    @app.route('/api/system/status')
    def system_status():
        result = {'service': 'MiroFish-Offline Backend'}
        try:
            from .utils.llm_perf_tracker import get_tracker
            tracker = get_tracker()
            result['perf'] = tracker.get_summary()
            result['bottlenecks'] = tracker.get_bottlenecks()
        except Exception:
            pass
        try:
            from .utils.dynamic_config import get_dynamic_config
            dc = get_dynamic_config()
            result['dynamic_config'] = {
                'profile_gen_workers': dc.profile_gen_workers,
                'llm_concurrency': dc.llm_concurrency,
                'available_memory_gb': round(dc.available_memory_gb, 1),
                'system_under_pressure': dc.system_under_pressure,
            }
        except Exception:
            pass
        return result

    # Benchmark endpoints
    @app.route('/api/benchmark/leaderboard')
    def benchmark_leaderboard():
        from .utils.benchmark import get_bench
        task = request.args.get('task_type')
        return {'data': get_bench().leaderboard(task_type=task)}

    @app.route('/api/benchmark/sims')
    def benchmark_sims():
        from .utils.benchmark import get_bench
        return {'data': get_bench().sim_leaderboard()}

    @app.route('/api/benchmark/trends')
    def benchmark_trends():
        from .utils.benchmark import get_bench
        hours = int(request.args.get('hours', 24))
        return {'data': get_bench().trends(hours=hours)}

    @app.route('/api/benchmark/compare')
    def benchmark_compare():
        from .utils.benchmark import get_bench
        a = request.args.get('model_a', '')
        b = request.args.get('model_b', '')
        if not a or not b:
            return {'error': 'Provide model_a and model_b query params'}, 400
        return {'data': get_bench().model_comparison(a, b)}

    @app.route('/api/benchmark/summary')
    def benchmark_summary():
        from .utils.benchmark import get_bench
        return {'data': get_bench().summary()}

    if should_log_startup:
        logger.info("MiroFish-Offline Backend startup complete")

    return app
