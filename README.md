# hermes-model-router

> Automatic complexity-based model routing for Hermes Agent and Claude Code. Routes LLM calls to the cheapest model tier that can handle the task — saving 60–75% on inference costs without changing your workflow.

## What it does

- **Hermes**: Hooks into `pre_llm_call` — internal calls (title gen, memory review, tool loops, sub-agents) are automatically routed to cheaper models. Your selected chat model is never touched.
- **Claude Code**: A `PreToolUse` hook intercepts every Agent spawn and rewrites the `model` field based on task complexity before execution. Hard enforcement, not a suggestion.

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

### Call-type offsets
| Call type | Offset |
|-----------|--------|
| PLAN / orchestrate | +0 |
| ANALYZE / read files | -10 |
| CODEGEN / write code | -10 |
| VERIFY / review | -20 |
| SUMMARIZE / format | -30 |
| TITLE generation | -40 |

## Installation

### Hermes
```bash
# 1. Copy files into your hermes-agent directory
cp hermes/agent/model_router.py ~/.hermes/hermes-agent/agent/
cp hermes/agent/model_router_claude.py ~/.hermes/hermes-agent/agent/
cp -r hermes/plugins/model-router ~/.hermes/hermes-agent/plugins/

# 2. Enable the plugin
hermes plugins enable model-router

# 3. Restart Hermes
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

## Analyzing Routing Decisions

Every Agent spawn is logged to `~/.claude/router-logs/routing.jsonl`.

```bash
# Full report
node ~/.claude/helpers/router-analyze.mjs

# Last 20 decisions
node ~/.claude/helpers/router-analyze.mjs --last 20

# Raw JSON
node ~/.claude/helpers/router-analyze.mjs --raw
```

Sample output:
```
═══════════════════════════════════════════
  Model Router — Analysis Report
═══════════════════════════════════════════

  Total calls logged : 47
  Calls re-routed    : 43 (91%)
  Avg complexity     : 38/100
  Est. cost saving   : ~78% vs always-opus

  Model distribution:
    haiku          31  ██████████████████  66%
    sonnet         13  ████████  28%
    opus            3  ██  6%
```

## Kill Switch

```bash
# Disable routing entirely
export HERMES_MODEL_ROUTER=0    # Hermes
export CLAUDE_MODEL_ROUTER=0    # Claude Code
```

## Architecture

```
User Message
     │
     ▼
Layer 0: Rule triage (~0ms, free)
  • Obvious cheap: greetings, titles, yes/no
  • Obvious expensive: /analyze, code blocks, multi-file
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
Call-type offset applied
  • PLAN gets full score
  • Internal calls discounted
     │
     ▼
Tier → Model
```

## Why a Plugin (not a hook in the main loop)?

The main chat turn model should always be what the user selected in the UI — overriding it would be wrong. The plugin hooks into `pre_llm_call` which fires for ALL calls including internal ones, and has enough context (`turn_type`, `api_call_count`) to distinguish user-facing from internal calls.
