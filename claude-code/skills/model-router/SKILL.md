---
name: model-router
description: "Apply complexity-based model routing when spawning sub-agents. Use haiku for simple tasks, sonnet for coding/analysis, opus for planning/architecture."
trigger: /model-router
---

# Model Router for Claude Code

When spawning sub-agents via the Agent tool, always apply this routing logic instead of defaulting to one model.

## Routing Rules

Score the sub-task before spawning. Pick the model tier:

| Score | Model | Use when |
|-------|-------|----------|
| 0–20 | `haiku` | Greetings, lookups, title gen, single-fact questions |
| 21–45 | `haiku` | Simple Q&A, summaries, verify/check tasks |
| 46–70 | `sonnet` | Coding, analysis, structured output, file edits |
| 71–100 | `opus` | Planning, architecture, multi-file refactors, deep reasoning |

## Scoring a Sub-Task (do this mentally before every Agent spawn)

Add up:
- Base: **30**
- Message > 200 chars: **+10**
- Message > 500 chars: **+20**
- Keywords (refactor, architect, design, implement, analyze, debug, migrate, security): **+8 each, max +30**
- Has code block in prompt: **+15**
- Multiple files involved: **+10**
- Destructive/high-stakes op (delete, deploy, prod, auth): **+15**
- Simple lookup/search/read-only: **-15**
- Title gen / summarize / format only: **-25**

Session floor: if the current session has been handling complex tasks (score > 60), don't go below `sonnet` even for short follow-up tasks.

## Call-Type Offsets

Apply AFTER scoring:

| Call type | Offset |
|-----------|--------|
| PLAN / orchestrate | +0 |
| ANALYZE / read files | -10 |
| CODEGEN / write code | -10 |
| VERIFY / review | -20 |
| SUMMARIZE / format | -30 |
| TITLE / label | -40 |
| SUBAGENT (child of complex task) | -10 |

## Examples

```
"search for where model is set"       → score 35  → haiku (Explore agent)
"refactor auth system across 5 files" → score 85  → opus
"write tests for the router"          → score 55  → sonnet
"summarize what changed"              → score 15  → haiku
"design the database schema"          → score 75  → opus
"fix this one typo"                   → score 20  → haiku
```

## How to Apply

When using the Agent tool, set the `model` parameter:

```
Agent({ model: "haiku",  ... })   // score < 46
Agent({ model: "sonnet", ... })   // score 46–70
Agent({ model: "opus",   ... })   // score > 70
```

Never spawn all agents at the same tier. Always score first.
