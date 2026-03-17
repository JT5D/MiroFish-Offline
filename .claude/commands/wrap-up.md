# /wrap-up - Session Wrap-Up

End-of-session routine for MiroFish-Offline: review changes, update knowledgebase, commit, push.

## Steps

1. **Gather changes**:
   - `git diff --stat` — what files changed
   - `git log --oneline -5` — recent commits this session
   - Review what was accomplished

2. **Promote insights by tier** (applies to wins AND failures/anti-patterns equally):

   **T1 — Insights** → write to `~/.claude/knowledgebase/_UPPERCASE_NAME.md`:
   - New patterns, gotchas, operational lessons, model quirks

   **T2 — Massive Wins** → add to `CLAUDE.md` or `~/GLOBAL_RULES.md`:
   - Patterns that save significant time/tokens across projects
   - "Never do X" rules from recurring failures
   - Promote when same insight helped in 3+ sessions or 2+ projects

   **T3 — Quantum Leaps** → enforce via git hooks, pre-commit, or CI:
   - Rules where violation caused data loss, wasted >30min, or broke production
   - Add automated prevention, not just documentation

3. **Check for stale state**:
   - Any running simulations that should be stopped?
   - Any background processes still active?
   - Services that should be stopped?

4. **Commit & push**:
   - Stage specific files (never `git add -A`)
   - Commit with conventional format
   - Push to origin (JT5D/MiroFish-Offline)

5. **Report summary**:
   - What was done this session
   - State of services (running/stopped)
   - Suggested next steps
