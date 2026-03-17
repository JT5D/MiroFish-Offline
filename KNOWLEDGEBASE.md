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

### Infrastructure (stdlib-only unless noted)

| Module | Purpose | Dependencies |
|--------|---------|-------------|
| `retry.py` | Exponential backoff decorators + batch retry | stdlib only |
| `task_manager.py` | Thread-safe async task tracking | stdlib only |
| `ipc.py` | File-based inter-process communication | stdlib only |
| `llm_client.py` | OpenAI-compatible LLM wrapper | `openai` |
| `file_parser.py` | Text extraction + chunking | `PyMuPDF`, `charset-normalizer`, `chardet` |
| `logger.py` | Dual-output rotating log setup | stdlib only |

### Agent Patterns (higher-level, composable)

| Module | Purpose | Dependencies |
|--------|---------|-------------|
| `json_repair.py` | Multi-stage JSON repair for LLM output | stdlib only |
| `llm_agent.py` | Base class + stepwise orchestrator for LLM agents | `llm_client`, `json_repair` |
| `batch_processor.py` | Parallel batch processing with per-item isolation | stdlib only |

### Process Management (cross-platform)

| Module | Purpose | Dependencies |
|--------|---------|-------------|
| `subprocess_manager.py` | Cross-platform process spawning, monitoring, and cleanup | stdlib only |
| `streaming_log_reader.py` | Incremental JSONL log reader with event dispatch | stdlib only |
| `react_agent.py` | ReACT loop with tool calling (Thought → Tool → Observation → Answer) | `llm_client`, `json_repair` |

### How They Compose

```
llm_client.py ──→ llm_agent.py (LLMAgent base class)
                      │
json_repair.py ──────┘ (used internally for JSON parsing)
                      │
batch_processor.py ──→ (runs many LLMAgent.run() calls in parallel)
                      │
task_manager.py ─────→ (tracks overall progress of batch operations)
                      │
retry.py ────────────→ (wraps external API calls with backoff)

subprocess_manager.py ──→ (spawns long-running processes)
                            │
streaming_log_reader.py ───→ (reads JSONL output from subprocess)
                            │
react_agent.py ──→ llm_client.py (LLM calls for reasoning)
                      │
                   json_repair.py (parse tool call JSON)
```

### Usage Example: Building a New LLM Agent

```python
from tools.llm_client import LLMClient
from tools.llm_agent import LLMAgent
from tools.batch_processor import BatchProcessor

class SummaryAgent(LLMAgent):
    def build_system_prompt(self, ctx):
        return "You are a text summarization expert. Return JSON."

    def build_user_prompt(self, ctx):
        return f"Summarize this text in 2 sentences:\n\n{ctx['text']}"

    def fallback(self, ctx, error):
        return {"summary": ctx["text"][:200] + "..."}

client = LLMClient()
agent = SummaryAgent(client, required_fields=["summary"])

# Single item:
result = agent.run({"text": "Long document here..."})

# Batch processing:
processor = BatchProcessor(
    worker_fn=lambda item: agent.run(item),
    parallel_count=3,
    progress_callback=lambda cur, total, msg: print(f"{cur}/{total}"),
)
results, failures = processor.run([{"text": t} for t in documents])
```

## Deep Patterns: How the Codebase Uses LLMs

### Pattern: Decreasing Temperature on Retry
Every LLM agent in the codebase starts at temperature 0.7 and decreases by 0.1 on each retry attempt. Lower temperature = more deterministic = more likely to produce valid JSON. This is implemented in `LLMAgent.run()`.

### Pattern: Stepwise Generation to Avoid Token Overflow
`SimulationConfigGenerator` splits one large generation task into 4+ steps:
1. Time config (small JSON, ~500 tokens)
2. Event config (medium JSON, ~1000 tokens)
3. Agent configs (batched, 15 per call, ~2000 tokens each)
4. Platform config (rule-based, no LLM needed)

