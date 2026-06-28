---
name: model-router
description: "Apply complexity-based model routing when spawning sub-agents. Use haiku for simple tasks, sonnet for coding/analysis, opus for planning/architecture. Context tree floor from Hermes prevents mid-session downgrades."
trigger: /model-router
---

# Model Router for Claude Code

When spawning sub-agents via the Agent tool, always apply this routing logic instead of defaulting to one model.

**Limitation**: This hook only controls sub-agent spawns (Agent tool). The main chat model in Claude Code is set internally before any hook runs — it's NOT controllable via hooks. Only Hermes has full `pre_llm_call` control over all calls.

## Routing Rules

Score the sub-task before spawning. Pick the model tier:

| Score | Model | Use when |
|-------|-------|----------|
| 0–45 | `haiku` | Greetings, lookups, title gen, simple Q&A, summaries |
| 46–70 | `sonnet` | Coding, analysis, structured output, file edits |
| 71–100 | `opus` | Planning, architecture, multi-file refactors, deep reasoning |

## Scoring a Sub-Task (do this mentally before every Agent spawn)

Add up:
- Base: **30**
- Message length: up to **+20** (min(len/40, 20))
- Keywords (refactor, architect, design, implement, analyze, debug, migrate, security): **+8 each, max +30**
- Has code block in prompt: **+15**
- Multiple files involved: **+15**
- Questions: **+4 each, max +10**
- **Trivial scope (v1.2)**: message contains "just", "only", "one", "quick", "simple", "small" → **-15**
- **Micro-task (v1.2)**: message contains "typo", "rename", "comment", "format", "indent", "spelling" → **-20**
- **Short+keyword (v1.2)**: keyword present but message < 60 chars → **-10**

Low-signal override: greetings/acknowledgements ("hi", "ok", "thanks", "done") → score 5.

**Manual override (v1.2)**: prefix message with `/t0`, `/t1`, `/t2`, `/t3` to force a tier for one call — bypasses all scoring.

## Context Tree Floor

The `agent-router.mjs` hook reads `~/.hermes/router-logs/context-graph.json` (written by the Hermes plugin) to compute a semantic complexity floor. If you've been working on `auth.py` (complexity 72) in Hermes, a sub-agent spawn for "fix the typo" will inherit a floor of ~55, keeping it on `sonnet` instead of dropping to `haiku`.

This is cross-runtime: the Hermes plugin writes the graph, the Claude Code hook reads it.

## Call-Type Offsets

Apply AFTER scoring and floor:

| Call type | Offset |
|-----------|--------|
| PLAN / orchestrate | +0 |
| ANALYZE / read files | -10 |
| CODEGEN / write code | -10 |
| VERIFY / review | -20 |
| SUMMARIZE / format | -30 |
| TITLE / label | -99 (always cheapest) |
| SUBAGENT (child of complex task) | -10 |

Session floor: if the current session has been handling complex tasks (score > 60), don't go below `sonnet` even for short follow-up tasks. Floor decays by 5 per simple turn.

## Examples

```
"search for where model is set"       → score 35  → haiku (Explore agent)
"refactor auth system across 5 files" → score 85  → opus
"write tests for the router"          → score 55  → sonnet
"summarize what changed"              → score 15  → haiku
"design the database schema"          → score 75  → opus
"fix this one typo"                   → score 5   → haiku (v1.2: "one" -15, "typo" -20; ctx floor may still push up)
"just rename this"                    → score 5   → haiku (v1.2: "just" -15, "rename" -20)
```

## How to Apply

When using the Agent tool, the `PreToolUse` hook (`agent-router.mjs`) automatically rewrites the `model` field. You don't need to set it manually — the hook handles it.

If the hook is not installed, set the `model` parameter manually:

```
Agent({ model: "haiku",  ... })   // score ≤ 45
Agent({ model: "sonnet", ... })   // score 46–70
Agent({ model: "opus",   ... })   // score > 70
```

Never spawn all agents at the same tier. Always score first.