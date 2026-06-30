# hermes-model-router

> Complexity-based model routing for Hermes Agent and Claude Code. Routes ALL LLM calls to the cheapest model tier that can handle the task — saving 30–45% on Claude API costs and significant latency on local Ollama calls. A semantic context tree prevents mid-session downgrades.

## What it does

- **Hermes**: Hooks into `pre_llm_call` — **ALL** calls (main chat, sub-agents, title gen, memory review, tool loops) are routed to the cheapest capable tier. A context tree tracks files read/written, tool calls, and turn history to compute a semantic complexity floor that prevents mid-session downgrades. Busy/overloaded models are detected and automatically retried with a fallback from a per-model chain.
- **Claude Code**: A reverse proxy (`scripts/claude_proxy.py`) intercepts every `/v1/messages` call at the network layer and rewrites the `model` field before it reaches Anthropic. `ANTHROPIC_BASE_URL=http://localhost:8082` is set in `~/.claude/settings.json` so all calls route through it automatically. The proxy is auto-started by a `SessionStart` hook. A `PostToolUse` hook builds the context graph from every tool call in real time.

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

### Scoring — 4-Dimensional Complexity Matrix

Each call is scored across four independent dimensions (0–100 each), then combined into a weighted score:

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| **Text** | 40% | Keyword signals, message length, question count |
| **Code** | 30% | Fenced blocks, file references, multi-file scope |
| **Vision** | 20% | Image attachments or image-related keywords |
| **Context** | 10% | Conversation depth, tool call history, floor boosts |

**Combined score** = `text×0.4 + code×0.3 + vision×0.2 + context×0.1`

#### Text dimension signals
- Base: 30
- Message length bonus: up to +20
- High-signal keywords (refactor, architect, implement, debug, migrate, design, optimize, api, schema, algorithm, end-to-end...): +8 each, max +30
- Code block in prompt: +15 (code dimension)
- Multi-file task: +15 (code dimension)
- **Trivial scope** ("just", "only", "one", "quick", "simple", "small"): −15
- **Micro-task** ("typo", "rename", "comment", "format", "indent", "spelling"): −20
- **Short+keyword penalty**: keyword present but message < 60 chars → −10

#### Priority routing (overrides combined score)
1. **Vision ≥ 100** (image attached) → Sonnet (simple image) or Opus (text-heavy image)
2. **Code ≥ 50** (multi-file, entire codebase) → Opus
3. **Code > 25 and code ≥ text×0.5** → Sonnet minimum
4. **Text ≥ 55** (3+ high-signal keywords) → Sonnet; text ≥ 75 → Opus
5. **Context ≥ 30** (deep tool loop) → Sonnet minimum
6. Combined score thresholds (below)

### Tier boundaries
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
- **FILE nodes** — every file read or written (tags: auth, security, database, api, test, config, ui, infra, ml, graphics, networking, game — see `hermes/agent/context_tree/tags.json` for the full extensible list)
- **TURN nodes** — each user message
- **CALL nodes** — tool calls (bash, delegate_task, search_files, patch, terminal)

Tag patterns are loaded from `hermes/agent/context_tree/tags.json` (user-extensible JSON, hot-reloadable via `reload_tags()`). Both `builder.py` and `query.py` load from the same file. v1.2 added `ml`, `graphics`, `networking`, and `game` domains.

When routing a new message, the query engine scores all nodes by tag overlap + recency + complexity, returns the top-k relevant nodes, and computes a semantic complexity floor. This floor is merged with the session floor — so if you've been working on `auth.py` (complexity 72) and say "fix the typo", the router keeps you on `devstral-small-2:24b` instead of downgrading to `gemma3:12b`.

### Complexity decay (v1.2)

Files not touched recently contribute less to the context floor:

| Recency (position in touched history) | Contribution |
|----------------------------------------|-------------|
| Recent (last 10 touched) | 100% |
| 10–30 back | 80% |
| 30–50 back | 60% |
| Beyond 50 | 40% |

This prevents stale files from inflating the floor long after you've moved on.

### Feedback loop (fully automatic)

After every routed call, the router automatically records success or failure — no manual API calls needed:

- **Success** (`on_post_llm_call`): `record_result(call_type, success=True)` → floor boost decays by 3
- **Failure** (`on_llm_error`): `record_result(call_type, success=False)` → floor boost grows by +10 (capped at +30)
- Feedback is **immediately persisted** to `~/.hermes/router-logs/feedback.json` after every call
- Loaded on session start so the router learns across sessions

This means: if Haiku keeps failing on `codegen` calls, the router automatically escalates to Sonnet for those call types until success rate recovers.

### Manual override (v1.2)

Prefix a message with `/t0`, `/t1`, `/t2`, or `/t3` to force a specific tier for one call. This bypasses all scoring and floors. Useful for testing or when you know better than the router.

| Message | No context tree | With context tree (auth.py touched) |
|---------|-----------------|--------------------------------------|
| "hi" | ministral-3:3b | devstral-small-2:24b |
| "fix the typo" | gemma3:12b | devstral-small-2:24b |
| TITLE gen | ministral-3:3b | ministral-3:3b (always cheap) |

## Plugin Hooks

### Hermes plugin hooks

| Hook | When | What it does |
|------|------|-------------|
| `on_session_start` | Session begins | Creates ContextGraph + ContextTreeBuilder, attaches to agent |
| `pre_llm_call` | Before every LLM call | Scores message (4D matrix), checks busy state, applies floors, routes to tier |
| `post_llm_call` | After each main turn | Updates session floor + records success in feedback loop |
| `on_llm_error` | On API error | Records failure in feedback loop; if busy/503, marks model busy and returns fallback |
| `on_tool_result` | After every tool call | Feeds tool result to context tree builder |
| `on_turn_start` | User sends message | Adds TURN node to graph |
| `on_turn_end` | Turn completes | Saves graph to `~/.hermes/router-logs/context-graph.json` |

