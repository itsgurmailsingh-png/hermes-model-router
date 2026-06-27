# Architecture — hermes-model-router v1.1

## Overview

The model router is a two-layer system that classifies each LLM call before it reaches the API. Layer 1 scores features from the message and context. The context tree adds a semantic floor from files you've been touching. The output is a score (0–100) which maps to a tier (0–3) which maps to a concrete model name via a configurable table.

The same scoring logic runs in both Hermes (Python) and Claude Code (Node.js/MJS), with identical tier boundaries, so routing decisions are predictable regardless of which runtime you are in.

**v1.1 changes**: ALL calls are now routed (main chat included, not just internal calls). The context tree is wired into the router — it's no longer "planned v2", it's live.

---

## Layer 1 — Feature Scoring

Produces a score in the range 0–100 from a weighted sum of features.

### Base score

Every call starts at **30**. This means a totally empty prompt routes to Tier 1, not Tier 0 — a conservative default.

### Length bonus (0–20)

```
bonus = min(len(prompt) / 200, 1.0) * 20
```

A 200-character prompt adds +10. A 400+ character prompt adds the full +20. Length is a weak signal on its own but combines reliably with other features.

### Keyword hits (+8 each, capped at +30)

The following words are high-signal complexity indicators. Each hit adds 8 points, total capped at 30 (so 4+ hits all add the same as 3 hits).

```
refactor, architect, implement, debug, design, optimize,
integrate, migrate, deploy, security, performance, scale,
concurrency, algorithm, database, api, framework, pipeline
```

The words are matched case-insensitively on word boundaries to avoid false positives (e.g. "apis" does not match "api").

### Code block (+15)

