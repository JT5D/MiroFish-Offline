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
        timeout=httpx.Timeout(30.0, connect=5.0),
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
    for proj in resp.get("data", []):
        ont = proj.get("ontology", {})
        if ont:
            proj["ontology"] = {
                "entity_types": [e.get("name") for e in ont.get("entity_types", [])],
                "edge_types": [e.get("name") for e in ont.get("edge_types", [])],
            }
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
    # 1. Create
    resp = await _post("/api/simulation/create", {"project_id": project_id})
    if not resp.get("success"):
        return {"step": "create", "result": resp}
    sim_id = resp["data"]["simulation_id"]

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


if __name__ == "__main__":
    mcp.run()
