# Architecture — hermes-model-router v1.2

## Overview

The model router is a two-layer system that classifies each LLM call before it reaches the API. Layer 1 scores features from the message and context. The context tree adds a semantic floor from files you've been touching. The output is a score (0–100) which maps to a tier (0–3) which maps to a concrete model name via a configurable table.

The same scoring logic runs in both Hermes (Python) and Claude Code (Node.js/MJS), with identical tier boundaries, so routing decisions are predictable regardless of which runtime you are in.

**v1.1 changes**: ALL calls are now routed (main chat included, not just internal calls). The context tree is wired into the router — it's no longer "planned v2", it's live.

**v1.2 changes**: Feedback loop (success/failure tracking that adjusts the floor), smarter scoring (trivial scope / micro-task / short+keyword penalties), custom extensible tags loaded from JSON, complexity decay by file recency, configurable context floor cap, manual tier override (`/t0`–`/t3`), session-aware plugin (`_RouterAgent` replacing `_FakeAgent`), and a routing monitor script.

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

### Trivial scope detection (v1.2, -15)

If the message contains any of: `just`, `only`, `one`, `quick`, `simple`, `small` → -15. These words signal a small task that doesn't need a big model. Matched case-insensitively on word boundaries.

### Micro-task detection (v1.2, -20)

If the message contains any of: `typo`, `rename`, `comment`, `format`, `indent`, `spelling` → -20. These are mechanical edits any tier can handle. Matched case-insensitively on word boundaries.

### Short+keyword penalty (v1.2, -10)

If a high-signal keyword is present but the message is < 60 characters → -10. A short message with one keyword rarely needs a big model — the keyword is usually incidental context, not a complexity signal.

### Scoring examples (v1.2)

| Message | v1.1 score | v1.2 score | Breakdown |
|---------|-----------|-----------|-----------|
| "refactor this one variable" | 38 | 23 | base 30 + "refactor" +8 + "one" trivial scope -15 = 23 |
| "just rename this" | 20 | 5 | base 30 + "just" trivial scope -15 + "rename" micro-task -20 = 5 (floored at 5) |

### Low-signal override

If the message matches a greeting/acknowledgement pattern (`hi`, `thanks`, `ok`, `done`, etc.) it scores 5, regardless of other features. This ensures trivial messages route to the cheapest tier — unless the context tree floor overrides it.

### Manual override (v1.2)

