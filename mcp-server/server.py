# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0.0", "httpx>=0.27.0"]
# ///
"""MiroFish MCP Server — thin proxy over the MiroFish-Offline HTTP API.

Follows official MCP SDK best practices:
- Lifespan context for shared httpx.AsyncClient (connection pooling, proper cleanup)
- Task-oriented tools (run_pipeline) over raw CRUD
- Errors returned as descriptive strings, not raised
- Compact responses to stay within LLM context limits
"""

import os
import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("MIROFISH_BASE_URL", "http://localhost:5001")
CONNECT_HINT = (
    f"Cannot connect to MiroFish at {BASE_URL}. "
    "Start it: cd backend && uv run python run.py"
)


@dataclass
class AppContext:
    client: httpx.AsyncClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Single httpx client shared across all tool calls."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(15.0, connect=2.0),
    ) as client:
        yield AppContext(client=client)


mcp = FastMCP("mirofish", lifespan=app_lifespan)


def _client() -> httpx.AsyncClient:
    """Get the shared HTTP client from lifespan context."""
    return mcp.get_context().request_context.lifespan_context.client


async def _get(path: str, **kwargs) -> dict:
    try:
        r = await _client().get(path, **kwargs)
        return r.json()
    except httpx.ConnectError:
        return {"error": CONNECT_HINT}
    except Exception as e:
        return {"error": str(e)}