### Claude Code hooks (in `~/.claude/settings.json`)

| Hook | Trigger | What it does |
|------|---------|-------------|
| `SessionStart` | Claude Code starts | Auto-starts proxy if not running (`pgrep` guard) |
| `UserPromptSubmit` | User sends message | Adds TURN node to context graph (async) |
| `PostToolUse` | After any tool | Adds FILE/BASH/DELEGATE node to graph (async) |
| `PreToolUse: Agent` | Before spawning sub-agent | Routes sub-agent to correct model tier |

### Busy model fallback

When a model returns 429, 503, 529, 502, or 504 (or a message containing "busy", "overloaded", "rate limit", "capacity"):

1. Model is marked busy for 120 seconds (persisted to `~/.hermes/router-logs/busy-models.json`)
2. Router walks the fallback chain for that model and returns `{"model": fallback, "retry": True}`
3. Hermes retries the call immediately with the fallback — user sees no error

Fallback chains are defined in `_OLLAMA_FALLBACK` and `_CLAUDE_FALLBACK` in the plugin.

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

# 3. Add to ~/.claude/settings.json:
#    (a) env section — route all calls through the proxy
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8082"
  }
}

#    (b) SessionStart hook — auto-start proxy
{
  "type": "command",
  "command": "sh -c 'pgrep -f claude_proxy.py >/dev/null || python3 /path/to/scripts/claude_proxy.py &'",
  "timeout": 3000,
  "async": true
}

#    (c) PostToolUse hook — build context graph from every tool call
{
  "matcher": "Read|Write|Edit|MultiEdit|Bash|Glob|Grep|Agent|Task",
  "hooks": [{
    "type": "command",
    "command": "python3 /path/to/scripts/build_graph.py",
    "timeout": 5000,
    "async": true
  }]
}

#    (d) PreToolUse hook — route sub-agent spawns
{
  "matcher": "Agent",
  "hooks": [{
    "type": "command",
    "command": "sh -c 'exec node \"$HOME/.claude/helpers/agent-router.mjs\"'",
    "timeout": 3000
  }]
}
```

The proxy listens on `http://localhost:8082`, intercepts `/v1/messages`, scores the prompt using the same 4D matrix, rewrites the `model` field, and forwards to Anthropic. All routing decisions are logged to `~/.claude/router-logs/routing.jsonl`.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HERMES_MODEL_ROUTER` | `1` | Kill switch — set to `0` to disable all routing |
| `HERMES_ROUTER_SKIP_TYPES` | (empty) | Comma-separated turn types to skip (e.g. `title,memory`) |
| `HERMES_ROUTER_TIER0` | `ministral-3:3b` | Tier 0 model |
| `HERMES_ROUTER_TIER1` | `gemma3:12b` | Tier 1 model |
| `HERMES_ROUTER_TIER2` | `devstral-small-2:24b` | Tier 2 model |
| `HERMES_ROUTER_TIER3` | `glm-5.2` | Tier 3 model |
| `HERMES_ROUTER_CTX_FLOOR_CAP` | `70` | Cap for context tree floor contribution. Set to `90` to let context alone push a call to Tier 3 |
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

### Routing monitor (v1.2)

`scripts/router_monitor.py` reads `routing.jsonl` + `feedback.json` + `context-graph.json` and shows model distribution, call types, feedback state, and recent decisions with color-coded tiers.

```bash
# Summary + last 20 decisions
python scripts/router_monitor.py

# Last 50 decisions only
python scripts/router_monitor.py --last 50

# Live watch mode (refreshes every 5s)
python scripts/router_monitor.py --watch
```

## Architecture

```
User Message
     │
     ▼
Manual override? (/t0–/t3)
  • Yes → skip all scoring, force tier
  • No  → continue
     │
     ▼
Layer 1: Feature scoring (~1ms, free)
  • Keyword hits, length, code presence
  • Trivial scope / micro-task penalties (v1.2)
  • Context dependency signals
     │
     ▼
Session floor applied
  • Never routes below floor mid-session
  • Floor decays 5pts per simple turn
  • Feedback boosts: +10/failure (cap +30), -3/success (v1.2)
     │
     ▼
Context tree floor applied
  • Semantic floor from files touched (auth, db, api...)
  • Complexity decay by recency (v1.2)
  • Merged with session floor (max wins)
  • Capped at HERMES_ROUTER_CTX_FLOOR_CAP (default 70)
     │
     ▼
Call-type offset applied
  • PLAN gets full score
  • Internal calls discounted
  • TITLE always cheapest (-99)
     │
     ▼
Tier → Model
     │
     ▼
Feedback recorded (v1.2)
  • record_call_result(agent, call_type, success)
  • Persists to ~/.hermes/router-logs/feedback.json
```

## What CAN and CANNOT be routed

### Hermes (full control)
- ✅ Main chat turns — routed via `pre_llm_call` hook
- ✅ Sub-agent spawns — routed via `pre_llm_call` hook
- ✅ Internal calls (title gen, memory review, tool loops) — routed via `pre_llm_call`
- ✅ All other turn types

### Claude Code (full control via proxy)
- ✅ **Main chat model** — intercepted at network layer by `claude_proxy.py`. `ANTHROPIC_BASE_URL=http://localhost:8082` redirects all calls through the proxy, which rewrites the model before forwarding to Anthropic.
- ✅ Sub-agent spawns (Agent tool) — also routed via `PreToolUse` hook (`agent-router.mjs`)
- ✅ Context graph — built automatically via `PostToolUse` + `UserPromptSubmit` hooks (`build_graph.py`)