# /sim-update - Enrich Graph & Improve Simulations

Closed-loop feedback: gather signals → summarize into domain prose → enrich graph → re-simulate.

## Prerequisites

- MiroFish backend running (`/status` to check)
- An existing project with a built graph (use `mirofish_list_projects` to find one)

## Steps

### 1. Identify Target Graph

Use `mirofish_list_projects` to find the project. Note the `graph_id` and `project_id`.
If the user specified a project, use that one. Otherwise, use the most recent project with a completed graph.

### 2. Gather Signals

Ask the user which sources to pull from. Multiple sources can be combined in one run.

| Source | How to gather |
|--------|---------------|
| **portals_v4 git changes** | `git -C ~/Documents/GitHub/portals_v4 log --oneline --since="7d"` and `git -C ~/Documents/GitHub/portals_v4 diff HEAD~10..HEAD --stat` then read key changed files |
| **Voice prompts** | Read `VoiceIntentManager.cs` or voice-related source files from portals_v4 |
| **Worlds saved** | Read scene persistence code, saved world metadata from portals_v4 |
| **Features most used** | Read analytics events, UI interaction patterns from portals_v4 |
| **Build/test results** | Run `dotnet test` or read recent CI/build output |
| **Performance metrics** | Read profiler data, frame time budgets from portals_v4 code |
| **Dev input** | Developer provides feedback directly in conversation |
| **User feedback** | User describes simulation quality issues or persona adjustments |
| **KB learnings** | Read `~/.claude/knowledgebase/_MIROFISH_AGENT_SIMULATION_PATTERNS.md` |
| **CVPR paper** | Read portals 4D world models paper for architectural principles |

### 3. Summarize into Domain Prose

**Critical step.** Do NOT feed raw diffs, code, or logs into the graph. Summarize each signal source into NER-friendly domain-knowledge paragraphs.

Good example:
> "Portals now supports AR scene recording via ArViewRecorder, capturing at 30 FPS using AVAssetWriter. Users can scrub recorded clips for cover frames and publish directly to social feeds. The recording pipeline integrates with the existing VFX system."

Bad example:
> "Added ArViewRecorder.cs with StartRecording() and StopRecording() methods. Uses AVAssetWriter..."

The summary should:
- Name technologies, features, people, organizations as proper nouns (NER targets)
- Describe relationships between entities ("integrates with", "publishes to", "captures")
- Use complete sentences, not bullet lists
- Focus on what users/agents would know, not implementation details

### 4. Enrich the Graph

Call the MCP tool to feed summarized text into the graph:

```
mirofish_enrich_graph(
  graph_id="<graph_id>",
  text="<summarized paragraphs>",
  source="<source_label>"  // e.g. "portals_v4_commits", "dev_input", "user_feedback"
)
```

This returns a `task_id`. Poll with `mirofish_task_status(task_id)` until complete.

### 5. Verify Enrichment

Search the graph to confirm new entities were added:

```
mirofish_graph_search(
  graph_id="<graph_id>",
  query="<key entity from your summary>"
)
```

Report what new entities and relations appeared.

### 6. Re-simulate (Optional)

If the user wants to see improved agents, re-run the simulation:

```
mirofish_run_pipeline(project_id="<project_id>", platform="parallel", max_rounds=10)
```

The profile generator will now draw from the enriched graph, producing agents with richer context.

### 7. Generate Comparative Report (Optional)

After simulation completes:

```
mirofish_generate_report(simulation_id="<new_sim_id>")
```

Then retrieve with `mirofish_get_report(simulation_id)` and compare against previous reports.

## Example Workflow

```
User: /sim-update
Claude: Which sources should I pull from?
User: portals_v4 commits and dev input
Claude: [reads git log, summarizes changes into prose]
Claude: [calls mirofish_enrich_graph with summary]
Claude: [verifies with mirofish_graph_search]
Claude: Graph enriched with 12 new entities. Want me to re-run the simulation?
User: Yes
Claude: [calls mirofish_run_pipeline]
Claude: Simulation complete. Agents now reference AR recording and VFX features.
```

## Notes

- Each enrichment compounds — entities merge with existing ones via MERGE semantics in Neo4j
- The `source` label is stored for audit trail, not used in NER
- Multiple sources can be enriched in sequence within one `/sim-update` run
- If enrichment task fails, check that the graph_id exists and Neo4j is running
