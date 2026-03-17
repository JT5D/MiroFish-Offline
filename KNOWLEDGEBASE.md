# MiroFish-Offline Knowledgebase

Key architectural insights, patterns, and operational knowledge extracted from the codebase.

---

## Architecture Overview

MiroFish-Offline is a local-first social media simulation platform built on:
- **Backend**: Flask (Python) with OpenAI-compatible LLM calls via Ollama
- **Frontend**: Vue.js (Vite) on port 3000
- **Graph DB**: Neo4j 5.15 (community edition) for knowledge graph storage
- **LLM**: Ollama serving local models (qwen2.5:14b for generation, nomic-embed-text for embeddings)
- **Simulation Engine**: CAMEL-AI / OASIS framework for multi-agent social simulation

## Core Pipeline

```
Upload Documents → Text Extraction → Ontology Generation (LLM) →
Knowledge Graph Building (Neo4j) → Entity Filtering →
Profile Generation (LLM per entity) → Simulation Config (LLM) →
Run Simulation (OASIS/CAMEL subprocess) → Report Generation (LLM)
```

## Key Patterns

### 1. LLM-Backed Agents
All LLM-powered services follow the same pattern:
- Build system prompt with domain-specific instructions
- Inject user context (graph entities, documents, requirements)
- Call LLM via `LLMClient.chat_json()` for structured output
- Parse JSON with fallback repair for truncated responses
- Fall back to rule-based generation if LLM fails entirely

**Files**: `services/ontology_generator.py`, `services/oasis_profile_generator.py`, `services/simulation_config_generator.py`, `services/report_agent.py`

### 2. Async Task Lifecycle
Long-running operations use a singleton `TaskManager`:
```
create_task() → PENDING → PROCESSING (with progress updates) → COMPLETED/FAILED
```
- Thread-safe with `threading.Lock`
- Progress tracking with percentage + message + detail dict
- API endpoints poll task status via task_id

**Files**: `models/task.py`, `api/graph.py` (task polling endpoints)

### 3. Batch LLM Generation
When generating content for many entities (65+ profiles):
- Split into batches to respect token limits
- Use `ThreadPoolExecutor` for parallel LLM calls
- Per-item error isolation (one failure doesn't block others)
- Real-time progress callbacks for UI updates
- File-based incremental output (write profiles as they complete)

**Files**: `services/oasis_profile_generator.py:795-954`

### 4. File-Based IPC
Flask backend communicates with OASIS simulation subprocess via filesystem:
- `ipc_commands/` dir: Flask writes command JSON files
- `ipc_responses/` dir: Subprocess writes response JSON files
- UUID-based command correlation
- Polling with timeout + cleanup

**Files**: `services/simulation_ipc.py`

### 5. Simulation Lifecycle
```
POST /api/simulation/create   → creates record in Neo4j
POST /api/simulation/prepare  → async: generates profiles + config
POST /api/simulation/start    → spawns subprocess (run_parallel_simulation.py)
GET  /api/simulation/<id>/run-status → polls action_log.json for progress
POST /api/simulation/stop     → kills subprocess
```

### 6. Neo4j Storage Pattern
`Neo4jStorage` is a singleton injected via `app.extensions['neo4j_storage']`.
All services access it through Flask's app context. Graph operations use Cypher queries with APOC plugin for batch operations.

**Note**: Vector indexes on relationships are NOT supported in Neo4j Community 5.15 — the startup warning about `RELATION.fact_embedding` is harmless.

## Operational Notes

### Resource Management
- **Ollama qwen2.5:14b**: ~9GB VRAM/RAM. Profile generation: ~20s per entity.
- **Neo4j**: Docker container. Set heap to 512MB (not 2GB) to keep system responsive.
- **Simulation parallelism**: `parallel_profile_count=2` is safe for most machines. Higher values increase VRAM pressure.
- **OLLAMA_NUM_CTX**: Defaults to 8192. Ollama's own default (2048) causes silent prompt truncation. The LLM client injects this via `extra_body.options.num_ctx`.

### Model Quirks
- Some models emit `<think>` reasoning blocks — `LLMClient` strips these automatically.
- JSON mode output sometimes includes markdown fences — `chat_json()` strips these.
- Ollama's OpenAI-compatible API uses port 11434 and the `_is_ollama()` check is port-based.

### Configuration
All config flows from `.env` at project root → `backend/app/config.py` → `Config` class.
Key env vars:
```
LLM_API_KEY=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL_NAME=qwen2.5:14b
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=mirofish
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_BASE_URL=http://localhost:11434
```

### Git Setup
- **origin**: `JT5D/MiroFish-Offline` (your fork)
- **upstream**: `nikmcfly/MiroFish-Offline` (original)

## Extracted Reusable Tools

Located in `tools/` directory — each module is standalone and can be copied to other projects:

| Module | Purpose | Dependencies |
|--------|---------|-------------|
| `retry.py` | Exponential backoff decorators + batch retry | stdlib only |
| `task_manager.py` | Thread-safe async task tracking | stdlib only |
| `ipc.py` | File-based inter-process communication | stdlib only |
| `llm_client.py` | OpenAI-compatible LLM wrapper | `openai` |
| `file_parser.py` | Text extraction + chunking | `PyMuPDF`, `charset-normalizer`, `chardet` |
| `logger.py` | Dual-output rotating log setup | stdlib only |

## Lessons Learned

1. **Ollama num_ctx is critical** — without explicitly setting it, prompts get silently truncated at 2048 tokens. Always pass `num_ctx` via extra_body.
2. **Batch + fallback is the right pattern for LLM generation** — individual failures are expected; rule-based fallbacks ensure the pipeline completes.
3. **File-based IPC is surprisingly robust** for local multi-process apps — simpler than sockets, no dependency on Redis/RabbitMQ.
4. **Profile generation dominates wall-clock time** — 65 entities × 20s/each = ~20min. Parallelism helps but is bounded by VRAM.
5. **JSON mode + fence stripping is necessary** — multiple model families add markdown fences around JSON even in JSON mode.