Each step uses a focused prompt with truncated context, avoiding the failure mode of asking for too much in a single call. This pattern is generalized in `StepwiseAgent`.

### Pattern: Individual vs. Group Entity Handling
`OasisProfileGenerator` distinguishes between individual entities (Person, Student, Professor) and group entities (Company, University, GovernmentAgency). Each type gets a different prompt template. Group entities get fixed age=30, gender="other". This two-template pattern recurs whenever entity types have fundamentally different structures.

### Pattern: Context Aggregation with Priority Truncation
When building LLM prompts, context is assembled from multiple sources:
1. Entity attributes (always included)
2. Graph edges/relationships (always included)
3. Related node summaries (always included)
4. Hybrid graph search results (deduplicated against #2)
5. Original document text (truncated to fit remaining budget)

The truncation order ensures the most specific context (direct relationships) is never lost, while bulk text (documents) gets cut first. Context budgets are configurable per-step.

### Pattern: Type Alias Mapping for Fuzzy Entity Matching
When LLM-generated configs reference entity types, they may use synonyms or different casing. `_assign_initial_post_agents()` maintains a type alias mapping:
```python
{"official": ["official", "university", "governmentagency"],
 "student": ["student", "person"], ...}
```
This prevents brittle exact-match failures. Fallback: pick the agent with the highest influence weight.

## Claude Code Project Commands

Located in `.claude/commands/` (gitignored, local-only). These are slash commands usable in Claude Code sessions:

| Command | Purpose |
|---------|---------|
| `/start` | Start all MiroFish services (Ollama, Docker/Neo4j, backend, frontend) |
| `/sim` | Create, prepare, and run a simulation from an existing project |
| `/status` | Quick health check of all services |
| `/stop` | Gracefully shut down all services |
| `/wrap-up` | End-of-session routine: review changes, update knowledgebase, commit, push |
| `/sim-update` | Enrich graph from external signals (code changes, dev input, user feedback), optionally re-simulate |

`CLAUDE.md` at project root provides Claude Code with project context (ports, URLs, conventions, resource constraints).

## Deep Patterns: Process Management

### Pattern: Cross-Platform Process Groups
`SubprocessManager` spawns subprocesses in their own process group (Unix: `start_new_session=True`, Windows: `CREATE_NEW_PROCESS_GROUP`). This ensures `kill_process()` can terminate the entire process tree, not just the parent. The manager registers an `atexit` handler and signal handlers (SIGTERM, SIGINT) to clean up all spawned processes on exit.

### Pattern: Graceful Termination with Escalation
When stopping a subprocess, the manager sends SIGTERM first and waits a configurable timeout. If the process doesn't exit, it escalates to SIGKILL. This mirrors how the simulation runner terminates OASIS subprocesses.

### Pattern: Byte-Position Log Tracking
`StreamingLogReader` tracks the file read position in bytes (not lines), enabling efficient incremental reads of growing log files. Each `poll()` call reads only new bytes since the last read, parses complete JSON lines, and dispatches events by type. Incomplete lines (no trailing newline) are buffered for the next poll.

---

## Higher-Level Learnings

Strategic and conceptual insights that transfer beyond this codebase.

### Document-to-Knowledge Pipelines

**Generate ontologies from source material, don't predefine them.** MiroFish uses an LLM to read the uploaded documents and design the entity/relationship schema on the fly. This ensures the graph structure matches the actual domain rather than forcing content into a generic model. The constraint: cap entity types at ~10 (8 domain + 2 fallbacks: Person, Organization) to prevent schema explosion while keeping flexibility.

**Fallback types are essential.** Real documents always contain outlier entities (unnamed groups, passing references, anonymous actors). Rather than discarding them or forcing them into domain types, two generic fallback types (Person, Organization) catch anything the domain types miss. This pattern applies to any pipeline that classifies input into categories.

**Chunks are a pipeline stage, not an optimization.** Splitting text into overlapping chunks (500 chars, 50 overlap) before NER isn't about fitting token limits — it's about creating manageable extraction units while preserving cross-boundary relationships via overlap.

**Store ontology as metadata, not compiled code.** Rather than generating Pydantic classes from the ontology (the original design), MiroFish stores it as JSON in Neo4j. This eliminates code generation, supports schema evolution without restarts, and separates "what types exist" from "how data is stored."

### Graph Database Design (Neo4j)

**Use node labels as the ontology enforcement layer.** Entity types become Neo4j labels directly. `get_nodes_by_label(graph_id, "Student")` is the primary filtering pattern. This makes the graph self-documenting — querying for labels reveals what types exist without consulting a separate schema.

**Encode temporal validity on edges.** Every relationship carries `valid_at`, `invalid_at`, and `expired_at` timestamps. This creates a temporal knowledge graph where facts can expire naturally (a temporary alliance) or be invalidated (a court ruling). Query tools distinguish "active facts" from "historical facts," enabling both current-state queries and evolution narratives.

**Hybrid search (vector + BM25) with tiered depth.** Three search strategies serve different needs:
1. **QuickSearch** — single vector+BM25 query, fast, for simple factual retrieval
2. **PanoramaSearch** — exhaustive, includes expired/historical edges, for full-picture analysis
3. **InsightForge** — LLM decomposes the question into sub-queries, executes each, aggregates results. Slower but handles complex analytical questions that a single search can't answer.

The key insight: **use the LLM as a question decomposer, not a question answerer.** Don't ask the LLM to reason over a large knowledge base. Ask it to break the question into searchable sub-questions, search for each, then integrate.

**Graphs beat relational when relationships are the data.** Social networks, influence flows, entity-to-entity interactions — these are naturally graph-shaped. The ability to traverse 2-3 hops (entity → relationship → related entity → their relationships) in a single Cypher query would require multiple JOINs in SQL.

### Multi-Agent Simulation Design

**Entities ≠ Agents.** A graph entity (e.g., "Professor Chen") is raw data. An Agent is a behavioral projection of that entity: same identity, but with added personality parameters (MBTI, activity level, sentiment bias) and temporal patterns (active hours, response delay). The separation matters because one entity can theoretically produce different agents under different simulation configurations.

**Dual persona types for realism.** Individual entities (students, professors) get generated age, gender, MBTI, personal voice, catchphrases, and social media behavior patterns. Group entities (universities, government agencies) get fixed demographics (age=30, gender="other") but MBTI becomes an institutional voice descriptor (e.g., ISTJ = rigorous, conservative). This duality ensures both humans and organizations behave authentically.

**Parameterize behavior along independent axes:**
- **Temporal**: active_hours (when they appear), response_delay (how fast they react)
- **Frequency**: posts_per_hour, comments_per_hour, scaled by activity_level
- **Sentiment**: sentiment_bias (−1.0 to +1.0, pessimistic to optimistic lean)
- **Stance**: supportive / opposing / neutral / observer
- **Influence**: influence_weight (reach probability)

These parameters are generated by LLM based on entity type and scenario context, not hardcoded. The system provides baseline ranges per type (officials: 0.1–0.3 activity, media: 0.4–0.6, individuals: 0.6–0.9), and the LLM fills in specifics.

**Time configuration encodes real-world patterns.** Hour-by-hour activity multipliers (dead hours 0-5AM: 0.05×, evening peak 19-22: 1.5×) are built-in domain knowledge that makes simulations feel realistic without agents needing to "learn" daily patterns.

**Platform-specific behavior tuning.** The same agent behaves differently on Twitter vs Reddit:
- Twitter: higher recency weight (0.4), lower viral threshold (10 interactions)
- Reddit: higher popularity weight (0.4), stronger echo chamber (0.6)

This captures the structural differences between platforms without requiring separate agent models.

**Seed simulations with typed initial posts.** Generated "initial posts" each specify a poster_type (official, media, student), which gets matched to an appropriate agent. Officials publish official statements, media publish news reports, students publish opinions. Type alias mapping handles LLM-generated synonyms ("media" → "mediaoutlet"). Fallback: highest-influence agent.

### LLM Orchestration at Scale

**Asymmetric confidence in LLM outputs.** Trust LLMs for creative, open-ended tasks (persona generation, report writing, question decomposition). Be skeptical for deterministic tasks (config generation, ontology creation). Example: a persona "bio" field has no validation; a config "agents_per_hour_max" field is validated against entity count.

**Three-layer fallback for every LLM call:**
1. LLM call with structured JSON response
2. JSON repair (fix truncation, extract partial data)
3. Rule-based generation (hardcoded sensible defaults per entity type)

No LLM call in the system is a single point of failure.

**Temperature separates creative from deterministic tasks.** Creative tasks (persona generation) use temperature 0.7. Deterministic tasks (ontology, config, report) use 0.3. Within a single agent's retry loop, temperature decreases per attempt (0.7 → 0.6 → 0.5) to progressively favor validity over creativity.

**Truncate context upstream, don't hope the LLM ignores it.** Each pipeline step has an explicit context budget (ontology: 50k chars, time config: 10k, events: 8k, entity summaries: 300 each). Context is assembled in priority order (direct relationships first, then search results, then bulk text) and truncated at the budget. The most specific context is never lost; bulk text gets cut first.

**Batch heterogeneous items to amortize context overhead.** With 100+ entities, generating configs one-by-one wastes tokens repeating the scenario context. Instead, batch 15 entities per LLM call. Each call includes the full simulation context but only its batch of entities. This is 6-7× more token-efficient than individual calls.

### Report Generation (ReACT Agents in Practice)

**Two-phase report generation: plan then execute.** Phase 1: LLM generates a report outline (sections, questions each section should answer). Phase 2: for each section, enter a ReACT loop — thought → tool selection → tool call → observation → next thought → answer.

**Tool selection by complexity.** The report agent has access to QuickSearch, PanoramaSearch, and InsightForge. It self-selects based on question complexity. Simple factual queries use QuickSearch. Evolution/trend questions use PanoramaSearch. Root-cause analysis uses InsightForge. The agent learns which tool fits by including tool descriptions in its system prompt.

**Agent interviews as simulation introspection.** The report agent can call `interview_agents()` to ask simulated agents questions directly. Agents respond based on their persona and action history. This creates multi-perspective reports where the same event is narrated by agents with different viewpoints. It's a form of qualitative data collection from the simulation.

**Facts as citation units.** The report system distinguishes graph-sourced facts (marked for verbatim citation) from LLM-generated reasoning. This creates an implicit citation protocol where readers can trace claims to their data source.

**Full audit trail via JSONL logging.** Every ReACT iteration — tool calls, tool results, reasoning steps, section completions — is logged to a JSONL file. The report generation process is fully reproducible and auditable.

### Simulation as Knowledge Refinement

**The closed-loop feedback cycle:**
```
Documents → Graph → Agent Profiles → Simulation → Graph Updates → Richer Reports
                                         ↑                            │
                                         └────────────────────────────┘
```

Simulation actions (posts, likes, follows) are converted to natural language and fed back into the NER pipeline via GraphMemoryUpdater. The graph becomes richer and more detailed as simulations run. Subsequent reports draw on simulation-enriched knowledge, not just the original documents.

**External enrichment via `/api/graph/enrich`:**
Beyond in-simulation graph updates, the graph can be enriched from external sources between simulation runs. The `POST /api/graph/enrich` endpoint accepts prose text + graph_id, chunks it, and runs it through the existing NER pipeline (`storage.add_text()`). This enables a multi-source feedback loop:

- **Code changes** (portals_v4 git diffs → summarized as domain prose)
- **Developer input** (corrections, design decisions, stakeholder updates)
- **User feedback** (persona adjustments, missing behaviors, quality observations)
- **KB learnings** (architectural patterns, cross-project connections)

Claude Code serves as the orchestrator — it reads raw signals, summarizes them into NER-friendly paragraphs, and feeds them through the MCP tool `mirofish_enrich_graph`. The `/sim-update` slash command orchestrates this workflow end-to-end. This implements the CVPR paper's closed-loop refinement pattern at the simulation level: capture → reconstruct (graph enrichment) → compose (agent profiles) → share (simulation) → refine (report → next cycle).

**Information fidelity through layers:**
1. Documents — raw text with full context
2. Ontology — domain schema extracted via LLM
3. Graph — entities and relationships with temporal metadata
4. Agents — behavioral parameters derived from graph entities
5. Simulation — agent interactions and emergent behaviors
6. Reports — analytical synthesis via ReACT tool use

Each layer adds abstraction but preserves traceability back to its source.

### Architecture-Level Principles

**Domain knowledge lives in both code and metadata.** Hardcoded: daily activity schedules, entity type defaults, platform algorithms. Metadata: ontology types, agent behavioral parameters, time multipliers. The hybrid ensures flexibility (metadata can change without deploys) while remaining grounded (code enforces invariants).

**Subprocess isolation for unreliable workloads.** Simulations run in separate Python processes, not in the Flask server. IPC happens via filesystem (command/response files). A crashed simulation doesn't crash the backend. Status is tracked via real-time log reading, not in-process state.

**Graceful degradation everywhere.** If graph search fails → local keyword matching. If LLM fails → rule-based generation. If interview fails → helpful error message. If graph update fails → retry 3× with backoff. No component has a hard failure mode.

## Lessons Learned

1. **Ollama num_ctx is critical** — without explicitly setting it, prompts get silently truncated at 2048 tokens. Always pass `num_ctx` via extra_body.
2. **Batch + fallback is the right pattern for LLM generation** — individual failures are expected; rule-based fallbacks ensure the pipeline completes.
3. **File-based IPC is surprisingly robust** for local multi-process apps — simpler than sockets, no dependency on Redis/RabbitMQ.
4. **Profile generation dominates wall-clock time** — 65 entities × 20s/each = ~20min. Parallelism helps but is bounded by VRAM.
5. **JSON mode + fence stripping is necessary** — multiple model families add markdown fences around JSON even in JSON mode.
6. **Decreasing temperature on retry** improves JSON validity — start creative (0.7), get more deterministic (0.5) on failure.
7. **Stepwise generation beats monolithic prompts** — splitting into focused steps with small JSON outputs dramatically reduces parse failures. One 50-field JSON call fails often; five 10-field calls almost never do.
8. **Always have a rule-based fallback per entity type** — LLMs are unreliable for 100% of items. Rule-based defaults (based on entity type lookup tables) ensure the pipeline never stalls.
9. **Real-time file output during generation** gives users confidence the system is working — write results incrementally, not just at the end.
10. **Pre-allocate result lists for order preservation** — when using ThreadPoolExecutor with as_completed(), items return out of order. Pre-allocating `[None] * total` and writing by index preserves input ordering.
11. **Always use process groups for subprocesses** — without `start_new_session`, killing the parent leaves orphan child processes consuming resources. Process groups enable clean tree termination.
12. **Byte-position tracking beats line counting for streaming logs** — seeking to a byte offset is O(1) vs re-reading and counting lines which is O(n). Critical for long-running simulations that produce thousands of log entries.
13. **ReACT loops need iteration caps and tool timeouts** — without them, an LLM can loop indefinitely or a misbehaving tool can block forever. Always set `max_iterations` and per-tool `timeout` as safety bounds.
