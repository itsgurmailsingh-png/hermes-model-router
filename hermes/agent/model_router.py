"""Model router for Hermes — Ollama Cloud tier.

Routes each LLM call to the cheapest model tier that can handle it.

Tiers (configure via HERMES_ROUTER_* env vars or pass ModelTiers to init):
  0  → ministral-3:3b        (greetings, lookups, title gen)
  1  → gemma3:12b            (simple Q&A, summarise, verify)
  2  → devstral-small-2:24b  (coding, analysis, structured output)
  3  → glm-5.2               (planning, architecture, multi-file refactors)

All four are verified available on Ollama Cloud (/api/tags, 2026-06-27).

Call types and their tier offsets:
  PLAN      +0   (uses full session score)
  ANALYZE   -1   (one step down from plan)
  CODEGEN   -1
  VERIFY    -2
  SUMMARIZE -3   (always cheap)
  TITLE     -99  (always tier 0)
  SUBAGENT  -1   (children run one tier below parent)

Usage in conversation_loop.py — add near top of run_conversation():

    from agent.model_router import route_turn
    routed = route_turn(agent, user_message, conversation_history or [])
    if routed:
        agent._router_original_model = agent.model
        agent.model = routed

And restore after the turn if you want (optional — next turn re-routes anyway).
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Call types
# ---------------------------------------------------------------------------

class CallType(str, Enum):
    PLAN      = "plan"       # Top-level turn: understand + orchestrate
    ANALYZE   = "analyze"    # Examine tool output / read files
    CODEGEN   = "codegen"    # Write / edit code
    VERIFY    = "verify"     # Check correctness, lint, review
    SUMMARIZE = "summarize"  # Compress, recap, format
    TITLE     = "title"      # Generate a session/chat title
    SUBAGENT  = "subagent"   # Spawned child agent


CALL_TYPE_OFFSET: dict[CallType, int] = {
    CallType.PLAN:      0,
    CallType.ANALYZE:  -10,
    CallType.CODEGEN:  -10,
    CallType.VERIFY:   -20,
    CallType.SUMMARIZE:-30,
    CallType.TITLE:    -99,
    CallType.SUBAGENT: -10,
}


# ---------------------------------------------------------------------------
# Model tiers  (defaults = sensible Ollama Cloud choices)
# ---------------------------------------------------------------------------

@dataclass
class ModelTiers:
    """Maps score ranges to model names.

    Override via env vars:
      HERMES_ROUTER_TIER0=qwen2.5:1.5b
      HERMES_ROUTER_TIER1=qwen2.5:7b
      HERMES_ROUTER_TIER2=qwen2.5:32b
      HERMES_ROUTER_TIER3=qwen2.5:72b
    """
    tier0: str = ""   # resolved at runtime
    tier1: str = ""
    tier2: str = ""
    tier3: str = ""

    def __post_init__(self):
        # Defaults are actual Ollama Cloud models (verified against /api/tags).
        # Tier 0 → ministral-3:3b   (4.7 GB  — greetings, titles, lookups)
        # Tier 1 → gemma3:12b       (24 GB   — simple Q&A, short answers)
        # Tier 2 → devstral-small-2:24b (51.6 GB — coding, analysis)
        # Tier 3 → glm-5.2          (heavy   — planning, architecture)
        self.tier0 = self.tier0 or os.getenv("HERMES_ROUTER_TIER0", "ministral-3:3b")
        self.tier1 = self.tier1 or os.getenv("HERMES_ROUTER_TIER1", "gemma3:12b")
        self.tier2 = self.tier2 or os.getenv("HERMES_ROUTER_TIER2", "devstral-small-2:24b")
        self.tier3 = self.tier3 or os.getenv("HERMES_ROUTER_TIER3", "glm-5.2")

    def for_score(self, score: int) -> str:
        if score <= 20:
            return self.tier0
        if score <= 45:
            return self.tier1
        if score <= 70:
            return self.tier2
        return self.tier3


# ---------------------------------------------------------------------------
# Session score  (lives on agent._router_session)
# ---------------------------------------------------------------------------

@dataclass
class RouterSession:
    """Lightweight per-session routing state attached to the agent object."""
    complexity_floor: int = 0      # Never route below this within the session
    tools_used: set[str] = field(default_factory=set)
    files_touched: int = 0
    turns: int = 0
    last_score: int = 0

    def update(self, turn_score: int, tools: list[str], files_written: int):
        self.turns += 1
        self.last_score = turn_score
        self.tools_used.update(tools)
        self.files_touched += files_written
        # Floor rises when the session is getting complex; decays slowly
        if turn_score > self.complexity_floor:
            self.complexity_floor = turn_score
        else:
            # Decay: after a simple turn the floor drops by 5 (not instantly)
            self.complexity_floor = max(0, self.complexity_floor - 5)


# ---------------------------------------------------------------------------
# Scoring  (Layer 0 + Layer 1 — no LLM needed)
# ---------------------------------------------------------------------------

# Patterns that push score UP
_HIGH_SIGNALS = re.compile(
    r"\b(refactor|rewrite|architect|implement|migrate|debug|analyse|analyze|"
    r"design|optimize|secur|authent|deploy|integrat|build|create|explain why|"
    r"why does|how does|compare|review|audit|test suite|end.to.end|pipeline|"
    r"multi.file|codebase|system|database|schema|api|endpoint|algorithm)\b",
    re.IGNORECASE,
)

# Patterns that push score DOWN
_LOW_SIGNALS = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|great|done|"
    r"got it|sounds good|perfect|nice|cool)\s*[!.]?\s*$",
    re.IGNORECASE,
)

# Code block in the message (user pasted code → complexity)
_HAS_CODE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b", re.IGNORECASE)

# Conversational reference to previous turn ("it", "that", "the above")
_CONTEXT_REF = re.compile(r"\b(it|that|this|the above|the previous|as before|same as)\b", re.IGNORECASE)


def _score_message(message: str, history: List[dict]) -> int:
    """Return 0-100 complexity score for a single message + recent history."""
    msg = message or ""

    # Immediate low-signal override
    if _LOW_SIGNALS.match(msg):
        return 5

    score = 30  # baseline

    # Message length (longer = more complex, up to +20)
    length = len(msg)
    score += min(20, length // 40)

    # High-signal keyword hits (+8 each, cap at +30)
    hits = len(_HIGH_SIGNALS.findall(msg))
    score += min(30, hits * 8)

    # Code in message
    if _HAS_CODE.search(msg):
        score += 15

    # Multi-sentence (question count)
    questions = msg.count("?")
    score += min(10, questions * 4)

    # Context reference without much content = ambiguous but session-dependent
    if _CONTEXT_REF.search(msg) and length < 80:
        score += 10  # relies on history → complexity floor will handle it

    # History depth (more turns in flight = more context dependency)
    depth = len(history)
    score += min(10, depth // 3)

    # Tool results in recent history (agent is mid-task)
    tool_msgs = sum(1 for m in history[-6:] if m.get("role") == "tool")
    score += min(15, tool_msgs * 5)

    return min(100, score)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_TIERS = ModelTiers()


def route_call(
    agent: Any,
    call_type: CallType = CallType.PLAN,
    message: str = "",
    history: Optional[List[dict]] = None,
    tiers: Optional[ModelTiers] = None,
) -> str:
    """Return the model name to use for this call.

    Attaches/updates a RouterSession on agent._router_session.
    Does NOT mutate agent.model — caller decides whether to swap.

    If agent._context_graph exists (from ContextTreeBuilder), the context
    tree's semantic complexity floor is merged with the session floor —
    giving a floor that reflects what files you've been touching, not just
    the last message score.
    """
    tiers = tiers or _DEFAULT_TIERS
    history = history or []

    # Get or create session state
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None:
        session = RouterSession()
        agent._router_session = session

    # Score the message
    turn_score = _score_message(message, history)

    # Apply session complexity floor
    effective_score = max(turn_score, session.complexity_floor)

    # Boost floor from context tree if available
    ctx_floor_val = 0
    graph = getattr(agent, "_context_graph", None)
    if graph is not None and len(graph) > 0:
        try:
            try:
                from agent.context_tree import complexity_floor as _ctx_floor
            except ImportError:
                from hermes.agent.context_tree import complexity_floor as _ctx_floor
            ctx_floor_val = _ctx_floor(graph, message)
            effective_score = max(effective_score, ctx_floor_val)
        except Exception:
            pass

    # Apply call-type offset (planning gets full score; summarise always cheap)
    offset = CALL_TYPE_OFFSET.get(call_type, 0)
    final_score = max(0, min(100, effective_score + offset))

    # Resolve tier
    model = tiers.for_score(final_score)

    logger.debug(
        "model_router: call_type=%s msg_score=%d floor=%d ctx_floor=%d "
        "effective=%d final=%d → %s",
        call_type.value, turn_score, session.complexity_floor,
        ctx_floor_val, effective_score, final_score, model,
    )

    return model


def route_turn(
    agent: Any,
    user_message: str,
    history: Optional[List[dict]] = None,
    tiers: Optional[ModelTiers] = None,
) -> Optional[str]:
    """Convenience wrapper for the top-level turn (CallType.PLAN).

    Returns None when routing is disabled (HERMES_MODEL_ROUTER=0) so the
    caller can skip the swap cleanly:

        routed = route_turn(agent, message, history)
        if routed:
            agent.model = routed
    """
    if os.getenv("HERMES_MODEL_ROUTER", "1") == "0":
        return None

    return route_call(agent, CallType.PLAN, user_message, history, tiers)


def update_session_after_turn(
    agent: Any,
    turn_score: int,
    tools_used: Optional[list] = None,
    files_written: int = 0,
):
    """Call this at the end of a turn to update the session floor.

    tools_used: list of tool names invoked this turn (from agent._tool_call_log or similar)
    files_written: number of files written/edited this turn
    """
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None:
        return
    session.update(turn_score, tools_used or [], files_written)