Prefix a message with `/t0`, `/t1`, `/t2`, or `/t3` to force a specific tier for one call. This bypasses all scoring, floors, and offsets. The prefix is stripped from the message before routing. Useful for testing or when you know better than the router.

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
- **Feedback adjustments (v1.2)**: failures boost the floor by +10 each (capped at +30 total). Successes decay it by -3. See [Feedback Loop](#feedback-loop-v12) below.
- Floor is stored in memory per session (`RouterSession`), with feedback persisted to `~/.hermes/router-logs/feedback.json`.

This means:
- A coding session that hit score 68 will not route the next turn below 63.
- A long chain of simple turns (acknowledgements, confirmations) eventually lets the floor decay back to 0.
- A run of failures will keep the router on higher tiers until the model succeeds again (v1.2).

---

## Feedback Loop (v1.2)

After each routed call, the router tracks whether the call succeeded or failed. This feedback adjusts the session floor so that repeated failures push the router toward more capable models, while successes let it relax back down.

### Mechanism

- **Failure**: `floor += 10` (boost), capped at +30 above the base floor.
- **Success**: `floor -= 3` (decay).
- Feedback is persisted to `~/.hermes/router-logs/feedback.json` and loaded on session start.

### API

```python
record_call_result(agent, call_type, success)   # Called after each routed call
save_feedback(agent)                              # Persist feedback to disk
load_feedback()                                   # Load persisted feedback (called on session start)
```

### What it prevents

If a model keeps failing on a task (e.g. a small model produces broken code that needs re-routing), the feedback loop raises the floor so subsequent calls go to a more capable tier. Without feedback, the router would keep trying the same cheap model and failing.

---

## Context Tree Floor (v1.1 — LIVE)

The context tree maintains a live semantic graph of the agent's working context. It tracks:

- **FILE nodes** — every file read or written, with auto-extracted domain tags (auth, security, database, api, test, config, ui, infra, ml, graphics, networking, game) and a complexity score (0–100) based on lines, functions, classes, imports, and branching.
- **TURN nodes** — each user message, tagged by domain.
- **CALL nodes** — tool calls (bash, delegate_task, search_files, patch, terminal).

### Custom tags (v1.2)

Tag patterns are no longer hardcoded. They are loaded from `hermes/agent/context_tree/tags.json` — a user-extensible JSON file. v1.2 added `ml`, `graphics`, `networking`, and `game` domains. Both `builder.py` (which creates nodes) and `query.py` (which scores them) load from the same file, so adding a new domain tag in the JSON immediately affects both scoring and node creation.

Hot-reload via `reload_tags()` — call it after editing `tags.json` to pick up changes without restarting the session.

### How the floor is computed

`context_tree.query.complexity_floor(graph, prompt)`:

1. Extract domain tags from the prompt (e.g. "fix the auth token" → {auth}).
2. Score all graph nodes by tag overlap (×20) + node type weight (FILE=10, SYMBOL=8, TURN=6) + recency bonus (×15 if in last 10 touched) + complexity (×0.1).
3. Take the top-4 scored nodes with score > 0.
4. Floor = average of the top node's complexity and the mean complexity of the 4 nodes, capped at `HERMES_ROUTER_CTX_FLOOR_CAP` (default 70). Set this env var to 90 to let context alone push a call to Tier 3.

This floor is merged with the session floor (`max(session_floor, ctx_floor)`) before applying the call-type offset.

### Complexity decay (v1.2)

Files not touched recently contribute less to the context floor. Each node's complexity is scaled by its recency position in the touched history:

| Recency (position in touched history) | Contribution |
|----------------------------------------|-------------|
| Recent (last 10 touched) | 100% |
| 10–30 back | 80% |
| 30–50 back | 60% |
| Beyond 50 | 40% |

This prevents stale files from inflating the floor long after you've moved on to something else.

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

## Plugin Hooks (v1.2)

| Hook | When | What it does |
|------|------|-------------|
| `on_session_start` | Session begins | Creates ContextGraph + ContextTreeBuilder, attaches to agent. Creates `_RouterAgent` (v1.2) carrying the actual RouterSession + context graph. Loads persisted feedback on first call. |
| `pre_llm_call` | Before every LLM call | Scores message, applies session + context tree floor, routes to tier, returns `{"model": name}`. Checks for manual override prefix (`/t0`–`/t3`). |
| `post_llm_call` | After each main turn | Updates session complexity floor from message score. Records call result for feedback loop (v1.2). |
| `on_tool_result` | After every tool call | Feeds tool result to ContextTreeBuilder (read/write/bash/delegate/search/patch) |
| `on_turn_start` | User sends message | Adds TURN node to graph, sets as current turn |
| `on_turn_end` | Turn completes | Saves graph to `~/.hermes/router-logs/context-graph.json`. Saves feedback to `~/.hermes/router-logs/feedback.json` (v1.2). |

### Session-aware plugin (v1.2)

v1.1 used a `_FakeAgent` stub that didn't carry real session state. v1.2 replaces it with `_RouterAgent`, which carries the actual `RouterSession` and context graph. This means the plugin has access to the real session floor, feedback state, and context tree — not a placeholder. Persisted feedback is loaded on the first call of each session.

---

## Routing Monitor (v1.2)

`scripts/router_monitor.py` is a CLI tool for inspecting routing behavior in real time. It reads:

- `~/.hermes/router-logs/routing.jsonl` — routing decisions
- `~/.hermes/router-logs/feedback.json` — feedback state
- `~/.hermes/router-logs/context-graph.json` — current context graph

### Output

- **Model distribution** — how many calls went to each tier/model
- **Call types** — breakdown by call type (plan, codegen, analyze, etc.)
- **Feedback state** — current floor adjustments from failures/successes
- **Recent decisions** — last N routing decisions with color-coded tiers

### Usage

```bash
python scripts/router_monitor.py              # summary + last 20 decisions
python scripts/router_monitor.py --last 50    # last 50 decisions
python scripts/router_monitor.py --watch      # live mode, refreshes every 5s
```

---

## Future: Cost Attribution & Adaptive Weights

The context tree has the data structure for cost tracking (Node.meta can hold tokens_in/out), but this is not yet wired. Planned:

1. **Cost attribution** — each node tracks estimated tokens and cost. CLI report shows cost-per-file, cost-per-subtask.
2. **Adaptive keyword weights** — after each session, actual model performance updates keyword weights via a simple online learning rule. (Partially addressed by the v1.2 feedback loop, which adjusts the floor — not individual keyword weights — based on success/failure.)
3. **SYMBOL nodes** — track individual functions/classes, not just files.
4. **DEPENDS_ON edges** — traverse the DAG for inherited complexity (if Call B depends on Call A's output, B inherits A's floor even if B's text is simple).