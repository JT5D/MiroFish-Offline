"""
Graph-related API routes
Uses project context mechanism with server-side persistent state
"""

import os
import traceback
import threading
from flask import request, jsonify, current_app

from . import graph_bp
from ..config import Config
from ..services.ontology_generator import OntologyGenerator
from ..services.graph_builder import GraphBuilderService
from ..services.text_processor import TextProcessor
from ..utils.file_parser import FileParser
from ..utils.logger import get_logger
from ..models.task import TaskManager, TaskStatus
from ..models.project import ProjectManager, ProjectStatus

# Get logger
logger = get_logger('mirofish.api')


def _get_storage():
    """Get Neo4jStorage from Flask app extensions."""
    storage = current_app.extensions.get('neo4j_storage')
    if not storage:
        raise ValueError("GraphStorage not initialized — check Neo4j connection")
    return storage


def allowed_file(filename: str) -> bool:
    """Check if the file extension is allowed"""
    if not filename or '.' not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in Config.ALLOWED_EXTENSIONS


# ============== Project Management Endpoints ==============

@graph_bp.route('/project/<project_id>', methods=['GET'])
def get_project(project_id: str):
    """
    Get project details
    """
    project = ProjectManager.get_project(project_id)

    if not project:
        return jsonify({
            "success": False,
            "error": f"Project does not exist: {project_id}"
        }), 404

    return jsonify({
        "success": True,
        "data": project.to_dict()
    })


@graph_bp.route('/project/list', methods=['GET'])
def list_projects():
    """
    List all projects
    """
    limit = request.args.get('limit', 50, type=int)
    projects = ProjectManager.list_projects(limit=limit)

    return jsonify({
        "success": True,
        "data": [p.to_dict() for p in projects],
        "count": len(projects)
    })


@graph_bp.route('/project/<project_id>', methods=['DELETE'])
def delete_project(project_id: str):
    """
    Delete a project
    """
    success = ProjectManager.delete_project(project_id)

    if not success:
        return jsonify({
            "success": False,
            "error": f"Project does not exist or deletion failed: {project_id}"
        }), 404

    return jsonify({
        "success": True,
        "message": f"Project deleted: {project_id}"
    })


@graph_bp.route('/project/<project_id>/reset', methods=['POST'])
def reset_project(project_id: str):
    """
    Reset project state (for rebuilding the graph)
    """
    project = ProjectManager.get_project(project_id)

    if not project:
        return jsonify({
            "success": False,
            "error": f"Project does not exist: {project_id}"
        }), 404

    # Reset to ontology-generated state
    if project.ontology:
        project.status = ProjectStatus.ONTOLOGY_GENERATED
    else:
        project.status = ProjectStatus.CREATED

    project.graph_id = None
    project.graph_build_task_id = None
    project.error = None
    ProjectManager.save_project(project)

    return jsonify({
        "success": True,
        "message": f"Project has been reset: {project_id}",
        "data": project.to_dict()
    })


# ============== Endpoint 1: Upload Files and Generate Ontology ==============

