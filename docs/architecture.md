# Architecture — hermes-model-router

## Overview

The model router is a two-layer system that classifies each LLM call before it reaches the API. Layer 0 applies deterministic rules in microseconds. Layer 1 scores features when the rules don't produce a clear result. The output is a tier (0–3) which maps to a concrete model name via a configurable table.

The same scoring logic runs in both Hermes (Python) and Claude Code (Node.js/MJS), with identical tier boundaries, so routing decisions are predictable regardless of which runtime you are in.

---

## Layer 0 — Rule Triage

Fast path. If a call matches one of these patterns it is immediately assigned a tier and skips Layer 1.

**Tier 0 (cheapest) — forced cheap:**
- Call type is `TITLE` or `GREETING`
- Prompt is a single short phrase with no code and no question mark chain
- `api_call_count` is very high (internal tool loop iteration, not a user turn)

**Tier 3 (most capable) — forced expensive:**
- Prompt contains `/analyze`, `/architect`, or `/plan` slash commands
- Prompt has three or more fenced code blocks
- Prompt references four or more file paths
- Call type is `ORCHESTRATE`

If neither forced path matches, control passes to Layer 1.

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

### Tier boundaries

| Score | Tier |
|-------|------|
| 0–24 | 0 |
| 25–49 | 1 |
| 50–74 | 2 |
| 75–100 | 3 |

---

## Session Floor

The session floor prevents mid-session regression: once a complex task has been seen, simpler follow-up turns do not fall back to cheap models that have no context.

- Floor starts at 0.
- After each call, the floor is updated: `floor = max(floor, tier - 1)`.
- Floor decays by 1 tier for every 3 consecutive simple calls (score < 30).
- Floor is stored in memory per session, not persisted across restarts.

This means:
- A coding session that hits Tier 2 will not route the next turn below Tier 1.
- A long chain of simple turns (acknowledgements, confirmations) eventually lets the floor decay back to 0.

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
| TITLE generation | -40 | Always cheap; scores floor at Tier 0 |

Offsets are applied after the session floor check, so the floor still protects against regressions.

---

## Planned v2 — Context Tree

The current scorer treats each call independently (except for the session floor). v2 will maintain a lightweight context tree per session.

### Goals

1. **Dependency-aware routing**: if Call B is downstream of a complex Call A, B inherits A's tier as its floor, even if B's text looks simple.
2. **Conversation graph**: tool calls and sub-agent spawns form a DAG. The router will traverse ancestors to compute an inherited complexity score.
3. **Cost attribution**: each node in the tree tracks estimated tokens and cost. The UI (or CLI report) can show cost-per-subtask.
4. **Adaptive keyword weights**: after each session, actual model performance (did the routed model produce a correct result?) is used to update keyword weights via a simple online learning rule.

### Prototype data structure

```python
@dataclass
class CallNode:
    call_id: str
    parent_id: str | None
    tier: int
    score: int
    call_type: str
    prompt_hash: str          # for dedup
    tokens_in: int
    tokens_out: int
    model: str
    success: bool | None      # filled in post-call
```

The tree is stored in memory during the session and optionally serialized to `~/.hermes/router-sessions/<session-id>.json` for offline analysis.

### Why not now?

The context tree requires a reliable call-ID scheme across Hermes internals, which is not yet stable. The current floor mechanism covers 80% of the benefit at 5% of the complexity.

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

Claude Code logs every routing decision to `~/.claude/router-logs/routing.jsonl`. Each line is a JSON object:

```json
{
  "ts": "2026-06-27T10:15:32.001Z",
  "prompt_preview": "refactor the auth module to use...",
  "score": 68,
  "tier": 2,
  "model_before": "claude-opus-4-6",
  "model_after": "claude-sonnet-4-6",
  "rerouted": true,
  "call_type": "CODEGEN",
  "session_floor": 1
}
```

`router-analyze.mjs` reads this file and produces the summary report shown in the README.

Hermes logs to the standard Hermes plugin log at `DEBUG` level. No separate log file is created.
