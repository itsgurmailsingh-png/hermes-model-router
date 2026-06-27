# hermes-model-router

> Complexity-based model routing for Hermes Agent and Claude Code. Routes ALL LLM calls to the cheapest model tier that can handle the task — saving 60–75% on inference costs. A semantic context tree prevents mid-session downgrades.

## What it does

- **Hermes**: Hooks into `pre_llm_call` — **ALL** calls (main chat, sub-agents, title gen, memory review, tool loops) are routed to the cheapest capable tier. A context tree tracks files read/written, tool calls, and turn history to compute a semantic complexity floor that prevents mid-session downgrades.
- **Claude Code**: A `PreToolUse` hook intercepts every Agent spawn and rewrites the `model` field based on task complexity before execution. Reads the Hermes context graph for cross-runtime floor sharing. **Note**: Claude Code's main chat model is NOT controllable via hooks — only sub-agent spawns can be routed. Hermes has no such limitation.

## Model Tiers

### Ollama Cloud (Hermes default)
| Tier | Model | Size | Use case |
|------|-------|------|----------|
| 0 | `ministral-3:3b` | 4.7 GB | Greetings, titles, lookups |
| 1 | `gemma3:12b` | 24 GB | Simple Q&A, summaries |
| 2 | `devstral-small-2:24b` | 51.6 GB | Coding, analysis |
| 3 | `glm-5.2` | — | Planning, architecture |

### Claude (Claude Code / Anthropic provider)
| Tier | Model | Use case |
|------|-------|----------|
| 0–1 | `claude-haiku-4-5-20251001` | Simple tasks |
| 2 | `claude-sonnet-4-6` | Coding, analysis |
| 3 | `claude-opus-4-6` | Architecture, deep reasoning |

## How Routing Works

### Scoring (0–100)
Each call is scored before routing:
- Base: 30
- Message length bonus: up to +20
- High-signal keywords (refactor, architect, implement, debug...): +8 each, max +30
- Code block in prompt: +15
- Multi-file task: +15
- Session complexity floor: never routes below the floor mid-session
- **Context tree floor**: semantic floor from files you've been touching — prevents downgrade even when the message itself is simple

### Tier boundaries (actual code)
| Score | Tier | Ollama | Claude |
|-------|------|--------|--------|
| ≤ 20 | 0 | ministral-3:3b | haiku |
| 21–45 | 1 | gemma3:12b | haiku |
| 46–70 | 2 | devstral-small-2:24b | sonnet |
| 71–100 | 3 | glm-5.2 | opus |

### Call-type offsets
| Call type | Offset | Rationale |
|-----------|--------|-----------|
| PLAN / orchestrate | 0 | Full score — planning is expensive |
| ANALYZE / read files | -10 | Reading is cheaper than writing |
| CODEGEN / write code | -10 | Tier 1+ handles this fine |
| VERIFY / review | -20 | Lower stakes than generation |
| SUMMARIZE / format | -30 | Mechanical task |
| TITLE generation | -99 | Always cheapest tier |
| SUBAGENT | -10 | Children run one tier below parent |

### Context tree floor

The context tree maintains a live graph of:
- **FILE nodes** — every file read or written (tags: auth, security, database, api, test, config, ui, infra)
- **TURN nodes** — each user message
- **CALL nodes** — tool calls (bash, delegate_task, search_files, patch, terminal)

When routing a new message, the query engine scores all nodes by tag overlap + recency + complexity, returns the top-k relevant nodes, and computes a semantic complexity floor. This floor is merged with the session floor — so if you've been working on `auth.py` (complexity 72) and say "fix the typo", the router keeps you on `devstral-small-2:24b` instead of downgrading to `gemma3:12b`.

| Message | No context tree | With context tree (auth.py touched) |
|---------|-----------------|--------------------------------------|
| "hi" | ministral-3:3b | devstral-small-2:24b |
| "fix the typo" | gemma3:12b | devstral-small-2:24b |
| TITLE gen | ministral-3:3b | ministral-3:3b (always cheap) |

## Plugin Hooks

| Hook | When | What it does |
|------|------|-------------|
| `on_session_start` | Session begins | Creates ContextGraph + ContextTreeBuilder, attaches to agent |
| `pre_llm_call` | Before every LLM call | Scores message, applies floors, routes to tier |
| `post_llm_call` | After each main turn | Updates session complexity floor |
| `on_tool_result` | After every tool call | Feeds tool result to context tree builder |
| `on_turn_start` | User sends message | Adds TURN node to graph |
| `on_turn_end` | Turn completes | Saves graph to `~/.hermes/router-logs/context-graph.json` |