@graph_bp.route('/ontology/generate', methods=['POST'])
def generate_ontology():
    """
    Endpoint 1: Upload files, analyze and generate ontology definition

    Request method: multipart/form-data

    Parameters:
        files: Uploaded files (PDF/MD/TXT), multiple allowed
        simulation_requirement: Simulation requirement description (required)
        project_name: Project name (optional)
        additional_context: Additional notes (optional)

    Returns:
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "ontology": {
                    "entity_types": [...],
                    "edge_types": [...],
                    "analysis_summary": "..."
                },
                "files": [...],
                "total_text_length": 12345
            }
        }
    """
    try:
        logger.info("=== Starting ontology definition generation ===")

        # Get parameters
        simulation_requirement = request.form.get('simulation_requirement', '')
        project_name = request.form.get('project_name', 'Unnamed Project')
        additional_context = request.form.get('additional_context', '')

        logger.debug(f"Project name: {project_name}")
        logger.debug(f"Simulation requirement: {simulation_requirement[:100]}...")

        if not simulation_requirement:
            return jsonify({
                "success": False,
                "error": "Please provide a simulation requirement description (simulation_requirement)"
            }), 400

        # Get uploaded files
        uploaded_files = request.files.getlist('files')
        if not uploaded_files or all(not f.filename for f in uploaded_files):
            return jsonify({
                "success": False,
                "error": "Please upload at least one document file"
            }), 400

        # Create project
        project = ProjectManager.create_project(name=project_name)
        project.simulation_requirement = simulation_requirement
        logger.info(f"Created project: {project.project_id}")

        # Save files and extract text
        document_texts = []
        all_text = ""

        for file in uploaded_files:
            if file and file.filename and allowed_file(file.filename):
                # Save file to project directory
                file_info = ProjectManager.save_file_to_project(
                    project.project_id,
                    file,
                    file.filename
                )
                project.files.append({
                    "filename": file_info["original_filename"],
                    "size": file_info["size"]
                })

                # Extract text
                text = FileParser.extract_text(file_info["path"])
                text = TextProcessor.preprocess_text(text)
                document_texts.append(text)
                all_text += f"\n\n=== {file_info['original_filename']} ===\n{text}"

        if not document_texts:
            ProjectManager.delete_project(project.project_id)
            return jsonify({
                "success": False,
                "error": "No documents were successfully processed, please check the file format"
            }), 400

        # Save extracted text
        project.total_text_length = len(all_text)
        ProjectManager.save_extracted_text(project.project_id, all_text)
        logger.info(f"Text extraction complete, total {len(all_text)} characters")

        # Generate ontology
        logger.info("Calling LLM to generate ontology definition...")
        generator = OntologyGenerator()
        ontology = generator.generate(
            document_texts=document_texts,
            simulation_requirement=simulation_requirement,
            additional_context=additional_context if additional_context else None
        )

        # Save ontology to project
        entity_count = len(ontology.get("entity_types", []))
        edge_count = len(ontology.get("edge_types", []))
        logger.info(f"Ontology generation complete: {entity_count} entity types, {edge_count} relationship types")

        project.ontology = {
            "entity_types": ontology.get("entity_types", []),
            "edge_types": ontology.get("edge_types", [])
        }
        project.analysis_summary = ontology.get("analysis_summary", "")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        ProjectManager.save_project(project)
        logger.info(f"=== Ontology generation complete === Project ID: {project.project_id}")

        return jsonify({
            "success": True,
            "data": {
                "project_id": project.project_id,
                "project_name": project.name,
                "ontology": project.ontology,
                "analysis_summary": project.analysis_summary,
                "files": project.files,
                "total_text_length": project.total_text_length
            }
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Endpoint 2: Build Graph ==============

@graph_bp.route('/build', methods=['POST'])
def build_graph():
    """
    Endpoint 2: Build graph based on project_id

    Request (JSON):
        {
            "project_id": "proj_xxxx",  // Required, from Endpoint 1
            "graph_name": "Graph Name",  // Optional
            "chunk_size": 500,          // Optional, default 500
            "chunk_overlap": 50         // Optional, default 50
        }

    Returns:
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "task_id": "task_xxxx",
                "message": "Graph build task has been started"
            }
        }
    """
    try:
        logger.info("=== Starting graph build ===")

        # Parse request
        data = request.get_json() or {}
        project_id = data.get('project_id')
        logger.debug(f"Request parameters: project_id={project_id}")

        if not project_id:
            return jsonify({
                "success": False,
                "error": "Please provide project_id"
            }), 400

        # Get project
        project = ProjectManager.get_project(project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": f"Project does not exist: {project_id}"
            }), 404

        # Check project status
        force = data.get('force', False)  # Force rebuild

        if project.status == ProjectStatus.CREATED:
            return jsonify({
                "success": False,
                "error": "Project has not generated ontology yet, please call /ontology/generate first"
            }), 400

        if project.status == ProjectStatus.GRAPH_BUILDING and not force:
            return jsonify({
                "success": False,
                "error": "Graph is currently being built, please do not submit again. To force rebuild, add force: true",
                "task_id": project.graph_build_task_id
            }), 400

        # If force rebuild, reset state
        if force and project.status in [ProjectStatus.GRAPH_BUILDING, ProjectStatus.FAILED, ProjectStatus.GRAPH_COMPLETED]:
            project.status = ProjectStatus.ONTOLOGY_GENERATED
            project.graph_id = None
            project.graph_build_task_id = None
            project.error = None

        # Get configuration
        graph_name = data.get('graph_name', project.name or 'MiroFish Graph')
        chunk_size = data.get('chunk_size', project.chunk_size or Config.DEFAULT_CHUNK_SIZE)
        chunk_overlap = data.get('chunk_overlap', project.chunk_overlap or Config.DEFAULT_CHUNK_OVERLAP)

        # Update project configuration
        project.chunk_size = chunk_size
        project.chunk_overlap = chunk_overlap

        # Get extracted text
        text = ProjectManager.get_extracted_text(project_id)
        if not text:
            return jsonify({
                "success": False,
                "error": "Extracted text content not found"
            }), 400

        # Get ontology
        ontology = project.ontology
        if not ontology:
            return jsonify({
                "success": False,
                "error": "Ontology definition not found"
            }), 400

        # Get storage within request context (background thread cannot access current_app)
        storage = _get_storage()

        # Create async task
        task_manager = TaskManager()
        task_id = task_manager.create_task(f"Build graph: {graph_name}")
        logger.info(f"Created graph build task: task_id={task_id}, project_id={project_id}")

        # Update project status
        project.status = ProjectStatus.GRAPH_BUILDING
        project.graph_build_task_id = task_id
        ProjectManager.save_project(project)

        # Start background task
        def build_task():
            build_logger = get_logger('mirofish.build')
            try:
                build_logger.info(f"[{task_id}] Starting graph build...")
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.PROCESSING,
                    message="Initializing graph build service..."
                )

                # Create graph build service (storage passed from outer closure)
                builder = GraphBuilderService(storage=storage)

                # Chunking
                task_manager.update_task(
                    task_id,
                    message="Splitting text into chunks...",
                    progress=5
                )
                chunks = TextProcessor.split_text(
                    text,
                    chunk_size=chunk_size,
                    overlap=chunk_overlap
                )
                total_chunks = len(chunks)

                # Create graph
                task_manager.update_task(
                    task_id,
                    message="Creating Zep graph...",
                    progress=10
                )
                graph_id = builder.create_graph(name=graph_name)

                # Update project's graph_id
                project.graph_id = graph_id
                ProjectManager.save_project(project)

                # Set ontology
                task_manager.update_task(
                    task_id,
                    message="Setting ontology definition...",
                    progress=15
                )
                builder.set_ontology(graph_id, ontology)

                # Add text (progress_callback signature is (msg, progress_ratio))
                def add_progress_callback(msg, progress_ratio):
                    progress = 15 + int(progress_ratio * 40)  # 15% - 55%
                    task_manager.update_task(
                        task_id,
                        message=msg,
                        progress=progress
                    )

                task_manager.update_task(
                    task_id,
                    message=f"Starting to add {total_chunks} text chunks...",
                    progress=15
                )

                episode_uuids = builder.add_text_batches(
                    graph_id,
                    chunks,
                    batch_size=3,
                    progress_callback=add_progress_callback
                )

                # Neo4j processing is synchronous, no need to wait
                task_manager.update_task(
                    task_id,
                    message="Text processing complete, generating graph data...",
                    progress=90
                )

                # Get graph data
                task_manager.update_task(
                    task_id,
                    message="Retrieving graph data...",
                    progress=95
                )
                graph_data = builder.get_graph_data(graph_id)

                # Update project status
                project.status = ProjectStatus.GRAPH_COMPLETED
                ProjectManager.save_project(project)

                node_count = graph_data.get("node_count", 0)
                edge_count = graph_data.get("edge_count", 0)
                build_logger.info(f"[{task_id}] Graph build complete: graph_id={graph_id}, nodes={node_count}, edges={edge_count}")

                # Done
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED,
                    message="Graph build complete",
                    progress=100,
                    result={
                        "project_id": project_id,
                        "graph_id": graph_id,
                        "node_count": node_count,
                        "edge_count": edge_count,
                        "chunk_count": total_chunks
                    }
                )

            except Exception as e:
                # Update project status to failed
                build_logger.error(f"[{task_id}] Graph build failed: {str(e)}")
                build_logger.debug(traceback.format_exc())

                project.status = ProjectStatus.FAILED
                project.error = str(e)
                ProjectManager.save_project(project)

                task_manager.update_task(
                    task_id,
                    status=TaskStatus.FAILED,
                    message=f"Build failed: {str(e)}",
                    error=traceback.format_exc()
                )

        # Start background thread
        thread = threading.Thread(target=build_task, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "data": {
                "project_id": project_id,
                "task_id": task_id,
                "message": "Graph build task has been started, check progress via /task/{task_id}"
            }
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Task Query Endpoints ==============

@graph_bp.route('/task/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """
    Query task status
    """
    task = TaskManager().get_task(task_id)

    if not task:
        return jsonify({
            "success": False,
            "error": f"Task does not exist: {task_id}"
        }), 404

    return jsonify({
        "success": True,
        "data": task.to_dict()
    })


@graph_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """
    List all tasks
    """
    tasks = TaskManager().list_tasks()

    return jsonify({
        "success": True,
        "data": [t.to_dict() for t in tasks],
        "count": len(tasks)
    })


# ============== Graph Data Endpoints ==============

@graph_bp.route('/data/<graph_id>', methods=['GET'])
def get_graph_data(graph_id: str):
    """
    Get graph data (nodes and edges)
    """
    try:
        storage = _get_storage()
        builder = GraphBuilderService(storage=storage)
        graph_data = builder.get_graph_data(graph_id)

        return jsonify({
            "success": True,
            "data": graph_data
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== Graph Enrichment Endpoint ==============

@graph_bp.route('/enrich', methods=['POST'])
def enrich_graph():
    """
    Enrich an existing graph with new text content.

    Chunks the text and runs it through the NER pipeline (storage.add_text()),
    adding new entities and relations to the graph. Async via TaskManager.

    Request (JSON):
        {
            "graph_id": "mirofish_xxxx",   // Required
            "text": "Natural language...",  // Required
            "source": "portals_v4_commits" // Optional, audit trail label
        }

    Returns:
        {
            "success": true,
            "data": {
                "task_id": "task_xxxx",
                "chunk_count": 5
            }
        }
    """
    try:
        data = request.get_json() or {}
        graph_id = data.get('graph_id')
        text = data.get('text', '')
        source = data.get('source', 'manual')

        if not graph_id:
            return jsonify({"success": False, "error": "Please provide graph_id"}), 400
        if not text.strip():
            return jsonify({"success": False, "error": "Please provide non-empty text"}), 400

        storage = _get_storage()

        # Chunk the text
        chunks = TextProcessor.split_text(text, chunk_size=500, overlap=50)
        chunk_count = len(chunks)
        logger.info(f"Enrich graph {graph_id}: {len(text)} chars → {chunk_count} chunks (source={source})")

        # Create async task
        task_manager = TaskManager()
        task_id = task_manager.create_task(f"Enrich graph: {source}")

        def enrich_task():
            enrich_logger = get_logger('mirofish.enrich')
            try:
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.PROCESSING,
                    message=f"Processing {chunk_count} chunks (source={source})...",
                    progress=5
                )

                builder = GraphBuilderService(storage=storage)
                episode_uuids = builder.add_text_batches(
                    graph_id,
                    chunks,
                    batch_size=3,
                    progress_callback=lambda msg, ratio: task_manager.update_task(
                        task_id, message=msg, progress=5 + int(ratio * 90)
                    )
                )

                task_manager.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED,
                    message="Enrichment complete",
                    progress=100,
                    result={
                        "graph_id": graph_id,
                        "source": source,
                        "chunk_count": chunk_count,
                        "episodes_added": len(episode_uuids)
                    }
                )
                enrich_logger.info(f"[{task_id}] Enrichment complete: {len(episode_uuids)} episodes added")

            except Exception as e:
                enrich_logger.error(f"[{task_id}] Enrichment failed: {str(e)}")
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.FAILED,
                    message=f"Enrichment failed: {str(e)}",
                    error=traceback.format_exc()
                )

        thread = threading.Thread(target=enrich_task, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "data": {
                "task_id": task_id,
                "chunk_count": chunk_count
            }
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/enrich-structured', methods=['POST'])
def enrich_structured():
    """
    Enrich a graph with pre-extracted entities and relations.

    Bypasses NER pipeline — caller already did extraction.
    Directly MERGEs entities and relations into Neo4j.

    Request (JSON):
        {
            "graph_id": "mirofish_xxxx",
            "entities": [
                {"name": "Foo", "type": "Person", "summary": "...", "attributes": {}},
                ...
            ],
            "relations": [
                {"source": "Foo", "target": "Bar", "relation": "WORKS_WITH", "fact": "..."},
                ...
            ],
            "source": "external_extraction"
        }

    Returns:
        {
            "success": true,
            "data": {
                "entities_merged": 5,
                "relations_merged": 3
            }
        }
    """
    try:
        data = request.get_json() or {}
        graph_id = data.get('graph_id')
        entities = data.get('entities', [])
        relations = data.get('relations', [])
        source = data.get('source', 'structured')

        if not graph_id:
            return jsonify({"success": False, "error": "Please provide graph_id"}), 400
        if not entities and not relations:
            return jsonify({"success": False, "error": "Please provide entities and/or relations"}), 400

        storage = _get_storage()

        entities_merged = 0
        relations_merged = 0

        def _merge(tx):
            nonlocal entities_merged, relations_merged

            # Merge entities
            for ent in entities:
                name = ent.get('name', '').strip()
                if not name:
                    continue
                entity_type = ent.get('type', 'Entity')
                summary = ent.get('summary', '')
                attributes = ent.get('attributes', {})

                # MERGE node by name + graph_id, set properties
                tx.run(
                    """
                    MERGE (n:Entity {name: $name, graph_id: $graph_id})
                    SET n.summary = CASE WHEN n.summary IS NULL OR n.summary = ''
                                         THEN $summary ELSE n.summary END,
                        n.source = $source,
                        n.updated_at = datetime()
                    WITH n
                    CALL apoc.create.addLabels(n, [$entity_type]) YIELD node
                    RETURN node
                    """,
                    name=name,
                    graph_id=graph_id,
                    summary=summary,
                    source=source,
                    entity_type=entity_type,
                )
                entities_merged += 1

            # Merge relations
            for rel in relations:
                src = rel.get('source', '').strip()
                tgt = rel.get('target', '').strip()
                relation = rel.get('relation', 'RELATED_TO')
                fact = rel.get('fact', '')
                if not src or not tgt:
                    continue

                tx.run(
                    """
                    MATCH (a:Entity {name: $src, graph_id: $graph_id})
                    MATCH (b:Entity {name: $tgt, graph_id: $graph_id})
                    MERGE (a)-[r:RELATES_TO {name: $relation, graph_id: $graph_id}]->(b)
                    SET r.fact = $fact,
                        r.source = $source,
                        r.updated_at = datetime()
                    """,
                    src=src,
                    tgt=tgt,
                    graph_id=graph_id,
                    relation=relation,
                    fact=fact,
                    source=source,
                )
                relations_merged += 1

        # Try direct transaction first; fall back to apoc-free version if apoc is missing
        try:
            with storage._driver.session() as session:
                session.execute_write(_merge)
        except Exception as apoc_err:
            if 'apoc' in str(apoc_err).lower():
                # Retry without apoc.create.addLabels
                entities_merged = 0
                relations_merged = 0

                def _merge_no_apoc(tx):
                    nonlocal entities_merged, relations_merged
                    for ent in entities:
                        name = ent.get('name', '').strip()
                        if not name:
                            continue
                        summary = ent.get('summary', '')
                        tx.run(
                            """
                            MERGE (n:Entity {name: $name, graph_id: $graph_id})
                            SET n.summary = CASE WHEN n.summary IS NULL OR n.summary = ''
                                                 THEN $summary ELSE n.summary END,
                                n.source = $source,
                                n.updated_at = datetime()
                            """,
                            name=name, graph_id=graph_id, summary=summary, source=source,
                        )
                        entities_merged += 1

                    for rel in relations:
                        src = rel.get('source', '').strip()
                        tgt = rel.get('target', '').strip()
                        relation = rel.get('relation', 'RELATED_TO')
                        fact = rel.get('fact', '')
                        if not src or not tgt:
                            continue
                        tx.run(
                            """
                            MATCH (a:Entity {name: $src, graph_id: $graph_id})
                            MATCH (b:Entity {name: $tgt, graph_id: $graph_id})
                            MERGE (a)-[r:RELATES_TO {name: $relation, graph_id: $graph_id}]->(b)
                            SET r.fact = $fact, r.source = $source, r.updated_at = datetime()
                            """,
                            src=src, tgt=tgt, graph_id=graph_id,
                            relation=relation, fact=fact, source=source,
                        )
                        relations_merged += 1

                with storage._driver.session() as session:
                    session.execute_write(_merge_no_apoc)
            else:
                raise

        logger.info(f"Structured enrichment: {entities_merged} entities, {relations_merged} relations merged into {graph_id}")

        return jsonify({
            "success": True,
            "data": {
                "graph_id": graph_id,
                "entities_merged": entities_merged,
                "relations_merged": relations_merged,
                "source": source,
            }
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/cross-search', methods=['POST'])
def cross_search():
    """
    Search across multiple knowledge graphs.

    Embeds the query once, searches each graph, and re-ranks by score.

    Request (JSON):
        {
            "query": "search text",
            "graph_ids": ["mirofish_aaa", "mirofish_bbb"],
            "limit": 20,          // Optional, default 20
            "scope": "edges"      // Optional: "edges", "nodes", "both"
        }

    Returns:
        {
            "success": true,
            "data": {
                "query": "...",
                "results": [...],
                "count": 15
            }
        }
    """
    try:
        data = request.get_json() or {}
        query = data.get('query', '')
        graph_ids = data.get('graph_ids', [])
        limit = data.get('limit', 20)
        scope = data.get('scope', 'edges')

        if not query.strip():
            return jsonify({"success": False, "error": "Please provide a query"}), 400
        if not graph_ids or not isinstance(graph_ids, list):
            return jsonify({"success": False, "error": "Please provide graph_ids as a list"}), 400

        storage = _get_storage()
        results = storage.search_cross_graph(
            query=query,
            graph_ids=graph_ids,
            limit=limit,
            scope=scope,
        )

        return jsonify({
            "success": True,
            "data": {
                "query": query,
                "results": results,
                "count": len(results),
            }
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/llm/discover', methods=['POST'])
def discover_llm_providers():
    """
    Auto-discover local LLM providers and configure optimal routing.

    Scans known ports (Ollama, LM Studio, llama.cpp, etc.),
    ranks models by capability, and assigns them to task types.

    Request (JSON, all optional):
        {
            "dry_run": false,     // If true, don't apply changes
            "reload_router": true // If true, reload the LLM router after applying
        }

    Returns:
        {
            "success": true,
            "data": {
                "providers": [...],
                "assignments": {...},
                "changes": {...}
            }
        }
    """
    try:
        data = request.get_json() or {}
        dry_run = data.get('dry_run', False)
        reload_router = data.get('reload_router', True)

        from ..utils.llm_discovery import discover_and_configure
        result = discover_and_configure(dry_run=dry_run)

        # Reload the router singleton to pick up new env vars
        if not dry_run and reload_router and result.get('changes'):
            from ..utils.llm_router import _router_lock
            import app.utils.llm_router as router_mod
            with _router_lock:
                router_mod._router_instance = None
            # Next call to get_router() will re-init with new env vars
            logger.info("LLM router will reload on next use")

        return jsonify({
            "success": True,
            "data": result,
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/llm/status', methods=['GET'])
def llm_status():
    """
    Get current LLM router status — provider health, chains, and assignments.
    """
    try:
        from ..utils.llm_router import get_router
        router = get_router()
        # Include perf tracker data
        perf_data = {}
        try:
            from ..utils.llm_perf_tracker import get_tracker
            tracker = get_tracker()
            perf_data = {
                "summary": tracker.get_summary(),
                "bottlenecks": tracker.get_bottlenecks(),
                "recommendations": tracker.get_recommendations(),
            }
        except Exception:
            pass

        return jsonify({
            "success": True,
            "data": {
                "chains": router.get_chains(),
                "health": router.get_status(),
                "performance": perf_data,
            }
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


@graph_bp.route('/delete/<graph_id>', methods=['DELETE'])
def delete_graph(graph_id: str):
    """
    Delete a graph
    """
    try:
        storage = _get_storage()
        builder = GraphBuilderService(storage=storage)
        builder.delete_graph(graph_id)

        return jsonify({
            "success": True,
            "message": f"Graph deleted: {graph_id}"
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500
