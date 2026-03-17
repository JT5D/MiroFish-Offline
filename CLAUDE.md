# MiroFish-Offline

Local-first social media simulation platform. Neo4j + Ollama + Flask + Vue.

## Quick Reference

- Backend: `http://localhost:5001` (Flask, port 5001)
- Frontend: `http://localhost:3000` (Vite/Vue)
- Neo4j: `bolt://localhost:7687` (Docker, user: neo4j, pass: mirofish)
- Ollama: `http://localhost:11434` (models: qwen2.5:14b, nomic-embed-text)
- Fork: `JT5D/MiroFish-Offline` (origin), upstream: `nikmcfly/MiroFish-Offline`

## Commands

- `/start` — Start all services (Ollama, Neo4j, backend, frontend)
- `/status` — Health check all services
- `/sim` — Create and run a simulation
- `/sim-update` — Enrich graph from external signals, re-simulate (closed-loop feedback)
- `/stop` — Gracefully stop all services
- `/wrap-up` — End-of-session: update knowledgebase, commit, push

## Project Structure

```
backend/
  app/
    api/          # Flask blueprints (graph, simulation, report)
    models/       # Task manager, project models
    services/     # Core logic (profile gen, sim runner, report agent, graph tools)
    storage/      # Neo4j storage abstraction
    utils/        # LLM client, retry, file parser, logger
  scripts/        # Standalone simulation runners (twitter, reddit, parallel)
frontend/
  src/components/ # Vue step-wizard: Step1-5 + GraphPanel + HistoryDatabase
tools/            # Extracted reusable toolkit (see KNOWLEDGEBASE.md)
```

## Key Conventions

- All backend code uses English comments and docstrings
- LLM calls go through `utils/llm_client.py` (OpenAI-compatible, Ollama-aware)
- Long-running ops return task_id immediately, polled via status endpoint
- Config from `.env` → `app/config.py` → `Config` class
- Neo4j accessed via singleton `app.extensions['neo4j_storage']`

## Resource Constraints

- Keep Neo4j heap at 512MB (not 2GB) to avoid starving other apps
- Profile generation parallelism: 2 workers (not 5) to limit VRAM pressure
- Set `OLLAMA_NUM_CTX=8192` to prevent silent prompt truncation (Ollama default is 2048)

## API & Context Discipline

- **Max 3 retries**, short waits (0-1s). Never loop 10 times on failures.
- **MCP tool responses < 5KB.** Trim ontologies, action logs, post lists before returning.
- **Start simple.** Minimal working version first. Add complexity only when needed.
- **On 529/overloaded/500 from Anthropic: skip and continue.** Retry once after 1s. If it fails again, skip entirely and move on. Never block progress on upstream API instability.
- **Anthropic errors are not local bugs.** Errors with `request_id: "req_..."` are server-side. Don't debug local code for them. Skip and continue.
- **MCP server**: `mcp-server/server.py` — 13 tools, lifespan-managed httpx client. Add tools sparingly.

## Key Files

- `KNOWLEDGEBASE.md` — Architecture, patterns, lessons learned
- `.env` — All configuration (LLM, Neo4j, embedding endpoints)
- `tools/` — Reusable toolkit: 12 standalone modules