## Installation

### Hermes
```bash
# 1. Copy agent files (router + context tree)
cp hermes/agent/model_router.py ~/.hermes/hermes-agent/agent/
cp hermes/agent/model_router_claude.py ~/.hermes/hermes-agent/agent/
cp -r hermes/agent/context_tree ~/.hermes/hermes-agent/agent/

# 2. Copy plugin
cp -r hermes/plugins/model-router ~/.hermes/hermes-agent/plugins/

# 3. Enable the plugin
hermes plugins enable model-router

# 4. Restart Hermes
```

Override tiers via env:
```bash
export HERMES_ROUTER_TIER0=ministral-3:3b
export HERMES_ROUTER_TIER1=gemma3:12b
export HERMES_ROUTER_TIER2=devstral-small-2:24b
export HERMES_ROUTER_TIER3=glm-5.2
```

### Claude Code
```bash
# 1. Copy helpers
cp claude-code/helpers/agent-router.mjs ~/.claude/helpers/
cp claude-code/helpers/router-analyze.mjs ~/.claude/helpers/

# 2. Copy skill
mkdir -p ~/.claude/skills/model-router
cp claude-code/skills/model-router/SKILL.md ~/.claude/skills/model-router/

# 3. Add hook to ~/.claude/settings.json
# In the "PreToolUse" array, add:
{
  "matcher": "Agent",
  "hooks": [{
    "type": "command",
    "command": "sh -c 'exec node \"$HOME/.claude/helpers/agent-router.mjs\"'",
    "timeout": 3000
  }]
}
```

The Claude Code hook reads `~/.hermes/router-logs/context-graph.json` (written by the Hermes plugin) for cross-runtime context tree floor sharing.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERMES_MODEL_ROUTER` | `1` | Kill switch — set to `0` to disable all routing |
| `HERMES_ROUTER_SKIP_TYPES` | (empty) | Comma-separated turn types to skip (e.g. `title,memory`) |
| `HERMES_ROUTER_TIER0` | `ministral-3:3b` | Tier 0 model |
| `HERMES_ROUTER_TIER1` | `gemma3:12b` | Tier 1 model |
| `HERMES_ROUTER_TIER2` | `devstral-small-2:24b` | Tier 2 model |
| `HERMES_ROUTER_TIER3` | `glm-5.2` | Tier 3 model |
| `CLAUDE_MODEL_ROUTER` | `1` | Kill switch for Claude Code hook |
| `CLAUDE_ROUTER_TIER0–3` | Claude defaults | Claude tier overrides |

## Analyzing Routing Decisions

Both runtimes log to `~/.hermes/router-logs/routing.jsonl` (Hermes) and `~/.claude/router-logs/routing.jsonl` (Claude Code).

```bash
# Claude Code report
node ~/.claude/helpers/router-analyze.mjs

# Last 20 decisions
node ~/.claude/helpers/router-analyze.mjs --last 20

# Raw JSON
node ~/.claude/helpers/router-analyze.mjs --raw
```

The Hermes plugin also saves the context graph to `~/.hermes/router-logs/context-graph.json` after every turn — inspect it to see what files/tags/complexity the router sees.

## Architecture

```
User Message
     │
     ▼
Layer 1: Feature scoring (~1ms, free)
  • Keyword hits, length, code presence
  • Context dependency signals
     │
     ▼
Session floor applied
  • Never routes below floor mid-session
  • Floor decays 5pts per simple turn
     │
     ▼
Context tree floor applied
  • Semantic floor from files touched (auth, db, api...)
  • Merged with session floor (max wins)
     │
     ▼
Call-type offset applied
  • PLAN gets full score
  • Internal calls discounted
  • TITLE always cheapest (-99)
     │
     ▼
Tier → Model
```

## What CAN and CANNOT be routed

### Hermes (full control)
- ✅ Main chat turns — routed via `pre_llm_call` hook
- ✅ Sub-agent spawns — routed via `pre_llm_call` hook
- ✅ Internal calls (title gen, memory review, tool loops) — routed via `pre_llm_call`
- ✅ All other turn types

### Claude Code (partial control)
- ✅ Sub-agent spawns (Agent tool) — routed via `PreToolUse` hook
- ❌ Main chat model — set by Claude Code internally before any hook runs, not controllable via hooks. Would need a reverse proxy to intercept at the network layer.