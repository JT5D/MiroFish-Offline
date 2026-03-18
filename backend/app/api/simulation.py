"""
Simulation-related API routes
Step 2: Entity reading and filtering, OASIS simulation preparation and execution (fully automated)
"""

import os
import traceback
from flask import request, jsonify, send_file, current_app

from . import simulation_bp
from ..config import Config
from ..services.entity_reader import EntityReader
from ..services.oasis_profile_generator import OasisProfileGenerator
from ..services.simulation_manager import SimulationManager, SimulationStatus
from ..services.simulation_runner import SimulationRunner, RunnerStatus
from ..utils.logger import get_logger
from ..models.project import ProjectManager

logger = get_logger('mirofish.api.simulation')


# Interview prompt optimization prefix
# Adding this prefix prevents the Agent from calling tools and forces direct text response
INTERVIEW_PROMPT_PREFIX = "Based on your persona, all past memories and actions, respond directly with text without calling any tools: "


def optimize_interview_prompt(prompt: str) -> str:
    """
    Optimize Interview prompt by adding a prefix to prevent Agent from calling tools

    Args:
        prompt: Original prompt

    Returns:
        Optimized prompt
    """
    if not prompt:
        return prompt
    # Avoid adding the prefix repeatedly
    if prompt.startswith(INTERVIEW_PROMPT_PREFIX):
        return prompt
    return f"{INTERVIEW_PROMPT_PREFIX}{prompt}"


# ============== Entity Reading Endpoints ==============

