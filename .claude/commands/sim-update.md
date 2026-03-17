# /sim-update - Enrich Graph & Improve Simulations

Closed-loop feedback: gather signals → summarize into domain prose → enrich graph → re-simulate.

## Prerequisites

- MiroFish backend running (`/status` to check)
- An existing project with a built graph (use `mirofish_list_projects` to find one)

## Steps

### 1. Identify Target Graph

Use `mirofish_list_projects` to find the project. Note the `graph_id` and `project_id`.
If the user specified a project, use that one. Otherwise, use the most recent project with a completed graph.

**After identifying the graph, persist it for the zero-token git hook:**
```bash
echo "<graph_id>" > ~/.mirofish_active_graph_id
```
This enables the portals_v4 post-commit hook to automatically enrich on every commit (zero Claude tokens).

### 2. Gather Signals

Ask the user which sources to pull from. Multiple sources can be combined in one run.

| Source | How to gather |
|--------|---------------|
| **portals_v4 git changes** | `git -C ~/Documents/GitHub/portals_v4 log --oneline --since="7d"` and `git -C ~/Documents/GitHub/portals_v4 diff HEAD~10..HEAD --stat` then read key changed files |
| **Dev input** | Developer provides feedback directly in conversation |
| **User feedback** | User describes simulation quality issues or persona adjustments |
| **KB learnings** | Read `~/.claude/knowledgebase/_MIROFISH_AGENT_SIMULATION_PATTERNS.md` |

### 3. Summarize into Domain Prose

**Critical step.** Do NOT feed raw diffs, code, or logs into the graph. Summarize each signal source into NER-friendly domain-knowledge paragraphs.

Good example:
> "Portals now supports AR scene recording via ArViewRecorder, capturing at 30 FPS using AVAssetWriter. Users can scrub recorded clips for cover frames and publish directly to social feeds."

Bad example:
> "Added ArViewRecorder.cs with StartRecording() and StopRecording() methods. Uses AVAssetWriter..."

The summary should name entities as proper nouns, describe relationships, use complete sentences, focus on domain knowledge not implementation.

### 4. Enrich the Graph

```
mirofish_enrich_graph(graph_id="<graph_id>", text="<prose>", source="<label>")
```

Poll with `mirofish_task_status(task_id)` until complete.

### 5. Verify Enrichment

```
mirofish_graph_search(graph_id="<graph_id>", query="<key entity>")
```

### 6. Re-simulate (Optional)

```
mirofish_run_pipeline(project_id="<project_id>", platform="parallel", max_rounds=10)
```

### 7. Generate Report (Optional)

```
mirofish_generate_report(simulation_id="<sim_id>")
```

## Zero-Token Automation

The portals_v4 post-commit hook (`portals_v4/.git/hooks/post-commit`) automatically POSTs commit messages to `/api/graph/enrich` when:
1. `~/.mirofish_active_graph_id` file exists (set by step 1 above)
2. MiroFish backend is running on localhost:5001

This means every portals_v4 commit enriches the graph with zero Claude tokens. The hook is fire-and-forget (non-blocking, backgrounded with `&`). `/sim-update` is only needed for manual enrichment, re-simulation, or report generation.

## Notes

- Each enrichment compounds — entities merge via MERGE semantics in Neo4j
- Multiple sources can be enriched in sequence within one run
- The git hook is non-blocking — it backgrounds the curl and never delays commits