Any fenced code block (`` ``` ``) in the prompt adds 15 points. The rationale: if the user is pasting code, they almost certainly want a model that can reason about code.

### Multi-file signal (+15)

If the prompt references two or more distinct file paths (heuristic: strings containing `/` or ending in a known extension), +15 is added. Multi-file tasks require cross-file reasoning which cheaper models handle poorly.

### Low-signal override

If the message matches a greeting/acknowledgement pattern (`hi`, `thanks`, `ok`, `done`, etc.) it scores 5, regardless of other features. This ensures trivial messages route to the cheapest tier — unless the context tree floor overrides it.

### Tier boundaries (actual code)

| Score | Tier | Ollama model | Claude model |
|-------|------|-------------|-------------|
| 0–20 | 0 | ministral-3:3b | claude-haiku-4-5-20251001 |
| 21–45 | 1 | gemma3:12b | claude-haiku-4-5-20251001 |
| 46–70 | 2 | devstral-small-2:24b | claude-sonnet-4-6 |
| 71–100 | 3 | glm-5.2 | claude-opus-4-6 |

---

## Session Floor

The session floor prevents mid-session regression: once a complex task has been seen, simpler follow-up turns do not fall back to cheap models that have no context.

- Floor starts at 0.
- After each call, the floor is updated: `floor = max(floor, turn_score)`.
- Floor decays by 5 per simple turn (score < current floor).
- Floor is stored in memory per session (`RouterSession`), not persisted across restarts.

This means:
- A coding session that hit score 68 will not route the next turn below 63.
- A long chain of simple turns (acknowledgements, confirmations) eventually lets the floor decay back to 0.

---

## Context Tree Floor (v1.1 — LIVE)

The context tree maintains a live semantic graph of the agent's working context. It tracks:

- **FILE nodes** — every file read or written, with auto-extracted domain tags (auth, security, database, api, test, config, ui, infra) and a complexity score (0–100) based on lines, functions, classes, imports, and branching.
- **TURN nodes** — each user message, tagged by domain.
- **CALL nodes** — tool calls (bash, delegate_task, search_files, patch, terminal).

### How the floor is computed

`context_tree.query.complexity_floor(graph, prompt)`:

1. Extract domain tags from the prompt (e.g. "fix the auth token" → {auth}).
2. Score all graph nodes by tag overlap (×20) + node type weight (FILE=10, SYMBOL=8, TURN=6) + recency bonus (×15 if in last 10 touched) + complexity (×0.1).
3. Take the top-4 scored nodes with score > 0.
4. Floor = average of the top node's complexity and the mean complexity of the 4 nodes, capped at 70.

This floor is merged with the session floor (`max(session_floor, ctx_floor)`) before applying the call-type offset.

### What this prevents

Without the context tree, a short follow-up like "fix the typo" after a complex auth refactor scores 30 → routes to `gemma3:12b` (Tier 1). That model has no idea what you've been doing. With the context tree, the floor from `auth.py` (complexity 72) pushes the effective score to 70 → routes to `devstral-small-2:24b` (Tier 2). The model can't see your code, but the router knows you're in a complex session.

### Graph persistence

The graph is saved to `~/.hermes/router-logs/context-graph.json` after every turn. It's loaded on session start if it exists. The Claude Code `agent-router.mjs` hook reads this same file for cross-runtime floor sharing.

### Node data structure (actual implementation)

```python
@dataclass
class Node:
    id:         str          # "file:src/auth.py", "turn:sess:1", "call:sess:1:2"
    type:       str          # FILE, TURN, CALL, SYMBOL, TASK
    label:      str          # "auth.py", "refactor auth", "bash: pytest"
    tags:       List[str]    # ["auth", "security"]
    complexity: int          # 0-100
    ts:         str          # ISO timestamp
    meta:       Dict         # {"path": "...", "last_op": "read"}
```

---

## Call-Type Offsets

Call type is determined from context metadata, not from the prompt text. Hermes provides `turn_type` directly. Claude Code infers it from the Agent `prompt` field prefix or the spawning context.

| Call type | Score offset | Rationale |
|-----------|-------------|-----------|
| PLAN / orchestrate | 0 | Full score — planning is expensive |
| ANALYZE / read files | -10 | Slightly discounted — reading is cheaper than writing |
| CODEGEN / write code | -10 | Discounted — models at Tier 1+ handle this fine |
| VERIFY / review | -20 | Review is lower stakes than generation |
| SUMMARIZE / format | -30 | Mechanical task, Tier 0/1 is fine |
| TITLE generation | -99 | Always cheapest; scores floor at Tier 0 regardless of context |
| SUBAGENT | -10 | Children run one tier below parent |

Offsets are applied after both the session floor and context tree floor checks, so both floors still protect against regressions. TITLE (-99) overrides everything — title generation always routes to Tier 0.

---

## What Gets Routed

### Hermes (full control via `pre_llm_call` hook)

ALL calls are routed in v1.1:

| Call source | Routed? | Call type | Notes |
|-------------|---------|-----------|-------|
| Main user turn | ✅ | plan | Full score, no offset |
| Tool loop iteration | ✅ | analyze | -10 offset |
| Title generation | ✅ | title | -99 → always Tier 0 |
| Memory review | ✅ | summarize | -30 offset |
| Sub-agent spawn | ✅ | subagent | -10 offset |
| Background review | ✅ | verify | -20 offset |
| Context compression | ✅ | summarize | -30 offset |
| Session search | ✅ | analyze | -10 offset |

To skip specific turn types: `export HERMES_ROUTER_SKIP_TYPES=title,memory`

### Claude Code (partial control via `PreToolUse` hook)

| Call source | Routed? | Notes |
|-------------|---------|-------|
| Sub-agent spawn (Agent tool) | ✅ | Hook rewrites `model` field before execution |
| Main chat model | ❌ | Set by Claude Code internally before any hook runs |

Claude Code's main chat model is NOT controllable via hooks. The hook fires on tool calls (Agent spawns), not on the main conversation loop. A reverse proxy would be needed to intercept main chat API calls at the network layer.

---

## Provider Abstraction

Both runtimes share the same tier-to-model mapping logic but load different tables depending on the active provider.

```
PROVIDER=ollama  →  model_router.py    (Hermes)
PROVIDER=claude  →  model_router_claude.py  (Hermes) / agent-router.mjs (Claude Code)
```

Adding a new provider requires:
1. A new mapping table with 4 entries (Tier 0–3).
2. Registering the provider name in `plugin.yaml` (Hermes) or `agent-router.mjs` (Claude Code).
3. No changes to the scoring logic.

---

## Logging Format

### Hermes

Logs every routing decision to `~/.hermes/router-logs/routing.jsonl`:

```json
{
  "ts": "2026-06-28T10:15:32.001Z",
  "source": "hermes",
  "session_id": "abc123",
  "turn_type": "user",
  "api_call": 1,
  "call_type": "plan",
  "model_was": "glm-5.2",
  "model_used": "devstral-small-2:24b",
  "routed": true,
  "prompt": "fix the typo in auth.py..."
}
```

### Claude Code

Logs to `~/.claude/router-logs/routing.jsonl`:

```json
{
  "ts": "2026-06-28T10:15:32.001Z",
  "prompt": "refactor the auth module to use...",
  "subtype": "coder",
  "call_type": "codegen",
  "raw_score": 68,
  "offset": -10,
  "ctx_floor": 55,
  "final_score": 58,
  "model_was": "opus",
  "model_used": "sonnet",
  "routed": true
}
```

`router-analyze.mjs` reads this file and produces a summary report.

### Context graph

Saved to `~/.hermes/router-logs/context-graph.json` after every turn:

```json
{
  "session": "abc123",
  "nodes": [
    {"id": "file:src/auth.py", "type": "FILE", "label": "auth.py", "tags": ["auth","security"], "complexity": 72, ...},
    {"id": "turn:abc123:1", "type": "TURN", "label": "refactor auth", "tags": ["auth"], ...}
  ],
  "edges": [
    {"src": "turn:abc123:1", "dst": "file:src/auth.py", "type": "REFERENCED"}
  ],
  "recent": ["file:src/auth.py", "turn:abc123:1"]
}
```

---

## Plugin Hooks (v1.1)

| Hook | When | What it does |
|------|------|-------------|
| `on_session_start` | Session begins | Creates ContextGraph + ContextTreeBuilder, attaches to agent |
| `pre_llm_call` | Before every LLM call | Scores message, applies session + context tree floor, routes to tier, returns `{"model": name}` |
| `post_llm_call` | After each main turn | Updates session complexity floor from message score |
| `on_tool_result` | After every tool call | Feeds tool result to ContextTreeBuilder (read/write/bash/delegate/search/patch) |
| `on_turn_start` | User sends message | Adds TURN node to graph, sets as current turn |
| `on_turn_end` | Turn completes | Saves graph to `~/.hermes/router-logs/context-graph.json` |

---

## Future: Cost Attribution & Adaptive Weights

The context tree has the data structure for cost tracking (Node.meta can hold tokens_in/out), but this is not yet wired. Planned:

1. **Cost attribution** — each node tracks estimated tokens and cost. CLI report shows cost-per-file, cost-per-subtask.
2. **Adaptive keyword weights** — after each session, actual model performance updates keyword weights via a simple online learning rule.
3. **SYMBOL nodes** — track individual functions/classes, not just files.
4. **DEPENDS_ON edges** — traverse the DAG for inherited complexity (if Call B depends on Call A's output, B inherits A's floor even if B's text is simple).