@simulation_bp.route('/entities/<graph_id>', methods=['GET'])
def get_graph_entities(graph_id: str):
    """
    Get all entities in the graph (filtered)

    Only returns nodes that match predefined entity types (nodes with Labels beyond just Entity)

    Query parameters:
        entity_types: Comma-separated list of entity types (optional, for further filtering)
        enrich: Whether to get related edge information (default true)
    """
    try:
        entity_types_str = request.args.get('entity_types', '')
        entity_types = [t.strip() for t in entity_types_str.split(',') if t.strip()] if entity_types_str else None
        enrich = request.args.get('enrich', 'true').lower() == 'true'

        logger.info(f"Getting graph entities: graph_id={graph_id}, entity_types={entity_types}, enrich={enrich}")

        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            raise ValueError("GraphStorage not initialized")
        reader = EntityReader(storage)
        result = reader.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=entity_types,
            enrich_with_edges=enrich
        )

        return jsonify({
            "success": True,
            "data": result.to_dict()
        })

    except Exception as e:
        logger.error(f"Failed to get graph entities: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/entities/<graph_id>/<entity_uuid>', methods=['GET'])
def get_entity_detail(graph_id: str, entity_uuid: str):
    """Get detailed information for a single entity"""
    try:
        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            raise ValueError("GraphStorage not initialized")
        reader = EntityReader(storage)
        entity = reader.get_entity_with_context(graph_id, entity_uuid)

        if not entity:
            return jsonify({
                "success": False,
                "error": f"Entity does not exist: {entity_uuid}"
            }), 404

        return jsonify({
            "success": True,
            "data": entity.to_dict()
        })

    except Exception as e:
        logger.error(f"Failed to get entity details: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/entities/<graph_id>/by-type/<entity_type>', methods=['GET'])
def get_entities_by_type(graph_id: str, entity_type: str):
    """Get all entities of a specified type"""
    try:
        enrich = request.args.get('enrich', 'true').lower() == 'true'

        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            raise ValueError("GraphStorage not initialized")
        reader = EntityReader(storage)
        entities = reader.get_entities_by_type(
            graph_id=graph_id,
            entity_type=entity_type,
            enrich_with_edges=enrich
        )

        return jsonify({
            "success": True,
            "data": {
                "entity_type": entity_type,
                "count": len(entities),
                "entities": [e.to_dict() for e in entities]
            }
        })

    except Exception as e:
        logger.error(f"Failed to get entities: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Simulation Management Endpoints ==============

@simulation_bp.route('/create', methods=['POST'])
def create_simulation():
    """
    Create a new simulation

    Note: Parameters like max_rounds are intelligently generated by LLM, no manual setup needed

    Request (JSON):
        {
            "project_id": "proj_xxxx",      // Required
            "graph_id": "mirofish_xxxx",    // Optional, obtained from project if not provided
            "enable_twitter": true,          // Optional, default true
            "enable_reddit": true            // Optional, default true
        }

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "project_id": "proj_xxxx",
                "graph_id": "mirofish_xxxx",
                "status": "created",
                "enable_twitter": true,
                "enable_reddit": true,
                "created_at": "2025-12-01T10:00:00"
            }
        }
    """
    try:
        data = request.get_json() or {}

        project_id = data.get('project_id')
        if not project_id:
            return jsonify({
                "success": False,
                "error": "Please provide project_id"
            }), 400

        project = ProjectManager.get_project(project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"Project does not exist: {project_id}"
            }), 404

        graph_id = data.get('graph_id') or project.graph_id
        if not graph_id:
            return jsonify({
                "success": False,
                "error": "Project has not built a graph yet, please call /api/graph/build first"
            }), 400

        manager = SimulationManager()
        state = manager.create_simulation(
            project_id=project_id,
            graph_id=graph_id,
            enable_twitter=data.get('enable_twitter', True),
            enable_reddit=data.get('enable_reddit', True),
        )

        return jsonify({
            "success": True,
            "data": state.to_dict()
        })

    except Exception as e:
        logger.error(f"Failed to create simulation: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


def _check_simulation_prepared(simulation_id: str) -> tuple:
    """
    Check if the simulation preparation is complete

    Check conditions:
    1. state.json exists and status is "ready"
    2. Required files exist: reddit_profiles.json, twitter_profiles.csv, simulation_config.json

    Note: Run scripts (run_*.py) remain in the backend/scripts/ directory and are no longer copied to the simulation directory

    Args:
        simulation_id: Simulation ID

    Returns:
        (is_prepared: bool, info: dict)
    """
    import os
    from ..config import Config

    simulation_dir = os.path.join(Config.OASIS_SIMULATION_DATA_DIR, simulation_id)

    # Check if directory exists
    if not os.path.exists(simulation_dir):
        return False, {"reason": "Simulation directory does not exist"}

    # Required file list (excludes scripts, which are in backend/scripts/)
    required_files = [
        "state.json",
        "simulation_config.json",
        "reddit_profiles.json",
        "twitter_profiles.csv"
    ]

    # Check if files exist
    existing_files = []
    missing_files = []
    for f in required_files:
        file_path = os.path.join(simulation_dir, f)
        if os.path.exists(file_path):
            existing_files.append(f)
        else:
            missing_files.append(f)

    if missing_files:
        return False, {
            "reason": "Missing required files",
            "missing_files": missing_files,
            "existing_files": existing_files
        }

    # Check status in state.json
    state_file = os.path.join(simulation_dir, "state.json")
    try:
        import json
        with open(state_file, 'r', encoding='utf-8') as f:
            state_data = json.load(f)

        status = state_data.get("status", "")
        config_generated = state_data.get("config_generated", False)

        # Detailed logging
        logger.debug(f"Checking simulation preparation status: {simulation_id}, status={status}, config_generated={config_generated}")

        # If config_generated=True and files exist, consider preparation complete
        # The following statuses all indicate preparation work is complete:
        # - ready: Preparation complete, ready to run
        # - preparing: If config_generated=True, it means already complete
        # - running: Currently running, meaning preparation was completed long ago
        # - completed: Run completed, meaning preparation was completed long ago
        # - stopped: Stopped, meaning preparation was completed long ago
        # - failed: Run failed (but preparation was complete)
        prepared_statuses = ["ready", "preparing", "running", "completed", "stopped", "failed"]
        if status in prepared_statuses and config_generated:
            # Get file statistics
            profiles_file = os.path.join(simulation_dir, "reddit_profiles.json")
            config_file = os.path.join(simulation_dir, "simulation_config.json")

            profiles_count = 0
            if os.path.exists(profiles_file):
                with open(profiles_file, 'r', encoding='utf-8') as f:
                    profiles_data = json.load(f)
                    profiles_count = len(profiles_data) if isinstance(profiles_data, list) else 0

            # If status is preparing but files are complete, automatically update status to ready
            if status == "preparing":
                try:
                    state_data["status"] = "ready"
                    from datetime import datetime
                    state_data["updated_at"] = datetime.now().isoformat()
                    with open(state_file, 'w', encoding='utf-8') as f:
                        json.dump(state_data, f, ensure_ascii=False, indent=2)
                    logger.info(f"Auto-updated simulation status: {simulation_id} preparing -> ready")
                    status = "ready"
                except Exception as e:
                    logger.warning(f"Failed to auto-update status: {e}")

            logger.info(f"Simulation {simulation_id} check result: preparation complete (status={status}, config_generated={config_generated})")
            return True, {
                "status": status,
                "entities_count": state_data.get("entities_count", 0),
                "profiles_count": profiles_count,
                "entity_types": state_data.get("entity_types", []),
                "config_generated": config_generated,
                "created_at": state_data.get("created_at"),
                "updated_at": state_data.get("updated_at"),
                "existing_files": existing_files
            }
        else:
            logger.warning(f"Simulation {simulation_id} check result: preparation not complete (status={status}, config_generated={config_generated})")
            return False, {
                "reason": f"Status not in prepared list or config_generated is false: status={status}, config_generated={config_generated}",
                "status": status,
                "config_generated": config_generated
            }

    except Exception as e:
        return False, {"reason": f"Failed to read state file: {str(e)}"}


@simulation_bp.route('/prepare', methods=['POST'])
def prepare_simulation():
    """
    Prepare simulation environment (async task, LLM intelligently generates all parameters)

    This is a time-consuming operation. The endpoint returns a task_id immediately.
    Use GET /api/simulation/prepare/status to check progress.

    Features:
    - Automatically detects completed preparation work to avoid redundant generation
    - If already prepared, directly returns existing results
    - Supports forced regeneration (force_regenerate=true)

    Steps:
    1. Check if preparation work is already complete
    2. Read and filter entities from Zep graph
    3. Generate OASIS Agent Profile for each entity (with retry mechanism)
    4. LLM intelligently generates simulation configuration (with retry mechanism)
    5. Save configuration files and preset scripts

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",                   // Required, simulation ID
            "entity_types": ["Student", "PublicFigure"],  // Optional, specify entity types
            "use_llm_for_profiles": true,                 // Optional, whether to use LLM for profile generation
            "parallel_profile_count": 5,                  // Optional, number of profiles to generate in parallel, default 5
            "force_regenerate": false                     // Optional, force regeneration, default false
        }

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "task_id": "task_xxxx",           // Returned for new tasks
                "status": "preparing|ready",
                "message": "Preparation task started|Preparation work already complete",
                "already_prepared": true|false    // Whether preparation is already complete
            }
        }
    """
    import threading
    import os
    from ..models.task import TaskManager, TaskStatus
    from ..config import Config

    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)

        if not state:
            return jsonify({
                "success": False,
                "error": f"Simulation does not exist: {simulation_id}"
            }), 404

        # Check if forced regeneration
        force_regenerate = data.get('force_regenerate', False)
        logger.info(f"Processing /prepare request: simulation_id={simulation_id}, force_regenerate={force_regenerate}")

        # Check if preparation is already complete (avoid redundant generation)
        if not force_regenerate:
            logger.debug(f"Checking if simulation {simulation_id} is already prepared...")
            is_prepared, prepare_info = _check_simulation_prepared(simulation_id)
            logger.debug(f"Check result: is_prepared={is_prepared}, prepare_info={prepare_info}")
            if is_prepared:
                logger.info(f"Simulation {simulation_id} is already prepared, skipping redundant generation")
                return jsonify({
                    "success": True,
                    "data": {
                        "simulation_id": simulation_id,
                        "status": "ready",
                        "message": "Preparation work already complete, no need for redundant generation",
                        "already_prepared": True,
                        "prepare_info": prepare_info
                    }
                })
            else:
                logger.info(f"Simulation {simulation_id} is not prepared, starting preparation task")

        # Get necessary information from project
        project = ProjectManager.get_project(state.project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"Project does not exist: {state.project_id}"
            }), 404

        # Get simulation requirement
        simulation_requirement = project.simulation_requirement or ""
        if not simulation_requirement:
            return jsonify({
                "success": False,
                "error": "Project is missing simulation requirement description (simulation_requirement)"
            }), 400

        # Get document text
        document_text = ProjectManager.get_extracted_text(state.project_id) or ""

        entity_types_list = data.get('entity_types')
        use_llm_for_profiles = data.get('use_llm_for_profiles', True)
        parallel_profile_count = data.get('parallel_profile_count', 5)

        # ========== Get GraphStorage (capture reference before starting background task) ==========
        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            raise ValueError("GraphStorage not initialized — check Neo4j connection")

        # ========== Synchronously get entity count (before starting background task) ==========
        # This way the frontend can get the expected total Agent count immediately after calling prepare
        try:
            logger.info(f"Synchronously getting entity count: graph_id={state.graph_id}")
            reader = EntityReader(storage)
            # Quick read entities (no edge info needed, just count)
            filtered_preview = reader.filter_defined_entities(
                graph_id=state.graph_id,
                defined_entity_types=entity_types_list,
                enrich_with_edges=False  # Don't get edge info, faster
            )
            # Save entity count to state (for frontend to retrieve immediately)
            state.entities_count = filtered_preview.filtered_count
            state.entity_types = list(filtered_preview.entity_types)
            logger.info(f"Expected entity count: {filtered_preview.filtered_count}, types: {filtered_preview.entity_types}")
        except Exception as e:
            logger.warning(f"Failed to synchronously get entity count (will retry in background task): {e}")
            # Failure doesn't affect subsequent flow, background task will re-fetch

        # Create async task
        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="simulation_prepare",
            metadata={
                "simulation_id": simulation_id,
                "project_id": state.project_id
            }
        )

        # Update simulation status (including pre-fetched entity count)
        state.status = SimulationStatus.PREPARING
        manager._save_simulation_state(state)

        # Define background task
        def run_prepare():
            try:
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.PROCESSING,
                    progress=0,
                    message="Starting simulation environment preparation..."
                )

                # Prepare simulation (with progress callback)
                # Store stage progress details
                stage_details = {}

                def progress_callback(stage, progress, message, **kwargs):
                    # Calculate total progress
                    stage_weights = {
                        "reading": (0, 20),           # 0-20%
                        "generating_profiles": (20, 70),  # 20-70%
                        "generating_config": (70, 90),    # 70-90%
                        "copying_scripts": (90, 100)       # 90-100%
                    }

                    start, end = stage_weights.get(stage, (0, 100))
                    current_progress = int(start + (end - start) * progress / 100)

                    # Build detailed progress information
                    stage_names = {
                        "reading": "Reading graph entities",
                        "generating_profiles": "Generating Agent profiles",
                        "generating_config": "Generating simulation configuration",
                        "copying_scripts": "Preparing simulation scripts"
                    }

                    stage_index = list(stage_weights.keys()).index(stage) + 1 if stage in stage_weights else 1
                    total_stages = len(stage_weights)

                    # Update stage details
                    stage_details[stage] = {
                        "stage_name": stage_names.get(stage, stage),
                        "stage_progress": progress,
                        "current": kwargs.get("current", 0),
                        "total": kwargs.get("total", 0),
                        "item_name": kwargs.get("item_name", "")
                    }

                    # Build detailed progress information
                    detail = stage_details[stage]
                    progress_detail_data = {
                        "current_stage": stage,
                        "current_stage_name": stage_names.get(stage, stage),
                        "stage_index": stage_index,
                        "total_stages": total_stages,
                        "stage_progress": progress,
                        "current_item": detail["current"],
                        "total_items": detail["total"],
                        "item_description": message
                    }

                    # Build concise message
                    if detail["total"] > 0:
                        detailed_message = (
                            f"[{stage_index}/{total_stages}] {stage_names.get(stage, stage)}: "
                            f"{detail['current']}/{detail['total']} - {message}"
                        )
                    else:
                        detailed_message = f"[{stage_index}/{total_stages}] {stage_names.get(stage, stage)}: {message}"

                    task_manager.update_task(
                        task_id,
                        progress=current_progress,
                        message=detailed_message,
                        progress_detail=progress_detail_data
                    )

                result_state = manager.prepare_simulation(
                    simulation_id=simulation_id,
                    simulation_requirement=simulation_requirement,
                    document_text=document_text,
                    defined_entity_types=entity_types_list,
                    use_llm_for_profiles=use_llm_for_profiles,
                    progress_callback=progress_callback,
                    parallel_profile_count=parallel_profile_count,
                    storage=storage,
                )

                # Task complete
                task_manager.complete_task(
                    task_id,
                    result=result_state.to_simple_dict()
                )

            except Exception as e:
                logger.error(f"Failed to prepare simulation: {str(e)}")
                task_manager.fail_task(task_id, str(e))

                # Update simulation status to failed
                state = manager.get_simulation(simulation_id)
                if state:
                    state.status = SimulationStatus.FAILED
                    state.error = str(e)
                    manager._save_simulation_state(state)

        # Start background thread
        thread = threading.Thread(target=run_prepare, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "task_id": task_id,
                "status": "preparing",
                "message": "Preparation task started, check progress via /api/simulation/prepare/status",
                "already_prepared": False,
                "expected_entities_count": state.entities_count,  # Expected total Agent count
                "entity_types": state.entity_types  # Entity type list
            }
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 404

    except Exception as e:
        logger.error(f"Failed to start preparation task: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/prepare/status', methods=['POST'])
def get_prepare_status():
    """
    Query preparation task progress

    Supports two query methods:
    1. Query ongoing task progress via task_id
    2. Check if preparation work is already complete via simulation_id

    Request (JSON):
        {
            "task_id": "task_xxxx",          // Optional, task_id returned by prepare
            "simulation_id": "sim_xxxx"      // Optional, simulation ID (for checking completed preparation)
        }

    Returns:
        {
            "success": true,
            "data": {
                "task_id": "task_xxxx",
                "status": "processing|completed|ready",
                "progress": 45,
                "message": "...",
                "already_prepared": true|false,  // Whether preparation is already complete
                "prepare_info": {...}            // Detailed info when preparation is complete
            }
        }
    """
    from ..models.task import TaskManager

    try:
        data = request.get_json() or {}

        task_id = data.get('task_id')
        simulation_id = data.get('simulation_id')

        # If simulation_id is provided, first check if preparation is complete
        if simulation_id:
            is_prepared, prepare_info = _check_simulation_prepared(simulation_id)
            if is_prepared:
                return jsonify({
                    "success": True,
                    "data": {
                        "simulation_id": simulation_id,
                        "status": "ready",
                        "progress": 100,
                        "message": "Preparation work already complete",
                        "already_prepared": True,
                        "prepare_info": prepare_info
                    }
                })

        # If no task_id, return error
        if not task_id:
            if simulation_id:
                # Has simulation_id but preparation not complete
                return jsonify({
                    "success": True,
                    "data": {
                        "simulation_id": simulation_id,
                        "status": "not_started",
                        "progress": 0,
                        "message": "Preparation not started, please call /api/simulation/prepare to begin",
                        "already_prepared": False
                    }
                })
            return jsonify({
                "success": False,
                "error": "Please provide task_id or simulation_id"
            }), 400

        task_manager = TaskManager()
        task = task_manager.get_task(task_id)

        if not task:
            # Task does not exist, but if simulation_id is available, check if preparation is complete
            if simulation_id:
                is_prepared, prepare_info = _check_simulation_prepared(simulation_id)
                if is_prepared:
                    return jsonify({
                        "success": True,
                        "data": {
                            "simulation_id": simulation_id,
                            "task_id": task_id,
                            "status": "ready",
                            "progress": 100,
                            "message": "Task completed (preparation work already exists)",
                            "already_prepared": True,
                            "prepare_info": prepare_info
                        }
                    })

            return jsonify({
                "success": False,
                "error": f"Task does not exist: {task_id}"
            }), 404

        task_dict = task.to_dict()
        task_dict["already_prepared"] = False

        return jsonify({
            "success": True,
            "data": task_dict
        })

    except Exception as e:
        logger.error(f"Failed to query task status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@simulation_bp.route('/<simulation_id>', methods=['GET'])
def get_simulation(simulation_id: str):
    """Get simulation status"""
    try:
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)

        if not state:
            return jsonify({
                "success": False,
                "error": f"Simulation does not exist: {simulation_id}"
            }), 404

        result = state.to_dict()

        # If simulation is ready, attach run instructions
        if state.status == SimulationStatus.READY:
            result["run_instructions"] = manager.get_run_instructions(simulation_id)

        return jsonify({
            "success": True,
            "data": result
        })

    except Exception as e:
        logger.error(f"Failed to get simulation status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/list', methods=['GET'])
def list_simulations():
    """
    List all simulations

    Query parameters:
        project_id: Filter by project ID (optional)
    """
    try:
        project_id = request.args.get('project_id')

        manager = SimulationManager()
        simulations = manager.list_simulations(project_id=project_id)

        return jsonify({
            "success": True,
            "data": [s.to_dict() for s in simulations],
            "count": len(simulations)
        })

    except Exception as e:
        logger.error(f"Failed to list simulations: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


def _get_report_id_for_simulation(simulation_id: str) -> str:
    """
    Get the latest report_id corresponding to a simulation

    Traverses the reports directory, finds reports matching the simulation_id,
    and returns the most recent one if there are multiple (sorted by created_at)

    Args:
        simulation_id: Simulation ID

    Returns:
        report_id or None
    """
    import json
    from datetime import datetime

    # Reports directory path: backend/uploads/reports
    # __file__ is app/api/simulation.py, need to go up two levels to backend/
    reports_dir = os.path.join(os.path.dirname(__file__), '../../uploads/reports')
    if not os.path.exists(reports_dir):
        return None

    matching_reports = []

    try:
        for report_folder in os.listdir(reports_dir):
            report_path = os.path.join(reports_dir, report_folder)
            if not os.path.isdir(report_path):
                continue

            meta_file = os.path.join(report_path, "meta.json")
            if not os.path.exists(meta_file):
                continue

            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)

                if meta.get("simulation_id") == simulation_id:
                    matching_reports.append({
                        "report_id": meta.get("report_id"),
                        "created_at": meta.get("created_at", ""),
                        "status": meta.get("status", "")
                    })
            except Exception:
                continue

        if not matching_reports:
            return None

        # Sort by creation time descending, return the most recent
        matching_reports.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return matching_reports[0].get("report_id")

    except Exception as e:
        logger.warning(f"Failed to find report for simulation {simulation_id}: {e}")
        return None


@simulation_bp.route('/history', methods=['GET'])
def get_simulation_history():
    """
    Get historical simulation list (with project details)

    Used for homepage historical project display, returns simulation list with rich information
    including project name, description, etc.

    Query parameters:
        limit: Return count limit (default 20)

    Returns:
        {
            "success": true,
            "data": [
                {
                    "simulation_id": "sim_xxxx",
                    "project_id": "proj_xxxx",
                    "project_name": "Public Opinion Analysis",
                    "simulation_requirement": "If the university publishes...",
                    "status": "completed",
                    "entities_count": 68,
                    "profiles_count": 68,
                    "entity_types": ["Student", "Professor", ...],
                    "created_at": "2024-12-10",
                    "updated_at": "2024-12-10",
                    "total_rounds": 120,
                    "current_round": 120,
                    "report_id": "report_xxxx",
                    "version": "v1.0.2"
                },
                ...
            ],
            "count": 7
        }
    """
    try:
        limit = request.args.get('limit', 20, type=int)

        manager = SimulationManager()
        simulations = manager.list_simulations()[:limit]

        # Enrich simulation data, read only from Simulation files
        enriched_simulations = []
        for sim in simulations:
            sim_dict = sim.to_dict()

            # Get simulation config info (read simulation_requirement from simulation_config.json)
            config = manager.get_simulation_config(sim.simulation_id)
            if config:
                sim_dict["simulation_requirement"] = config.get("simulation_requirement", "")
                time_config = config.get("time_config", {})
                sim_dict["total_simulation_hours"] = time_config.get("total_simulation_hours", 0)
                # Recommended rounds (fallback value)
                recommended_rounds = int(
                    time_config.get("total_simulation_hours", 0) * 60 /
                    max(time_config.get("minutes_per_round", 60), 1)
                )
            else:
                sim_dict["simulation_requirement"] = ""
                sim_dict["total_simulation_hours"] = 0
                recommended_rounds = 0

            # Get run state (read user-set actual rounds from run_state.json)
            run_state = SimulationRunner.get_run_state(sim.simulation_id)
            if run_state:
                sim_dict["current_round"] = run_state.current_round
                sim_dict["runner_status"] = run_state.runner_status.value
                # Use user-set total_rounds, fallback to recommended rounds
                sim_dict["total_rounds"] = run_state.total_rounds if run_state.total_rounds > 0 else recommended_rounds
            else:
                sim_dict["current_round"] = 0
                sim_dict["runner_status"] = "idle"
                sim_dict["total_rounds"] = recommended_rounds

            # Get associated project's file list (max 3)
            project = ProjectManager.get_project(sim.project_id)
            if project and hasattr(project, 'files') and project.files:
                sim_dict["files"] = [
                    {"filename": f.get("filename", "Unknown file")}
                    for f in project.files[:3]
                ]
            else:
                sim_dict["files"] = []

            # Get associated report_id (find the latest report for this simulation)
            sim_dict["report_id"] = _get_report_id_for_simulation(sim.simulation_id)

            # Add version number
            sim_dict["version"] = "v1.0.2"

            # Format date
            try:
                created_date = sim_dict.get("created_at", "")[:10]
                sim_dict["created_date"] = created_date
            except:
                sim_dict["created_date"] = ""

            enriched_simulations.append(sim_dict)

        return jsonify({
            "success": True,
            "data": enriched_simulations,
            "count": len(enriched_simulations)
        })

    except Exception as e:
        logger.error(f"Failed to get simulation history: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/profiles', methods=['GET'])
def get_simulation_profiles(simulation_id: str):
    """
    Get simulation Agent Profiles

    Query parameters:
        platform: Platform type (reddit/twitter, default reddit)
    """
    try:
        platform = request.args.get('platform', 'reddit')

        manager = SimulationManager()
        profiles = manager.get_profiles(simulation_id, platform=platform)

        return jsonify({
            "success": True,
            "data": {
                "platform": platform,
                "count": len(profiles),
                "profiles": profiles
            }
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 404

    except Exception as e:
        logger.error(f"Failed to get profiles: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/profiles/realtime', methods=['GET'])
def get_simulation_profiles_realtime(simulation_id: str):
    """
    Get simulation Agent Profiles in real-time (for viewing progress during generation)

    Differences from the /profiles endpoint:
    - Reads files directly, not through SimulationManager
    - Suitable for real-time viewing during generation
    - Returns additional metadata (e.g., file modification time, whether generation is in progress)

    Query parameters:
        platform: Platform type (reddit/twitter, default reddit)

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "platform": "reddit",
                "count": 15,
                "total_expected": 93,  // Expected total (if available)
                "is_generating": true,  // Whether generation is in progress
                "file_exists": true,
                "file_modified_at": "2025-12-04T18:20:00",
                "profiles": [...]
            }
        }
    """
    import json
    import csv
    from datetime import datetime

    try:
        platform = request.args.get('platform', 'reddit')

        # Get simulation directory
        sim_dir = os.path.join(Config.OASIS_SIMULATION_DATA_DIR, simulation_id)

        if not os.path.exists(sim_dir):
            return jsonify({
                "success": False,
                "error": f"Simulation does not exist: {simulation_id}"
            }), 404

        # Determine file path
        if platform == "reddit":
            profiles_file = os.path.join(sim_dir, "reddit_profiles.json")
        else:
            profiles_file = os.path.join(sim_dir, "twitter_profiles.csv")

        # Check if file exists
        file_exists = os.path.exists(profiles_file)
        profiles = []
        file_modified_at = None

        if file_exists:
            # Get file modification time
            file_stat = os.stat(profiles_file)
            file_modified_at = datetime.fromtimestamp(file_stat.st_mtime).isoformat()

            try:
                if platform == "reddit":
                    with open(profiles_file, 'r', encoding='utf-8') as f:
                        profiles = json.load(f)
                else:
                    with open(profiles_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        profiles = list(reader)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to read profiles file (may be writing in progress): {e}")
                profiles = []

        # Check if generation is in progress (determined via state.json)
        is_generating = False
        total_expected = None

        state_file = os.path.join(sim_dir, "state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state_data = json.load(f)
                    status = state_data.get("status", "")
                    is_generating = status == "preparing"
                    total_expected = state_data.get("entities_count")
            except Exception:
                pass

        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "platform": platform,
                "count": len(profiles),
                "total_expected": total_expected,
                "is_generating": is_generating,
                "file_exists": file_exists,
                "file_modified_at": file_modified_at,
                "profiles": profiles
            }
        })

    except Exception as e:
        logger.error(f"Failed to get profiles in real-time: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/config/realtime', methods=['GET'])
def get_simulation_config_realtime(simulation_id: str):
    """
    Get simulation configuration in real-time (for viewing progress during generation)

    Differences from the /config endpoint:
    - Reads files directly, not through SimulationManager
    - Suitable for real-time viewing during generation
    - Returns additional metadata (e.g., file modification time, whether generation is in progress)
    - Can return partial information even if config generation is not yet complete

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "file_exists": true,
                "file_modified_at": "2025-12-04T18:20:00",
                "is_generating": true,  // Whether generation is in progress
                "generation_stage": "generating_config",  // Current generation stage
                "config": {...}  // Configuration content (if exists)
            }
        }
    """
    import json
    from datetime import datetime

    try:
        # Get simulation directory
        sim_dir = os.path.join(Config.OASIS_SIMULATION_DATA_DIR, simulation_id)

        if not os.path.exists(sim_dir):
            return jsonify({
                "success": False,
                "error": f"Simulation does not exist: {simulation_id}"
            }), 404

        # Configuration file path
        config_file = os.path.join(sim_dir, "simulation_config.json")

        # Check if file exists
        file_exists = os.path.exists(config_file)
        config = None
        file_modified_at = None

        if file_exists:
            # Get file modification time
            file_stat = os.stat(config_file)
            file_modified_at = datetime.fromtimestamp(file_stat.st_mtime).isoformat()

            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to read config file (may be writing in progress): {e}")
                config = None

        # Check if generation is in progress (determined via state.json)
        is_generating = False
        generation_stage = None
        config_generated = False

        state_file = os.path.join(sim_dir, "state.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    state_data = json.load(f)
                    status = state_data.get("status", "")
                    is_generating = status == "preparing"
                    config_generated = state_data.get("config_generated", False)

                    # Determine current stage
                    if is_generating:
                        if state_data.get("profiles_generated", False):
                            generation_stage = "generating_config"
                        else:
                            generation_stage = "generating_profiles"
                    elif status == "ready":
                        generation_stage = "completed"
            except Exception:
                pass

        # Build response data
        response_data = {
            "simulation_id": simulation_id,
            "file_exists": file_exists,
            "file_modified_at": file_modified_at,
            "is_generating": is_generating,
            "generation_stage": generation_stage,
            "config_generated": config_generated,
            "config": config
        }

        # If config exists, extract some key statistics
        if config:
            response_data["summary"] = {
                "total_agents": len(config.get("agent_configs", [])),
                "simulation_hours": config.get("time_config", {}).get("total_simulation_hours"),
                "initial_posts_count": len(config.get("event_config", {}).get("initial_posts", [])),
                "hot_topics_count": len(config.get("event_config", {}).get("hot_topics", [])),
                "has_twitter_config": "twitter_config" in config,
                "has_reddit_config": "reddit_config" in config,
                "generated_at": config.get("generated_at"),
                "llm_model": config.get("llm_model")
            }

        return jsonify({
            "success": True,
            "data": response_data
        })

    except Exception as e:
        logger.error(f"Failed to get config in real-time: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/config', methods=['GET'])
def get_simulation_config(simulation_id: str):
    """
    Get simulation configuration (complete configuration intelligently generated by LLM)

    Returns:
        - time_config: Time configuration (simulation duration, rounds, peak/off-peak periods)
        - agent_configs: Activity configuration for each Agent (activity level, posting frequency, stance, etc.)
        - event_config: Event configuration (initial posts, hot topics)
        - platform_configs: Platform configuration
        - generation_reasoning: LLM's configuration reasoning explanation
    """
    try:
        manager = SimulationManager()
        config = manager.get_simulation_config(simulation_id)

        if not config:
            return jsonify({
                "success": False,
                "error": f"Simulation configuration does not exist, please call /prepare endpoint first"
            }), 404

        return jsonify({
            "success": True,
            "data": config
        })

    except Exception as e:
        logger.error(f"Failed to get configuration: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/config/download', methods=['GET'])
def download_simulation_config(simulation_id: str):
    """Download simulation configuration file"""
    try:
        manager = SimulationManager()
        sim_dir = manager._get_simulation_dir(simulation_id)
        config_path = os.path.join(sim_dir, "simulation_config.json")

        if not os.path.exists(config_path):
            return jsonify({
                "success": False,
                "error": "Configuration file does not exist, please call /prepare endpoint first"
            }), 404

        return send_file(
            config_path,
            as_attachment=True,
            download_name="simulation_config.json"
        )

    except Exception as e:
        logger.error(f"Failed to download configuration: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/script/<script_name>/download', methods=['GET'])
def download_simulation_script(script_name: str):
    """
    Download simulation run script file (shared scripts, located in backend/scripts/)

    script_name options:
        - run_twitter_simulation.py
        - run_reddit_simulation.py
        - run_parallel_simulation.py
        - action_logger.py
    """
    try:
        # Scripts are in the backend/scripts/ directory
        scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts'))

        # Validate script name
        allowed_scripts = [
            "run_twitter_simulation.py",
            "run_reddit_simulation.py",
            "run_parallel_simulation.py",
            "action_logger.py"
        ]

        if script_name not in allowed_scripts:
            return jsonify({
                "success": False,
                "error": f"Unknown script: {script_name}, options: {allowed_scripts}"
            }), 400

        script_path = os.path.join(scripts_dir, script_name)

        if not os.path.exists(script_path):
            return jsonify({
                "success": False,
                "error": f"Script file does not exist: {script_name}"
            }), 404

        return send_file(
            script_path,
            as_attachment=True,
            download_name=script_name
        )

    except Exception as e:
        logger.error(f"Failed to download script: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Profile Generation Endpoints (standalone use) ==============

@simulation_bp.route('/generate-profiles', methods=['POST'])
def generate_profiles():
    """
    Generate OASIS Agent Profiles directly from graph (without creating a simulation)

    Request (JSON):
        {
            "graph_id": "mirofish_xxxx",     // Required
            "entity_types": ["Student"],      // Optional
            "use_llm": true,                  // Optional
            "platform": "reddit"              // Optional
        }
    """
    try:
        data = request.get_json() or {}

        graph_id = data.get('graph_id')
        if not graph_id:
            return jsonify({
                "success": False,
                "error": "Please provide graph_id"
            }), 400

        entity_types = data.get('entity_types')
        use_llm = data.get('use_llm', True)
        platform = data.get('platform', 'reddit')

        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            raise ValueError("GraphStorage not initialized")
        reader = EntityReader(storage)
        filtered = reader.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=entity_types,
            enrich_with_edges=True
        )

        if filtered.filtered_count == 0:
            return jsonify({
                "success": False,
                "error": "No entities matching the criteria were found"
            }), 400

        generator = OasisProfileGenerator()
        profiles = generator.generate_profiles_from_entities(
            entities=filtered.entities,
            use_llm=use_llm
        )

        if platform == "reddit":
            profiles_data = [p.to_reddit_format() for p in profiles]
        elif platform == "twitter":
            profiles_data = [p.to_twitter_format() for p in profiles]
        else:
            profiles_data = [p.to_dict() for p in profiles]

        return jsonify({
            "success": True,
            "data": {
                "platform": platform,
                "entity_types": list(filtered.entity_types),
                "count": len(profiles_data),
                "profiles": profiles_data
            }
        })

    except Exception as e:
        logger.error(f"Failed to generate profiles: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Simulation Run Control Endpoints ==============

@simulation_bp.route('/start', methods=['POST'])
def start_simulation():
    """
    Start running a simulation

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",          // Required, simulation ID
            "platform": "parallel",                // Optional: twitter / reddit / parallel (default)
            "max_rounds": 100,                     // Optional: maximum simulation rounds, used to truncate overly long simulations
            "enable_graph_memory_update": false,   // Optional: whether to dynamically update Agent activities to Zep graph memory
            "force": false                         // Optional: force restart (will stop running simulation and clean up logs)
        }

    About the force parameter:
        - When enabled, if the simulation is running or completed, it will be stopped first and run logs cleaned up
        - Cleaned content includes: run_state.json, actions.jsonl, simulation.log, etc.
        - Configuration files (simulation_config.json) and profile files will not be cleaned
        - Suitable for scenarios requiring re-running the simulation

    About enable_graph_memory_update:
        - When enabled, all Agent activities (posting, commenting, liking, etc.) during simulation are updated to the Zep graph in real-time
        - This allows the graph to "remember" the simulation process for subsequent analysis or AI conversation
        - Requires the simulation's associated project to have a valid graph_id
        - Uses batch update mechanism to reduce API call frequency

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "runner_status": "running",
                "process_pid": 12345,
                "twitter_running": true,
                "reddit_running": true,
                "started_at": "2025-12-01T10:00:00",
                "graph_memory_update_enabled": true,  // Whether graph memory update is enabled
                "force_restarted": true               // Whether it was force restarted
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        platform = data.get('platform', 'parallel')
        max_rounds = data.get('max_rounds')  # Optional: maximum simulation rounds
        enable_graph_memory_update = data.get('enable_graph_memory_update', False)  # Optional: whether to enable graph memory update
        force = data.get('force', False)  # Optional: force restart

        # Validate max_rounds parameter
        if max_rounds is not None:
            try:
                max_rounds = int(max_rounds)
                if max_rounds <= 0:
                    return jsonify({
                        "success": False,
                        "error": "max_rounds must be a positive integer"
                    }), 400
            except (ValueError, TypeError):
                return jsonify({
                    "success": False,
                    "error": "max_rounds must be a valid integer"
                }), 400

        if platform not in ['twitter', 'reddit', 'parallel']:
            return jsonify({
                "success": False,
                "error": f"Invalid platform type: {platform}, options: twitter/reddit/parallel"
            }), 400

        # Check if simulation is ready
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)

        if not state:
            return jsonify({
                "success": False,
                "error": f"Simulation does not exist: {simulation_id}"
            }), 404

        force_restarted = False

        # Smart state handling: if preparation work is complete, allow restart
        if state.status != SimulationStatus.READY:
            # Check if preparation work is complete
            is_prepared, prepare_info = _check_simulation_prepared(simulation_id)

            if is_prepared:
                # Preparation work is complete, check if there's a running process
                if state.status == SimulationStatus.RUNNING:
                    # Check if the simulation process is actually running
                    run_state = SimulationRunner.get_run_state(simulation_id)
                    if run_state and run_state.runner_status.value == "running":
                        # Process is indeed running
                        if force:
                            # Force mode: stop the running simulation
                            logger.info(f"Force mode: stopping running simulation {simulation_id}")
                            try:
                                SimulationRunner.stop_simulation(simulation_id)
                            except Exception as e:
                                logger.warning(f"Warning while stopping simulation: {str(e)}")
                        else:
                            return jsonify({
                                "success": False,
                                "error": f"Simulation is currently running, please call /stop endpoint first, or use force=true to force restart"
                            }), 400

                # If force mode, clean up run logs
                if force:
                    logger.info(f"Force mode: cleaning simulation logs {simulation_id}")
                    cleanup_result = SimulationRunner.cleanup_simulation_logs(simulation_id)
                    if not cleanup_result.get("success"):
                        logger.warning(f"Warning while cleaning logs: {cleanup_result.get('errors')}")
                    force_restarted = True

                # Process doesn't exist or has ended, reset status to ready
                logger.info(f"Simulation {simulation_id} preparation work complete, resetting status to ready (original status: {state.status.value})")
                state.status = SimulationStatus.READY
                manager._save_simulation_state(state)
            else:
                # Preparation work not complete
                return jsonify({
                    "success": False,
                    "error": f"Simulation not ready, current status: {state.status.value}, please call /prepare endpoint first"
                }), 400

        # Get graph ID (for graph memory update)
        graph_id = None
        if enable_graph_memory_update:
            # Get graph_id from simulation state or project
            graph_id = state.graph_id
            if not graph_id:
                # Try to get from project
                project = ProjectManager.get_project(state.project_id)
                if project:
                    graph_id = project.graph_id

            if not graph_id:
                return jsonify({
                    "success": False,
                    "error": "Enabling graph memory update requires a valid graph_id, please ensure the project has a built graph"
                }), 400

            logger.info(f"Graph memory update enabled: simulation_id={simulation_id}, graph_id={graph_id}")

        # Auto-validate & fix profiles before start
        try:
            sim_dir = manager._get_simulation_dir(simulation_id)
            fixes = manager._validate_and_fix_profiles(sim_dir, state)
            if fixes:
                logger.info(f"Auto-fixed {len(fixes)} profile issues before start: {fixes[:5]}")
        except Exception as e:
            logger.warning(f"Profile validation warning (non-blocking): {e}")

        # Start simulation
        run_state = SimulationRunner.start_simulation(
            simulation_id=simulation_id,
            platform=platform,
            max_rounds=max_rounds,
            enable_graph_memory_update=enable_graph_memory_update,
            graph_id=graph_id
        )

        # Update simulation status
        state.status = SimulationStatus.RUNNING
        manager._save_simulation_state(state)

        response_data = run_state.to_dict()
        if max_rounds:
            response_data['max_rounds_applied'] = max_rounds
        response_data['graph_memory_update_enabled'] = enable_graph_memory_update
        response_data['force_restarted'] = force_restarted
        if enable_graph_memory_update:
            response_data['graph_id'] = graph_id

        return jsonify({
            "success": True,
            "data": response_data
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except Exception as e:
        logger.error(f"Failed to start simulation: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/stop', methods=['POST'])
def stop_simulation():
    """
    Stop simulation

    Request (JSON):
        {
            "simulation_id": "sim_xxxx"  // Required, simulation ID
        }

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "runner_status": "stopped",
                "completed_at": "2025-12-01T12:00:00"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        run_state = SimulationRunner.stop_simulation(simulation_id)

        # Update simulation status
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        if state:
            state.status = SimulationStatus.PAUSED
            manager._save_simulation_state(state)

        return jsonify({
            "success": True,
            "data": run_state.to_dict()
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except Exception as e:
        logger.error(f"Failed to stop simulation: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Real-time Status Monitoring Endpoints ==============

@simulation_bp.route('/<simulation_id>/run-status', methods=['GET'])
def get_run_status(simulation_id: str):
    """
    Get simulation real-time run status (for frontend polling)

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "runner_status": "running",
                "current_round": 5,
                "total_rounds": 144,
                "progress_percent": 3.5,
                "simulated_hours": 2,
                "total_simulation_hours": 72,
                "twitter_running": true,
                "reddit_running": true,
                "twitter_actions_count": 150,
                "reddit_actions_count": 200,
                "total_actions_count": 350,
                "started_at": "2025-12-01T10:00:00",
                "updated_at": "2025-12-01T10:30:00"
            }
        }
    """
    try:
        run_state = SimulationRunner.get_run_state(simulation_id)

        if not run_state:
            return jsonify({
                "success": True,
                "data": {
                    "simulation_id": simulation_id,
                    "runner_status": "idle",
                    "current_round": 0,
                    "total_rounds": 0,
                    "progress_percent": 0,
                    "twitter_actions_count": 0,
                    "reddit_actions_count": 0,
                    "total_actions_count": 0,
                }
            })

        return jsonify({
            "success": True,
            "data": run_state.to_dict()
        })

    except Exception as e:
        logger.error(f"Failed to get run status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/run-status/detail', methods=['GET'])
def get_run_status_detail(simulation_id: str):
    """
    Get detailed simulation run status (including all actions)

    Used for frontend real-time activity display

    Query parameters:
        platform: Filter by platform (twitter/reddit, optional)

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "runner_status": "running",
                "current_round": 5,
                ...
                "all_actions": [
                    {
                        "round_num": 5,
                        "timestamp": "2025-12-01T10:30:00",
                        "platform": "twitter",
                        "agent_id": 3,
                        "agent_name": "Agent Name",
                        "action_type": "CREATE_POST",
                        "action_args": {"content": "..."},
                        "result": null,
                        "success": true
                    },
                    ...
                ],
                "twitter_actions": [...],  # All actions on the Twitter platform
                "reddit_actions": [...]    # All actions on the Reddit platform
            }
        }
    """
    try:
        run_state = SimulationRunner.get_run_state(simulation_id)
        platform_filter = request.args.get('platform')

        if not run_state:
            return jsonify({
                "success": True,
                "data": {
                    "simulation_id": simulation_id,
                    "runner_status": "idle",
                    "all_actions": [],
                    "twitter_actions": [],
                    "reddit_actions": []
                }
            })

        # Get complete action list
        all_actions = SimulationRunner.get_all_actions(
            simulation_id=simulation_id,
            platform=platform_filter
        )

        # Get actions by platform
        twitter_actions = SimulationRunner.get_all_actions(
            simulation_id=simulation_id,
            platform="twitter"
        ) if not platform_filter or platform_filter == "twitter" else []

        reddit_actions = SimulationRunner.get_all_actions(
            simulation_id=simulation_id,
            platform="reddit"
        ) if not platform_filter or platform_filter == "reddit" else []

        # Get current round actions (recent_actions only shows the latest round)
        current_round = run_state.current_round
        recent_actions = SimulationRunner.get_all_actions(
            simulation_id=simulation_id,
            platform=platform_filter,
            round_num=current_round
        ) if current_round > 0 else []

        # Get base status information
        result = run_state.to_dict()
        result["all_actions"] = [a.to_dict() for a in all_actions]
        result["twitter_actions"] = [a.to_dict() for a in twitter_actions]
        result["reddit_actions"] = [a.to_dict() for a in reddit_actions]
        result["rounds_count"] = len(run_state.rounds)
        # recent_actions only shows content from both platforms for the current latest round
        result["recent_actions"] = [a.to_dict() for a in recent_actions]

        return jsonify({
            "success": True,
            "data": result
        })

    except Exception as e:
        logger.error(f"Failed to get detailed status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/actions', methods=['GET'])
def get_simulation_actions(simulation_id: str):
    """
    Get Agent action history in the simulation

    Query parameters:
        limit: Return count (default 100)
        offset: Offset (default 0)
        platform: Filter by platform (twitter/reddit)
        agent_id: Filter by Agent ID
        round_num: Filter by round number

    Returns:
        {
            "success": true,
            "data": {
                "count": 100,
                "actions": [...]
            }
        }
    """
    try:
        limit = request.args.get('limit', 100, type=int)
        offset = request.args.get('offset', 0, type=int)
        platform = request.args.get('platform')
        agent_id = request.args.get('agent_id', type=int)
        round_num = request.args.get('round_num', type=int)

        actions = SimulationRunner.get_actions(
            simulation_id=simulation_id,
            limit=limit,
            offset=offset,
            platform=platform,
            agent_id=agent_id,
            round_num=round_num
        )

        return jsonify({
            "success": True,
            "data": {
                "count": len(actions),
                "actions": [a.to_dict() for a in actions]
            }
        })

    except Exception as e:
        logger.error(f"Failed to get action history: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/timeline', methods=['GET'])
def get_simulation_timeline(simulation_id: str):
    """
    Get simulation timeline (summarized by round)

    Used for frontend progress bar and timeline view display

    Query parameters:
        start_round: Starting round (default 0)
        end_round: Ending round (default all)

    Returns summary information for each round
    """
    try:
        start_round = request.args.get('start_round', 0, type=int)
        end_round = request.args.get('end_round', type=int)

        timeline = SimulationRunner.get_timeline(
            simulation_id=simulation_id,
            start_round=start_round,
            end_round=end_round
        )

        return jsonify({
            "success": True,
            "data": {
                "rounds_count": len(timeline),
                "timeline": timeline
            }
        })

    except Exception as e:
        logger.error(f"Failed to get timeline: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/agent-stats', methods=['GET'])
def get_agent_stats(simulation_id: str):
    """
    Get statistics for each Agent

    Used for frontend display of Agent activity ranking, action distribution, etc.
    """
    try:
        stats = SimulationRunner.get_agent_stats(simulation_id)

        return jsonify({
            "success": True,
            "data": {
                "agents_count": len(stats),
                "stats": stats
            }
        })

    except Exception as e:
        logger.error(f"Failed to get Agent statistics: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Database Query Endpoints ==============

@simulation_bp.route('/<simulation_id>/posts', methods=['GET'])
def get_simulation_posts(simulation_id: str):
    """
    Get posts in the simulation

    Query parameters:
        platform: Platform type (twitter/reddit)
        limit: Return count (default 50)
        offset: Offset

    Returns post list (read from SQLite database)
    """
    try:
        platform = request.args.get('platform', 'reddit')
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)

        sim_dir = os.path.join(
            os.path.dirname(__file__),
            f'../../uploads/simulations/{simulation_id}'
        )

        db_file = f"{platform}_simulation.db"
        db_path = os.path.join(sim_dir, db_file)

        if not os.path.exists(db_path):
            return jsonify({
                "success": True,
                "data": {
                    "platform": platform,
                    "count": 0,
                    "posts": [],
                    "message": "Database does not exist, simulation may not have run yet"
                }
            })

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT * FROM post
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))

            posts = [dict(row) for row in cursor.fetchall()]

            cursor.execute("SELECT COUNT(*) FROM post")
            total = cursor.fetchone()[0]

        except sqlite3.OperationalError:
            posts = []
            total = 0

        conn.close()

        return jsonify({
            "success": True,
            "data": {
                "platform": platform,
                "total": total,
                "count": len(posts),
                "posts": posts
            }
        })

    except Exception as e:
        logger.error(f"Failed to get posts: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/<simulation_id>/comments', methods=['GET'])
def get_simulation_comments(simulation_id: str):
    """
    Get comments in the simulation (Reddit only)

    Query parameters:
        post_id: Filter by post ID (optional)
        limit: Return count
        offset: Offset
    """
    try:
        post_id = request.args.get('post_id')
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)

        sim_dir = os.path.join(
            os.path.dirname(__file__),
            f'../../uploads/simulations/{simulation_id}'
        )

        db_path = os.path.join(sim_dir, "reddit_simulation.db")

        if not os.path.exists(db_path):
            return jsonify({
                "success": True,
                "data": {
                    "count": 0,
                    "comments": []
                }
            })

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            if post_id:
                cursor.execute("""
                    SELECT * FROM comment
                    WHERE post_id = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """, (post_id, limit, offset))
            else:
                cursor.execute("""
                    SELECT * FROM comment
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset))

            comments = [dict(row) for row in cursor.fetchall()]

        except sqlite3.OperationalError:
            comments = []

        conn.close()

        return jsonify({
            "success": True,
            "data": {
                "count": len(comments),
                "comments": comments
            }
        })

    except Exception as e:
        logger.error(f"Failed to get comments: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Interview Endpoints ==============

@simulation_bp.route('/interview', methods=['POST'])
def interview_agent():
    """
    Interview a single Agent

    Note: This feature requires the simulation environment to be in a running state
    (entered waiting-for-command mode after completing simulation cycles)

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",       // Required, simulation ID
            "agent_id": 0,                     // Required, Agent ID
            "prompt": "What do you think about this?",  // Required, interview question
            "platform": "twitter",             // Optional, specify platform (twitter/reddit)
                                               // When not specified: dual-platform simulation interviews both platforms simultaneously
            "timeout": 60                      // Optional, timeout in seconds, default 60
        }

    Returns (without specifying platform, dual-platform mode):
        {
            "success": true,
            "data": {
                "agent_id": 0,
                "prompt": "What do you think about this?",
                "result": {
                    "agent_id": 0,
                    "prompt": "...",
                    "platforms": {
                        "twitter": {"agent_id": 0, "response": "...", "platform": "twitter"},
                        "reddit": {"agent_id": 0, "response": "...", "platform": "reddit"}
                    }
                },
                "timestamp": "2025-12-08T10:00:01"
            }
        }

    Returns (with specified platform):
        {
            "success": true,
            "data": {
                "agent_id": 0,
                "prompt": "What do you think about this?",
                "result": {
                    "agent_id": 0,
                    "response": "I think...",
                    "platform": "twitter",
                    "timestamp": "2025-12-08T10:00:00"
                },
                "timestamp": "2025-12-08T10:00:01"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        agent_id = data.get('agent_id')
        prompt = data.get('prompt')
        platform = data.get('platform')  # Optional: twitter/reddit/None
        timeout = data.get('timeout', 60)

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        if agent_id is None:
            return jsonify({
                "success": False,
                "error": "Please provide agent_id"
            }), 400

        if not prompt:
            return jsonify({
                "success": False,
                "error": "Please provide prompt (interview question)"
            }), 400

        # Validate platform parameter
        if platform and platform not in ("twitter", "reddit"):
            return jsonify({
                "success": False,
                "error": "platform parameter can only be 'twitter' or 'reddit'"
            }), 400

        # Check environment status
        if not SimulationRunner.check_env_alive(simulation_id):
            return jsonify({
                "success": False,
                "error": "Simulation environment is not running or has been closed. Please ensure the simulation has completed and entered waiting-for-command mode."
            }), 400

        # Optimize prompt by adding prefix to prevent Agent from calling tools
        optimized_prompt = optimize_interview_prompt(prompt)

        result = SimulationRunner.interview_agent(
            simulation_id=simulation_id,
            agent_id=agent_id,
            prompt=optimized_prompt,
            platform=platform,
            timeout=timeout
        )

        return jsonify({
            "success": result.get("success", False),
            "data": result
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except TimeoutError as e:
        return jsonify({
            "success": False,
            "error": f"Timed out waiting for Interview response: {str(e)}"
        }), 504

    except Exception as e:
        logger.error(f"Interview failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/interview/batch', methods=['POST'])
def interview_agents_batch():
    """
    Batch interview multiple Agents

    Note: This feature requires the simulation environment to be in a running state

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",       // Required, simulation ID
            "interviews": [                    // Required, interview list
                {
                    "agent_id": 0,
                    "prompt": "What do you think about A?",
                    "platform": "twitter"      // Optional, specify platform for this Agent's interview
                },
                {
                    "agent_id": 1,
                    "prompt": "What do you think about B?"  // Not specifying platform uses the default
                }
            ],
            "platform": "reddit",              // Optional, default platform (overridden by each item's platform)
                                               // When not specified: dual-platform simulation interviews each Agent on both platforms simultaneously
            "timeout": 120                     // Optional, timeout in seconds, default 120
        }

    Returns:
        {
            "success": true,
            "data": {
                "interviews_count": 2,
                "result": {
                    "interviews_count": 4,
                    "results": {
                        "twitter_0": {"agent_id": 0, "response": "...", "platform": "twitter"},
                        "reddit_0": {"agent_id": 0, "response": "...", "platform": "reddit"},
                        "twitter_1": {"agent_id": 1, "response": "...", "platform": "twitter"},
                        "reddit_1": {"agent_id": 1, "response": "...", "platform": "reddit"}
                    }
                },
                "timestamp": "2025-12-08T10:00:01"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        interviews = data.get('interviews')
        platform = data.get('platform')  # Optional: twitter/reddit/None
        timeout = data.get('timeout', 120)

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        if not interviews or not isinstance(interviews, list):
            return jsonify({
                "success": False,
                "error": "Please provide interviews (interview list)"
            }), 400

        # Validate platform parameter
        if platform and platform not in ("twitter", "reddit"):
            return jsonify({
                "success": False,
                "error": "platform parameter can only be 'twitter' or 'reddit'"
            }), 400

        # Validate each interview item
        for i, interview in enumerate(interviews):
            if 'agent_id' not in interview:
                return jsonify({
                    "success": False,
                    "error": f"Interview list item {i+1} is missing agent_id"
                }), 400
            if 'prompt' not in interview:
                return jsonify({
                    "success": False,
                    "error": f"Interview list item {i+1} is missing prompt"
                }), 400
            # Validate each item's platform (if present)
            item_platform = interview.get('platform')
            if item_platform and item_platform not in ("twitter", "reddit"):
                return jsonify({
                    "success": False,
                    "error": f"Interview list item {i+1}'s platform can only be 'twitter' or 'reddit'"
                }), 400

        # Check environment status
        if not SimulationRunner.check_env_alive(simulation_id):
            return jsonify({
                "success": False,
                "error": "Simulation environment is not running or has been closed. Please ensure the simulation has completed and entered waiting-for-command mode."
            }), 400

        # Optimize each interview item's prompt by adding prefix to prevent Agent from calling tools
        optimized_interviews = []
        for interview in interviews:
            optimized_interview = interview.copy()
            optimized_interview['prompt'] = optimize_interview_prompt(interview.get('prompt', ''))
            optimized_interviews.append(optimized_interview)

        result = SimulationRunner.interview_agents_batch(
            simulation_id=simulation_id,
            interviews=optimized_interviews,
            platform=platform,
            timeout=timeout
        )

        return jsonify({
            "success": result.get("success", False),
            "data": result
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except TimeoutError as e:
        return jsonify({
            "success": False,
            "error": f"Timed out waiting for batch Interview response: {str(e)}"
        }), 504

    except Exception as e:
        logger.error(f"Batch Interview failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/interview/all', methods=['POST'])
def interview_all_agents():
    """
    Global interview - Interview all Agents with the same question

    Note: This feature requires the simulation environment to be in a running state

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",            // Required, simulation ID
            "prompt": "What is your overall view on this matter?",  // Required, interview question (same for all Agents)
            "platform": "reddit",                   // Optional, specify platform (twitter/reddit)
                                                    // When not specified: dual-platform simulation interviews each Agent on both platforms simultaneously
            "timeout": 180                          // Optional, timeout in seconds, default 180
        }

    Returns:
        {
            "success": true,
            "data": {
                "interviews_count": 50,
                "result": {
                    "interviews_count": 100,
                    "results": {
                        "twitter_0": {"agent_id": 0, "response": "...", "platform": "twitter"},
                        "reddit_0": {"agent_id": 0, "response": "...", "platform": "reddit"},
                        ...
                    }
                },
                "timestamp": "2025-12-08T10:00:01"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        prompt = data.get('prompt')
        platform = data.get('platform')  # Optional: twitter/reddit/None
        timeout = data.get('timeout', 180)

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        if not prompt:
            return jsonify({
                "success": False,
                "error": "Please provide prompt (interview question)"
            }), 400

        # Validate platform parameter
        if platform and platform not in ("twitter", "reddit"):
            return jsonify({
                "success": False,
                "error": "platform parameter can only be 'twitter' or 'reddit'"
            }), 400

        # Check environment status
        if not SimulationRunner.check_env_alive(simulation_id):
            return jsonify({
                "success": False,
                "error": "Simulation environment is not running or has been closed. Please ensure the simulation has completed and entered waiting-for-command mode."
            }), 400

        # Optimize prompt by adding prefix to prevent Agent from calling tools
        optimized_prompt = optimize_interview_prompt(prompt)

        result = SimulationRunner.interview_all_agents(
            simulation_id=simulation_id,
            prompt=optimized_prompt,
            platform=platform,
            timeout=timeout
        )

        return jsonify({
            "success": result.get("success", False),
            "data": result
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except TimeoutError as e:
        return jsonify({
            "success": False,
            "error": f"Timed out waiting for global Interview response: {str(e)}"
        }), 504

    except Exception as e:
        logger.error(f"Global Interview failed: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/interview/history', methods=['POST'])
def get_interview_history():
    """
    Get Interview history records

    Read all Interview records from the simulation database

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",  // Required, simulation ID
            "platform": "reddit",          // Optional, platform type (reddit/twitter)
                                           // Not specified returns history from both platforms
            "agent_id": 0,                 // Optional, get only this Agent's interview history
            "limit": 100                   // Optional, return count, default 100
        }

    Returns:
        {
            "success": true,
            "data": {
                "count": 10,
                "history": [
                    {
                        "agent_id": 0,
                        "response": "I think...",
                        "prompt": "What do you think about this?",
                        "timestamp": "2025-12-08T10:00:00",
                        "platform": "reddit"
                    },
                    ...
                ]
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        platform = data.get('platform')  # Not specified returns history from both platforms
        agent_id = data.get('agent_id')
        limit = data.get('limit', 100)

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        history = SimulationRunner.get_interview_history(
            simulation_id=simulation_id,
            platform=platform,
            agent_id=agent_id,
            limit=limit
        )

        return jsonify({
            "success": True,
            "data": {
                "count": len(history),
                "history": history
            }
        })

    except Exception as e:
        logger.error(f"Failed to get Interview history: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/env-status', methods=['POST'])
def get_env_status():
    """
    Get simulation environment status

    Check if the simulation environment is alive (can receive Interview commands)

    Request (JSON):
        {
            "simulation_id": "sim_xxxx"  // Required, simulation ID
        }

    Returns:
        {
            "success": true,
            "data": {
                "simulation_id": "sim_xxxx",
                "env_alive": true,
                "twitter_available": true,
                "reddit_available": true,
                "message": "Environment is running and can receive Interview commands"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        env_alive = SimulationRunner.check_env_alive(simulation_id)

        # Get more detailed status information
        env_status = SimulationRunner.get_env_status_detail(simulation_id)

        if env_alive:
            message = "Environment is running and can receive Interview commands"
        else:
            message = "Environment is not running or has been closed"

        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "env_alive": env_alive,
                "twitter_available": env_status.get("twitter_available", False),
                "reddit_available": env_status.get("reddit_available", False),
                "message": message
            }
        })

    except Exception as e:
        logger.error(f"Failed to get environment status: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@simulation_bp.route('/close-env', methods=['POST'])
def close_simulation_env():
    """
    Close simulation environment

    Send a close environment command to the simulation, allowing it to gracefully exit waiting-for-command mode.

    Note: This is different from the /stop endpoint. /stop forcefully terminates the process,
    while this endpoint allows the simulation to gracefully close the environment and exit.

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",  // Required, simulation ID
            "timeout": 30                  // Optional, timeout in seconds, default 30
        }

    Returns:
        {
            "success": true,
            "data": {
                "message": "Close environment command has been sent",
                "result": {...},
                "timestamp": "2025-12-08T10:00:01"
            }
        }
    """
    try:
        data = request.get_json() or {}

        simulation_id = data.get('simulation_id')
        timeout = data.get('timeout', 30)

        if not simulation_id:
            return jsonify({
                "success": False,
                "error": "Please provide simulation_id"
            }), 400

        result = SimulationRunner.close_simulation_env(
            simulation_id=simulation_id,
            timeout=timeout
        )

        # Update simulation status
        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        if state:
            state.status = SimulationStatus.COMPLETED
            manager._save_simulation_state(state)

        return jsonify({
            "success": result.get("success", False),
            "data": result
        })

    except ValueError as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

    except Exception as e:
        logger.error(f"Failed to close environment: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


def _classify_sim_error(error_str: str) -> str:
    """Classify simulation error for targeted auto-fix."""
    if not error_str:
        return "unknown"
    err = error_str.lower()
    if "keyerror" in err and ("mbti" in err or "age" in err or "gender" in err or "country" in err):
        return "missing_profile_field"
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if "connection" in err or "refused" in err:
        return "connection"
    if "memory" in err or "oom" in err:
        return "memory"
    if "json" in err and ("decode" in err or "parse" in err):
        return "json_parse"
    return "unknown"


def _log_auto_improve_result(simulation_id: str, attempt: int, fixes: list, error_class: str, success: bool, actions: int = 0, elapsed: float = 0):
    """Log auto-improve results for learning. Append to perf CSV."""
    try:
        import csv
        from datetime import datetime
        log_path = os.path.expanduser("~/.mirofish_autofix_log.csv")
        is_new = not os.path.exists(log_path)
        with open(log_path, 'a', newline='') as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["timestamp", "simulation_id", "attempt", "fixes", "error_class", "success", "actions", "elapsed_s"])
            w.writerow([datetime.now().isoformat(), simulation_id, attempt, len(fixes), error_class, success, actions, f"{elapsed:.1f}"])
    except Exception:
        pass


@simulation_bp.route('/auto-improve', methods=['POST'])
def auto_improve():
    """
    Self-healing simulation loop with error classification and targeted fixes.

    Flow per attempt:
    1. Validate & auto-fix profiles (missing fields, format issues)
    2. Start simulation
    3. Wait for completion with progress tracking
    4. On failure: classify error, apply targeted fix, retry with backoff
    5. On success: log performance metrics for cross-session learning

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",
            "platform": "parallel",
            "max_rounds": 10,
            "max_retries": 2
        }
    """
    import threading
    from ..models.task import TaskManager, TaskStatus

    try:
        data = request.get_json() or {}
        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({"success": False, "error": "simulation_id required"}), 400

        platform = data.get('platform', 'parallel')
        max_rounds = data.get('max_rounds', 10)
        max_retries = min(data.get('max_retries', 2), 3)

        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        if not state:
            return jsonify({"success": False, "error": f"Simulation not found: {simulation_id}"}), 404

        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="auto_improve",
            metadata={"simulation_id": simulation_id}
        )

        def run_auto_improve():
            import time

            for attempt in range(max_retries + 1):
                attempt_start = time.time()
                error_class = "none"
                fixes = []

                try:
                    # Step 1: Auto-validate & fix profiles
                    sim_dir = manager._get_simulation_dir(simulation_id)
                    fixes = manager._validate_and_fix_profiles(sim_dir, state)
                    pct = int(10 + (attempt / (max_retries + 1)) * 80)
                    task_manager.update_task(
                        task_id, progress=pct,
                        message=f"[{attempt+1}/{max_retries+1}] {len(fixes)} fixes applied, starting sim..."
                    )

                    # Step 2: Clean previous run if retrying
                    if attempt > 0:
                        try:
                            SimulationRunner.cleanup_simulation_logs(simulation_id)
                        except Exception:
                            pass
                        # Backoff: 2s, 4s, 8s
                        time.sleep(min(2 ** (attempt + 1), 8))

                    # Step 3: Run simulation
                    SimulationRunner.start_simulation(
                        simulation_id=simulation_id,
                        platform=platform,
                        max_rounds=max_rounds,
                        enable_graph_memory_update=bool(state.graph_id),
                        graph_id=state.graph_id
                    )

                    # Step 4: Wait for completion (5s polls, 10 min max)
                    for tick in range(120):
                        time.sleep(5)
                        rs = SimulationRunner.get_run_state(simulation_id)
                        if rs and rs.runner_status.value in ("completed", "failed"):
                            break
                        # Progress update every 30s
                        if tick % 6 == 0:
                            actions = rs.total_actions_count if rs else 0
                            task_manager.update_task(
                                task_id, progress=pct + 5,
                                message=f"[{attempt+1}] Running... {actions} actions"
                            )

                    # Step 5: Check result
                    rs = SimulationRunner.get_run_state(simulation_id)
                    elapsed = time.time() - attempt_start

                    if rs and rs.runner_status.value == "completed" and not rs.error:
                        # Success
                        _log_auto_improve_result(
                            simulation_id, attempt + 1, fixes, "none", True,
                            rs.total_actions_count, elapsed
                        )
                        task_manager.complete_task(task_id, result={
                            "simulation_id": simulation_id,
                            "attempt": attempt + 1,
                            "fixes_applied": len(fixes),
                            "total_actions": rs.total_actions_count,
                            "elapsed_seconds": round(elapsed, 1),
                            "status": "completed"
                        })
                        return

                    # Failure: classify and log
                    err = rs.error if rs else "Unknown error"
                    error_class = _classify_sim_error(err)
                    _log_auto_improve_result(
                        simulation_id, attempt + 1, fixes, error_class, False,
                        rs.total_actions_count if rs else 0, elapsed
                    )
                    logger.warning(f"Auto-improve [{attempt+1}] failed ({error_class}): {err[:200]}")

                    if attempt < max_retries:
                        task_manager.update_task(
                            task_id, progress=pct + 10,
                            message=f"[{attempt+1}] Failed ({error_class}), retrying..."
                        )
                    else:
                        task_manager.fail_task(
                            task_id,
                            f"All {max_retries+1} attempts failed. Last error ({error_class}): {err[:300]}"
                        )

                except Exception as e:
                    elapsed = time.time() - attempt_start
                    error_class = _classify_sim_error(str(e))
                    _log_auto_improve_result(
                        simulation_id, attempt + 1, fixes, error_class, False, 0, elapsed
                    )
                    logger.error(f"Auto-improve [{attempt+1}] exception: {e}")
                    if attempt == max_retries:
                        task_manager.fail_task(task_id, str(e))
                        return

        thread = threading.Thread(target=run_auto_improve, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "data": {
                "simulation_id": simulation_id,
                "task_id": task_id,
                "max_retries": max_retries,
                "status": "running",
                "message": "Auto-improve loop started (validate -> run -> classify errors -> fix -> retry)"
            }
        })

    except Exception as e:
        logger.error(f"Auto-improve failed: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============== Feedback Loop Endpoint ==============

def _extract_insights_from_actions(simulation_id: str) -> str:
    """
    Extract insights from simulation output for graph enrichment.

    Reads from SQLite databases (primary) and JSONL action logs (fallback).
    Extracts:
    - Top posts by engagement (likes, comments)
    - Emerging topics / trending keywords
    - Agent influence patterns (follows, activity)
    - Key discussion threads (comments on posts)

    Returns natural language paragraphs suitable for NER enrichment.
    """
    import json as _json
    import sqlite3
    import collections

    sim_dir = os.path.join(Config.UPLOAD_FOLDER, 'simulations', simulation_id)

    insights_parts = []

    # ── Strategy 1: SQLite databases (OASIS format) ──
    for platform in ['reddit', 'twitter']:
        db_path = os.path.join(sim_dir, f'{platform}_simulation.db')
        if not os.path.exists(db_path):
            continue

        try:
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row

            # Get user name mapping
            users = {}
            for row in db.execute("SELECT user_id, name, bio FROM user"):
                users[row["user_id"]] = {
                    "name": row["name"] or f"user_{row['user_id']}",
                    "bio": (row["bio"] or "")[:100],
                }

            # Top posts by engagement (likes + comments)
            posts = db.execute("""
                SELECT p.post_id, p.user_id, p.content, p.num_likes,
                       (SELECT COUNT(*) FROM comment c WHERE c.post_id = p.post_id) as num_comments
                FROM post p
                ORDER BY (p.num_likes + (SELECT COUNT(*) FROM comment c WHERE c.post_id = p.post_id)) DESC
                LIMIT 10
            """).fetchall()

            if posts:
                post_lines = []
                for p in posts:
                    author = users.get(p["user_id"], {}).get("name", "unknown")
                    engagement = p["num_likes"] + p["num_comments"]
                    content = (p["content"] or "")[:200]
                    if content:
                        post_lines.append(
                            f"{author} posted ({engagement} engagements): {content}"
                        )
                if post_lines:
                    insights_parts.append(
                        f"Top {platform} posts by engagement:\n" + "\n".join(post_lines)
                    )

            # Extract topics from all post content
            topics = collections.Counter()
            stop_words = {'this', 'that', 'with', 'have', 'from', 'they', 'been',
                          'will', 'their', 'would', 'could', 'about', 'which', 'there',
                          'these', 'those', 'more', 'some', 'into', 'than', 'other'}
            for row in db.execute("SELECT content FROM post WHERE content IS NOT NULL"):
                words = (row["content"] or "").lower().split()
                for w in words:
                    w = w.strip(".,!?;:\"'()[]{}#@")
                    if len(w) > 4 and w not in stop_words and w.isalpha():
                        topics[w] += 1

            top_topics = [w for w, c in topics.most_common(25) if c >= 2]
            if top_topics:
                insights_parts.append(
                    f"Trending {platform} discussion topics: {', '.join(top_topics[:15])}"
                )

            # Most active users (by trace actions)
            user_activity = collections.Counter()
            for row in db.execute("SELECT user_id, COUNT(*) as cnt FROM trace GROUP BY user_id ORDER BY cnt DESC LIMIT 10"):
                uname = users.get(row["user_id"], {}).get("name", f"user_{row['user_id']}")
                user_activity[uname] = row["cnt"]

            if user_activity:
                agent_lines = [f"{name}: {count} actions" for name, count in user_activity.most_common(10)]
                insights_parts.append(
                    f"Most active {platform} agents:\n" + "\n".join(agent_lines)
                )

            # Key discussion threads (posts with most comments)
            threads = db.execute("""
                SELECT p.content, GROUP_CONCAT(c.content, ' | ') as comments
                FROM post p
                JOIN comment c ON c.post_id = p.post_id
                GROUP BY p.post_id
                ORDER BY COUNT(c.comment_id) DESC
                LIMIT 5
            """).fetchall()

            if threads:
                thread_lines = []
                for t in threads:
                    post_text = (t["content"] or "")[:150]
                    comments_text = (t["comments"] or "")[:300]
                    if post_text:
                        thread_lines.append(f"Post: {post_text}\n  Responses: {comments_text}")
                if thread_lines:
                    insights_parts.append(
                        f"Key {platform} discussion threads:\n" + "\n\n".join(thread_lines)
                    )

            # Follow network (influence patterns)
            follows = db.execute("""
                SELECT followee_id, COUNT(*) as follower_count
                FROM follow
                GROUP BY followee_id
                ORDER BY follower_count DESC
                LIMIT 5
            """).fetchall()

            if follows:
                influence_lines = []
                for f in follows:
                    uname = users.get(f["followee_id"], {}).get("name", f"user_{f['followee_id']}")
                    influence_lines.append(f"{uname} gained {f['follower_count']} followers")
                insights_parts.append(
                    f"{platform.title()} influence leaders:\n" + "\n".join(influence_lines)
                )

            db.close()

        except Exception as e:
            logger.warning(f"Failed to read {db_path}: {e}")
            continue

    # ── Strategy 2: JSONL action logs (fallback) ──
    if not insights_parts:
        for platform in ['reddit', 'twitter']:
            actions_path = os.path.join(sim_dir, f'{platform}_actions.jsonl')
            if not os.path.exists(actions_path):
                continue

            actions = []
            try:
                with open(actions_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                actions.append(_json.loads(line))
                            except _json.JSONDecodeError:
                                continue
            except Exception as e:
                logger.warning(f"Failed to read {actions_path}: {e}")
                continue

            if not actions:
                continue

            post_content = {}
            post_engagement = collections.Counter()
            agent_actions = collections.Counter()
            topics = collections.Counter()

            for action in actions:
                action_type = action.get('action_type', action.get('type', ''))
                agent_name = action.get('agent_name', action.get('username', 'unknown'))
                content = action.get('content', action.get('text', ''))
                post_id = action.get('post_id', action.get('target_id', ''))

                agent_actions[agent_name] += 1

                if action_type in ('CREATE_POST', 'create_post') and content:
                    post_content[post_id or content[:50]] = content
                    words = content.lower().split()
                    for w in words:
                        if len(w) > 4:
                            topics[w] += 1

                if action_type in ('LIKE_POST', 'REPOST', 'like_post', 'repost') and post_id:
                    post_engagement[post_id] += 1

            top_posts = post_engagement.most_common(10)
            if top_posts:
                post_lines = [
                    f"Post with {count} engagements: {post_content.get(pid, f'Post {pid}')[:200]}"
                    for pid, count in top_posts
                ]
                insights_parts.append(
                    f"Top {platform} posts by engagement:\n" + "\n".join(post_lines)
                )

            top_agents = agent_actions.most_common(10)
            if top_agents:
                agent_lines = [f"{name}: {count} actions" for name, count in top_agents]
                insights_parts.append(
                    f"Most active {platform} agents:\n" + "\n".join(agent_lines)
                )

            top_topics = [w for w, c in topics.most_common(20) if c >= 3]
            if top_topics:
                insights_parts.append(
                    f"Trending {platform} topics: {', '.join(top_topics[:15])}"
                )

    if not insights_parts:
        return ""

    return "\n\n".join(insights_parts)


@simulation_bp.route('/feedback-loop', methods=['POST'])
def feedback_loop():
    """
    Auto-improvement feedback loop: simulate -> extract insights -> enrich graph -> re-simulate.

    1. Run simulation (or use existing completed sim)
    2. Extract insights from action_log (top posts, trending topics, interaction patterns)
    3. Feed insights text to /api/graph/enrich with source="feedback_loop"
    4. Re-prepare simulation (regenerate profiles from enriched graph)
    5. Re-run simulation
    6. Repeat for max_iterations (default 2)

    Request (JSON):
        {
            "simulation_id": "sim_xxxx",
            "platform": "parallel",
            "max_rounds": 10,
            "max_iterations": 2,
            "skip_first_run": false   // If true, skip step 1 (sim already completed)
        }

    Returns:
        {
            "success": true,
            "data": {
                "task_id": "task_xxxx",
                "simulation_id": "sim_xxxx",
                "max_iterations": 2
            }
        }
    """
    import threading
    import time as _time
    from ..models.task import TaskManager, TaskStatus
    from ..services.graph_builder import GraphBuilderService
    from ..services.text_processor import TextProcessor

    try:
        data = request.get_json() or {}
        simulation_id = data.get('simulation_id')
        if not simulation_id:
            return jsonify({"success": False, "error": "simulation_id required"}), 400

        platform = data.get('platform', 'parallel')
        max_rounds = data.get('max_rounds', 10)
        max_iterations = min(data.get('max_iterations', 2), 5)
        skip_first_run = data.get('skip_first_run', False)

        manager = SimulationManager()
        state = manager.get_simulation(simulation_id)
        if not state:
            return jsonify({"success": False, "error": f"Simulation not found: {simulation_id}"}), 404

        if not state.graph_id:
            return jsonify({"success": False, "error": "Simulation has no graph_id — cannot enrich"}), 400

        # Get storage from Flask app context (before spawning thread)
        storage = current_app.extensions.get('neo4j_storage')
        if not storage:
            return jsonify({"success": False, "error": "GraphStorage not initialized"}), 500

        task_manager = TaskManager()
        task_id = task_manager.create_task(f"Feedback loop: {simulation_id}")

        def run_feedback_loop():
            total_enriched_chars = 0

            for iteration in range(max_iterations):
                iter_label = f"[{iteration+1}/{max_iterations}]"
                pct = int((iteration / max_iterations) * 90) + 5

                try:
                    # Step 1: Run simulation (skip on first if requested)
                    if not (iteration == 0 and skip_first_run):
                        task_manager.update_task(
                            task_id, progress=pct,
                            message=f"{iter_label} Running simulation..."
                        )
                        SimulationRunner.start_simulation(
                            simulation_id=simulation_id,
                            platform=platform,
                            max_rounds=max_rounds,
                            enable_graph_memory_update=True,
                            graph_id=state.graph_id,
                        )

                        # Wait for completion (5s polls, 10 min max)
                        for tick in range(120):
                            _time.sleep(5)
                            rs = SimulationRunner.get_run_state(simulation_id)
                            if rs and rs.runner_status.value in ("completed", "failed"):
                                break

                        rs = SimulationRunner.get_run_state(simulation_id)
                        if not rs or rs.runner_status.value != "completed":
                            err = rs.error if rs else "Unknown"
                            logger.warning(f"{iter_label} Simulation failed: {err[:200]}")
                            task_manager.update_task(
                                task_id, progress=pct + 5,
                                message=f"{iter_label} Sim failed ({err[:100]}), extracting partial insights..."
                            )

                    # Step 2: Extract insights from action logs
                    task_manager.update_task(
                        task_id, progress=pct + 10,
                        message=f"{iter_label} Extracting insights from action logs..."
                    )
                    insights = _extract_insights_from_actions(simulation_id)

                    if not insights.strip():
                        logger.info(f"{iter_label} No insights extracted, stopping loop")
                        task_manager.update_task(
                            task_id, progress=pct + 15,
                            message=f"{iter_label} No new insights found, stopping"
                        )
                        break

                    # Step 3: Enrich graph with insights
                    task_manager.update_task(
                        task_id, progress=pct + 20,
                        message=f"{iter_label} Enriching graph with {len(insights)} chars of insights..."
                    )

                    chunks = TextProcessor.split_text(insights, chunk_size=500, overlap=50)
                    builder = GraphBuilderService(storage=storage)
                    builder.add_text_batches(state.graph_id, chunks, batch_size=3)
                    total_enriched_chars += len(insights)

                    logger.info(f"{iter_label} Enriched graph with {len(insights)} chars ({len(chunks)} chunks)")

                    # Step 4: Re-prepare profiles (if more iterations remain)
                    if iteration < max_iterations - 1:
                        task_manager.update_task(
                            task_id, progress=pct + 30,
                            message=f"{iter_label} Regenerating profiles from enriched graph..."
                        )
                        # The next simulation run will use updated graph data
                        # via the existing prepare flow

                except Exception as e:
                    logger.error(f"{iter_label} Feedback loop error: {e}")
                    task_manager.update_task(
                        task_id, progress=pct + 35,
                        message=f"{iter_label} Error: {str(e)[:200]}, continuing..."
                    )
                    continue

            # Complete
            task_manager.update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                message="Feedback loop complete",
                result={
                    "simulation_id": simulation_id,
                    "iterations_completed": max_iterations,
                    "total_enriched_chars": total_enriched_chars,
                    "graph_id": state.graph_id,
                }
            )

        thread = threading.Thread(target=run_feedback_loop, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "data": {
                "task_id": task_id,
                "simulation_id": simulation_id,
                "max_iterations": max_iterations,
                "status": "running",
                "message": "Feedback loop started (simulate -> extract -> enrich -> repeat)"
            }
        })

    except Exception as e:
        logger.error(f"Feedback loop failed: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500