async def _post(path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    try:
        r = await _client().post(path, json=body, timeout=timeout)
        return r.json()
    except httpx.ConnectError:
        return {"error": CONNECT_HINT}
    except Exception as e:
        return {"error": str(e)}


# ── Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
async def mirofish_health() -> dict:
    """Check if the MiroFish backend is running."""
    return await _get("/health")


@mcp.tool()
async def mirofish_list_projects(limit: int = 50) -> dict:
    """List all MiroFish projects. Ontology details are summarized for compactness."""
    resp = await _get("/api/graph/project/list", params={"limit": limit})
    try:
        for proj in resp.get("data", []):
            ont = proj.get("ontology", {})
            if ont:
                proj["ontology"] = {
                    "entity_types": [e.get("name") for e in ont.get("entity_types", [])],
                    "edge_types": [e.get("name") for e in ont.get("edge_types", [])],
                }
    except Exception:
        pass  # return raw resp if trimming fails
    return resp


@mcp.tool()
async def mirofish_create_project(
    simulation_requirement: str,
    file_paths: list[str] | None = None,
    project_name: str | None = None,
    additional_context: str | None = None,
) -> dict:
    """Upload documents and generate an ontology, creating a new project.

    Args:
        simulation_requirement: What to simulate (e.g. "AR developer community discourse").
        file_paths: Local paths (PDF/MD/TXT) to analyze. Optional.
        project_name: Project name. Optional.
        additional_context: Extra notes for the LLM. Optional.
    """
    try:
        client = _client()
        data = {"simulation_requirement": simulation_requirement}
        if project_name:
            data["project_name"] = project_name
        if additional_context:
            data["additional_context"] = additional_context

        files = []
        handles = []
        if file_paths:
            for fp in file_paths:
                fh = open(fp, "rb")
                handles.append(fh)
                files.append(("files", (os.path.basename(fp), fh)))
        try:
            r = await client.post(
                "/api/graph/ontology/generate",
                data=data,
                files=files or None,
                timeout=120.0,
            )
            return r.json()
        finally:
            for fh in handles:
                fh.close()
    except httpx.ConnectError:
        return {"error": CONNECT_HINT}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def mirofish_build_graph(
    project_id: str,
    graph_name: str | None = None,
) -> dict:
    """Build a knowledge graph for a project. Returns task_id — poll with mirofish_task_status.

    Args:
        project_id: Project ID from mirofish_create_project or mirofish_list_projects.
        graph_name: Optional graph name.
    """
    body: dict = {"project_id": project_id}
    if graph_name:
        body["graph_name"] = graph_name
    return await _post("/api/graph/build", body, timeout=60.0)


@mcp.tool()
async def mirofish_run_pipeline(
    project_id: str,
    platform: str = "parallel",
    max_rounds: int | None = None,
    prepare_timeout: int = 600,
) -> dict:
    """Create, prepare, and start a simulation in one call.

    Combines create -> prepare (polls until ready) -> start.

    Args:
        project_id: Project ID (must have a built graph).
        platform: "twitter", "reddit", or "parallel" (default).
        max_rounds: Cap on simulation rounds. Optional.
        prepare_timeout: Max seconds to wait for preparation (default 600).
    """
    try:
        # 1. Create
        resp = await _post("/api/simulation/create", {"project_id": project_id})
        if not resp.get("success"):
            return {"step": "create", "result": resp}
        sim_id = resp.get("data", {}).get("simulation_id")
        if not sim_id:
            return {"error": "create succeeded but no simulation_id in response", "raw": resp}

        # 2. Prepare
        resp = await _post("/api/simulation/prepare", {"simulation_id": sim_id}, timeout=60.0)
        if not resp.get("success"):
            return {"step": "prepare", "simulation_id": sim_id, "result": resp}

        task_id = resp.get("data", {}).get("task_id")
        if task_id and not resp.get("data", {}).get("already_prepared"):
            elapsed = 0
            while elapsed < prepare_timeout:
                await asyncio.sleep(5)
                elapsed += 5
                tr = await _get(f"/api/graph/task/{task_id}")
                status = tr.get("data", {}).get("status", "")
                if status in ("completed", "success"):
                    break
                if status in ("failed", "error"):
                    return {"step": "prepare_poll", "simulation_id": sim_id, "result": tr}
            else:
                return {"step": "prepare_poll", "simulation_id": sim_id, "error": f"Timed out after {prepare_timeout}s"}

        # 3. Start
        start_body: dict = {"simulation_id": sim_id, "platform": platform}
        if max_rounds is not None:
            start_body["max_rounds"] = max_rounds
        resp = await _post("/api/simulation/start", start_body, timeout=60.0)
        if not resp.get("success"):
            return {"step": "start", "simulation_id": sim_id, "result": resp}

        return {"success": True, "simulation_id": sim_id, "status": "running", "data": resp.get("data")}
    except Exception as e:
        return {"error": f"run_pipeline failed: {e}"}


@mcp.tool()
async def mirofish_list_simulations(project_id: str | None = None) -> dict:
    """List simulations, optionally filtered by project.

    Args:
        project_id: Filter to a specific project. Optional.
    """
    params = {}
    if project_id:
        params["project_id"] = project_id
    return await _get("/api/simulation/list", params=params)


@mcp.tool()
async def mirofish_simulation_status(simulation_id: str) -> dict:
    """Get live status of a simulation (round, progress, action counts).

    Args:
        simulation_id: The simulation to check.
    """
    return await _get(f"/api/simulation/{simulation_id}/run-status")


@mcp.tool()
async def mirofish_get_posts(
    simulation_id: str,
    platform: str = "reddit",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Read posts generated by a simulation.

    Args:
        simulation_id: The simulation to read from.
        platform: "twitter" or "reddit" (default "reddit").
        limit: Max posts to return (default 50).
        offset: Pagination offset (default 0).
    """
    return await _get(
        f"/api/simulation/{simulation_id}/posts",
        params={"platform": platform, "limit": limit, "offset": offset},
    )


@mcp.tool()
async def mirofish_task_status(task_id: str) -> dict:
    """Poll status of any async task (graph build, simulation prepare, etc.).

    Args:
        task_id: Task ID returned by an async operation.
    """
    return await _get(f"/api/graph/task/{task_id}")


@mcp.tool()
async def mirofish_discover_providers(dry_run: bool = False) -> dict:
    """Auto-discover local LLM providers and configure optimal model routing.

    Scans Ollama, LM Studio, llama.cpp, vLLM, GPT4All, Jan on known ports.
    Ranks models and assigns them to task types (quality vs throughput).
    Reloads the LLM router with new assignments.

    Args:
        dry_run: If True, show what would change without applying.
    """
    return await _post(
        "/api/graph/llm/discover",
        {"dry_run": dry_run, "reload_router": True},
        timeout=15.0,
    )


@mcp.tool()
async def mirofish_llm_status() -> dict:
    """Get current LLM router status — provider chains, health, and model assignments."""
    return await _get("/api/graph/llm/status")


@mcp.tool()
async def mirofish_enrich_graph(
    graph_id: str,
    text: str,
    source: str = "manual",
) -> dict:
    """Feed new context into an existing knowledge graph.

    Chunks the text and runs NER pipeline to add entities/relations.
    Returns a task_id — poll with mirofish_task_status.

    Args:
        graph_id: Graph to enrich (from project's graph_id).
        text: Natural language paragraphs to process through NER.
        source: Audit trail label (e.g. "portals_v4_commits", "dev_input", "user_feedback").
    """
    return await _post(
        "/api/graph/enrich",
        {"graph_id": graph_id, "text": text, "source": source},
        timeout=60.0,
    )


@mcp.tool()
async def mirofish_feedback_loop(
    simulation_id: str,
    platform: str = "parallel",
    max_rounds: int = 10,
    max_iterations: int = 2,
    skip_first_run: bool = False,
) -> dict:
    """Run auto-improvement feedback loop: simulate -> extract insights -> enrich graph -> repeat.

    Returns a task_id — poll with mirofish_task_status.

    Args:
        simulation_id: Simulation to run the feedback loop on.
        platform: "twitter", "reddit", or "parallel" (default).
        max_rounds: Max rounds per simulation run.
        max_iterations: Number of simulate->enrich cycles (default 2, max 5).
        skip_first_run: If True, skip initial simulation (use existing results).
    """
    return await _post(
        "/api/simulation/feedback-loop",
        {
            "simulation_id": simulation_id,
            "platform": platform,
            "max_rounds": max_rounds,
            "max_iterations": max_iterations,
            "skip_first_run": skip_first_run,
        },
        timeout=60.0,
    )


@mcp.tool()
async def mirofish_generate_report(simulation_id: str) -> dict:
    """Generate a post-simulation analysis report.

    Returns task_id — poll with mirofish_task_status.

    Args:
        simulation_id: The simulation to analyze.
    """
    return await _post(
        "/api/report/generate",
        {"simulation_id": simulation_id},
        timeout=60.0,
    )


@mcp.tool()
async def mirofish_get_report(simulation_id: str) -> dict:
    """Get the generated report for a simulation.

    Args:
        simulation_id: The simulation whose report to retrieve.
    """
    resp = await _get(f"/api/report/by-simulation/{simulation_id}")
    # Trim large markdown to stay within context limits
    try:
        data = resp.get("data", {})
        md = data.get("markdown_content", "")
        if len(md) > 3000:
            data["markdown_content"] = md[:3000] + "\n\n... [truncated, use download endpoint for full report]"
    except Exception:
        pass
    return resp


@mcp.tool()
async def mirofish_enrich_structured(
    graph_id: str,
    entities: list[dict] | None = None,
    relations: list[dict] | None = None,
    source: str = "mcp",
) -> dict:
    """Inject pre-extracted entities and relations directly into a graph.

    Bypasses NER — use when caller already did entity extraction.
    Each entity needs at minimum a 'name' field. Relations need 'source' and 'target'.

    Args:
        graph_id: Graph to enrich.
        entities: List of {"name": "...", "type": "...", "summary": "..."}.
        relations: List of {"source": "...", "target": "...", "relation": "...", "fact": "..."}.
        source: Audit trail label.
    """
    body: dict = {"graph_id": graph_id, "source": source}
    if entities:
        body["entities"] = entities
    if relations:
        body["relations"] = relations
    return await _post("/api/graph/enrich-structured", body, timeout=30.0)


@mcp.tool()
async def mirofish_cross_search(
    query: str,
    graph_ids: list[str],
    limit: int = 20,
    scope: str = "edges",
) -> dict:
    """Search across multiple knowledge graphs at once.

    Embeds the query once, searches each graph, and re-ranks by score.

    Args:
        query: Natural language search query.
        graph_ids: List of graph IDs to search across.
        limit: Max results (default 20).
        scope: "edges", "nodes", or "both".
    """
    return await _post(
        "/api/graph/cross-search",
        {"query": query, "graph_ids": graph_ids, "limit": limit, "scope": scope},
        timeout=30.0,
    )


@mcp.tool()
async def mirofish_graph_search(
    graph_id: str,
    query: str,
    limit: int = 10,
) -> dict:
    """Search entities and relations in a knowledge graph.

    Useful for verifying enrichment worked or exploring graph content.

    Args:
        graph_id: The graph to search.
        query: Natural language search query.
        limit: Max results to return (default 10).
    """
    return await _post(
        "/api/report/tools/search",
        {"graph_id": graph_id, "query": query, "limit": limit},
        timeout=15.0,
    )


if __name__ == "__main__":
    mcp.run()
